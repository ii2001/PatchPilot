"""Execute repository tests only in a tightly constrained Docker container."""

import io
import json
import re
import subprocess
import tarfile
import tempfile
import time
import uuid

from .config import Settings
from .policy import check_relative_path, workspace_digest
from .state import TestExecution, TestResult


class SandboxError(RuntimeError):
    pass


class DockerTestRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        for target in settings.test_targets:
            path = target.split("::")[0]
            check_relative_path(path)
            if not path.endswith(".py") or any(char in target for char in "\x00\n\r"):
                raise ValueError("Test targets must be repository-relative Python test paths/node IDs")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in settings.required_env):
            raise ValueError("Required environment variable names are invalid")

    def _docker(self, *args, input_data=None, expected=(0,)):
        try:
            result = subprocess.run(
                ["docker", *args], input=input_data, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Docker unavailable ({type(exc).__name__})") from None
        if result.returncode not in expected:
            raise SandboxError(f"Docker {args[0]} failed; verify daemon and trusted image configuration")
        if len(result.stdout) > 4_000_000:
            raise SandboxError("Docker control response exceeds size limit")
        return result.stdout.decode("utf-8", errors="replace")

    def _preflight(self):
        image = self.settings.sandbox_image
        if not re.fullmatch(r"(?:[A-Za-z0-9_./:-]+@)?sha256:[0-9a-f]{64}", image):
            raise SandboxError("PATCHPILOT_SANDBOX_IMAGE must be an immutable sha256 image ID/digest")
        info = json.loads(self._docker("info", "--format", "{{json .}}"))
        if (
            info.get("OSType") != "linux" or not info.get("MemoryLimit")
            or not info.get("PidsLimit") or not info.get("CpuCfsQuota")
            or not any("seccomp" in option for option in info.get("SecurityOptions", []))
        ):
            raise SandboxError("Docker daemon lacks required Linux resource limits or seccomp")
        metadata = json.loads(self._docker("image", "inspect", image))[0]
        if (metadata["Config"].get("Labels") or {}).get("patchpilot.sandbox.version") != "1":
            raise SandboxError("Image is not a configured PatchPilot sandbox")
        for value in metadata["Config"].get("Env", []):
            if any(word in value.split("=", 1)[0].upper() for word in ("TOKEN", "SECRET", "API_KEY")):
                raise SandboxError("Sandbox image contains a secret-like environment variable")

    def _archive(self, files):
        if len(files) > 200 or sum(len(content.encode()) for content in files.values()) > 1_000_000:
            raise SandboxError("Workspace exceeds sandbox source limits")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for path, content in sorted(files.items()):
                check_relative_path(path)
                if any(part.casefold() == ".git" for part in path.split("/")):
                    raise SandboxError("Reserved Git path in sandbox source")
                data = content.encode()
                entry = tarfile.TarInfo(path)
                entry.size, entry.mode, entry.uid, entry.gid = len(data), 0o444, 0, 0
                archive.addfile(entry, io.BytesIO(data))
        return stream.getvalue()

    def _verify_container(self, container, volume):
        details = json.loads(self._docker("inspect", container))[0]
        host, config = details["HostConfig"], details["Config"]
        mounts = details["Mounts"]
        if (
            host.get("Privileged") or not host.get("ReadonlyRootfs")
            or host.get("NetworkMode") != "none" or host.get("Binds")
            or host.get("Devices") or host.get("CapAdd")
            or host.get("Memory") != 512 * 1024 * 1024
            or host.get("NanoCpus") != 1_000_000_000 or host.get("PidsLimit") != 64
            or "ALL" not in (host.get("CapDrop") or [])
            or not any(value.startswith("no-new-privileges") for value in host.get("SecurityOpt", []))
            or config.get("User") != "65532:65532"
            or len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != volume or mounts[0].get("RW")
            or mounts[0].get("Destination") != "/workspace"
        ):
            raise SandboxError("Docker container configuration failed safety verification")

    def _start(self, container):
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(["docker", "start", "--attach", container], stdout=output, stderr=output)
            deadline = time.monotonic() + self.settings.test_timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise SandboxError("Sandbox test timeout")
                    if output.tell() > 1_000_000:
                        raise SandboxError("Sandbox output exceeds limit")
                    time.sleep(0.05)
                if output.tell() > 1_000_000:
                    raise SandboxError("Sandbox output exceeds limit")
                output.seek(0)
                return output.read().decode("utf-8", errors="replace")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def run(self, files, *, phase, attempt) -> TestResult:
        digest = workspace_digest(files)
        volume = f"patchpilot-{uuid.uuid4().hex}"
        containers = []
        allocated = False
        try:
            self._preflight()
            archive = self._archive(files)
            allocated = True
            self._docker("volume", "create", "--label", "patchpilot.temporary=true", volume)
            metadata = json.loads(self._docker("volume", "inspect", volume))[0]
            if metadata.get("Driver") != "local" or metadata.get("Options"):
                raise SandboxError("Temporary volume has unsafe driver/options")
            image = self.settings.sandbox_image
            # This staging container is never started: only the Docker daemon copies static files.
            staging = f"{volume}-stage"
            containers.append(staging)
            self._docker(
                "create", "--name", staging, "--label", "patchpilot.temporary=true", "--network", "none", "--user", "65532:65532", "--entrypoint", "/bin/false",
                "--mount", f"type=volume,source={volume},target=/workspace", image,
            )
            self._docker("cp", "-", f"{staging}:/workspace", input_data=archive)
            container = f"{volume}-test"
            containers.append(container)
            self._docker(
                "create", "--name", container, "--label", "patchpilot.temporary=true", "--network", "none", "--read-only", "--user", "65532:65532",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
                "--ulimit", "nofile=256:256", "--log-driver", "none",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=134217728,mode=1777",
                "--mount", f"type=volume,source={volume},target=/workspace,readonly",
                "--workdir", "/workspace", "--entrypoint", "python",
                image, "-I", "/opt/patchpilot/runner.py",
                json.dumps(self.settings.test_targets), json.dumps(self.settings.required_env),
            )
            self._verify_container(container, volume)
            output = self._start(container)
            details = json.loads(self._docker("inspect", container))[0]["State"]
            if details.get("OOMKilled"):
                raise SandboxError("Sandbox exceeded memory limit")
            lines = [line for line in output.splitlines() if line.startswith("PATCHPILOT_RESULT=")]
            if len(lines) != 1:
                raise SandboxError("Missing or ambiguous sandbox result envelope")
            result = json.loads(lines[0].split("=", 1)[1])
            executions = [TestExecution.model_validate(item) for item in result["executions"]]
            expected_exit = 0 if result["outcome"] == "passed" else 1
            if details.get("ExitCode") != expected_exit:
                raise SandboxError("Sandbox exit code contradicts its report")
            return TestResult(
                outcome=result["outcome"], output=output[-64_000:], simulated=False,
                runner="docker", workspace_digest=digest, executions=executions,
            )
        except (SandboxError, ValueError, KeyError, OSError) as exc:
            return TestResult(
                outcome="environment_error", output=str(exc), simulated=False, runner="docker",
                workspace_digest=digest,
                executions=[TestExecution(stage="preflight", command=["docker", "preflight"], exit_code=125, output=str(exc))],
            )
        finally:
            cleanup_errors = []
            for container in reversed(containers):
                try:
                    self._docker("rm", "--force", container)
                except SandboxError:
                    cleanup_errors.append(container)
            if allocated:
                try:
                    self._docker("volume", "rm", volume)
                except SandboxError:
                    cleanup_errors.append(volume)
            if cleanup_errors:
                raise SandboxError("Sandbox cleanup failed; manual cleanup required: " + ", ".join(cleanup_errors))
