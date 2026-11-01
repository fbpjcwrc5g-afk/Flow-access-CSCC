"""Orchestrates: fetch from Epic -> determine pathway -> derive events -> trigger case manager tasks."""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .fhir.client import FhirClient
from .fhir.extract import ref_id
from .models import ClinicalEvent, EventType, Evaluation, PatientContext
from .pathway import PathwayClassifier
from .sinks import TaskSink
from .store import Store
from .workflows import WorkflowEngine, encounter_events

log = logging.getLogger(__name__)

Resource = dict[str, Any]
BULK_CURSOR = "bulk_export_since"


class PathwayService:
    def __init__(
        self,
        classifier: PathwayClassifier,
        engine: WorkflowEngine,
        store: Store,
        sinks: list[TaskSink],
        fhir: FhirClient | None = None,
    ):
        self.classifier = classifier
        self.engine = engine
        self.store = store
        self.sinks = sinks
        self.fhir = fhir

    def evaluate(
        self, context: PatientContext, now: datetime | None = None, dispatch: bool = True
    ) -> Evaluation:
        now = now or datetime.now(timezone.utc)
        determination = self.classifier.determine(context.coverages, as_of=now)

        events = [e for enc in context.encounters for e in encounter_events(enc)]
        if dispatch:
            previous = self.store.swap_pathway(context.patient_id, determination.pathway)
            if previous is not None and previous != determination.pathway:
                events.append(ClinicalEvent(type=EventType.PATHWAY_CHANGED, occurred_at=now))

        tasks = self.engine.tasks_for(context, determination, events, now=now)
        evaluation = Evaluation(
            patient_id=context.patient_id, determination=determination, events=events, tasks=tasks
        )
        if dispatch:
            for task in tasks:
                if self._dispatch(task):
                    evaluation.dispatched.append(task.dedup_key)
        return evaluation

    def _dispatch(self, task) -> bool:
        if not self.store.claim_task(task):
            return False
        try:
            for sink in self.sinks:
                sink.send(task)
        except Exception:
            # Release the claim so the task is retried on the next notification or poll.
            self.store.release_task(task.dedup_key)
            log.exception("Failed to dispatch task %s", task.rule_id)
            raise
        return True

    # --- Live Epic access -------------------------------------------------------------------

    def _require_fhir(self) -> FhirClient:
        if not self.fhir:
            raise RuntimeError("Epic FHIR connection is not configured (set FLOW_EPIC_FHIR_BASE_URL).")
        return self.fhir

    def fetch_context(
        self,
        patient_id: str,
        encounter_ids: list[str] | None = None,
        encounters: list[Resource] | None = None,
    ) -> PatientContext:
        fhir = self._require_fhir()
        return PatientContext(
            patient=fhir.read("Patient", patient_id),
            coverages=list(fhir.search("Coverage", {"patient": patient_id})),
            encounters=(encounters or []) + [fhir.read("Encounter", eid) for eid in encounter_ids or []],
        )

    def evaluate_patient(self, patient_id: str, encounter_ids: list[str] | None = None) -> Evaluation:
        return self.evaluate(self.fetch_context(patient_id, encounter_ids))

    def handle_notification(self, payload: Resource) -> list[Evaluation]:
        """Process an inbound event notification and evaluate each affected patient."""
        fhir = self._require_fhir()
        encounters_by_patient: dict[str, list[Resource]] = defaultdict(list)
        patients: set[str] = set()

        for resource_type, resource_id, resource in parse_notification(payload):
            if resource_type == "Patient":
                patients.add(resource_id)
            elif resource_type == "Encounter":
                # Always evaluate the current Encounter state, not a partial or stale copy.
                resource = fhir.read("Encounter", resource_id)
                pid = ref_id(resource.get("subject"), "Patient")
                if pid:
                    patients.add(pid)
                    encounters_by_patient[pid].append(resource)
            elif resource_type == "Coverage":
                resource = resource or fhir.read("Coverage", resource_id)
                pid = ref_id(resource.get("beneficiary"), "Patient")
                if pid:
                    patients.add(pid)

        return [
            self.evaluate(self.fetch_context(pid, encounters=encounters_by_patient.get(pid)))
            for pid in sorted(patients)
        ]

    def poll_bulk(self, group_id: str) -> list[Evaluation]:
        """Run an incremental Group $export and evaluate every patient with changed data."""
        fhir = self._require_fhir()
        since = self.store.get_cursor(BULK_CURSOR)
        manifest = fhir.bulk_export(group_id, ["Patient", "Encounter", "Coverage"], since=since)

        patients: dict[str, Resource] = {}
        encounters: dict[str, list[Resource]] = defaultdict(list)
        changed: set[str] = set()
        for resource in fhir.iter_bulk_output(manifest):
            rtype = resource.get("resourceType")
            if rtype == "Patient":
                patients[resource["id"]] = resource
                changed.add(resource["id"])
            elif rtype == "Encounter" and (pid := ref_id(resource.get("subject"), "Patient")):
                encounters[pid].append(resource)
                changed.add(pid)
            elif rtype == "Coverage" and (pid := ref_id(resource.get("beneficiary"), "Patient")):
                changed.add(pid)

        evaluations = []
        for pid in sorted(changed):
            # An incremental export omits unchanged Coverage, so always fetch the full current set.
            context = PatientContext(
                patient=patients.get(pid) or fhir.read("Patient", pid),
                coverages=list(fhir.search("Coverage", {"patient": pid})),
                encounters=encounters.get(pid, []),
            )
            evaluations.append(self.evaluate(context))

        if manifest.get("transactionTime"):
            self.store.set_cursor(BULK_CURSOR, manifest["transactionTime"])
        return evaluations


def parse_notification(payload: Resource) -> list[tuple[str, str, Resource | None]]:
    """Extract (resourceType, id, resource-if-included) from the notification shapes we accept.

    Supported:
      * FHIR R4 Subscriptions Backport notification Bundles (focus references and/or full resources)
      * A bare FHIR resource (Encounter, Coverage, Patient)
      * A simple JSON body: {"patient_id": "...", "encounter_id": "..."}, e.g. from an
        integration engine translating HL7v2 ADT messages.
    """
    found: dict[tuple[str, str], Resource | None] = {}

    def add(rtype: str | None, rid: str | None, resource: Resource | None = None):
        if rtype in {"Patient", "Encounter", "Coverage"} and rid:
            found[(rtype, rid)] = resource or found.get((rtype, rid))

    def add_ref(ref: dict | None):
        if ref and "/" in ref.get("reference", ""):
            rtype, rid = ref["reference"].rstrip("/").split("/")[-2:]
            add(rtype, rid)

    rtype = payload.get("resourceType")
    if rtype == "Bundle":
        for entry in payload.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "Parameters":
                for param in resource.get("parameter", []):
                    if param.get("name") == "notification-event":
                        for part in param.get("part", []):
                            if part.get("name") == "focus":
                                add_ref(part.get("valueReference"))
            elif resource.get("resourceType") == "SubscriptionStatus":
                for event in resource.get("notificationEvent", []):
                    add_ref(event.get("focus"))
            else:
                add(resource.get("resourceType"), resource.get("id"), resource)
    elif rtype:
        add(rtype, payload.get("id"), payload)
    else:
        add("Patient", payload.get("patient_id"))
        add("Encounter", payload.get("encounter_id"))
    return [(rtype, rid, res) for (rtype, rid), res in found.items()]
