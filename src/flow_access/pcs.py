"""Pre-filled Physician Certification Statement (PCS) forms for non-emergency medical transportation.

The form is always a DRAFT: the prescribing provider must review the justification, select the mode,
and sign. This module never signs or certifies on a provider's behalf.
"""

import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from pydantic import BaseModel, Field

from .clinical import Diagnosis, TransportAssessment, TransportCriteria, age_on, today
from .fhir.extract import patient_mrn
from .plans import PcsFormConfig

Resource = dict[str, Any]

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
MAX_CERTIFICATION_DAYS = 365
MAX_DIAGNOSES = 6
DRAFT_STATUS = "DRAFT: requires prescriber review, mode selection, and signature"
CERTIFICATION_TEXT = (
    "I certify that the information above is true and correct and that the patient's medical condition "
    "requires the mode of transportation indicated, because the patient cannot reasonably ambulate or be "
    "transported by public or private vehicle, including taxi, without risk to their health."
)

_env = Environment(loader=PackageLoader("flow_access", "templates"), autoescape=select_autoescape(["html"]))


class TransportRequest(BaseModel):
    """Case manager input that cannot be derived from the chart."""

    mode: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    trip_type: str = "one_way"  # one_way | round_trip | recurring
    frequency: str | None = None  # e.g. "3x weekly (dialysis)"
    origin: str | None = None
    destination: str | None = None
    reason: str | None = None  # e.g. "Discharge to skilled nursing facility"
    additional_limitations: list[str] = Field(default_factory=list)


class PcsPatient(BaseModel):
    name: str = ""
    birth_date: str = ""
    age: int | None = None
    gender: str = ""
    member_id: str = ""
    mrn: str = ""
    phone: str = ""
    address: str = ""


class PcsPrescriber(BaseModel):
    name: str = ""
    npi: str = ""
    phone: str = ""


class PcsForm(BaseModel):
    form_name: str
    plan_name: str = ""
    status: str = DRAFT_STATUS
    patient: PcsPatient
    prescriber: PcsPrescriber
    diagnoses: list[Diagnosis]
    recommended_mode: str | None
    mode: str | None
    mode_label: str = ""
    function_limitations: list[str]
    clinical_justification: str
    start_date: date
    end_date: date
    trip_type: str
    frequency: str = ""
    origin: str = ""
    destination: str = ""
    reason: str = ""
    certification_statement: str = CERTIFICATION_TEXT
    missing: list[str] = Field(default_factory=list)


def _human_name(resource: Resource) -> str:
    names = resource.get("name") or []
    name = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    if name.get("text"):
        return name["text"]
    return " ".join([*name.get("given", []), name.get("family", "")]).strip()


def _telecom(resource: Resource, system: str = "phone") -> str:
    return next((t.get("value", "") for t in resource.get("telecom", []) if t.get("system") == system), "")


def _address(resource: Resource) -> str:
    addr = next(iter(resource.get("address", [])), None)
    if not addr:
        return ""
    if addr.get("text"):
        return addr["text"]
    state_zip = " ".join(p for p in [addr.get("state"), addr.get("postalCode")] if p)
    return ", ".join(p for p in [*addr.get("line", []), addr.get("city"), state_zip] if p)


def _npi(practitioner: Resource) -> str:
    return next((i.get("value", "") for i in practitioner.get("identifier", []) if i.get("system") == NPI_SYSTEM), "")


def build_pcs(
    patient: Resource,
    dx: list[Diagnosis],
    assessment: TransportAssessment,
    criteria: TransportCriteria,
    form_config: PcsFormConfig,
    plan_name: str = "",
    member_id: str | None = None,
    prescriber: Resource | None = None,
    request: TransportRequest | None = None,
    now: datetime | None = None,
) -> PcsForm:
    request = request or TransportRequest()
    on = today(now)
    start = request.start_date or on
    if request.end_date:
        end = request.end_date
    elif request.trip_type == "recurring":
        end = start + timedelta(days=MAX_CERTIFICATION_DAYS - 1)
    else:
        end = start
    end = min(end, start + timedelta(days=MAX_CERTIFICATION_DAYS - 1))

    pcs_patient = PcsPatient(
        name=_human_name(patient),
        birth_date=patient.get("birthDate", ""),
        age=age_on(patient.get("birthDate"), on),
        gender=patient.get("gender", ""),
        member_id=member_id or "",
        mrn=patient_mrn(patient) or "",
        phone=_telecom(patient),
        address=_address(patient),
    )
    pcs_prescriber = PcsPrescriber(
        name=_human_name(prescriber) if prescriber else "",
        npi=_npi(prescriber) if prescriber else "",
        phone=_telecom(prescriber) if prescriber else "",
    )
    mode = request.mode or assessment.recommended_mode
    limitations = [f.statement for f in assessment.findings] + request.additional_limitations
    form = PcsForm(
        form_name=form_config.name,
        plan_name=plan_name,
        patient=pcs_patient,
        prescriber=pcs_prescriber,
        diagnoses=dx[:MAX_DIAGNOSES],
        recommended_mode=assessment.recommended_mode,
        mode=mode,
        mode_label=criteria.modes.get(mode, mode or ""),
        function_limitations=limitations,
        clinical_justification=_justification(pcs_patient, dx, limitations, criteria.modes.get(mode, mode)),
        start_date=start,
        end_date=end,
        trip_type=request.trip_type,
        frequency=request.frequency or "",
        origin=request.origin or "",
        destination=request.destination or "",
        reason=request.reason or "",
    )
    form.missing = _missing(form)
    return form


def _justification(patient: PcsPatient, dx: list[Diagnosis], limitations: list[str], mode_label: str | None) -> str:
    who = f"{patient.age}-year-old patient" if patient.age is not None else "Patient"
    sentences = []
    if dx:
        primary = f"{dx[0].display} ({dx[0].code})"
        others = ", ".join(f"{d.display} ({d.code})" for d in dx[1:MAX_DIAGNOSES])
        sentences.append(f"{who} with {primary}" + (f", with comorbidities including {others}." if others else "."))
    else:
        sentences.append(f"{who}. [Prescriber to document the diagnosis requiring transport.]")
    if limitations:
        sentences.append("Chart-coded findings supporting transport need: " + " ".join(limitations))
        transport = f"{mode_label.lower()} transportation" if mode_label else "the mode of transportation selected below"
        sentences.append(
            "Because of these limitations, the patient cannot safely travel by public transportation, "
            f"private vehicle, or taxi and requires {transport}."
        )
    else:
        sentences.append(
            "[Prescriber to document the functional limitations that prevent the patient from using "
            "public or private transportation.]"
        )
    return " ".join(sentences)


def _missing(form: PcsForm) -> list[str]:
    missing = []
    if not form.patient.member_id:
        missing.append("Member ID (Medi-Cal CIN / plan ID)")
    if not form.diagnoses:
        missing.append("Diagnosis")
    if not form.function_limitations:
        missing.append("Functional limitation justification")
    if not form.mode:
        missing.append("Mode of transportation")
    if not form.prescriber.name or not form.prescriber.npi:
        missing.append("Prescribing provider name and NPI")
    missing.append("Prescriber signature and date")
    return missing


def render_html(form: PcsForm) -> str:
    return _env.get_template("pcs_form.html").render(form=form)


def _resolve(data: dict, path: str) -> str:
    value: Any = data
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)] if part.isdigit() and int(part) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        if value is None:
            return ""
    if isinstance(value, list):
        return "; ".join(f"{v['code']} {v['display']}" if isinstance(v, dict) and "code" in v else str(v) for v in value)
    return str(value)


def fill_pdf(form: PcsForm, template: Path, field_map: dict[str, str]) -> bytes:
    """Fill the plan's own fillable PCS PDF using a field-name -> data-path mapping."""
    from pypdf import PdfReader, PdfWriter

    data = form.model_dump(mode="json")
    values = {field: _resolve(data, path) for field, path in field_map.items()}
    writer = PdfWriter(clone_from=PdfReader(str(template)))
    for page in writer.pages:
        writer.update_page_form_field_values(page, values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
