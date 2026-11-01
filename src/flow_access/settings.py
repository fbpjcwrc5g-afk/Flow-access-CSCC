from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).parent / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLOW_", env_file=".env", extra="ignore")

    # Epic FHIR R4 endpoint, e.g. https://fhir.example-health.org/api/FHIR/R4
    epic_fhir_base_url: str = ""
    # Epic OAuth2 token endpoint, e.g. https://fhir.example-health.org/oauth2/token
    epic_token_url: str = ""
    # Non-production or production client ID from the Epic app registration (Backend Systems app).
    epic_client_id: str = ""
    epic_private_key_path: Path | None = None
    epic_key_id: str = "flow-access-1"
    epic_scopes: str = (
        "system/Patient.read system/Coverage.read system/Encounter.read system/Condition.read "
        "system/ServiceRequest.read system/Practitioner.read"
    )
    # Epic Group FHIR ID used for Bulk Data $export (the registry/panel of patients to monitor).
    epic_bulk_group_id: str = ""

    # Shared secret that inbound event notifications must present as a Bearer token.
    webhook_token: str = ""

    # Where triggered case manager tasks are delivered: "log", "webhook", "fhir_task" (comma-separated).
    task_sinks: str = "log"
    task_webhook_url: str = ""
    task_webhook_token: str = ""

    db_path: Path = Path("flow_access.db")
    pathway_rules_path: Path = CONFIG_DIR / "pathway_rules.yaml"
    workflow_rules_path: Path = CONFIG_DIR / "workflow_rules.yaml"
    plans_path: Path = CONFIG_DIR / "plans.yaml"
    agencies_path: Path = CONFIG_DIR / "contracted_agencies.csv"
    transport_criteria_path: Path = CONFIG_DIR / "transport_criteria.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()
