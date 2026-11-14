import pytest

from omniharness.command_exit_audit import audit_command_exit_codes

ARTIFACT = "61b78660-6971-4d7f-99d4-c3d7c975d0b3"
OTHER = "0869cde6-86a0-42b7-a19e-9b96a9ae4eb5"
OPERATION = "f" * 64
HEADER = "| 检查 | 退出码 | 结果 / 工件 |\n|---|---|---|\n"


def row(exit_code="0", citation=None):
    citation = citation if citation is not None else f"工件 `{ARTIFACT}`，该次 exit 1"
    return f"| 扩展行为检查 | {exit_code} | 通过（{citation}） |\n"


def test_real_failed_command_citation_catches_contradiction_without_rewriting_answer():
    answer = HEADER + row()
    result = audit_command_exit_codes(answer, {ARTIFACT: (OPERATION, 1)})
    assert answer == HEADER + row()
    assert result == {"status": "conflict", "conflicts": [{"artifact_id": ARTIFACT,
        "operation_id": OPERATION, "claimed_exit_code": 0, "actual_exit_code": 1, "line": 3}],
        "truncated": False}


def test_matching_exit_does_not_claim_all_output_is_verified():
    assert audit_command_exit_codes(HEADER + row("1"), {ARTIFACT: (OPERATION, 1)}) == {
        "status": "no_conflict_detected", "conflicts": [], "truncated": False}


@pytest.mark.parametrize("citation", [
    "没有引用日志", f"`{ARTIFACT}`", f"工件 `{ARTIFACT[:20]}...`",
    f"工件 `{ARTIFACT}` 和工件 `{OTHER}`", f"工件 `{OTHER}`", f"notartifact `{ARTIFACT}`",
    f"操作 `{ARTIFACT}`", f"工件 `{ARTIFACT}` \\| 其他", f"工件 `{ARTIFACT}0`",
])
def test_unknown_or_ambiguous_log_attribution_is_not_inferred(citation):
    result = audit_command_exit_codes(HEADER + row(citation=citation), {ARTIFACT: (OPERATION, 1)})
    assert result["status"] == "no_conflict_detected" and not result["conflicts"]


@pytest.mark.parametrize("claimed", ["未知", "0 或 1", "0/1", "成功", "", "0 \\| 1"])
def test_nonliteral_exit_cells_are_not_reinterpreted(claimed):
    assert not audit_command_exit_codes(HEADER + row(claimed), {ARTIFACT: (OPERATION, 1)})["conflicts"]


def test_fenced_code_quote_and_prose_are_not_result_tables():
    answer = "```markdown\n" + HEADER + row() + "```\n" + "> " + HEADER.replace("\n", "\n> ") + row()
    assert not audit_command_exit_codes(answer, {ARTIFACT: (OPERATION, 1)})["conflicts"]


def test_explicit_english_header_and_label_are_supported():
    answer = "| Check | Exit code | Evidence |\n|---|---|---|\n" + f"| test | `0` | log: `{ARTIFACT}` |\n"
    assert audit_command_exit_codes(answer, {ARTIFACT: (OPERATION, 1)})["conflicts"][0]["actual_exit_code"] == 1


@pytest.mark.parametrize("evidence", [
    {}, {OTHER: (OPERATION, 1)}, {ARTIFACT: (OPERATION, None)}, {ARTIFACT: (OPERATION, True)},
    {ARTIFACT: ("not-an-operation", 1)}, {ARTIFACT: [OPERATION, 1]},
    {ARTIFACT: (OPERATION, 1), ARTIFACT.upper(): ("a" * 64, 0)},
])
def test_missing_invalid_conflicting_or_out_of_scope_evidence_never_proves_conflict(evidence):
    assert not audit_command_exit_codes(HEADER + row(), evidence)["conflicts"]


def test_multiple_headers_or_malformed_tables_are_skipped():
    answer = "| 检查 | 退出码 | 退出码 |\n|---|---|---|\n" + row()
    assert not audit_command_exit_codes(answer, {ARTIFACT: (OPERATION, 1)})["conflicts"]


def test_conflict_limit_is_explicitly_marked_truncated():
    result = audit_command_exit_codes(HEADER + row() * 21, {ARTIFACT: (OPERATION, 1)})
    assert len(result["conflicts"]) == 20 and result["truncated"] is True
    assert result["conflicts"][0]["line"] == 3 and result["conflicts"][-1]["line"] == 22


def test_character_limit_never_audits_a_partial_row():
    answer = HEADER + row() + "x" * 200_000
    result = audit_command_exit_codes(answer, {ARTIFACT: (OPERATION, 1)})
    assert result["truncated"] is True and len(result["conflicts"]) == 1


def test_nontext_answer_is_not_silently_accepted():
    with pytest.raises(TypeError, match="must be text"):
        audit_command_exit_codes(None, {})
