import json
import subprocess

import pytest

from patchpilot.config import Settings
from patchpilot.docker_runner import DockerTestRunner, SandboxError


def test_missing_image_fails_closed_without_subprocess(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("No Docker call with unconfigured image"))
    result = DockerTestRunner(Settings()).run({"app.py": "x = 1"}, phase="reproduce", attempt=0)
    assert result.outcome == "environment_error" and not result.simulated
    assert result.executions[0].stage == "preflight"


def test_missing_docker_is_explicit(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", missing)
    result = DockerTestRunner(Settings(sandbox_image="sha256:" + "a" * 64)).run({"app.py": "x = 1"}, phase="reproduce", attempt=0)
    assert result.outcome == "environment_error" and "Docker unavailable" in result.output


@pytest.mark.parametrize("unsafe", ["network", "mount", "user", "caps", "resources"])
def test_container_safety_check_rejects_unsafe_configuration(unsafe):
    details = {
        "HostConfig": {"Privileged": False, "ReadonlyRootfs": True, "NetworkMode": "none", "Memory": 512 * 1024 * 1024,
                       "NanoCpus": 1_000_000_000, "PidsLimit": 64, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges=true"]},
        "Config": {"User": "65532:65532"},
        "Mounts": [{"Type": "volume", "Name": "test-volume", "RW": False, "Destination": "/workspace"}],
    }
    if unsafe == "network": details["HostConfig"]["NetworkMode"] = "host"
    elif unsafe == "mount": details["Mounts"][0]["Type"] = "bind"
    elif unsafe == "user": details["Config"]["User"] = "0"
    elif unsafe == "caps": details["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    else: details["HostConfig"]["Memory"] = 0
    runner = DockerTestRunner(Settings())
    runner._docker = lambda *args: json.dumps([details])
    with pytest.raises(SandboxError): runner._verify_container("container", "test-volume")


def test_targets_reject_host_paths_and_invalid_environment():
    with pytest.raises(ValueError): DockerTestRunner(Settings(test_targets=("../host.py",)))
    with pytest.raises(ValueError): DockerTestRunner(Settings(required_env=("TOKEN=secret",)))


def test_create_response_loss_still_cleans_named_resources():
    runner = DockerTestRunner(Settings(sandbox_image="sha256:" + "a" * 64))
    runner._preflight = lambda: None
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("volume", "inspect"):
            return json.dumps([{"Driver": "local", "Options": None}])
        if args[0] == "create":
            raise SandboxError("Simulated lost Docker create response")
        return ""
    runner._docker = docker
    result = runner.run({"app.py": "x = 1"}, phase="reproduce", attempt=0)
    assert result.outcome == "environment_error"
    assert any(call[0] == "rm" and call[-1].endswith("-stage") for call in calls)
    assert any(call[:2] == ("volume", "rm") for call in calls)
