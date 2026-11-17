from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMNI_", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://omniharness:omniharness@127.0.0.1:5432/omniharness", repr=False
    )
    data_dir: Path = Path(".omniharness/data")
    secret_dir: Path = Path(".omniharness/secrets")
    static_dir: Path = Path("web/dist")
    allowed_origins: str = "http://127.0.0.1:8787,http://localhost:8787"
    enable_demo: bool = True
    # Opt-in until the Agent Worker / checkpoint integration is fully verified.
    enable_agent: bool = False
    enable_tools: bool = False
    instance_id: str = "local"
    # Opt-in only for a separately provisioned, single-purpose preview stack.
    v6_preview: bool = False
    # Interactive workflow is independently opt-in within that same preview.
    interactive_preview: bool = False
    # Unreleased development isolation switch; existing previews remain on v9.
    v10_preview: bool = False
    # Opt-in review failure handling frozen into newly created v4-v6 Runs.
    review_failure_policy: Literal["fail", "deliver_unreviewed"] = "fail"
    workspace_broker_url: str = "http://127.0.0.1:19082"
    workspace_service_key_file: Path | None = Field(default=None, repr=False)
    sandbox_endpoint: str = "http://127.0.0.1:19080"
    sandbox_key_file: Path | None = Field(default=None, repr=False)
    sandbox_image: str = ""
    sandbox_snapshot_root: str = "/var/lib/omniharness-m4a/snapshots"
    sandbox_max_command_seconds: int = Field(default=600, ge=1, le=600)
    # Optional independent create/reclaim service, frozen only for new Runs.
    sandbox_creation_url: str = ""
    sandbox_creation_service_id: str = ""
    sandbox_creation_key_file: Path | None = Field(default=None, repr=False)
    mcp_config_file: Path | None = None
    checkpoint_schema: str = "omniharness_checkpoints"
    allow_sqlite: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    poll_seconds: float = 0.2
    session_days: int = 30
    max_chat_messages: int = 100
    max_chat_characters: int = 200_000
    budget_max_model_calls: int = Field(default=50, ge=1, le=10000)
    budget_max_tool_calls: int = Field(default=100, ge=1, le=100000)
    budget_max_revisions: int = Field(default=2, ge=0, le=1000)
    budget_max_active_seconds: int = Field(default=1800, ge=1, le=86400)

    @field_validator("data_dir", "secret_dir", "static_dir")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def validate_database(self):
        if self.database_url.startswith("sqlite") and not self.allow_sqlite:
            raise ValueError("SQLite is available only with OMNI_ALLOW_SQLITE=true for local development/tests")
        if not self.database_url.startswith(("sqlite", "postgresql+psycopg://")):
            raise ValueError("Use a postgresql+psycopg URL")
        if self.v6_preview and (self.instance_id != "omni-v6-preview"
                                or not self.enable_agent or not self.enable_tools):
            raise ValueError("V6 preview requires its dedicated instance with Agent and tools enabled")
        if self.interactive_preview and (not self.v6_preview or self.instance_id != "omni-v6-preview"
                                         or not self.enable_agent or not self.enable_tools):
            raise ValueError("Interactive preview requires the dedicated V6 preview with Agent and tools enabled")
        if self.v10_preview and not self.interactive_preview:
            raise ValueError("V10 preview requires the interactive preview")
        return self

    @property
    def stage_workflow_version(self) -> str:
        from .stage_contract import INTERACTIVE_STAGE_WORKFLOW_VERSION, STAGE_WORKFLOW_VERSION
        return INTERACTIVE_STAGE_WORKFLOW_VERSION if self.interactive_preview else STAGE_WORKFLOW_VERSION

    @property
    def workspace_workflow_version(self) -> str:
        from .m4_contract import (
            COLLAB_WORKFLOW_VERSION,
            EVIDENCE_WORKFLOW_VERSION,
            INTERACTIVE_V9_WORKFLOW_VERSION,
            INTERACTIVE_V10_WORKFLOW_VERSION,
        )
        return (INTERACTIVE_V10_WORKFLOW_VERSION if self.v10_preview else
                INTERACTIVE_V9_WORKFLOW_VERSION if self.interactive_preview else
                EVIDENCE_WORKFLOW_VERSION if self.v6_preview else COLLAB_WORKFLOW_VERSION)

    @property
    def origins(self) -> set[str]:
        return {x.strip().rstrip("/") for x in self.allowed_origins.split(",") if x.strip()}
