from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .action_contract import ActionRequirement, validate_action_dependencies


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairRequest(StrictModel):
    code: SecretStr


class ModelConfigRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    adapter_type: Literal["openai", "anthropic", "gemini", "openai-compatible", "mock"]
    model_name: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    api_key: SecretStr | None = None
    enabled: bool = True
    parameters: dict = Field(default_factory=dict)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    max_output_tokens: int = Field(default=100_000, ge=16, le=131072)
    context_policy: dict = Field(default_factory=dict)

    @field_validator("context_policy")
    @classmethod
    def validate_context_policy(cls, value):
        from .context_policy import context_configuration_dict, parse_context_configuration
        return context_configuration_dict(*parse_context_configuration(value))

    @field_validator("name", "model_name")
    @classmethod
    def strip_text(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        if not value:
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("expected http(s) endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("credentials, query and fragment are not allowed in endpoint URLs")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
            raise ValueError("remote endpoints require HTTPS")
        return value

    @field_validator("parameters")
    @classmethod
    def parameter_allowlist(cls, value):
        from .request_policy import REQUEST_POLICY_KEY, parse_request_policy

        allowed = {"temperature", "top_p", "reasoning_effort", "seed", "top_k", "thinking", REQUEST_POLICY_KEY}
        if not set(value).issubset(allowed):
            raise ValueError("unsupported or reserved provider parameters")
        parse_request_policy(value)
        return value

    @model_validator(mode="after")
    def compatible_endpoint(self):
        if self.adapter_type == "openai-compatible" and not self.base_url:
            raise ValueError("OpenAI-compatible models require an explicit endpoint")
        return self


class ProbeRequest(StrictModel):
    probes: list[Literal["chat", "streaming", "tools", "structured", "summary_non_thinking"]] = Field(
        default_factory=lambda: ["chat", "streaming", "tools", "structured"], min_length=1, max_length=5
    )


class ConversationRequest(StrictModel):
    title: str = Field(default="新对话", min_length=1, max_length=160)


class ClarificationAnswerRequest(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=4000)

    @field_validator("answer", "client_request_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class StageOverride(StrictModel):
    stage: Literal["think", "plan", "dispatch", "execute", "review"]
    model_config_id: str = Field(min_length=1, max_length=36)
    model_version: int = Field(ge=1)
    provider_version: int = Field(ge=1)


class HistoryScopeRequest(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["unscoped", "workspace"]
    workspace_id: str | None = Field(default=None, min_length=1, max_length=36)
    additional_run_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def scope_consistent(self):
        if (self.kind == "workspace") != (self.workspace_id is not None):
            raise ValueError("workspace scope requires exactly one workspace ID")
        if any(not isinstance(identity, str) or not 1 <= len(identity) <= 36 for identity in self.additional_run_ids):
            raise ValueError("invalid history source ID")
        if len(self.additional_run_ids) != len(set(self.additional_run_ids)):
            raise ValueError("history sources must be unique")
        return self


class SendMessageRequest(StrictModel):
    content: str = Field(min_length=1, max_length=50000)
    model_config_id: str
    client_request_id: str = Field(min_length=1, max_length=100)
    workflow_type: Literal["chat", "agent"] = "chat"
    workspace_id: str | None = Field(default=None, min_length=1, max_length=36)
    agent_strategy: Literal["staged", "single", "compact"] = "staged"
    stage_overrides: list[StageOverride] | None = Field(default=None, min_length=5, max_length=5)
    history_scope: HistoryScopeRequest | None = None
    action_requirements: list[ActionRequirement] | None = Field(default=None, max_length=8)
    auto_correction_enabled: bool | None = None

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @model_validator(mode="after")
    def stage_overrides_match_workflow(self):
        if (self.action_requirements is not None or self.auto_correction_enabled is not None) and (
                self.workflow_type != "agent" or not self.workspace_id):
            raise ValueError("action requirements require an Agent workspace")
        if self.workspace_id and self.workflow_type != "agent":
            raise ValueError("workspace tools require an Agent run")
        if (self.workflow_type == "agent" and self.history_scope is not None
                and (self.history_scope.workspace_id != self.workspace_id)):
            raise ValueError("Agent history scope must match its tool workspace")
        if self.agent_strategy in {"single", "compact"} and (self.workflow_type != "agent" or not self.workspace_id):
            raise ValueError("single or compact execution requires an explicit Agent workspace")
        if self.stage_overrides is not None:
            if self.workflow_type != "agent":
                raise ValueError("stage overrides require an Agent run")
            if len({item.stage for item in self.stage_overrides}) != 5:
                raise ValueError("all five stages must appear exactly once")
        return self


class ActionRequirementsUpdate(StrictModel):
    expected_version: int = Field(ge=1, le=8)
    requirements: list[ActionRequirement] = Field(max_length=8)

    @model_validator(mode="after")
    def action_dependencies_valid(self):
        validate_action_dependencies(self.requirements, require_dependencies=True)
        return self


class ImportRequest(StrictModel):
    schema_version: Literal[1]
    model_configs: list[ModelConfigRequest] = Field(max_length=100)

    @model_validator(mode="after")
    def no_embedded_credentials(self):
        if any(m.api_key and m.api_key.get_secret_value() for m in self.model_configs):
            raise ValueError("configuration import does not accept credentials; configure them separately")
        return self


class RunCommandRequest(StrictModel):
    command_type: Literal["pause", "resume", "append_requirement"]
    client_request_id: str = Field(min_length=1, max_length=100)
    content: str | None = Field(default=None, max_length=50000)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("content must not be blank")
        return value

    @model_validator(mode="after")
    def content_matches_command(self):
        if self.command_type == "append_requirement" and not self.content:
            raise ValueError("append_requirement requires non-empty content")
        if self.command_type != "append_requirement" and self.content is not None:
            raise ValueError("content is only allowed for append_requirement")
        return self


class RoleBindingItem(StrictModel):
    role: Literal["think", "plan", "execute", "review", "verifier"]
    model_config_id: str | None = None


class RoleBindingsRequest(StrictModel):
    bindings: list[RoleBindingItem] = Field(max_length=8)


class StageBindingItem(StrictModel):
    stage: Literal["think", "plan", "dispatch", "execute", "review"]
    model_config_id: str | None = None


class StageBindingsRequest(StrictModel):
    expected_version: int = Field(ge=0)
    bindings: list[StageBindingItem] = Field(min_length=5, max_length=5)


class WorkspaceRequest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    host_path: str = Field(min_length=3, max_length=4096)
    read_allow: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=100)
    write_allow: list[str] = Field(default_factory=list, max_length=100)
    excludes: list[str] = Field(default_factory=list, max_length=100)
    command_approval: bool = False

    @field_validator("read_allow", "write_allow")
    @classmethod
    def relative_prefixes(cls, value):
        from .workspace_io import WorkspaceError, _relative
        try:
            return [_relative(item, directory=True) for item in value]
        except WorkspaceError:
            raise ValueError("Use relative directory or file prefixes") from None

    @field_validator("excludes")
    @classmethod
    def exclusion_patterns(cls, value):
        if any(not item or len(item) > 256 or any(ord(c) < 32 for c in item) for item in value):
            raise ValueError("Invalid exclusion pattern")
        return value


class WorkspaceEnabledRequest(StrictModel):
    enabled: bool


class ApprovalDecisionRequest(StrictModel):
    decision: Literal["approve", "reject"]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    client_request_id: str = Field(min_length=1, max_length=100)
