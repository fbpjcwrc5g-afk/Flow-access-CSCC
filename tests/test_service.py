import pytest
from conftest import RecordingSink, coverage, encounter, patient
from fastapi.testclient import TestClient

from flow_access.api import create_app
from flow_access.models import PatientContext
from flow_access.service import PathwayService, parse_notification
from flow_access.settings import Settings


def ctx(coverages, encounters):
    return PatientContext(patient=patient(), coverages=coverages, encounters=encounters)


def test_tasks_are_dispatched_once(service, sink):
    context = ctx([coverage("c1", "Medicare", code="1")], [encounter()])
    first = service.evaluate(context)
    second = service.evaluate(context)
    assert len(first.dispatched) == 2
    assert second.dispatched == []
    assert len(sink.sent) == 2
    assert len(service.store.list_tasks("p1")) == 2


def test_pathway_change_triggers_review(service, sink):
    service.evaluate(ctx([coverage("c1", "Aetna", code="5")], []))
    assert sink.sent == []
    result = service.evaluate(ctx([coverage("c2", "Medicare", code="1")], []))
    assert [t.rule_id for t in sink.sent] == ["pathway-change-review"]
    assert result.tasks[0].pathway.value == "medicare_traditional"


def test_failed_sink_releases_claim_for_retry(classifier, engine, store, packets):
    context = ctx([coverage("c1", "Aetna", code="5")], [encounter()])
    with pytest.raises(RuntimeError):
        PathwayService(classifier, engine, store, [RecordingSink(fail=True)], packets).evaluate(context)
    good = RecordingSink()
    PathwayService(classifier, engine, store, [good], packets).evaluate(context)
    assert [t.rule_id for t in good.sent] == ["managed-care-admission-notification"]


def test_parse_subscription_backport_bundle():
    bundle = {
        "resourceType": "Bundle",
        "type": "history",
        "entry": [{
            "resource": {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "type", "valueCode": "event-notification"},
                    {"name": "notification-event", "part": [
                        {"name": "event-number", "valueString": "1"},
                        {"name": "focus", "valueReference": {"reference": "Encounter/e9"}},
                    ]},
                ],
            }
        }],
    }
    assert parse_notification(bundle) == [("Encounter", "e9", None)]
    assert parse_notification({"patient_id": "p1", "encounter_id": "e1"}) == [
        ("Patient", "p1", None), ("Encounter", "e1", None)
    ]


def test_notification_end_to_end_through_api(service, sink, fake_epic, tmp_path):
    fake_epic.add(
        patient(),
        encounter(eid="e1", cls="IMP"),
        coverage("c1", "Medicare", code="1", order=1),
        coverage("c2", "Medicaid", code="2", order=2),
        coverage("c3", "Old Plan", code="5", status="cancelled"),
    )
    app = create_app(settings=Settings(webhook_token="s3cret", db_path=tmp_path / "x.db"), service=service)
    client = TestClient(app)

    assert client.post("/notifications/fhir", json={"encounter_id": "e1"}).status_code == 401
    resp = client.post(
        "/notifications/fhir", json={"resourceType": "Encounter", "id": "e1"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert resp.status_code == 202
    assert {t.rule_id for t in sink.sent} == {
        "medicare-im-admission", "medicare-snf-planning", "medicaid-eligibility-verification",
        "dual-benefit-coordination",
    }
    assert all(t.pathway.value == "dual_eligible" for t in sink.sent)
    assert fake_epic.token_requests == 1

    tasks = client.get("/tasks", params={"patient_id": "p1"}).json()
    assert len(tasks) == 4


def test_bundle_dry_run_does_not_dispatch(service, sink, tmp_path):
    app = create_app(settings=Settings(db_path=tmp_path / "x.db"), service=service)
    bundle = {"resourceType": "Bundle", "entry": [
        {"resource": patient()}, {"resource": coverage("c1", "Aetna", code="5")}, {"resource": encounter()},
    ]}
    resp = TestClient(app).post("/evaluate/bundle", json={"bundle": bundle})
    assert resp.status_code == 200
    body = resp.json()
    assert body["determination"]["pathway"] == "commercial"
    assert [t["rule_id"] for t in body["tasks"]] == ["managed-care-admission-notification"]
    assert body["dispatched"] == []
    assert sink.sent == []


def test_bulk_poll_evaluates_changed_patients_and_advances_cursor(service, sink, fake_epic):
    fake_epic.add(patient(), coverage("c1", "Self Pay", code="81"))
    fake_epic.bulk_output = [encounter(eid="e5", cls="EMER")]
    sleeps = []
    service.fhir.bulk_export = _no_sleep(service.fhir.bulk_export, sleeps)

    evaluations = service.poll_bulk("group-1")

    assert [e.patient_id for e in evaluations] == ["p1"]
    assert [t.rule_id for t in sink.sent] == ["financial-counseling-referral"]
    assert fake_epic.export_params == {"_type": "Patient,Encounter,Coverage"}
    assert sleeps == [1.0]
    assert service.store.get_cursor("bulk_export_since") == "2026-11-01T12:00:00Z"

    service.poll_bulk("group-1")
    assert fake_epic.export_params["_since"] == "2026-11-01T12:00:00Z"


def _no_sleep(fn, record):
    def wrapper(*args, **kwargs):
        return fn(*args, sleep=record.append, **kwargs)
    return wrapper
