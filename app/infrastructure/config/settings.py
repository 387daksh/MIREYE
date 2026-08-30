from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Side-effect-free application configuration loaded from env or `.env`."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "demo", "production"] = "development"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000"]

    database_url: str | None = None
    workspace_db: Path = ROOT / "app" / "data" / "workspaces.db"

    artifact_store_backend: Literal["local", "s3"] = "local"
    world_asset_dir: Path = ROOT / "app" / "data" / "world-assets"
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")

    mireye_api_key: SecretStr = SecretStr("")
    mireye_base_url: str = "https://api.mireye.com"
    mireye_enrichment_batch_size: int = Field(default=2, ge=1, le=100)

    openai_api_key: SecretStr = SecretStr("")
    sandbox_agent_model: str = "gpt-5.6-sol"
    sandbox_agent_reasoning_effort: str = "high"
    model_pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=1, le=1536)

    redis_url: str = "redis://localhost:6379/0"
    workflow_backend: Literal["local", "temporal"] = "local"
    temporal_target: str | None = None
    temporal_namespace: str = "default"
    temporal_task_queue: str = "mireye"
    nats_url: str = "nats://localhost:4222"
    nats_stream: str = "MIREYE"

    otel_enabled: bool = False
    otel_service_name: str = "mireye-api"
    otel_exporter_otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if self.app_env == "production":
            if not self.database_url:
                raise ValueError("DATABASE_URL must be explicitly configured in production.")
            if not self.mireye_api_key.get_secret_value():
                raise ValueError("MIREYE_API_KEY must be configured in production.")
            if self.database_url.startswith("sqlite"):
                raise ValueError("Production cannot use SQLite as authoritative state.")
            if self.artifact_store_backend != "s3" or not self.s3_bucket:
                raise ValueError("Production requires S3 artifact storage and S3_BUCKET.")
            if self.workflow_backend != "temporal" or not self.temporal_target:
                raise ValueError("Production requires TEMPORAL_TARGET and WORKFLOW_BACKEND=temporal.")
            if "redis_url" not in self.model_fields_set:
                raise ValueError("REDIS_URL must be explicitly configured in production.")
            if "nats_url" not in self.model_fields_set:
                raise ValueError("NATS_URL must be explicitly configured in production.")
            if "cors_origins" not in self.model_fields_set:
                raise ValueError("CORS_ORIGINS must be explicitly configured in production.")
            if not self.openai_api_key.get_secret_value():
                raise ValueError("OPENAI_API_KEY must be configured in production.")
        return self

    @property
    def data_mode(self) -> str:
        return "live" if self.mireye_api_key.get_secret_value() else "local"

    @property
    def effective_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.workspace_db.as_posix()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
