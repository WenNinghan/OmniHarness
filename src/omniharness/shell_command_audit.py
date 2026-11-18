"""Lexical inspection for compound shell operators that can mask non-zero exit codes.

This module never modifies, parses as executable code, or blocks commands. It
provides bounded, deterministic metadata so execution agents and reviewers do
not mistake a trailing ``0`` exit status in ``cmd1; cmd2`` or ``cmd1 | cmd2``
for proof that earlier segments succeeded.
"""
from __future__ import annotations


def detect_compound_exit_masking(command: object) -> tuple[str, ...]:
    """Return ordered tuple of unquoted shell operators that can mask prior exit codes.

    ``&&`` is intentionally excluded because POSIX shells short-circuit on a
    non-zero left-hand exit status rather than overwriting a failure with ``0``.
    Argv lists (``list[str]``) execute without shell compound chaining and return ``()``.
    """
    if not isinstance(command, str) or not command:
        return ()

    found: list[str] = []

    def record(op: str) -> None:
        if op not in found:
            found.append(op)

    in_single = False
    in_double = False
    escaped = False
    index = 0
    length = len(command)

    while index < length:
        ch = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            index += 1
            continue

        if ch == "\\":
            escaped = True
            index += 1
            continue
        if ch == "'":
            in_single = True
            index += 1
            continue
        if ch == '"':
            in_double = True
            index += 1
            continue
        if ch == "#":
            # Unquoted shell comment runs until newline.
            newline_pos = command.find("\n", index)
            if newline_pos == -1:
                break
            index = newline_pos
            continue
        if ch == ";":
            # Ignore trailing ';' at end of command with only whitespace/comments after.
            tail = command[index + 1 :].strip()
            if tail and not tail.startswith("#"):
                record(";")
            index += 1
            continue
        if ch == "\n":
            tail = command[index + 1 :].strip()
            if tail and not tail.startswith("#"):
                record("\\n")
            index += 1
            continue
        if ch == "|":
            if index + 1 < length and command[index + 1] == "|":
                record("||")
                index += 2
                continue
            # Exclude POSIX noclobber redirect '>|'.
            prev = command[index - 1] if index > 0 else ""
            if prev != ">":
                record("|")
            index += 1
            continue
        index += 1

    return tuple(found)
