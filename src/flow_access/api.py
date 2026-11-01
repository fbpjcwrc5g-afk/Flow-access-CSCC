import hmac
import logging

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .fhir.auth import BackendServicesAuth
from .models import CaseManagerTask, Evaluation, PatientContext
from .service import PathwayService
from .settings import Settings, get_settings
from .wiring import build_auth, build_service

log = logging.getLogger(__name__)


class BundleRequest(BaseModel):
    bundle: dict
    dispatch: bool = False


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

    return app


def context_from_bundle(bundle: dict) -> PatientContext:
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])]
    patients = [r for r in resources if r.get("resourceType") == "Patient"]
    if len(patients) != 1:
        raise ValueError("Bundle must contain exactly one Patient resource")
    return PatientContext(
        patient=patients[0],
        coverages=[r for r in resources if r.get("resourceType") == "Coverage"],
        encounters=[r for r in resources if r.get("resourceType") == "Encounter"],
    )
