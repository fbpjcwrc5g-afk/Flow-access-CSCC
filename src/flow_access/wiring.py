from .clinical import TransportCriteria
from .fhir.auth import BackendServicesAuth
from .fhir.client import FhirClient
from .packets import PacketBuilder
from .pathway import PathwayClassifier
from .plans import PlanDirectory
from .service import PathwayService
from .settings import Settings
from .sinks import FhirTaskSink, LogSink, TaskSink, WebhookSink
from .store import Store
from .workflows import WorkflowEngine, load_rules


def build_auth(settings: Settings) -> BackendServicesAuth | None:
    if not (settings.epic_client_id and settings.epic_token_url and settings.epic_private_key_path):
        return None
    return BackendServicesAuth(
        client_id=settings.epic_client_id,
        token_url=settings.epic_token_url,
        private_key_pem=settings.epic_private_key_path.read_bytes(),
        key_id=settings.epic_key_id,
        scopes=settings.epic_scopes,
    )


def build_service(settings: Settings, auth: BackendServicesAuth | None = None) -> PathwayService:
    fhir = FhirClient(settings.epic_fhir_base_url, auth) if settings.epic_fhir_base_url else None

    sinks: list[TaskSink] = []
    for name in (s.strip() for s in settings.task_sinks.split(",") if s.strip()):
        if name == "log":
            sinks.append(LogSink())
        elif name == "webhook":
            if not settings.task_webhook_url:
                raise ValueError("task_sinks includes 'webhook' but FLOW_TASK_WEBHOOK_URL is not set")
            sinks.append(WebhookSink(settings.task_webhook_url, settings.task_webhook_token))
        elif name == "fhir_task":
            if not fhir:
                raise ValueError("task_sinks includes 'fhir_task' but FLOW_EPIC_FHIR_BASE_URL is not set")
            sinks.append(FhirTaskSink(fhir))
        else:
            raise ValueError(f"Unknown task sink: {name}")

    return PathwayService(
        classifier=PathwayClassifier.from_file(settings.pathway_rules_path),
        engine=WorkflowEngine(load_rules(settings.workflow_rules_path)),
        store=Store(settings.db_path),
        sinks=sinks,
        packets=build_packet_builder(settings),
        fhir=fhir,
    )


def build_packet_builder(settings: Settings) -> PacketBuilder:
    return PacketBuilder(
        PlanDirectory.from_files(settings.plans_path, settings.agencies_path),
        TransportCriteria.from_file(settings.transport_criteria_path),
    )
