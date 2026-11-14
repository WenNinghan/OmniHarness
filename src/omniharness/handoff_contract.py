"""Bounded, host-issued v1 dependency handoffs for workspace agents.

This module does not look up or authorize database rows.  The trusted host must
first prove the Run, workspace, goal, task dependency, artifact and operation
ownership and read the verified artifact contents.  This module checks the
shape and digest of those already-authorized inputs and never turns a model's
report, a recorded operation, or a hash into a fact-verification verdict.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_DEPENDENCIES = 8
MAX_REPORT_CHARACTERS = 2500
MAX_OPERATIONS = 20
MAX_EVENT_BYTES = 16 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelReport(_Contract):
    kind: Literal["unstructured_model_report"] = "unstructured_model_report"
    authority: Literal["model_claim"] = "model_claim"
    artifact_id: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    excerpt: str = Field(min_length=1, max_length=MAX_REPORT_CHARACTERS)
    truncated: bool


class HostOperation(_Contract):
    operation_id: str = Field(min_length=1, max_length=80)
    # RECORDED means the host found a completed matching operation and ledger;
    # it does not imply the operation or its result proves a claim.
    status: Literal["RECORDED", "UNVERIFIED"]


class HandoffValidation(_Contract):
    # The caller may construct a handoff only after independently checking the
    # scopes and artifact integrity under the authorized Run transaction.
    scope: Literal["VALIDATED"] = "VALIDATED"
    integrity: Literal["VALIDATED"] = "VALIDATED"
    claim_support: Literal["UNVERIFIED"] = "UNVERIFIED"


class HandoffEnvelope(_Contract):
    schema_version: Literal[1] = 1
    handoff_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_version: Literal["stage-agent-v5"]
    run_id: str = Field(min_length=1, max_length=80)
    workspace_id: str = Field(min_length=1, max_length=80)
    source_task_id: str = Field(min_length=1, max_length=80)
    target_task_id: str = Field(min_length=1, max_length=80)
    goal_message_id: str = Field(min_length=1, max_length=80)
    model_report: ModelReport
    host_operations: tuple[HostOperation, ...] = Field(max_length=MAX_OPERATIONS)
    operation_count: int = Field(ge=0)
    operations_truncated: bool
    # Some unsuccessful/in-flight tool records do not have a host_task_id;
    # this list therefore cannot claim to cover every operation for the task.
    operation_coverage: Literal["UNVERIFIED"] = "UNVERIFIED"
    validation: HandoffValidation = Field(default_factory=HandoffValidation)

    @field_validator("run_id", "workspace_id", "source_task_id", "target_task_id", "goal_message_id")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if not value.strip() or any(ord(ch) < 32 for ch in value):
            raise ValueError("Invalid handoff identifier")
        return value


def build_handoff(*, run_id: str, workspace_id: str, workflow_version: str,
                  source_task_id: str, target_task_id: str, goal_message_id: str,
                  artifact_id: str, artifact_sha256: str, report_text: str,
                  operations: list[dict], task_fingerprint: str) -> HandoffEnvelope:
    """Create one deterministic envelope from **host-verified** records.

    ``report_text`` must be the full immutable artifact read through the host's
    integrity-checking store, not a model-supplied excerpt.  Each operation must
    already be checked against this Run and source task.  A RECORDED operation
    requires both the tool operation and its same-Run ledger record to have
    completed; all other known outcomes are UNVERIFIED.  Caller-supplied model
    fields must never be used as host verification results.

    The trusted host computes ``task_fingerprint`` from the frozen target and
    source task definitions, dependency graph, goal and applicable constraints
    or requirements.  A changed task input must produce a different binding.
    """
    if not isinstance(report_text, str) or not report_text.strip():
        raise ValueError("A nonempty verified report is required")
    if not isinstance(operations, list):
        raise ValueError("Host operation records must be a list")
    report_bytes = report_text.encode("utf-8")
    actual_sha = hashlib.sha256(report_bytes).hexdigest()
    if actual_sha != artifact_sha256:
        raise ValueError("Report does not match its immutable artifact")
    references = [HostOperation.model_validate(item) for item in operations]
    references.sort(key=lambda item: item.operation_id)
    if len({item.operation_id for item in references}) != len(references):
        raise ValueError("Duplicate host operation reference")
    report = ModelReport(artifact_id=artifact_id, sha256=actual_sha,
                         size_bytes=len(report_bytes), excerpt=report_text[:MAX_REPORT_CHARACTERS],
                         truncated=len(report_text) > MAX_REPORT_CHARACTERS)
    # Include even omitted operation IDs and statuses in the binding. A change
    # outside the visible 20 entries must not reuse a prior handoff identity.
    binding = _digest({"schema_version": 1, "workflow_version": workflow_version,
                       "run_id": run_id, "workspace_id": workspace_id,
                       "source_task_id": source_task_id, "target_task_id": target_task_id,
                       "goal_message_id": goal_message_id, "task_fingerprint": task_fingerprint,
                       "artifact_id": artifact_id,
                       "artifact_sha256": actual_sha,
                       "operations": [item.model_dump(mode="json") for item in references]})
    return HandoffEnvelope(handoff_id=_digest({"handoff_v1_binding": binding}),
                           binding_fingerprint=binding, task_fingerprint=task_fingerprint,
                           workflow_version=workflow_version,
                           run_id=run_id, workspace_id=workspace_id,
                           source_task_id=source_task_id, target_task_id=target_task_id,
                           goal_message_id=goal_message_id, model_report=report,
                           host_operations=tuple(references[:MAX_OPERATIONS]),
                           operation_count=len(references),
                           operations_truncated=len(references) > MAX_OPERATIONS)


def handoff_event_payload(envelope: HandoffEnvelope) -> dict:
    """Return bounded metadata only; never persist report text in an event."""
    payload = {
        "schema_version": envelope.schema_version,
        "handoff_id": envelope.handoff_id,
        "binding_fingerprint": envelope.binding_fingerprint,
        "workflow_version": envelope.workflow_version,
        # Task IDs are model-generated strings and may contain user text or
        # secrets. Events expose only per-Run pseudonymous references.
        "source_task_ref": _digest({"run_id": envelope.run_id,
                                    "task_id": envelope.source_task_id}),
        "target_task_ref": _digest({"run_id": envelope.run_id,
                                    "task_id": envelope.target_task_id}),
        "artifact_id": envelope.model_report.artifact_id,
        "artifact_sha256": envelope.model_report.sha256,
        "report_truncated": envelope.model_report.truncated,
        "operation_count": envelope.operation_count,
        "operations_included": len(envelope.host_operations),
        "operations_truncated": envelope.operations_truncated,
        "operation_coverage": envelope.operation_coverage,
        "host_operations": [item.model_dump(mode="json") for item in envelope.host_operations],
        "validation": envelope.validation.model_dump(mode="json"),
        "meaning": "prepared_not_provider_receipt",
    }
    # The API and database serializers may emit escaped Unicode or whitespace.
    # Bound both common representations instead of only the compact digest form.
    encoded_sizes = (len(_canonical(payload)),
                     len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")),
                     len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")))
    if max(encoded_sizes) > MAX_EVENT_BYTES:
        raise ValueError("Handoff event metadata exceeds the limit")
    return payload


class HandoffEnvelopeV6(HandoffEnvelope):
    """Independent v6 wire contract; the v5 schema and digest remain frozen."""
    schema_version: Literal[2] = 2
    workflow_version: Literal["stage-agent-v6"]


def build_handoff_v6(*, run_id: str, workspace_id: str, workflow_version: str,
                     source_task_id: str, target_task_id: str, goal_message_id: str,
                     artifact_id: str, artifact_sha256: str, report_text: str,
                     operations: list[dict], task_fingerprint: str) -> HandoffEnvelopeV6:
    """Derive a separate v6 digest domain from validated, frozen v5 inputs."""
    if workflow_version != "stage-agent-v6":
        raise ValueError("Invalid v6 handoff workflow")
    base = build_handoff(run_id=run_id, workspace_id=workspace_id,
                         workflow_version="stage-agent-v5", source_task_id=source_task_id,
                         target_task_id=target_task_id, goal_message_id=goal_message_id,
                         artifact_id=artifact_id, artifact_sha256=artifact_sha256,
                         report_text=report_text, operations=operations,
                         task_fingerprint=task_fingerprint)
    binding = _digest({"handoff_v2_input": base.binding_fingerprint,
                       "workflow_version": workflow_version})
    data = base.model_dump()
    data.update(schema_version=2, workflow_version=workflow_version,
                binding_fingerprint=binding, handoff_id=_digest({"handoff_v2_binding": binding}))
    return HandoffEnvelopeV6.model_validate(data)


class HandoffEnvelopeV7(HandoffEnvelope):
    """V7 keeps V6 evidence semantics in its own frozen digest domain."""
    schema_version: Literal[2] = 2
    workflow_version: Literal["stage-agent-v7"]


def build_handoff_v7(**kwargs) -> HandoffEnvelopeV7:
    if kwargs.get("workflow_version") != "stage-agent-v7":
        raise ValueError("Invalid v7 handoff workflow")
    base = build_handoff_v6(**{**kwargs, "workflow_version": "stage-agent-v6"})
    binding = _digest({"handoff_v2_input": base.binding_fingerprint,
                       "workflow_version": "stage-agent-v7"})
    data = base.model_dump()
    data.update(workflow_version="stage-agent-v7", binding_fingerprint=binding,
                handoff_id=_digest({"handoff_v2_binding": binding}))
    return HandoffEnvelopeV7.model_validate(data)


class HandoffEnvelopeV8(HandoffEnvelope):
    """V8 has a separate workflow-bound evidence domain."""
    schema_version: Literal[2] = 2
    workflow_version: Literal["stage-agent-v8"]


def build_handoff_v8(**kwargs) -> HandoffEnvelopeV8:
    if kwargs.get("workflow_version") != "stage-agent-v8":
        raise ValueError("Invalid v8 handoff workflow")
    base = build_handoff_v6(**{**kwargs, "workflow_version": "stage-agent-v6"})
    binding = _digest({"handoff_v2_input": base.binding_fingerprint,
                       "workflow_version": "stage-agent-v8"})
    data = base.model_dump()
    data.update(workflow_version="stage-agent-v8", binding_fingerprint=binding,
                handoff_id=_digest({"handoff_v2_binding": binding}))
    return HandoffEnvelopeV8.model_validate(data)


class HandoffEnvelopeV9(HandoffEnvelope):
    """V9 uses an independent workflow-bound handoff digest domain."""
    schema_version: Literal[2] = 2
    workflow_version: Literal["stage-agent-v9"]


def build_handoff_v9(**kwargs) -> HandoffEnvelopeV9:
    if kwargs.get("workflow_version") != "stage-agent-v9":
        raise ValueError("Invalid v9 handoff workflow")
    base = build_handoff_v6(**{**kwargs, "workflow_version": "stage-agent-v6"})
    binding = _digest({"handoff_v2_input": base.binding_fingerprint,
                       "workflow_version": "stage-agent-v9"})
    data = base.model_dump()
    data.update(workflow_version="stage-agent-v9", binding_fingerprint=binding,
                handoff_id=_digest({"handoff_v2_binding": binding}))
    return HandoffEnvelopeV9.model_validate(data)


class HandoffEnvelopeV10(HandoffEnvelope):
    """V10 has its own workflow-bound handoff identity."""
    schema_version: Literal[2] = 2
    workflow_version: Literal["stage-agent-v10"]


def build_handoff_v10(**kwargs) -> HandoffEnvelopeV10:
    if kwargs.get("workflow_version") != "stage-agent-v10":
        raise ValueError("Invalid v10 handoff workflow")
    base = build_handoff_v6(**{**kwargs, "workflow_version": "stage-agent-v6"})
    binding = _digest({"handoff_v2_input": base.binding_fingerprint,
                       "workflow_version": "stage-agent-v10"})
    data = base.model_dump()
    data.update(workflow_version="stage-agent-v10", binding_fingerprint=binding,
                handoff_id=_digest({"handoff_v2_binding": binding}))
    return HandoffEnvelopeV10.model_validate(data)
