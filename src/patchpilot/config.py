"""Explicit environment configuration; secrets never enter graph checkpoints."""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""
    temperature: float = Field(default=0, ge=0, le=2)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    llm_timeout: float = Field(default=60, gt=0, le=300)
    github_token: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    allowed_repos: frozenset[str] = frozenset()
    data_dir: Path = Path(".patchpilot")
    sandbox_image: str = ""
    test_timeout: int = Field(default=120, ge=1, le=600)
    test_targets: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()

    @classmethod
    def from_env(cls):
        def items(name):
            return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())

        return cls(
            openai_api_key=SecretStr(os.getenv("OPENAI_API_KEY", "")),
            openai_model=os.getenv("PATCHPILOT_OPENAI_MODEL", ""),
            temperature=os.getenv("PATCHPILOT_TEMPERATURE", "0"),
            reasoning_effort=os.getenv("PATCHPILOT_REASONING_EFFORT", "").strip() or None,
            llm_timeout=os.getenv("PATCHPILOT_LLM_TIMEOUT", "60"),
            github_token=SecretStr(os.getenv("GITHUB_TOKEN", "")),
            webhook_secret=SecretStr(os.getenv("PATCHPILOT_WEBHOOK_SECRET", "")),
            allowed_repos=frozenset(repo.casefold() for repo in items("PATCHPILOT_ALLOWED_REPOS")),
            data_dir=Path(os.getenv("PATCHPILOT_DATA_DIR", ".patchpilot")),
            sandbox_image=os.getenv("PATCHPILOT_SANDBOX_IMAGE", ""),
            test_timeout=os.getenv("PATCHPILOT_TEST_TIMEOUT", "120"),
            test_targets=items("PATCHPILOT_TEST_TARGETS"),
            required_env=items("PATCHPILOT_REQUIRED_ENV"),
        )

    def allow_repository(self, repo: str) -> None:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or repo.casefold() not in self.allowed_repos:
            raise ValueError("Repository is not in PATCHPILOT_ALLOWED_REPOS")
