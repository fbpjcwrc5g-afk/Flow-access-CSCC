"""Turn encounters and pathway determinations into RN case manager tasks."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .fhir.extract import encounter_class_code, parse_datetime, patient_mrn
from .models import (
    CaseManagerTask,
    ClinicalEvent,
    EventType,
    Pathway,
    PathwayDetermination,
    PatientContext,
)

Resource = dict[str, Any]

# v3 ActEncounterCode values Epic uses for Encounter.class.
INPATIENT_CLASSES = {"IMP", "ACUTE", "NONAC"}
OBSERVATION_CLASSES = {"OBSENC"}
EMERGENCY_CLASSES = {"EMER"}
ACTIVE_STATUSES = {"arrived", "triaged", "in-progress", "onleave"}


class WorkflowRule(BaseModel):
    id: str
    title: str
    description: str
    events: list[EventType]
    pathways: list[Pathway] | None = None
    priority: str = "normal"
    assignee_role: str = "rn_case_manager"
    due_within_hours: float | None = None

    def matches(self, pathway: Pathway, event: EventType) -> bool:
        return event in self.events and (self.pathways is None or pathway in self.pathways)


def load_rules(path: Path) -> list[WorkflowRule]:
    return [WorkflowRule(**r) for r in yaml.safe_load(path.read_text())["rules"]]


def encounter_events(encounter: Resource) -> list[ClinicalEvent]:
    cls = encounter_class_code(encounter)
    status = encounter.get("status")
    period = encounter.get("period", {})
    start, end = parse_datetime(period.get("start")), parse_datetime(period.get("end"))
    eid = encounter.get("id")

    if cls in INPATIENT_CLASSES:
        if status in ACTIVE_STATUSES:
            return [ClinicalEvent(type=EventType.INPATIENT_ADMISSION, encounter_id=eid, occurred_at=start)]
        if status == "finished":
            return [ClinicalEvent(type=EventType.INPATIENT_DISCHARGE, encounter_id=eid, occurred_at=end)]
    elif cls in OBSERVATION_CLASSES and status in ACTIVE_STATUSES:
        return [ClinicalEvent(type=EventType.OBSERVATION_START, encounter_id=eid, occurred_at=start)]
    elif cls in EMERGENCY_CLASSES and status in ACTIVE_STATUSES | {"finished"}:
        return [ClinicalEvent(type=EventType.EMERGENCY_VISIT, encounter_id=eid, occurred_at=start)]
    return []


class WorkflowEngine:
    def __init__(self, rules: list[WorkflowRule]):
        self.rules = rules

    def tasks_for(
        self,
        context: PatientContext,
        determination: PathwayDetermination,
        events: list[ClinicalEvent],
        now: datetime | None = None,
    ) -> list[CaseManagerTask]:
        now = now or datetime.now(timezone.utc)
        mrn = patient_mrn(context.patient)
        payor = determination.primary.payor if determination.primary else None
        tasks = []
        for event in events:
            for rule in self.rules:
                if not rule.matches(determination.pathway, event.type):
                    continue
                anchor = event.occurred_at or now
                scope = event.encounter_id or f"pathway:{determination.pathway.value}"
                tasks.append(
                    CaseManagerTask(
                        dedup_key=f"{rule.id}|{context.patient_id}|{scope}",
                        rule_id=rule.id,
                        title=rule.title,
                        description=" ".join(rule.description.split()),
                        priority=rule.priority,
                        assignee_role=rule.assignee_role,
                        patient_id=context.patient_id,
                        patient_mrn=mrn,
                        encounter_id=event.encounter_id,
                        pathway=determination.pathway,
                        payor=payor,
                        event=event.type,
                        due_at=anchor + timedelta(hours=rule.due_within_hours)
                        if rule.due_within_hours is not None
                        else None,
                        created_at=now,
                    )
                )
        return tasks
