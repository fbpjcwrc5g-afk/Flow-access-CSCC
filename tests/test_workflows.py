from datetime import datetime, timezone

from conftest import coverage, encounter, patient

from flow_access.models import EventType, PatientContext
from flow_access.workflows import encounter_events

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def run(classifier, engine, coverages, encounters):
    ctx = PatientContext(patient=patient(), coverages=coverages, encounters=encounters)
    det = classifier.determine(coverages, NOW)
    events = [e for enc in encounters for e in encounter_events(enc)]
    return engine.tasks_for(ctx, det, events, now=NOW)


def test_encounter_events():
    assert [e.type for e in encounter_events(encounter(cls="IMP"))] == [EventType.INPATIENT_ADMISSION]
    finished = encounter(cls="IMP", status="finished", end="2026-11-04T10:00:00Z")
    assert [e.type for e in encounter_events(finished)] == [EventType.INPATIENT_DISCHARGE]
    assert [e.type for e in encounter_events(encounter(cls="OBSENC"))] == [EventType.OBSERVATION_START]
    assert [e.type for e in encounter_events(encounter(cls="EMER"))] == [EventType.EMERGENCY_VISIT]
    assert encounter_events(encounter(cls="AMB")) == []
    assert encounter_events(encounter(cls="IMP", status="planned")) == []


def test_medicare_admission_triggers_im_with_48h_deadline(classifier, engine):
    tasks = run(classifier, engine, [coverage("c1", "Medicare", code="1")], [encounter()])
    by_rule = {t.rule_id: t for t in tasks}
    assert set(by_rule) == {"medicare-im-admission", "medicare-snf-planning"}
    im = by_rule["medicare-im-admission"]
    assert im.priority == "urgent"
    assert im.due_at == datetime(2026, 11, 3, 8, tzinfo=timezone.utc)
    assert im.patient_mrn == "E1001"
    assert im.dedup_key == "medicare-im-admission|p1|e1"


def test_medicare_advantage_observation_gets_moon_and_plan_notification(classifier, engine):
    tasks = run(classifier, engine, [coverage("c1", "Plan", code="11")], [encounter(cls="OBSENC")])
    assert {t.rule_id for t in tasks} == {"medicare-moon-observation", "managed-care-admission-notification"}


def test_commercial_admission_requires_payer_notification(classifier, engine):
    tasks = run(classifier, engine, [coverage("c1", "Aetna", code="5")], [encounter()])
    assert [t.rule_id for t in tasks] == ["managed-care-admission-notification"]


def test_uninsured_ed_visit_gets_financial_counseling(classifier, engine):
    tasks = run(classifier, engine, [], [encounter(cls="EMER")])
    assert [t.rule_id for t in tasks] == ["financial-counseling-referral"]


def test_dual_admission_combines_requirements(classifier, engine):
    tasks = run(
        classifier, engine,
        [coverage("c1", "Medicare", code="1", order=1), coverage("c2", "Medicaid", code="2", order=2)],
        [encounter()],
    )
    assert {t.rule_id for t in tasks} == {
        "medicare-im-admission", "medicare-snf-planning", "medicaid-eligibility-verification",
        "dual-benefit-coordination",
    }
