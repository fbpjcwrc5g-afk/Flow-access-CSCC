"""Clinical data used for referrals: diagnoses, post-acute needs from orders, transport findings."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .fhir.extract import find_keyword, keyword_matcher, parse_datetime, ref_id
from .models import ClinicalEvent, EventType, NeedType

Resource = dict[str, Any]

ICD10_SYSTEMS = {"http://hl7.org/fhir/sid/icd-10-cm", "urn:oid:2.16.840.1.113883.6.90", "http://hl7.org/fhir/sid/icd-10"}
INACTIVE_CLINICAL = {"inactive", "resolved", "remission"}
EXCLUDED_VERIFICATION = {"refuted", "entered-in-error"}
ACTIVE_ORDER_STATUSES = {"active", "on-hold"}
MODE_RANK = {"wheelchair_van": 1, "litter_van": 2, "ambulance": 3, "air": 4}


class Diagnosis(BaseModel):
    code: str
    display: str
    category: str | None = None
    condition_id: str | None = None


class TransportFinding(BaseModel):
    criterion_id: str
    code: str
    display: str
    statement: str
    minimum_mode: str | None = None


class TransportAssessment(BaseModel):
    findings: list[TransportFinding]
    recommended_mode: str | None


def _code(concept: dict) -> str:
    return (concept.get("coding") or [{}])[0].get("code", "")


def diagnoses(conditions: list[Resource], encounter_id: str | None = None) -> list[Diagnosis]:
    """Active ICD-10 diagnoses: current-encounter diagnoses first, then other encounter diagnoses, then problems."""
    ranked = []
    seen = set()
    for cond in conditions:
        if _code(cond.get("clinicalStatus", {})) in INACTIVE_CLINICAL:
            continue
        if _code(cond.get("verificationStatus", {})) in EXCLUDED_VERIFICATION:
            continue
        icd = next((c for c in cond.get("code", {}).get("coding", []) if c.get("system") in ICD10_SYSTEMS), None)
        if not icd or icd.get("code") in seen:
            continue
        seen.add(icd["code"])
        category = next((_code(c) for c in cond.get("category", []) if _code(c)), None)
        this_encounter = encounter_id is not None and ref_id(cond.get("encounter"), "Encounter") == encounter_id
        rank = 0 if this_encounter else 1 if category == "encounter-diagnosis" else 2
        display = icd.get("display") or cond.get("code", {}).get("text") or icd["code"]
        ranked.append((rank, Diagnosis(code=icd["code"], display=display, category=category, condition_id=cond.get("id"))))
    return [d for _, d in sorted(ranked, key=lambda r: r[0])]


def order_text(order: Resource) -> str:
    parts = [order.get("code", {}).get("text")]
    parts += [c.get("display") for c in order.get("code", {}).get("coding", [])]
    for cat in order.get("category", []):
        parts += [cat.get("text")] + [c.get("display") for c in cat.get("coding", [])]
    parts += [d.get("text") for d in order.get("orderDetail", [])]
    return " | ".join(p for p in parts if p)


class NeedDetector:
    def __init__(self, need_keywords: dict[str, list[str]]):
        self.matchers = [(NeedType(need), keyword_matcher(words)) for need, words in need_keywords.items()]

    def need_for(self, order: Resource) -> NeedType | None:
        text = order_text(order)
        return next((need for need, matcher in self.matchers if find_keyword(text, matcher)), None)

    def events(self, orders: list[Resource]) -> list[ClinicalEvent]:
        events = []
        for order in orders:
            if order.get("status") not in ACTIVE_ORDER_STATUSES or order.get("intent") == "proposal":
                continue
            if need := self.need_for(order):
                events.append(ClinicalEvent(
                    type=EventType.POST_ACUTE_NEED,
                    need=need,
                    source_id=order.get("id"),
                    encounter_id=ref_id(order.get("encounter"), "Encounter"),
                    occurred_at=parse_datetime(order.get("authoredOn")),
                ))
        return events


class TransportCriteria:
    def __init__(self, config: dict):
        self.modes: dict[str, str] = config.get("modes", {})
        self.criteria = config.get("criteria", [])

    @classmethod
    def from_file(cls, path: Path) -> "TransportCriteria":
        return cls(yaml.safe_load(path.read_text()))

    def assess(self, dx: list[Diagnosis]) -> TransportAssessment:
        findings = []
        for criterion in self.criteria:
            prefixes = [p.replace(".", "").upper() for p in criterion["icd10_prefixes"]]
            for d in dx:
                if any(d.code.replace(".", "").upper().startswith(p) for p in prefixes):
                    findings.append(TransportFinding(
                        criterion_id=criterion["id"],
                        code=d.code,
                        display=d.display,
                        statement=criterion["statement"].format(code=d.code, display=d.display),
                        minimum_mode=criterion.get("minimum_mode"),
                    ))
                    break
        modes = [f.minimum_mode for f in findings if f.minimum_mode]
        return TransportAssessment(
            findings=findings, recommended_mode=max(modes, key=MODE_RANK.__getitem__) if modes else None
        )


def age_on(birth_date: str | None, on: date) -> int | None:
    if not birth_date or len(birth_date) < 10:
        return None
    born = date.fromisoformat(birth_date[:10])
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def today(now: datetime | None) -> date:
    return (now or datetime.now()).date()
