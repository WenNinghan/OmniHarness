"""Find narrowly provable exit-code conflicts in an assistant's final answer.

The caller must supply same-Run command-log artifact bindings already verified
against durable operation and ledger records. No match is not proof that other
claims are correct. This module never modifies or replays a command or answer.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ID = re.compile(rf"(?<![A-Za-z0-9-])({_UUID})(?![A-Za-z0-9-])")
_LABELED_ID = re.compile(
    rf"(?<![A-Za-z0-9_])(?:工件|日志|artifact(?:[_ -]?id)?|log(?:[_ -]?id)?)\s*[:：=]?\s*[`\[]?({_UUID})(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
_OPERATION = re.compile(r"[a-f0-9]{64}")
_SEPARATOR = re.compile(r":?-{3,}:?")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_EXIT_HEADERS = {"退出码", "exit code", "exit_code", "exit status"}
_MAX_CHARACTERS = 200_000
_MAX_CONFLICTS = 20


def _cells(line):
    line = line.strip()
    # Escaped pipes, unterminated rows and complex inline syntax are not
    # interpreted: a missed conflict is safer than associating the wrong log.
    if not line.startswith("|") or not line.endswith("|") or r"\|" in line:
        return None
    return [cell.strip() for cell in line[1:-1].split("|")]


def _bindings(evidence):
    if not isinstance(evidence, Mapping):
        return {}
    result, ambiguous = {}, set()
    for artifact_id, value in evidence.items():
        if not isinstance(artifact_id, str) or not _ID.fullmatch(artifact_id):
            continue
        identity = artifact_id.lower()
        if identity in result or identity in ambiguous:
            result.pop(identity, None)
            ambiguous.add(identity)
            continue
        if (not isinstance(value, tuple) or len(value) != 2 or not isinstance(value[0], str)
                or not _OPERATION.fullmatch(value[0]) or type(value[1]) is not int):
            ambiguous.add(identity)
            continue
        result[identity] = value
    return result


def audit_command_exit_codes(answer, evidence):
    """Return contradictions only for explicit single-log table citations.

    ``evidence`` maps a complete artifact UUID to ``(operation_id, exit_code)``.
    The mapping is a trust boundary: the caller must exclude other Runs,
    ambiguous references, unknown results and records lacking ledger support.
    ``truncated`` means the audit did not inspect every character/finding.
    """
    if not isinstance(answer, str):
        raise TypeError("The final answer must be text")
    bindings = _bindings(evidence)
    truncated = len(answer) > _MAX_CHARACTERS
    content = answer[:_MAX_CHARACTERS]
    if truncated:
        # A partial table row must not be treated as a complete statement.
        content = content.rpartition("\n")[0]
    lines = content.splitlines()
    conflicts = []
    fence = None
    header = None
    exit_index = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            header = exit_index = None
            index += 1
            continue
        if fence is not None:
            index += 1
            continue
        cells = _cells(line)
        if cells is None:
            header = exit_index = None
            index += 1
            continue
        if header is None:
            if index + 1 >= len(lines):
                index += 1
                continue
            separator = _cells(lines[index + 1])
            titles = [cell.strip(" *`").lower() for cell in cells]
            positions = [n for n, title in enumerate(titles) if title in _EXIT_HEADERS]
            if (separator is None or len(separator) != len(cells) or len(positions) != 1
                    or not all(_SEPARATOR.fullmatch(cell) for cell in separator)):
                index += 1
                continue
            header, exit_index = cells, positions[0]
            index += 2
            continue
        if len(cells) != len(header):
            header = exit_index = None
            index += 1
            continue
        claimed = cells[exit_index].strip("` ")
        ids = {match.group(1).lower() for match in _ID.finditer(line)}
        labels = {match.group(1).lower() for match in _LABELED_ID.finditer(line)}
        if (re.fullmatch(r"[+-]?\d{1,10}", claimed) and len(ids) == 1
                and labels == ids and (artifact_id := next(iter(ids))) in bindings):
            operation_id, actual = bindings[artifact_id]
            claimed_value = int(claimed)
            if claimed_value != actual:
                if len(conflicts) == _MAX_CONFLICTS:
                    truncated = True
                    break
                conflicts.append({"artifact_id": artifact_id, "operation_id": operation_id,
                                  "claimed_exit_code": claimed_value, "actual_exit_code": actual,
                                  "line": index + 1})
        index += 1
    return {"status": "conflict" if conflicts else "no_conflict_detected", "conflicts": conflicts,
            "truncated": truncated}
