"""Regression for a malformed candidate seen in a real v9 review."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from omniharness.m4_contract import (
    M4_V9_PLUGIN_VERSIONS,
    V8_STAGE_PROMPTS,
    V8_STAGE_VERSIONS,
    V9_STAGE_PROMPTS,
    V9_STAGE_VERSIONS,
)
from omniharness.review_contract import CandidateReference, CandidateReferenceV9, ReviewV9
from omniharness.review_evidence import _validate_review_reference
from omniharness.services import ServiceError, validate_agent_run_versions

DIGEST = "a" * 64
OBSERVED = {"operation_id": "operation-1", "dimension": "execution", "expected_sha256": DIGEST}


def test_observed_candidate_survives_review_without_gaining_evidence():
    review = ReviewV9.model_validate({
        "summary": "review", "findings": [{
            "finding_id": "f1", "location": "file", "severity": "INFO", "description": "claim",
            "model_status": "PASS", "recommendation": "inspect",
            "candidate_refs": [OBSERVED, {"operation_id": "operation-2", "dimension": "execution"}],
        }], "unverified": [],
    })
    candidate = review.findings[0].candidate_refs[0]
    # No DB or artifact access can elevate this malformed reference, even if its
    # digest coincides with an actual result. Other candidates remain available.
    result = _validate_review_reference(None, SimpleNamespace(workflow_version="stage-agent-v9"),
                                        None, {"capture_status": "MATCHED_HOST_PACKET"}, candidate, {})
    assert result["candidate"] == OBSERVED
    assert result["reference_status"] == result["claim_support"] == "UNVERIFIED"
    assert result["reason"] == "EXECUTION_DIGEST_WITHOUT_ARTIFACT_REFERENCE"
    report = {"schema_version": 2, "findings": [{"references": [result]}], "claim_support": "UNVERIFIED"}
    restored = json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True))
    roundtrip = CandidateReferenceV9.model_validate(restored["findings"][0]["references"][0]["candidate"])
    assert roundtrip.is_unbound_execution_digest()
    assert roundtrip.model_dump(exclude_unset=True) == OBSERVED
    assert review.findings[0].candidate_refs[1].dimension == "execution"


def test_other_candidate_serialization_stays_unchanged():
    candidate = CandidateReferenceV9(operation_id="op", dimension="execution")
    result = _validate_review_reference(None, SimpleNamespace(workflow_version="stage-agent-v9"),
                                        None, {}, candidate, {})
    assert result["candidate"] == candidate.model_dump(mode="json")
    legacy = CandidateReference(operation_id="op")
    result = _validate_review_reference(None, SimpleNamespace(workflow_version="stage-agent-v8"),
                                        None, {}, legacy, {})
    assert result["candidate"] == legacy.model_dump(mode="json")


@pytest.mark.parametrize("dimension", ["execution", "exit_code", "initial_snapshot", "runtime_source"])
def test_valid_operation_dimensions(dimension):
    assert CandidateReferenceV9.model_validate({"operation_id": "op", "dimension": dimension}).dimension == dimension


def test_valid_log_dimension():
    candidate = CandidateReferenceV9.model_validate({"operation_id": "op", "dimension": "log",
                                                       "artifact_id": "art", "expected_sha256": DIGEST})
    assert candidate.expected_sha256 == DIGEST


@pytest.mark.parametrize("candidate", [
    {**OBSERVED, "dimension": "exit_code"},
    {**OBSERVED, "dimension": "runtime_source"},
    {**OBSERVED, "dimension": "log"},
    {**OBSERVED, "artifact_id": "artifact"},
    {**OBSERVED, "artifact_id": None},
    {**OBSERVED, "start": 0, "end": 1},
    {**OBSERVED, "unknown": 1},
    {**OBSERVED, "expected_sha256": "A" * 64},
    {"operation_id": "op", "dimension": "log", "artifact_id": "art"},
    {"operation_id": "op", "dimension": "log", "expected_sha256": DIGEST},
    {"operation_id": "op", "dimension": "execution", "start": 0},
    {"operation_id": "op", "dimension": "unknown"},
])
def test_other_invalid_shapes_still_rejected(candidate):
    with pytest.raises(ValidationError):
        CandidateReferenceV9.model_validate(candidate)


def test_v8_still_rejects_unbound_digest_and_v9_prompt_is_coherent():
    with pytest.raises(ValidationError):
        CandidateReference.model_validate({"operation_id": "op", "expected_sha256": DIGEST})
    prompt = V9_STAGE_PROMPTS["review"]
    assert "每个引用仅可包含 operation_id、artifact_id" not in prompt
    assert "必须包含 operation_id 和 dimension" in prompt
    assert "不要给非 log 引用附加工件或摘要" in prompt
    assert "每个引用仅可包含 operation_id、artifact_id" in V8_STAGE_PROMPTS["review"]
    assert V9_STAGE_VERSIONS["review"] != V8_STAGE_VERSIONS["review"]


def test_previous_v9_frozen_plugin_or_role_is_rejected():
    version = "stage-agent-v9"
    snapshot = {"workflow_type": "agent", "agent": {
        "workflow_version": version, "plugin_versions": deepcopy(M4_V9_PLUGIN_VERSIONS),
        "roles": {name: {"role_version": value} for name, value in V9_STAGE_VERSIONS.items()},
    }}
    run = SimpleNamespace(id="run", thread_id="run", workflow_version=version, config_snapshot=snapshot)
    validate_agent_run_versions(run, version, role_versions=V9_STAGE_VERSIONS,
                                plugin_versions=M4_V9_PLUGIN_VERSIONS)
    snapshot["agent"]["plugin_versions"]["builtin-review-evidence"] = "1"
    with pytest.raises(ServiceError, match="不兼容"):
        validate_agent_run_versions(run, version, role_versions=V9_STAGE_VERSIONS,
                                    plugin_versions=M4_V9_PLUGIN_VERSIONS)
    snapshot["agent"]["plugin_versions"] = deepcopy(M4_V9_PLUGIN_VERSIONS)
    snapshot["agent"]["roles"]["review"]["role_version"] = V8_STAGE_VERSIONS["review"]
    with pytest.raises(ServiceError, match="不兼容"):
        validate_agent_run_versions(run, version, role_versions=V9_STAGE_VERSIONS,
                                    plugin_versions=M4_V9_PLUGIN_VERSIONS)

