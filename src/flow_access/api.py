import hmac
import logging
from enum import StrEnum

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .fhir.auth import BackendServicesAuth
from .models import CaseManagerTask, Evaluation, NeedType, PatientContext
from .packets import CarePacket
from .pcs import TransportRequest, fill_pdf, render_html
from .service import PathwayService
from .settings import Settings, get_settings
from .wiring import build_auth, build_service

log = logging.getLogger(__name__)


class BundleRequest(BaseModel):
    bundle: dict
    dispatch: bool = False


class PacketRequest(BaseModel):
    needs: list[NeedType] | None = None
    transport: TransportRequest | None = None
    encounter_id: str | None = None


class BundlePacketRequest(PacketRequest):
    bundle: dict


class PcsFormat(StrEnum):
    JSON = "json"
    HTML = "html"
    PDF = "pdf"


def create_app(settings: Settings | None = None, service: PathwayService | None = None,
               auth: BackendServicesAuth | None = None) -> FastAPI:
    settings = settings or get_settings()
    auth = auth or build_auth(settings)
    service = service or build_service(settings, auth)
    app = FastAPI(title="Flow Access: Epic FHIR insurance pathway engine", version="0.1.0")

    def require_webhook_token(authorization: str = Header(default="")) -> None:
        if not settings.webhook_token:
            raise HTTPException(503, "Inbound notifications are disabled: FLOW_WEBHOOK_TOKEN is not set")
        expected = f"Bearer {settings.webhook_token}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "Invalid notification token")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "epic_configured": service.fhir is not None}

    @app.get("/.well-known/jwks.json")
    def jwks() -> dict:
        if not auth:
            raise HTTPException(404, "No signing key configured")
        return auth.jwks()

    @app.post("/notifications/fhir", status_code=202, dependencies=[Depends(require_webhook_token)])
    def fhir_notification(payload: dict, background: BackgroundTasks) -> dict:
        """Receive Epic/FHIR event notifications (Subscription bundles, resources, or ADT-derived JSON)."""

        def run() -> None:
            try:
                service.handle_notification(payload)
            except Exception:
                log.exception("Failed to process FHIR notification")

        background.add_task(run)
        return {"status": "accepted"}

    @app.post("/patients/{patient_id}/evaluate", response_model=Evaluation)
    def evaluate_patient(patient_id: str, encounter_id: list[str] = Query(default=[])) -> Evaluation:
        """Pull the patient's current data from Epic, determine the pathway, and trigger workflows."""
        if not service.fhir:
            raise HTTPException(503, "Epic FHIR connection is not configured")
        return service.evaluate_patient(patient_id, encounter_id)

    @app.post("/evaluate/bundle", response_model=Evaluation)
    def evaluate_bundle(request: BundleRequest) -> Evaluation:
        """Evaluate a FHIR Bundle containing one Patient plus Coverage/Encounter resources.

        Defaults to a dry run (no tasks dispatched), which is useful for testing rule changes.
        """
        try:
            context = context_from_bundle(request.bundle)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return service.evaluate(context, dispatch=request.dispatch)

    @app.get("/tasks", response_model=list[CaseManagerTask])
    def list_tasks(patient_id: str | None = None, limit: int = Query(default=100, le=1000)):
        return service.store.list_tasks(patient_id, limit)

    def live_context(patient_id: str) -> PatientContext:
        if not service.fhir:
            raise HTTPException(503, "Epic FHIR connection is not configured")
        return service.fetch_context(patient_id)

    def bundle_context(bundle: dict) -> PatientContext:
        try:
            return context_from_bundle(bundle)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/patients/{patient_id}/packet", response_model=CarePacket)
    def patient_packet(patient_id: str, request: PacketRequest | None = None) -> CarePacket:
        """Plan-specific documents, contracted agencies, and PCS draft for the patient's post-acute needs."""
        request = request or PacketRequest()
        return service.build_packet(live_context(patient_id), request.needs, request.transport, request.encounter_id)

    @app.post("/packet/bundle", response_model=CarePacket)
    def bundle_packet(request: BundlePacketRequest) -> CarePacket:
        return service.build_packet(
            bundle_context(request.bundle), request.needs, request.transport, request.encounter_id
        )

    @app.post("/patients/{patient_id}/pcs")
    def patient_pcs(patient_id: str, request: PacketRequest | None = None, format: PcsFormat = PcsFormat.HTML):
        """Pre-filled PCS transportation form (DRAFT until the prescriber reviews and signs)."""
        request = request or PacketRequest()
        return render_pcs(live_context(patient_id), request, format)

    @app.post("/pcs/bundle")
    def bundle_pcs(request: BundlePacketRequest, format: PcsFormat = PcsFormat.HTML):
        return render_pcs(bundle_context(request.bundle), request, format)

    def render_pcs(context: PatientContext, request: PacketRequest, fmt: PcsFormat) -> Response:
        packet = service.build_packet(context, [NeedType.TRANSPORTATION], request.transport, request.encounter_id)
        form = packet.needs[0].pcs_form
        if fmt == PcsFormat.JSON:
            return Response(form.model_dump_json(), media_type="application/json")
        if fmt == PcsFormat.HTML:
            return HTMLResponse(render_html(form))
        profile = service.packets.directory.managing_profile(packet.plans)
        if not profile or not profile.pcs_form.template_pdf or not profile.pcs_form.field_map:
            raise HTTPException(
                409, "No fillable PCS PDF is configured for this plan; use format=html and print to PDF."
            )
        pdf = fill_pdf(form, profile.pcs_form.template_pdf, profile.pcs_form.field_map)
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="pcs-{context.patient_id}.pdf"'})

    return app


def context_from_bundle(bundle: dict) -> PatientContext:
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])]
    patients = [r for r in resources if r.get("resourceType") == "Patient"]
    if len(patients) != 1:
        raise ValueError("Bundle must contain exactly one Patient resource")

    def of(rtype: str) -> list[dict]:
        return [r for r in resources if r.get("resourceType") == rtype]

    return PatientContext(
        patient=patients[0],
        coverages=of("Coverage"),
        encounters=of("Encounter"),
        conditions=of("Condition"),
        service_requests=of("ServiceRequest"),
        practitioners=of("Practitioner"),
    )
