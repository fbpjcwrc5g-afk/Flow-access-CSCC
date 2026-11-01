from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

Resource = dict[str, Any]


class Pathway(StrEnum):
    MEDICARE_TRADITIONAL = "medicare_traditional"
    MEDICARE_ADVANTAGE = "medicare_advantage"
    MEDICAID = "medicaid"
    MEDICAID_MANAGED_CARE = "medicaid_managed_care"
    DUAL_ELIGIBLE = "dual_eligible"
    COMMERCIAL = "commercial"
    MILITARY = "military"
    VETERANS = "veterans"
    GOVERNMENT_OTHER = "government_other"
    WORKERS_COMP = "workers_comp"
    AUTO_LIABILITY = "auto_liability"
    SELF_PAY = "self_pay"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    INPATIENT_ADMISSION = "inpatient_admission"
    INPATIENT_DISCHARGE = "inpatient_discharge"
    OBSERVATION_START = "observation_start"
    EMERGENCY_VISIT = "emergency_visit"
    PATHWAY_CHANGED = "pathway_changed"


class CoverageClassification(BaseModel):
    coverage_id: str | None
    order: int | None
    payor: str | None
    plan: str | None
    pathway: Pathway
    matched_by: str  # "coding", "keyword", or "unmatched"
    evidence: str | None = None


class PathwayDetermination(BaseModel):
    pathway: Pathway
    confidence: str  # "high", "medium", "low"
    needs_verification: bool
    primary: CoverageClassification | None = None
    coverages: list[CoverageClassification] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ClinicalEvent(BaseModel):
    type: EventType
    encounter_id: str | None = None
    occurred_at: datetime | None = None


class CaseManagerTask(BaseModel):
    dedup_key: str
    rule_id: str
    title: str
    description: str
    priority: str
    assignee_role: str
    patient_id: str
    patient_mrn: str | None = None
    encounter_id: str | None = None
    pathway: Pathway
    payor: str | None = None
    event: EventType
    due_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Evaluation(BaseModel):
    patient_id: str
    determination: PathwayDetermination
    events: list[ClinicalEvent]
    tasks: list[CaseManagerTask]
    dispatched: list[str] = Field(default_factory=list)  # dedup keys newly dispatched


class PatientContext(BaseModel):
    """Everything the engine needs about one patient, as raw FHIR R4 resources."""

    patient: Resource
    coverages: list[Resource] = Field(default_factory=list)
    encounters: list[Resource] = Field(default_factory=list)

    @property
    def patient_id(self) -> str:
        return self.patient["id"]
