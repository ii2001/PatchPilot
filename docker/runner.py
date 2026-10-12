"""Trusted image harness. Imported/executed only inside Docker."""

import json
import os
import subprocess
import sys
import tempfile


PLUGIN = '''
import json
class Recorder:
    def __init__(self):
        self.ids = []
        self.passed = self.failed = self.skipped = 0
    def pytest_collection_finish(self, session):
        self.ids = [item.nodeid for item in session.items]
    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed += 1
        elif report.skipped:
            self.skipped += 1
        elif report.when == "call" and report.passed:
            self.passed += 1
    def pytest_sessionfinish(self, session, exitstatus):
        data = dict(collected=len(self.ids), node_ids=self.ids, passed=self.passed,
                    failed=self.failed, skipped=self.skipped)
        print("\\nPATCHPILOT_COUNTS=" + json.dumps(data), flush=True)
recorder = Recorder()
pytest_collection_finish = recorder.pytest_collection_finish
pytest_runtest_logreport = recorder.pytest_runtest_logreport
pytest_sessionfinish = recorder.pytest_sessionfinish
'''


def main():
    targets, required_env = json.loads(sys.argv[1]), json.loads(sys.argv[2])
    missing = [name for name in required_env if not os.environ.get(name)]
    if missing:
        executions = [{"stage": "preflight", "command": ["environment-preflight"], "exit_code": 3,
                       "output": "Missing required environment variables: " + ", ".join(missing)}]
        outcome = "environment_error"
    else:
        executions = []
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            with open(temporary + "/patchpilot_probe.py", "w") as plugin:
                plugin.write(PLUGIN)
            env = {
                "PATH": os.environ["PATH"], "HOME": "/tmp", "TMPDIR": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONPATH": temporary + ":/workspace:/workspace/src",
            }
            for stage, selection in (("targeted", targets or ["/workspace"]), ("full", ["/workspace"])):
                command = [sys.executable, "-m", "pytest", "-c", "/opt/patchpilot/pytest.ini",
                           "--rootdir", "/workspace", "-p", "no:cacheprovider", "-p", "patchpilot_probe", "--tb=short", "-q", "--", *selection]
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                output = result.stdout + result.stderr
                counts = [line for line in output.splitlines() if line.startswith("PATCHPILOT_COUNTS=")]
                stats = json.loads(counts[0].split("=", 1)[1]) if len(counts) == 1 else {}
                executions.append({"stage": stage, "command": command, "exit_code": result.returncode,
                                   "output": output[-32000:], **stats})
        outcome = "passed" if all(
            item["exit_code"] == 0 and item.get("collected", 0) > 0
            and item.get("passed", 0) > 0 and item.get("failed", 0) == 0 for item in executions
        ) else "failed"
    print("PATCHPILOT_RESULT=" + json.dumps({"outcome": outcome, "executions": executions}), flush=True)
    return 0 if outcome == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
