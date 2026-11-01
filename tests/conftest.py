import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from flow_access.clinical import TransportCriteria
from flow_access.fhir.auth import BackendServicesAuth
from flow_access.fhir.client import FhirClient
from flow_access.packets import PacketBuilder
from flow_access.pathway import PathwayClassifier
from flow_access.plans import PlanDirectory
from flow_access.service import PathwayService
from flow_access.settings import CONFIG_DIR
from flow_access.store import Store
from flow_access.workflows import WorkflowEngine, load_rules

BASE = "https://epic.test/api/FHIR/R4"
TOKEN_URL = "https://epic.test/oauth2/token"
SOPT = "https://nahdo.org/sopt"


def patient(pid="p1", mrn="E1001"):
    return {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [{"type": {"coding": [{"code": "MR"}], "text": "MRN"}, "value": mrn}],
    }


def coverage(cid, payor, code=None, system=SOPT, order=None, status="active", start="2020-01-01", end=None, plan=None):
    cov = {
        "resourceType": "Coverage",
        "id": cid,
        "status": status,
        "beneficiary": {"reference": "Patient/p1"},
        "payor": [{"display": payor}],
        "period": {"start": start, **({"end": end} if end else {})},
        "type": {"coding": [{"system": system, "code": code}] if code else []},
    }
    if order is not None:
        cov["order"] = order
    if plan:
        cov["class"] = [{"type": {"coding": [{"code": "plan"}]}, "value": plan, "name": plan}]
    return cov


def encounter(eid="e1", cls="IMP", status="in-progress", start="2026-11-01T08:00:00Z", end=None, pid="p1"):
    return {
        "resourceType": "Encounter",
        "id": eid,
        "status": status,
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": cls},
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": start, **({"end": end} if end else {})},
    }


class RecordingSink:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, task):
        if self.fail:
            raise RuntimeError("sink down")
        self.sent.append(task)


@pytest.fixture
def classifier():
    return PathwayClassifier.from_file(CONFIG_DIR / "pathway_rules.yaml")


@pytest.fixture
def engine():
    return WorkflowEngine(load_rules(CONFIG_DIR / "workflow_rules.yaml"))


@pytest.fixture
def private_key_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


class FakeEpic:
    """In-memory stand-in for Epic's OAuth token endpoint and FHIR R4 API."""

    def __init__(self, public_key, client_id="client-123"):
        self.public_key = public_key
        self.client_id = client_id
        self.resources: dict[tuple[str, str], dict] = {}
        self.created: list[dict] = []
        self.token_requests = 0
        self.export_polls = 0
        self.bulk_output: list[dict] = []
        self.export_params = None

    def add(self, *resources):
        for r in resources:
            self.resources[(r["resourceType"], r["id"])] = r

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            return self._token(request)
        if request.headers.get("Authorization") != "Bearer token-abc":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/api/FHIR/R4/")
        if path.startswith("Group/") and path.endswith("/$export"):
            self.export_params = dict(request.url.params)
            return httpx.Response(202, headers={"Content-Location": f"{BASE}/bulk/status/1"})
        if path == "bulk/status/1":
            self.export_polls += 1
            if self.export_polls == 1:
                return httpx.Response(202, headers={"Retry-After": "1"})
            return httpx.Response(200, json={
                "transactionTime": "2026-11-01T12:00:00Z",
                "output": [{"type": "mixed", "url": f"{BASE}/bulk/file/1"}],
            })
        if path == "bulk/file/1":
            return httpx.Response(200, text="\n".join(json.dumps(r) for r in self.bulk_output))
        if request.method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            return httpx.Response(201, json={**body, "id": f"new-{len(self.created)}"})
        parts = path.split("/")
        if len(parts) == 2:
            res = self.resources.get((parts[0], parts[1]))
            return httpx.Response(200, json=res) if res else httpx.Response(404)
        if parts[0] == "Coverage":
            return self._coverage_search(request)
        if parts[0] in ("Condition", "ServiceRequest"):
            return self._subject_search(parts[0], request)
        return httpx.Response(404)

    def _subject_search(self, rtype, request):
        pid = request.url.params.get("patient")
        category = request.url.params.get("category")
        matches = [
            r for (t, _), r in self.resources.items()
            if t == rtype and r["subject"]["reference"] == f"Patient/{pid}"
            and (not category or any(c["coding"][0]["code"] == category for c in r.get("category", [])))
        ]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": r} for r in matches]})

    def _token(self, request):
        self.token_requests += 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["grant_type"] == "client_credentials"
        assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        claims = jwt.decode(form["client_assertion"], self.public_key, algorithms=["RS384"], audience=TOKEN_URL)
        assert claims["iss"] == claims["sub"] == self.client_id
        assert claims["exp"] - claims["iat"] <= 300
        return httpx.Response(200, json={"access_token": "token-abc", "token_type": "bearer", "expires_in": 3600})

    def _coverage_search(self, request):
        """Return matching Coverage split across two pages, plus an OperationOutcome like Epic does."""
        pid = request.url.params.get("patient")
        page = request.url.params.get("page", "1")
        matches = [r for (t, _), r in self.resources.items()
                   if t == "Coverage" and r["beneficiary"]["reference"] == f"Patient/{pid}"]
        half = (len(matches) + 1) // 2
        entries = matches[:half] if page == "1" else matches[half:]
        bundle = {"resourceType": "Bundle", "type": "searchset",
                  "entry": [{"resource": r} for r in entries], "link": []}
        if page == "1":
            bundle["entry"].append({"resource": {"resourceType": "OperationOutcome", "issue": []}})
            if len(matches) > half:
                bundle["link"].append({"relation": "next", "url": f"{BASE}/Coverage?patient={pid}&page=2"})
        return httpx.Response(200, json=bundle)


@pytest.fixture
def fake_epic(private_key_pem):
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    return FakeEpic(key.public_key())


@pytest.fixture
def epic_http(fake_epic):
    return httpx.Client(transport=httpx.MockTransport(fake_epic.handler))


@pytest.fixture
def auth(private_key_pem, epic_http):
    return BackendServicesAuth("client-123", TOKEN_URL, private_key_pem, "kid-1", "system/Coverage.read", http=epic_http)


@pytest.fixture
def fhir(auth, epic_http):
    return FhirClient(BASE, auth, http=epic_http)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


@pytest.fixture
def sink():
    return RecordingSink()


@pytest.fixture
def packets():
    return PacketBuilder(
        PlanDirectory.from_files(CONFIG_DIR / "plans.yaml", CONFIG_DIR / "contracted_agencies.csv"),
        TransportCriteria.from_file(CONFIG_DIR / "transport_criteria.yaml"),
    )


@pytest.fixture
def service(classifier, engine, store, sink, packets, fhir):
    return PathwayService(classifier, engine, store, [sink], packets, fhir=fhir)


def condition(cid, code, display, category="encounter-diagnosis", status="active", encounter_id=None):
    cond = {
        "resourceType": "Condition",
        "id": cid,
        "clinicalStatus": {"coding": [{"code": status}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "category": [{"coding": [{"code": category}]}],
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code, "display": display}]},
        "subject": {"reference": "Patient/p1"},
    }
    if encounter_id:
        cond["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    return cond


def order(oid, text, status="active", requester="dr1", encounter_id="e1", authored="2026-11-01T10:00:00Z"):
    return {
        "resourceType": "ServiceRequest",
        "id": oid,
        "status": status,
        "intent": "order",
        "code": {"text": text},
        "subject": {"reference": "Patient/p1"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
        "requester": {"reference": f"Practitioner/{requester}"},
        "authoredOn": authored,
    }


def practitioner(pid="dr1", npi="1234567893"):
    return {
        "resourceType": "Practitioner",
        "id": pid,
        "name": [{"use": "official", "family": "Rivera", "given": ["Ana"], "prefix": ["Dr."]}],
        "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": npi}],
        "telecom": [{"system": "phone", "value": "213-555-0100"}],
    }
