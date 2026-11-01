from datetime import date, datetime, timezone

import pytest
from conftest import coverage, condition, encounter, order, patient, practitioner
from fastapi.testclient import TestClient

from flow_access.api import create_app
from flow_access.clinical import diagnoses
from flow_access.models import NeedType, Pathway, PatientContext
from flow_access.pcs import TransportRequest, fill_pdf
from flow_access.plans import PcsFormConfig
from flow_access.settings import Settings

NOW = datetime(2026, 11, 1, 18, tzinfo=timezone.utc)


def la_care(cid="c1", plan="L.A. Care Medi-Cal", member="91234567A", group=None):
    cov = coverage(cid, "L.A. CARE HEALTH PLAN", plan=plan)
    cov["subscriberId"] = member
    if group:
        cov["class"].append({"type": {"coding": [{"code": "group"}]}, "value": group, "name": group})
    return cov


def full_patient():
    p = patient()
    p.update({
        "birthDate": "1948-03-15",
        "gender": "female",
        "name": [{"use": "official", "family": "Lopez", "given": ["Maria"]}],
        "telecom": [{"system": "phone", "value": "323-555-0199"}],
        "address": [{"line": ["123 Main St"], "city": "Los Angeles", "state": "CA", "postalCode": "90012"}],
    })
    return p


def stroke_context(*orders, coverages=None):
    return PatientContext(
        patient=full_patient(),
        coverages=coverages or [la_care()],
        encounters=[encounter()],
        conditions=[
            condition("d1", "I63.512", "Cerebral infarction due to occlusion of right middle cerebral artery",
                      encounter_id="e1"),
            condition("d2", "G81.94", "Hemiplegia, unspecified affecting left nondominant side"),
            condition("d3", "Z99.81", "Dependence on supplemental oxygen", category="problem-list-item"),
            condition("d4", "E11.9", "Type 2 diabetes mellitus without complications", category="problem-list-item"),
            condition("d5", "J18.9", "Pneumonia, unspecified organism", status="resolved"),
        ],
        service_requests=list(orders),
        practitioners=[practitioner()],
    )


@pytest.mark.parametrize("payor,plan,expected_plan,expected_pathway", [
    ("L.A. CARE HEALTH PLAN", "L.A. Care Medi-Cal", "la_care", Pathway.MEDICAID_MANAGED_CARE),
    ("L.A. Care Health Plan", "L.A. Care Medicare Plus", "la_care", Pathway.DUAL_ELIGIBLE),
    ("L.A. Care Health Plan", "L.A. Care Covered", "la_care", Pathway.COMMERCIAL),
    ("Blue Shield of California Promise Health Plan", "Medi-Cal", "blue_shield_promise", Pathway.MEDICAID_MANAGED_CARE),
    ("MOLINA HEALTHCARE OF CALIFORNIA", "Molina Medi-Cal", "molina", Pathway.MEDICAID_MANAGED_CARE),
    ("Molina Healthcare", "Molina Medicare Complete Care", "molina", Pathway.DUAL_ELIGIBLE),
])
def test_priority_plans_identified(classifier, packets, payor, plan, expected_plan, expected_pathway):
    cov = coverage("c1", payor, plan=plan)
    assert [m.plan_id for m in packets.directory.match([cov], NOW)] == [expected_plan]
    assert classifier.determine([cov], NOW).pathway == expected_pathway


def test_plan_keywords_match_whole_words_only(classifier, packets):
    cov = coverage("c1", "Villa Care Benefits")
    assert packets.directory.match([cov], NOW) == []
    assert classifier.determine([cov], NOW).pathway == Pathway.UNKNOWN


def test_optum_delegated_group_takes_precedence_for_network(packets):
    cov = la_care(group="OPTUM CARE NETWORK - LOS ANGELES")
    matches = packets.directory.match([cov], NOW)
    assert [m.plan_id for m in matches] == ["optum", "la_care"]
    network, agencies = packets.directory.agencies_for(matches, NeedType.SNF)
    assert network.id == "optum"
    assert agencies[0].name.startswith("EXAMPLE Skilled Nursing Facility D")
    docs = packets.directory.documents_for(packets.directory.managing_profile(matches), NeedType.SNF)
    assert "PASRR Level I screening" in docs
    assert any("Optum SNF authorization" in d for d in docs)


def test_needs_detected_from_active_orders(packets):
    events = packets.detector.events([
        order("o1", "Ambulance / wheelchair van transport at discharge"),
        order("o2", "Referral to skilled nursing facility"),
        order("o3", "Home health PT/OT and RN"),
        order("o4", "DME: front-wheeled walker"),
        order("o5", "Home oxygen", status="completed"),
        order("o6", "CBC with differential"),
    ])
    assert [(e.need, e.source_id) for e in events] == [
        (NeedType.TRANSPORTATION, "o1"), (NeedType.SNF, "o2"), (NeedType.HOME_HEALTH, "o3"), (NeedType.DME, "o4"),
    ]


def test_diagnoses_prioritize_current_encounter_and_skip_resolved():
    ctx = stroke_context()
    codes = [d.code for d in diagnoses(ctx.conditions, "e1")]
    assert codes == ["I63.512", "G81.94", "Z99.81", "E11.9"]


def test_pcs_prefilled_with_diagnoses_and_justification(packets):
    ctx = stroke_context(order("o1", "Wheelchair van transport to SNF"))
    plans = packets.directory.match(ctx.coverages, NOW)
    request = TransportRequest(destination="Example SNF", reason="Discharge to skilled nursing facility")
    form = packets.pcs(ctx, plans, packets.directory.managing_profile(plans), ["o1"], request, "e1", NOW)

    assert form.form_name == "L.A. Care Physician Certification Statement (PCS)"
    assert form.patient.name == "Maria Lopez"
    assert form.patient.member_id == "91234567A"
    assert form.patient.age == 78
    assert form.prescriber.name == "Ana Rivera" and form.prescriber.npi == "1234567893"
    assert form.diagnoses[0].code == "I63.512"
    assert form.recommended_mode == form.mode == "wheelchair_van"
    assert any("G81.94" in limit for limit in form.function_limitations)
    assert any("supplemental oxygen" in limit for limit in form.function_limitations)
    assert form.clinical_justification.startswith("78-year-old patient with Cerebral infarction")
    assert "requires wheelchair van transportation" in form.clinical_justification
    assert form.start_date == form.end_date == date(2026, 11, 1)
    assert form.status.startswith("DRAFT")
    assert form.missing == ["Prescriber signature and date"]


def test_pcs_without_coded_limitations_leaves_justification_to_prescriber(packets):
    ctx = PatientContext(patient=full_patient(), coverages=[la_care()],
                         conditions=[condition("d1", "E11.9", "Type 2 diabetes mellitus without complications")])
    form = packets.pcs(ctx, packets.directory.match(ctx.coverages, NOW), None, now=NOW)
    assert form.mode is None
    assert "[Prescriber to document the functional limitations" in form.clinical_justification
    assert "Mode of transportation" in form.missing and "Prescribing provider name and NPI" in form.missing


def test_recurring_transport_capped_at_12_months(packets):
    ctx = stroke_context()
    request = TransportRequest(trip_type="recurring", frequency="3x weekly (dialysis)",
                               start_date=date(2026, 11, 2), end_date=date(2028, 1, 1), mode="litter_van")
    form = packets.pcs(ctx, [], None, transport=request, now=NOW)
    assert form.end_date == date(2027, 11, 1)
    assert form.mode == "litter_van" and form.mode_label == "Litter van (gurney)"


def test_evaluation_triggers_plan_and_need_tasks_with_details(service, sink):
    ctx = stroke_context(order("o1", "Wheelchair van transport at discharge"), order("o2", "Home health RN/PT"))
    result = service.evaluate(ctx, now=NOW)

    by_rule = {t.rule_id: t for t in sink.sent}
    assert set(by_rule) == {"plan-discharge-planning", "managed-care-admission-notification",
                            "medicaid-eligibility-verification", "pcs-transport", "home-health-referral"}
    assert [p.plan_id for p in result.plans] == ["la_care"]

    planning = by_rule["plan-discharge-planning"]
    assert planning.details["managing_entity"] == "L.A. Care Health Plan"

    hh = by_rule["home-health-referral"]
    assert hh.order_id == "o2"
    assert hh.details["agency_network"] == "L.A. Care Health Plan"
    assert hh.details["agencies"][0]["name"].startswith("EXAMPLE Home Health Agency A")
    assert "Face-to-face encounter documentation related to the primary reason for home health" in hh.details["documents"]

    pcs = by_rule["pcs-transport"]
    assert pcs.details["pcs_form"]["recommended_mode"] == "wheelchair_van"
    assert pcs.details["pcs_form"]["diagnoses"][0].startswith("I63.512")

    assert service.evaluate(ctx, now=NOW).dispatched == []


def test_live_packet_and_pcs_endpoints(service, fake_epic, tmp_path):
    ctx = stroke_context(order("o1", "Wheelchair van transport at discharge"))
    fake_epic.add(ctx.patient, *ctx.coverages, *ctx.conditions, *ctx.service_requests, *ctx.practitioners)
    client = TestClient(create_app(settings=Settings(db_path=tmp_path / "x.db"), service=service))

    packet = client.post("/patients/p1/packet", json={"needs": ["transportation", "snf"]}).json()
    assert packet["managing_entity"] == "L.A. Care Health Plan"
    transport, snf = packet["needs"]
    assert transport["ordered"] is True and transport["order_ids"] == ["o1"]
    assert transport["pcs_form"]["prescriber"]["npi"] == "1234567893"
    assert snf["ordered"] is False and snf["agencies"]

    html = client.post("/patients/p1/pcs", json={"transport": {"destination": "Home"}})
    assert html.status_code == 200 and "text/html" in html.headers["content-type"]
    assert "I63.512" in html.text and "Maria Lopez" in html.text and "DRAFT" in html.text

    pdf = client.post("/patients/p1/pcs", params={"format": "pdf"})
    assert pdf.status_code == 409


def _fillable_pdf(path):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for i, name in enumerate(["MemberName", "MemberID", "Diagnoses", "Mode", "Justification"]):
        c.acroForm.textfield(name=name, x=50, y=700 - i * 40, width=400, height=20)
    c.showPage()
    c.save()


def test_fill_plan_pdf_template(packets, tmp_path):
    from pypdf import PdfReader

    template = tmp_path / "pcs.pdf"
    _fillable_pdf(template)
    ctx = stroke_context()
    form = packets.pcs(ctx, packets.directory.match(ctx.coverages, NOW), None, now=NOW)
    pdf = fill_pdf(form, template, {
        "MemberName": "patient.name",
        "MemberID": "patient.member_id",
        "Diagnoses": "diagnoses",
        "Mode": "mode_label",
        "Justification": "clinical_justification",
    })
    out = tmp_path / "out.pdf"
    out.write_bytes(pdf)
    fields = {k: v.get("/V") for k, v in PdfReader(str(out)).get_fields().items()}
    assert fields["MemberName"] == "Maria Lopez"
    assert fields["MemberID"] == "91234567A"
    assert fields["Diagnoses"].startswith("I63.512 Cerebral infarction")
    assert fields["Mode"] == "Wheelchair van"
    assert PcsFormConfig().name.startswith("Physician Certification Statement")
