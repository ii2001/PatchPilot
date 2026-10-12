"""Validated data exchanged by the graph and adapters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ATTEMPTS = 3
Outcome = Literal["passed", "failed", "environment_error"]
Status = Literal[
    "pending", "success", "failed", "not_reproduced", "manual_action_required",
    "local_verified", "approved", "rejected", "publish_failed", "ci_pending", "ci_verified", "ci_failed",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureInfo(Model):
    repository: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    log: str = Field(min_length=1)
    head_sha: str = ""
    base_branch: str = ""
    tree_sha: str = ""
    run_attempt: int = Field(default=1, ge=1)
    workflow_id: int | None = None
    run_url: str = ""
    created_at: str = ""
    failed_jobs: list[dict] = Field(default_factory=list)
    blobs: dict[str, str] = Field(default_factory=dict)
    modes: dict[str, str] = Field(default_factory=dict)


class TestExecution(Model):
    stage: Literal["preflight", "targeted", "full"]
    command: list[str]
    exit_code: int
    output: str
    collected: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    node_ids: list[str] = Field(default_factory=list)


class TestResult(Model):
    outcome: Outcome
    output: str
    simulated: bool = True
    runner: Literal["mock", "docker"] = "mock"
    workspace_digest: str = ""
    executions: list[TestExecution] = Field(default_factory=list)

    @model_validator(mode="after")
    def verify_provenance(self):
        if not self.simulated and (
            self.runner != "docker" or len(self.workspace_digest) != 64 or not self.executions
        ):
            raise ValueError("Real results require Docker provenance, a workspace digest and execution evidence")
        if self.simulated and self.runner != "mock":
            raise ValueError("Mock results must use the mock runner")
        return self


class FailureClassification(Model):
    category: Literal["code", "environment", "unsupported"]
    reason: str = Field(min_length=1)


class Diagnosis(Model):
    cause: str = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)
    category: Literal["code", "environment", "unsupported"] = "code"
    evidence: list[str] = Field(default_factory=list)


class FileChange(Model):
    path: str = Field(min_length=1)
    before: str
    after: str


class Patch(Model):
    changes: list[FileChange] = Field(min_length=1)
    rationale: str = ""


class LLMCall(Model):
    operation: str
    model: str
    response: dict = Field(default_factory=dict)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0, ge=0)
    error: str | None = None
    cached: bool = False


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
    simulated: bool = True
    approved: bool = False
    diff: str = ""
    llm_calls: list[LLMCall] = Field(default_factory=list)
    elapsed_seconds: float = Field(default=0, ge=0)
    pr_url: str | None = None
    published_sha: str | None = None
