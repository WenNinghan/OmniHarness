"""Bounded, versioned user-input decisions for interactive workflows."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UserQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    reason: str = Field(default="", max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_text(self):
        if not self.question.strip() or any(not item.strip() or len(item) > 200 for item in self.options):
            raise ValueError("question or option is blank or too long")
        if len(set(self.options)) != len(self.options):
            raise ValueError("duplicate options")
        return self


class UserDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["continue", "needs_user"]
    questions: list[UserQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_decision(self):
        if (self.status == "needs_user") != bool(self.questions):
            raise ValueError("needs_user requires questions; continue must not contain questions")
        return self


class TaskDecision(BaseModel):
    """Fail closed when an executor still needs input after preflight."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "needs_user"]
    content: str = Field(max_length=16000)
    questions: list[UserQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_decision(self):
        if (self.status == "needs_user") != bool(self.questions):
            raise ValueError("needs_user requires questions; completed must not contain questions")
        if self.status == "completed" and not self.content.strip():
            raise ValueError("completed task requires output")
        return self


TASK_DECISION_INSTRUCTION = (
    "最终输出必须是 JSON 对象：{\"status\":\"completed\",\"content\":\"结果\",\"questions\":[]}；"
    "若确实需要用户回答才能继续，使用 {\"status\":\"needs_user\",\"content\":\"已做工作\","
    "\"questions\":[{\"question\":\"问题\",\"reason\":\"原因\",\"options\":[]}]}。"
    "不可将需要用户回复的问题作为已完成结果。"
)


DECISION_INSTRUCTION = (
    "判断是否有必须由用户回答才能安全或正确继续的关键信息；"
    "可以合理推断或不影响正确完成的细节直接采用合理假设，不要无谓提问。"
    "只输出 JSON：{\"status\":\"continue\",\"questions\":[]}，"
    "或 {\"status\":\"needs_user\",\"questions\":[{\"question\":\"问题\","
    "\"reason\":\"必须询问的原因\",\"options\":[]}]}。最多三个问题，可自由文本回答。"
)
