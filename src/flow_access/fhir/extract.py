"""Helpers for pulling the fields we need out of Epic FHIR R4 resources."""

import re
from datetime import date, datetime, timezone
from typing import Any

Resource = dict[str, Any]

MRN_TYPE_CODES = {"MR", "MRN"}


def ref_id(reference: dict | None, resource_type: str | None = None) -> str | None:
    """Return the logical id from a Reference like {"reference": "Patient/abc"}."""
    if not reference or "reference" not in reference:
        return None
    parts = reference["reference"].rstrip("/").split("/")
    if len(parts) < 2:
        return None
    if resource_type and parts[-2] != resource_type:
        return None
    return parts[-1]


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if len(value) == 10:
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def patient_mrn(patient: Resource) -> str | None:
    """Prefer an identifier typed MR/MRN; fall back to Epic's enterprise ID ("EPI")."""
    fallback = None
    for identifier in patient.get("identifier", []):
        codes = {c.get("code") for c in identifier.get("type", {}).get("coding", [])}
        text = identifier.get("type", {}).get("text", "").upper()
        if codes & MRN_TYPE_CODES or text in MRN_TYPE_CODES:
            return identifier.get("value")
        if text == "EPI" and fallback is None:
            fallback = identifier.get("value")
    return fallback


def coverage_is_active(coverage: Resource, as_of: datetime) -> bool:
    if coverage.get("status", "active") != "active":
        return False
    period = coverage.get("period", {})
    start = parse_datetime(period.get("start"))
    end = parse_datetime(period.get("end"))
    if end and len(period.get("end", "")) == 10:
        end = end.replace(hour=23, minute=59, second=59)
    if start and as_of < start:
        return False
    if end and as_of > end:
        return False
    return True


def coverage_payor(coverage: Resource) -> str | None:
    for payor in coverage.get("payor", []):
        if payor.get("display"):
            return payor["display"]
    return None


def coverage_plan(coverage: Resource) -> str | None:
    names = []
    for cls in coverage.get("class", []):
        codes = {c.get("code") for c in cls.get("type", {}).get("coding", [])}
        if codes & {"plan", "group", "subplan", "subgroup"}:
            names.append(cls.get("name") or cls.get("value"))
    return " / ".join(n for n in names if n) or None


def coverage_text(coverage: Resource) -> str:
    """All free text describing a coverage, used for keyword matching."""
    parts = [coverage_payor(coverage), coverage_plan(coverage), coverage.get("type", {}).get("text")]
    parts += [c.get("display") for c in coverage.get("type", {}).get("coding", [])]
    return " | ".join(p for p in parts if p)


def keyword_matcher(words: list[str]) -> list[tuple[str, re.Pattern]]:
    """Compile case-insensitive whole-word patterns for keyword matching."""
    return [(w, re.compile(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])")) for w in words]


def find_keyword(text: str, matcher: list[tuple[str, re.Pattern]]) -> str | None:
    lowered = text.lower()
    return next((word for word, pattern in matcher if pattern.search(lowered)), None)


def encounter_class_code(encounter: Resource) -> str | None:
    cls = encounter.get("class")
    if isinstance(cls, list):  # R5-style or nonstandard payloads
        cls = cls[0] if cls else None
    return cls.get("code") if cls else None
