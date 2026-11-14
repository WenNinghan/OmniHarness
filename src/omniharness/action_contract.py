"""Versioned action descriptions. These records neither authorize nor prove execution."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator

from .workspace_io import WorkspaceError, _relative

ACTION_CONTRACT_VERSION = 1
ACTION_DEPENDENCY_VERSION = 2
ACTION_OUTCOME_STATUSES = ("NOT_ASSESSED", "BLOCKED", "UNVERIFIED", "ACTIONS_RECORDED")


class ActionRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["workspace_file_write", "sandbox_command"]
    path: StrictStr | None = Field(default=None, max_length=1024)
    argv: list[StrictStr] | None = Field(default=None, min_length=1, max_length=32)
    cwd: StrictStr | None = Field(default=None, max_length=1024)
    step_id: StrictStr | None = Field(default=None, max_length=128)
    client_id: StrictStr | None = Field(default=None, max_length=64)
    depends_on: list[StrictStr] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def valid_action(self):
        if self.step_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.step_id):
            raise ValueError("invalid step ID")
        if self.client_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.client_id):
            raise ValueError("invalid client action ID")
        if self.depends_on is not None and (any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item)
                                                for item in self.depends_on)
                                            or len(self.depends_on) != len(set(self.depends_on))):
            raise ValueError("invalid action dependency")
        try:
            if self.kind == "workspace_file_write":
                if self.path is None or self.argv is not None or self.cwd is not None:
                    raise ValueError("file action requires only a path")
                _relative(self.path)
            else:
                if self.path is not None or self.argv is None or self.cwd is None:
                    raise ValueError("command action requires argv and cwd")
                if _relative(self.cwd, directory=True) != self.cwd:
                    raise ValueError("noncanonical cwd")
                if any(not value or len(value) > 2048 or "\x00" in value for value in self.argv):
                    raise ValueError("invalid argv")
        except WorkspaceError as exc:
            raise ValueError("invalid relative path") from exc
        return self


class ActionCandidate(ActionRequirement):
    source_message_id: StrictStr = Field(min_length=1, max_length=128)
    source_excerpt: StrictStr = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def meaningful_excerpt(self):
        if not self.source_excerpt.strip():
            raise ValueError("empty source excerpt")
        if self.client_id is not None or self.depends_on is not None:
            raise ValueError("planner candidates cannot supply user action dependencies")
        return self


def validate_action_dependencies(actions, *, require_dependencies):
    """Validate user-supplied local IDs; no dependency is inferred from order."""
    if not actions:
        return False
    declared = any(action.client_id is not None or action.depends_on is not None for action in actions)
    if not declared:
        if require_dependencies and len(actions) > 1:
            raise ValueError("multiple actions require explicit dependencies")
        return False
    if any(action.client_id is None or action.depends_on is None for action in actions):
        raise ValueError("all actions require an ID and dependency list")
    by_id = {action.client_id: action for action in actions}
    if len(by_id) != len(actions):
        raise ValueError("duplicate action ID")
    for action in actions:
        if action.client_id in action.depends_on or not set(action.depends_on).issubset(by_id):
            raise ValueError("unknown or self-referential action dependency")
    if len(actions) == 2 and not any(action.depends_on for action in actions):
        raise ValueError("two actions require a directed dependency")
    visited, visiting = set(), set()

    def visit(identity):
        if identity in visiting:
            raise ValueError("cyclic action dependency")
        if identity in visited:
            return
        visiting.add(identity)
        for predecessor in by_id[identity].depends_on:
            visit(predecessor)
        visiting.remove(identity)
        visited.add(identity)

    for identity in by_id:
        visit(identity)
    return True


def _empty(reason):
    return {"version": ACTION_CONTRACT_VERSION, "assessed": False, "reason": reason, "actions": []}


def _validate(raw, *, run_id, workspace_id, source, source_message_id=None, source_text=None, source_run_id=None,
              require_dependencies=False):
    if not isinstance(run_id, str) or not run_id or not isinstance(workspace_id, str) or not workspace_id:
        return _empty("invalid_scope")
    if not isinstance(raw, list) or not raw or len(raw) > 8:
        return _empty("no_actions" if raw == [] or raw is None else "invalid_actions")
    if source == "planner" and (source_run_id != run_id or not isinstance(source_text, str)
                                or not isinstance(source_message_id, str) or not source_message_id):
        return _empty("invalid_source_scope")
    actions, models, seen = [], [], set()
    try:
        for item in raw:
            model = (ActionCandidate if source == "planner" else ActionRequirement).model_validate(item)
            models.append(model)
            value = model.model_dump(exclude_none=True)
            if source == "planner" and (model.source_message_id != source_message_id
                                         or model.source_excerpt not in source_text):
                return _empty("invalid_source_anchor")
            # Duplicate executable parameters are ambiguous even if source/step differs.
            key = json.dumps({k: value[k] for k in ("kind", "path", "argv", "cwd") if k in value},
                             sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if key in seen:
                return _empty("duplicate_actions")
            seen.add(key)
            binding = json.dumps({"version": ACTION_CONTRACT_VERSION, "run_id": run_id,
                                  "workspace_id": workspace_id, "source": source, "action": value},
                                 sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            identity = hashlib.sha256(binding.encode("utf-8")).hexdigest()
            actions.append({"id": identity, "run_id": run_id, "workspace_id": workspace_id,
                            "source": source, **value})
    except (ValidationError, TypeError, ValueError):
        return _empty("invalid_action")
    try:
        has_dependencies = source == "user" and validate_action_dependencies(
            models, require_dependencies=require_dependencies)
    except ValueError:
        return _empty("invalid_dependencies")
    result = {"version": ACTION_DEPENDENCY_VERSION if has_dependencies else ACTION_CONTRACT_VERSION,
              "assessed": True, "reason": None, "actions": actions}
    if has_dependencies:
        by_client = {action["client_id"]: action["id"] for action in actions}
        result["dependency_graph"] = {action["id"]: [by_client[item] for item in action["depends_on"]]
                                      for action in actions}
    return result


def validate_candidates(raw, *, run_id, workspace_id, source_message_id, source_text, source_run_id):
    return _validate(raw, run_id=run_id, workspace_id=workspace_id, source="planner",
                     source_message_id=source_message_id, source_text=source_text, source_run_id=source_run_id)


def validate_explicit_actions(raw, *, run_id, workspace_id, require_dependencies=False):
    return _validate(raw, run_id=run_id, workspace_id=workspace_id, source="user",
                     require_dependencies=require_dependencies)
