"""Plan profiles: which health plan / delegated group a patient belongs to, and what that implies
for referral documents, contracted agencies, and PCS forms."""

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .fhir.extract import coverage_is_active, coverage_text, find_keyword, keyword_matcher
from .models import NeedType, PlanMatch

Resource = dict[str, Any]


class Agency(BaseModel):
    name: str
    phone: str = ""
    fax: str = ""
    address: str = ""
    service_area: str = ""
    notes: str = ""


class PcsFormConfig(BaseModel):
    name: str = "Physician Certification Statement (PCS)"
    template_pdf: Path | None = None
    # PDF form field name -> dotted path into the PcsForm data, e.g. {"MemberName": "patient.name"}.
    field_map: dict[str, str] = Field(default_factory=dict)


class PlanProfile(BaseModel):
    id: str
    display_name: str
    role: str = "health_plan"
    match: list[str]
    authorization_contact: str = ""
    transportation_contact: str = ""
    pcs_form: PcsFormConfig = Field(default_factory=PcsFormConfig)
    documents: dict[NeedType, list[str]] = Field(default_factory=dict)
    agencies: dict[NeedType, list[Agency]] = Field(default_factory=dict)


class PlanDirectory:
    def __init__(self, config: dict, agencies: list[dict] | None = None, base_dir: Path | None = None):
        defaults = config.get("defaults", {})
        self.default_documents = {NeedType(k): v for k, v in defaults.get("documents", {}).items()}
        self.need_keywords: dict[str, list[str]] = defaults.get("need_keywords", {})
        self.profiles: dict[str, PlanProfile] = {}
        for raw in config.get("plans", []):
            profile = PlanProfile(**raw)
            if profile.pcs_form.template_pdf and base_dir and not profile.pcs_form.template_pdf.is_absolute():
                profile.pcs_form.template_pdf = base_dir / profile.pcs_form.template_pdf
            self.profiles[profile.id] = profile
        for row in agencies or []:
            profile = self.profiles.get(row["plan_id"])
            if profile:
                fields = {k: (row.get(k) or "").strip() for k in Agency.model_fields}
                profile.agencies.setdefault(NeedType(row["service"]), []).append(Agency(**fields))
        self._matchers = {pid: keyword_matcher(p.match) for pid, p in self.profiles.items()}

    @classmethod
    def from_files(cls, plans_path: Path, agencies_path: Path | None) -> "PlanDirectory":
        rows = []
        if agencies_path and agencies_path.exists():
            with agencies_path.open(newline="") as f:
                rows = list(csv.DictReader(f))
        return cls(yaml.safe_load(plans_path.read_text()), rows, base_dir=plans_path.parent)

    def match(self, coverages: list[Resource], as_of: datetime | None = None) -> list[PlanMatch]:
        """Matched profiles for the active coverages, delegated groups first (they own UM and network)."""
        as_of = as_of or datetime.now(timezone.utc)
        matches: dict[str, PlanMatch] = {}
        for coverage in coverages:
            if not coverage_is_active(coverage, as_of):
                continue
            text = coverage_text(coverage)
            for pid, matcher in self._matchers.items():
                if pid not in matches and (word := find_keyword(text, matcher)):
                    profile = self.profiles[pid]
                    matches[pid] = PlanMatch(
                        plan_id=pid,
                        display_name=profile.display_name,
                        role=profile.role,
                        coverage_id=coverage.get("id"),
                        member_id=coverage.get("subscriberId"),
                        evidence=word,
                    )
        return sorted(matches.values(), key=lambda m: m.role != "delegated_group")

    def managing_profile(self, matches: list[PlanMatch]) -> PlanProfile | None:
        return self.profiles[matches[0].plan_id] if matches else None

    def documents_for(self, profile: PlanProfile | None, need: NeedType) -> list[str]:
        docs = list(self.default_documents.get(need, []))
        if profile:
            docs += [d for d in profile.documents.get(need, []) if d not in docs]
        return docs

    def agencies_for(self, matches: list[PlanMatch], need: NeedType) -> tuple[PlanProfile | None, list[Agency]]:
        """Agencies from the first matched profile (delegated group, then plan) that lists any."""
        for m in matches:
            agencies = self.profiles[m.plan_id].agencies.get(need)
            if agencies:
                return self.profiles[m.plan_id], agencies
        return None, []
