from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path("/opt/newsflow-mvp")
OPENCLAW_CONFIG = Path("/root/.openclaw/openclaw.json")


@dataclass
class Settings:
    postgres_dsn: str
    otlp_endpoint: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    timezone: str = "Asia/Shanghai"
    discussion_default_seconds: int = 600
    discussion_test_seconds: int = 45
    force_reject_once: bool = True
    grafana_url: str = "http://127.0.0.1:3000"
    grafana_user: str = "admin"
    grafana_password: str = "deepflow"
    public_base_url: str = "http://43.130.233.109"
    project_retrospective_default_seconds: int = 600
    project_retrospective_test_seconds: int = 45
    project_next_cycle_delay_default_seconds: int = 300
    project_next_cycle_delay_test_seconds: int = 15
    project_max_cycles_default: int = 10
    project_max_consecutive_failures: int = 2
    project_min_available_memory_mb: int = 1024
    project_min_free_disk_gb: int = 5


def _load_openclaw_provider() -> tuple[str, str, str]:
    data = json.loads(OPENCLAW_CONFIG.read_text())
    primary = data["agents"]["defaults"]["model"]["primary"]
    provider_name, model_name = primary.split("/", 1)
    provider = data["models"]["providers"][provider_name]
    return provider["baseUrl"], provider["apiKey"], model_name


def load_settings() -> Settings:
    base_url, api_key, model_name = _load_openclaw_provider()
    return Settings(
        postgres_dsn=os.getenv(
            "NEWSFLOW_POSTGRES_DSN",
            "postgresql://postgres:postgres@127.0.0.1:15432/newsflow_mvp",
        ),
        otlp_endpoint=os.getenv("NEWSFLOW_OTLP_ENDPOINT", "http://127.0.0.1:38086"),
        llm_base_url=os.getenv("NEWSFLOW_LLM_BASE_URL", base_url),
        llm_api_key=os.getenv("NEWSFLOW_LLM_API_KEY", api_key),
        llm_model=os.getenv("NEWSFLOW_LLM_MODEL", model_name),
        project_retrospective_default_seconds=int(
            os.getenv("NEWSFLOW_PROJECT_RETROSPECTIVE_DEFAULT_SECONDS", "600")
        ),
        project_retrospective_test_seconds=int(
            os.getenv("NEWSFLOW_PROJECT_RETROSPECTIVE_TEST_SECONDS", "45")
        ),
        project_next_cycle_delay_default_seconds=int(
            os.getenv("NEWSFLOW_PROJECT_NEXT_CYCLE_DELAY_DEFAULT_SECONDS", "300")
        ),
        project_next_cycle_delay_test_seconds=int(
            os.getenv("NEWSFLOW_PROJECT_NEXT_CYCLE_DELAY_TEST_SECONDS", "15")
        ),
        project_max_cycles_default=int(os.getenv("NEWSFLOW_PROJECT_MAX_CYCLES_DEFAULT", "10")),
        project_max_consecutive_failures=int(
            os.getenv("NEWSFLOW_PROJECT_MAX_CONSECUTIVE_FAILURES", "2")
        ),
        project_min_available_memory_mb=int(
            os.getenv("NEWSFLOW_PROJECT_MIN_AVAILABLE_MEMORY_MB", "1024")
        ),
        project_min_free_disk_gb=int(os.getenv("NEWSFLOW_PROJECT_MIN_FREE_DISK_GB", "5")),
    )
