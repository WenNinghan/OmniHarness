from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    adapter_type: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(Text)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    name: Mapped[str] = mapped_column(String(120))
    model_name: Mapped[str] = mapped_column(String(200))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=90)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=100_000)
    version: Mapped[int] = mapped_column(Integer, default=1)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    context_policy: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(160), default="新对话")
    next_ordinal: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "ordinal"),
        UniqueConstraint("conversation_id", "client_request_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    model_config_id: Mapped[str] = mapped_column(ForeignKey("model_configs.id"))
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    model_label: Mapped[str] = mapped_column(String(320))
    ordinal: Mapped[int] = mapped_column(Integer)
    client_request_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    mode: Mapped[str] = mapped_column(String(12))
    config_snapshot: Mapped[dict] = mapped_column(JSON)
    next_event_sequence: Mapped[int] = mapped_column(Integer, default=1)
    usage: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    workflow_version: Mapped[str | None] = mapped_column(String(32))
    thread_id: Mapped[str | None] = mapped_column(String(64))
    parent_id: Mapped[str | None] = mapped_column(String(36))
    attempt_index: Mapped[int | None] = mapped_column(Integer)
    strategy_id: Mapped[str | None] = mapped_column(String(64), default="manual")
    outcome: Mapped[dict | None] = mapped_column(JSON)
    verdict: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("run_id", "role"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    reasoning_content: Mapped[str] = mapped_column(Text, default="", server_default="")
    output_metadata: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(32), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Clarification(Base):
    """A user response belongs to one persisted Run and one stable request."""
    __tablename__ = "clarifications"
    __table_args__ = (UniqueConstraint("run_id", "request_key"),
                      UniqueConstraint("run_id", "client_request_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(120))
    task_id: Mapped[str | None] = mapped_column(String(120))
    stage: Mapped[str] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    answer: Mapped[str | None] = mapped_column(Text)
    client_request_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunCommand(Base):
    """Durable control command. Idempotent per (run_id, client_request_id)."""

    __tablename__ = "run_commands"
    __table_args__ = (
        UniqueConstraint("run_id", "client_request_id"),
        UniqueConstraint("run_id", "sequence_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    command_type: Mapped[str] = mapped_column(String(32))
    client_request_id: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """One dispatched execution node inside a Run; parent links form the dispatch tree."""

    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    parent_agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    model_config_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunBudget(Base):
    """Shared atomic budget counters for a Run. No scheduler reads these yet."""

    __tablename__ = "run_budgets"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    max_model_calls: Mapped[int] = mapped_column(Integer, default=50)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=100)
    max_revisions: Mapped[int] = mapped_column(Integer, default=2)
    max_active_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    active_segment_open: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RunBudgetReservation(Base):
    """Accounting identity; not authorization to replay a side effect."""

    __tablename__ = "run_budget_reservations"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    invocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    binding_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OperationRecord(Base):
    """Idempotency ledger for side effects that a re-entered graph node must not repeat."""

    __tablename__ = "operation_records"
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(120))
    parameters_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    workflow_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoleBinding(Base):
    """Per-role model selection. Configuration only; no execution layer reads it yet."""

    __tablename__ = "role_bindings"
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StageBindingSet(Base):
    """Versioned selection for the five-stage workflow; distinct from legacy roles."""

    __tablename__ = "stage_binding_sets"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="local")
    version: Mapped[int] = mapped_column(Integer, default=0)
    bindings: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LocalSession(Base):
    __tablename__ = "local_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PairingCode(Base):
    __tablename__ = "pairing_codes"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="local")
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    host_path: Mapped[str] = mapped_column(Text)
    physical_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    probe: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspacePolicy(Base):
    __tablename__ = "workspace_policies"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    grant: Mapped[dict] = mapped_column(JSON)
    tool_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceLease(Base):
    __tablename__ = "workspace_leases"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    owner_epoch: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceSnapshot(Base):
    __tablename__ = "workspace_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(120))
    policy_version: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[dict] = mapped_column(JSON)
    workdir: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolOperation(Base):
    """Request/approval evidence; OperationRecord remains the execution ledger."""
    __tablename__ = "tool_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    operation_record_id: Mapped[str | None] = mapped_column(ForeignKey("operation_records.operation_id"))
    agent_run_id: Mapped[str | None] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(String(120))
    tool_call_id: Mapped[str] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(120))
    tool_version: Mapped[str] = mapped_column(String(120))
    parameters: Mapped[dict] = mapped_column(JSON)
    binding_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="requested", index=True)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    sandbox_id: Mapped[str | None] = mapped_column(String(64))
    command_id: Mapped[str | None] = mapped_column(String(128))
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("workspace_snapshots.id"))
    slot_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    reclaimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    operation_id: Mapped[str] = mapped_column(ForeignKey("tool_operations.id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    decision_request_id: Mapped[str | None] = mapped_column(String(100))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    kind: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvaluationSession(Base):
    __tablename__ = "evaluation_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    specification: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
