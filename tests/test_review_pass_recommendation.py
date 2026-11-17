"""Completed reviews retain explicit statuses without invented recommendations."""
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from test_m3_stages import Hooks, StageModel

from omniharness.m3_stages import STAGE_KEYS, Review, ReviewFinding, build_stage_graph
from omniharness.review_contract import ReviewFindingV6, ReviewFindingV9


def finding(status="PASS", recommendation=""):
    return {"location": "pass.py", "severity": "INFO", "description": "matching requirement",
            "status": status, "recommendation": recommendation}


@pytest.mark.parametrize("status", ["PASS", "FAIL", "UNVERIFIED", "BLOCKED", "DISPUTED"])
def test_original_nonempty_recommendations_and_statuses_are_preserved(status):
    value = finding(status, " ")  # Existing min_length=1 accepted this; do not tighten it here.
    assert ReviewFinding.model_validate(value).model_dump() == value


@pytest.mark.parametrize("status", ["FAIL", "UNVERIFIED", "BLOCKED", "DISPUTED"])
def test_empty_advice_cannot_make_an_unresolved_finding_pass(status):
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(finding(status))


@pytest.mark.parametrize("bad", [None, 42, [], {}, "x" * 4001])
def test_pass_advice_keeps_type_and_size_guards(bad):
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(finding(recommendation=bad))


def test_pass_advice_is_still_required_and_unknown_fields_are_rejected():
    value = finding()
    del value["recommendation"]
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(value)
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate({**finding(), "invented": "value"})
    assert ReviewFinding.model_validate(finding()).model_dump() == finding()


@pytest.mark.parametrize("schema", [ReviewFindingV6, ReviewFindingV9])
def test_separate_evidence_review_contracts_keep_their_constraints(schema):
    value = finding()
    value["model_status"] = value.pop("status")
    with pytest.raises(ValidationError):
        schema.model_validate({**value, "finding_id": "f1", "candidate_refs": []})


@pytest.mark.asyncio
async def test_full_stage_graph_finishes_without_repair_calls_or_empty_advice_label():
    review = {"summary": "checked report", "findings": [finding(),
        {**finding("UNVERIFIED", "run another check"), "location": "unknown.py"}], "unverified": ["one check"]}
    replies = {"think": "understand", "plan": '{"steps":[{"id":"s1","task":"do","acceptance":"ok"}]}',
        "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"do"}]}',
        "execute": "done", "review": json.dumps(review)}
    observed, hooks = [], Hooks()
    graph = build_stage_graph(models={s: StageModel(s, replies, observed) for s in STAGE_KEYS},
                              hooks=hooks, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [("user", "verify") ]},
        {"configurable": {"thread_id": "empty-pass-advice"}}, durability="sync")
    assert result["review_data"] == Review.model_validate(review).model_dump()
    assert result["review_data"]["findings"][0]["recommendation"] == ""
    assert "pass.py：matching requirement；建议：" not in result["review"]
    assert "unknown.py：matching requirement；建议：run another check" in result["review"]
    assert [stage for stage, _ in observed] == list(STAGE_KEYS)
    assert hooks.finished == list(STAGE_KEYS) and hooks.uncertain == []
