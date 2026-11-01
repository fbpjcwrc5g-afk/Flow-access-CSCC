"""Destinations for triggered case manager tasks."""

import logging
from typing import Protocol

import httpx

from .fhir.client import FhirClient
from .models import CaseManagerTask

log = logging.getLogger(__name__)

FHIR_PRIORITY = {"urgent": "urgent", "high": "asap", "normal": "routine", "low": "routine"}


class TaskSink(Protocol):
    def send(self, task: CaseManagerTask) -> None: ...


class LogSink:
    """Logs task metadata only; patient demographics are never written to logs."""

    def send(self, task: CaseManagerTask) -> None:
        log.info(
            "case-manager task rule=%s priority=%s patient=%s encounter=%s pathway=%s due=%s",
            task.rule_id,
            task.priority,
            task.patient_id,
            task.encounter_id,
            task.pathway.value,
            task.due_at.isoformat() if task.due_at else None,
        )


class WebhookSink:
    """POSTs the task as JSON to a care management platform or integration engine."""

    def __init__(self, url: str, token: str = "", http: httpx.Client | None = None):
        self.url = url
        self.token = token
        self._http = http or httpx.Client(timeout=15)

    def send(self, task: CaseManagerTask) -> None:
        headers = {"Content-Type": "application/json", "Idempotency-Key": task.dedup_key}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resp = self._http.post(self.url, content=task.model_dump_json(), headers=headers)
        resp.raise_for_status()


def to_fhir_task(task: CaseManagerTask) -> dict:
    resource = {
        "resourceType": "Task",
        "status": "requested",
        "intent": "order",
        "priority": FHIR_PRIORITY.get(task.priority, "routine"),
        "code": {"text": task.title},
        "description": task.description,
        "for": {"reference": f"Patient/{task.patient_id}"},
        "authoredOn": task.created_at.isoformat(),
        "identifier": [{"system": "urn:flow-access:task", "value": task.dedup_key}],
        "performerType": [{"text": task.assignee_role}],
        "note": [{"text": f"Insurance pathway: {task.pathway.value}; payor: {task.payor or 'unknown'}"}],
    }
    if task.encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{task.encounter_id}"}
    if task.due_at:
        resource["restriction"] = {"period": {"end": task.due_at.isoformat()}}
    return resource


class FhirTaskSink:
    """Writes a FHIR Task back to the EHR. Requires Task.create write access in your Epic app registration."""

    def __init__(self, client: FhirClient):
        self.client = client

    def send(self, task: CaseManagerTask) -> None:
        self.client.create(to_fhir_task(task))
