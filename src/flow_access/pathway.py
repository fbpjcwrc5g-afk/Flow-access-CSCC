"""Determine a patient's insurance pathway from their FHIR Coverage resources."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .fhir.extract import coverage_is_active, coverage_payor, coverage_plan, coverage_text
from .models import CoverageClassification, Pathway, PathwayDetermination

Resource = dict[str, Any]

MEDICARE = {Pathway.MEDICARE_TRADITIONAL, Pathway.MEDICARE_ADVANTAGE}
MEDICAID = {Pathway.MEDICAID, Pathway.MEDICAID_MANAGED_CARE}


class PathwayClassifier:
    def __init__(self, rules: dict):
        self.coding_systems = []
        for block in rules.get("coding_systems", []):
            codes = {str(code): Pathway(p) for code, p in block["codes"].items()}
            self.coding_systems.append((set(block["systems"]), block.get("match", "exact"), codes))
        self.keywords = [
            (Pathway(k["pathway"]), [w.lower() for w in k["any"]]) for k in rules.get("keywords", [])
        ]
        self.combine_dual = rules.get("combine_medicare_and_medicaid_as_dual", True)

    @classmethod
    def from_file(cls, path: Path) -> "PathwayClassifier":
        return cls(yaml.safe_load(path.read_text()))

    def _match_coding(self, system: str | None, code: str | None) -> Pathway | None:
        if not system or not code:
            return None
        for systems, match, codes in self.coding_systems:
            if system not in systems:
                continue
            if match == "exact":
                return codes.get(code)
            for length in range(len(code), 0, -1):
                if code[:length] in codes:
                    return codes[code[:length]]
        return None

    def classify_coverage(self, coverage: Resource) -> CoverageClassification:
        base = {
            "coverage_id": coverage.get("id"),
            "order": coverage.get("order"),
            "payor": coverage_payor(coverage),
            "plan": coverage_plan(coverage),
        }
        for coding in coverage.get("type", {}).get("coding", []):
            pathway = self._match_coding(coding.get("system"), coding.get("code"))
            if pathway:
                return CoverageClassification(
                    **base, pathway=pathway, matched_by="coding", evidence=f"{coding['system']}|{coding['code']}"
                )
        text = coverage_text(coverage).lower()
        for pathway, words in self.keywords:
            for word in words:
                if word in text:
                    return CoverageClassification(**base, pathway=pathway, matched_by="keyword", evidence=word)
        return CoverageClassification(**base, pathway=Pathway.UNKNOWN, matched_by="unmatched")

    def determine(self, coverages: list[Resource], as_of: datetime | None = None) -> PathwayDetermination:
        as_of = as_of or datetime.now(timezone.utc)
        active = [c for c in coverages if coverage_is_active(c, as_of)]
        if not active:
            return PathwayDetermination(
                pathway=Pathway.SELF_PAY,
                confidence="low",
                needs_verification=True,
                reasons=["No active Coverage on file; treating as uninsured until registration confirms."],
            )

        classified = sorted(
            (self.classify_coverage(c) for c in active), key=lambda c: (c.order is None, c.order or 0)
        )
        reasons = [
            f"Coverage {c.coverage_id} ({c.payor or 'unknown payor'}) -> {c.pathway.value} via {c.matched_by}"
            + (f" [{c.evidence}]" if c.evidence else "")
            for c in classified
        ]
        known = [c for c in classified if c.pathway != Pathway.UNKNOWN]
        if not known:
            return PathwayDetermination(
                pathway=Pathway.UNKNOWN,
                confidence="low",
                needs_verification=True,
                primary=classified[0],
                coverages=classified,
                reasons=reasons + ["No active coverage could be classified."],
            )

        found = {c.pathway for c in known}
        primary = known[0]
        pathway = primary.pathway
        if Pathway.DUAL_ELIGIBLE in found:
            pathway = Pathway.DUAL_ELIGIBLE
            primary = next(c for c in known if c.pathway == Pathway.DUAL_ELIGIBLE)
        elif self.combine_dual and found & MEDICARE and found & MEDICAID:
            pathway = Pathway.DUAL_ELIGIBLE
            primary = next(c for c in known if c.pathway in MEDICARE)
            reasons.append("Active Medicare and Medicaid coverage -> dual eligible.")

        confidence = "high" if primary.matched_by == "coding" else "medium"
        needs_verification = len(known) < len(classified) or confidence != "high"
        return PathwayDetermination(
            pathway=pathway,
            confidence=confidence,
            needs_verification=needs_verification,
            primary=primary,
            coverages=classified,
            reasons=reasons,
        )
