"""Validated data exchanged by the graph and adapters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ATTEMPTS = 3
Outcome = Literal["passed", "failed", "environment_error"]
Status = Literal["pending", "success", "failed", "not_reproduced", "manual_action_required"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureInfo(Model):
    repository: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    log: str = Field(min_length=1)


class TestResult(Model):
    outcome: Outcome
    output: str
    simulated: Literal[True] = True


class Diagnosis(Model):
    cause: str = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)


class FileChange(Model):
    path: str = Field(min_length=1)
    before: str
    after: str


class Patch(Model):
    changes: list[FileChange] = Field(min_length=1)


class AppliedPatch(Model):
    attempt: int = Field(ge=1, le=MAX_ATTEMPTS)
    patch: Patch


class RepairState(Model):
    failure: FailureInfo
    original_files: dict[str, str] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    reproduction: TestResult | None = None
    classification: Literal["code", "environment"] | None = None
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    patch: Patch | None = None
    applied_patches: list[AppliedPatch] = Field(default_factory=list)
    attempt: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    test_results: list[TestResult] = Field(default_factory=list)
    status: Status = "pending"
    error: str | None = None
    trace: list[str] = Field(default_factory=list)
    report: str = ""
