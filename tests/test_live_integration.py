"""Opt-in read/LLM smoke tests. These never publish a PR or write to GitHub."""

import os

import pytest

from patchpilot.config import Settings
from patchpilot.github import RealGitHubClient
from patchpilot.llm import RealLLMClient
from patchpilot.scenarios import run_scenario

pytestmark = pytest.mark.live


def test_live_openai_structured_diagnosis():
    settings = Settings.from_env()
    if os.getenv("PATCHPILOT_RUN_LIVE_TESTS") != "1" or not settings.openai_api_key.get_secret_value() or not settings.openai_model:
        pytest.skip("Explicit live opt-in and OpenAI credentials/model required")
    client = RealLLMClient(settings)
    assert client.diagnose(run_scenario("success")).cause
    assert client.calls


def test_live_github_failed_run_read():
    settings = Settings.from_env()
    repo, run_id = os.getenv("PATCHPILOT_LIVE_REPO", ""), os.getenv("PATCHPILOT_LIVE_RUN_ID", "")
    if os.getenv("PATCHPILOT_RUN_LIVE_TESTS") != "1" or not settings.github_token.get_secret_value() or not repo or not run_id:
        pytest.skip("Explicit live opt-in, GitHub token and failed workflow required")
    client = RealGitHubClient(settings)
    try:
        failure = client.load_failure(repo, run_id)
        assert failure.head_sha and client.collect_context(failure)
    finally:
        client.close()
