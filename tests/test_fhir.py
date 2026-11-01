from conftest import coverage, patient

from flow_access.fhir.extract import patient_mrn
from flow_access.models import CaseManagerTask, EventType, Pathway
from flow_access.sinks import to_fhir_task


def test_token_is_cached_and_search_follows_pages(fhir, fake_epic):
    fake_epic.add(patient(), *[coverage(f"c{i}", "Aetna", code="5") for i in range(3)])
    results = list(fhir.search("Coverage", {"patient": "p1"}))
    assert [r["id"] for r in results] == ["c0", "c1", "c2"]
    assert fhir.read("Patient", "p1")["id"] == "p1"
    assert fake_epic.token_requests == 1


def test_expired_token_is_refreshed_on_401(fhir, fake_epic, auth):
    fake_epic.add(patient())
    auth.access_token()
    auth._token.value = "stale"
    assert fhir.read("Patient", "p1")["id"] == "p1"
    assert fake_epic.token_requests == 2


def test_jwks_exposes_public_key(auth):
    key = auth.jwks()["keys"][0]
    assert key["kty"] == "RSA" and key["alg"] == "RS384" and key["kid"] == "kid-1"
    assert "d" not in key


def test_mrn_preferred_over_epic_enterprise_id():
    p = {"identifier": [{"type": {"text": "EPI"}, "value": "203711"},
                        {"type": {"coding": [{"code": "MR"}]}, "value": "E3826"}]}
    assert patient_mrn(p) == "E3826"
    assert patient_mrn({"identifier": [{"type": {"text": "EPI"}, "value": "203711"}]}) == "203711"


def test_fhir_task_mapping():
    task = CaseManagerTask(
        dedup_key="r|p1|e1", rule_id="r", title="Deliver IM", description="d", priority="urgent",
        assignee_role="rn_case_manager", patient_id="p1", encounter_id="e1",
        pathway=Pathway.MEDICARE_TRADITIONAL, event=EventType.INPATIENT_ADMISSION,
    )
    resource = to_fhir_task(task)
    assert resource["for"] == {"reference": "Patient/p1"}
    assert resource["encounter"] == {"reference": "Encounter/e1"}
    assert resource["priority"] == "urgent"
    assert resource["identifier"][0]["value"] == "r|p1|e1"
