"""Versioned v6 review claims; schema conformance never proves truth."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ReviewContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CandidateReference(_ReviewContract):
    operation_id: str | None = Field(default=None, min_length=1, max_length=80)
    artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounded_reference(self):
        if not self.operation_id and not self.artifact_id:
            raise ValueError("A candidate requires a reference")
        if (self.start is None) != (self.end is None):
            raise ValueError("Both byte range endpoints are required")
        if self.start is not None and (not self.artifact_id or self.start >= self.end):
            raise ValueError("Invalid artifact byte range")
        if self.expected_sha256 and not self.artifact_id:
            raise ValueError("A digest requires an artifact reference")
        return self


class ReviewFindingV6(_ReviewContract):
    finding_id: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=1000)
    severity: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    description: str = Field(min_length=1, max_length=4000)
    model_status: Literal["PASS", "FAIL", "UNVERIFIED", "BLOCKED", "DISPUTED"]
    recommendation: str = Field(min_length=1, max_length=4000)
    candidate_refs: list[CandidateReference] = Field(max_length=8)


class ReviewV6(_ReviewContract):
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[ReviewFindingV6] = Field(max_length=20)
    unverified: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def distinct_findings(self):
        if len({finding.finding_id for finding in self.findings}) != len(self.findings):
            raise ValueError("Finding identifiers must be unique")
        if any(not item or len(item) > 4000 for item in self.unverified):
            raise ValueError("Invalid unverified item")
        return self


class CandidateReferenceV9(CandidateReference):
    # A reference checks provenance of one stated dimension, not claim truth.
    dimension: Literal["execution", "exit_code", "log", "initial_snapshot", "runtime_source"]

    def is_unbound_execution_digest(self):
        # Preserve this observed malformed shape for reporting, not validation.
        return (self.model_fields_set == {"operation_id", "dimension", "expected_sha256"}
                and self.dimension == "execution" and bool(self.operation_id)
                and bool(self.expected_sha256) and self.artifact_id is None)

    @model_validator(mode="after")
    def bounded_reference(self):
        if not self.is_unbound_execution_digest():
            super().bounded_reference()
        return self

    @model_validator(mode="after")
    def dimension_binding(self):
        if self.is_unbound_execution_digest():
            return self
        if self.dimension == "log":
            if not self.artifact_id or not self.expected_sha256:
                raise ValueError("Log references require an artifact and digest")
        elif self.artifact_id or self.expected_sha256:
            raise ValueError("Only log dimensions accept an artifact")
        if not self.operation_id:
            raise ValueError("A dimension requires an operation")
        return self


class ReviewFindingV9(ReviewFindingV6):
    candidate_refs: list[CandidateReferenceV9] = Field(max_length=8)


class ReviewV9(ReviewV6):
    findings: list[ReviewFindingV9] = Field(max_length=20)
