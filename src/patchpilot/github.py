"""Bounded GitHub REST reads and approval-gated, idempotent Git database writes."""

import base64
import hashlib
import re
from urllib.parse import urlparse

import httpx

from .config import Settings
from .policy import check_relative_path, check_scope, final_diff, workspace_digest
from .state import FailureInfo, RepairState

MAX_FILES = 200
MAX_SOURCE_BYTES = 1_000_000


class GitHubError(RuntimeError):
    pass


class RealGitHubClient:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        self.settings = settings
        if client is None and not settings.github_token.get_secret_value():
            raise ValueError("GITHUB_TOKEN is required")
        self.client = client or httpx.Client(
            base_url="https://api.github.com", timeout=30, follow_redirects=False,
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self):
        self.client.close()

    def _request(self, method, path, *, expected=(200,), **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed ({type(exc).__name__})") from None
        if response.status_code not in expected:
            message = f"GitHub {method} {path} returned HTTP {response.status_code}"
            permissions = response.headers.get("x-accepted-github-permissions")
            if permissions:
                message += f" (requires {permissions})"
            raise GitHubError(message)
        if len(response.content) > 4_000_000:
            raise GitHubError("GitHub response exceeds the supported size")
        return response

    def _json(self, method, path, **kwargs):
        return self._request(method, path, **kwargs).json()

    def _pages(self, path, key=None, **params):
        items = []
        for page in range(1, 11):
            result = self._json("GET", path, params={**params, "per_page": 100, "page": page})
            batch = result[key] if key else result
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise GitHubError("Pagination exceeds the supported limit")

    def _job_log(self, repo: str, job_id: int) -> str:
        response = self._request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", expected=(200, 302))
        if response.status_code == 200:
            return response.text[:64_000]
        url = response.headers.get("location", "")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or not (
            host.endswith(".githubusercontent.com") or host.endswith(".blob.core.windows.net")
        ):
            raise GitHubError("Unsupported GitHub log download host")
        # A separate unauthenticated request avoids forwarding the GitHub token to signed URLs.
        try:
            with httpx.stream("GET", url, timeout=30, follow_redirects=False) as download:
                download.raise_for_status()
                data = bytearray()
                for chunk in download.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1_000_000:
                        raise GitHubError("Job log exceeds the supported size")
        except httpx.HTTPError as exc:
            raise GitHubError(f"Log download failed ({type(exc).__name__})") from None
        return data.decode("utf-8", errors="replace")[:64_000]

    def load_failure(self, repo: str, run_id: str) -> FailureInfo:
        self.settings.allow_repository(repo)
        if not run_id.isdecimal():
            raise ValueError("Workflow run ID must be numeric")
        prefix = f"/repos/{repo}"
        run = self._json("GET", f"{prefix}/actions/runs/{run_id}")
        if run["status"] != "completed" or run["conclusion"] != "failure":
            raise GitHubError("Workflow run is not a completed failure")
        if run.get("head_repository", {}).get("full_name", "").casefold() != repo.casefold():
            raise GitHubError("Fork workflow runs are not supported")
        branch = run.get("head_branch") or ""
        if branch.startswith("patchpilot/"):
            raise GitHubError("PatchPilot repair branches are excluded to prevent repair loops")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
            raise GitHubError("Unsupported base branch")
        sha = run["head_sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError("Invalid workflow commit SHA")
        attempt = run.get("run_attempt", 1)
        jobs = self._pages(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        failed = [job for job in jobs if job.get("conclusion") in {"failure", "timed_out"}]
        if not failed:
            raise GitHubError("No failed jobs found")
        logs = []
        details = []
        for job in failed:
            steps = [step for step in job.get("steps", []) if step.get("conclusion") in {"failure", "timed_out"}]
            details.append({"id": job["id"], "name": job["name"], "steps": steps})
            logs.append(f"Job: {job['name']}\n{self._job_log(repo, job['id'])}")
        commit = self._json("GET", f"{prefix}/git/commits/{sha}")
        tree_sha = commit["tree"]["sha"]
        tree = self._json("GET", f"{prefix}/git/trees/{tree_sha}", params={"recursive": "1"})
        if tree.get("truncated"):
            raise GitHubError("Truncated source tree is unsupported")
        blobs, modes = {}, {}
        total = 0
        for item in tree["tree"]:
            path = item["path"]
            check_relative_path(path)
            if item["type"] == "tree":
                continue
            if item["type"] != "blob" or item["mode"] not in {"100644", "100755"}:
                raise GitHubError("Symlinks and submodules are unsupported")
            if any(part.casefold() == ".git" for part in path.split("/")):
                raise GitHubError("Source tree contains a reserved Git path")
            if path.split("/")[-1].startswith(".env") and not path.endswith(".example"):
                raise GitHubError("Repository contains an environment secret file")
            total += item.get("size", 0)
            blobs[path], modes[path] = item["sha"], item["mode"]
        if len(blobs) > MAX_FILES or total > MAX_SOURCE_BYTES:
            raise GitHubError("Repository exceeds supported source limits")
        return FailureInfo(
            repository=repo, run_id=run_id, log="\n\n".join(logs)[:128_000],
            head_sha=sha, base_branch=branch, tree_sha=tree_sha, run_attempt=attempt,
            workflow_id=run["workflow_id"], run_url=run["html_url"], created_at=run["created_at"],
            failed_jobs=details, blobs=blobs, modes=modes,
        )

    def collect_context(self, failure: FailureInfo) -> dict[str, str]:
        self.settings.allow_repository(failure.repository)
        files, total = {}, 0
        for path, sha in failure.blobs.items():
            result = self._json("GET", f"/repos/{failure.repository}/git/blobs/{sha}")
            if result.get("encoding") != "base64":
                raise GitHubError("Unsupported blob encoding")
            try:
                data = base64.b64decode(result["content"].replace("\n", ""), validate=True)
                if hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() != sha:
                    raise GitHubError("Source blob does not match its commit object")
                source = data.decode("utf-8")
            except (ValueError, UnicodeError):
                raise GitHubError("Only UTF-8 text repositories are currently supported") from None
            if "\x00" in source:
                raise GitHubError("Binary source files are unsupported")
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise GitHubError("Source exceeds supported size")
            files[path] = source
        return files

    def create_draft_pr(self, state: RepairState) -> dict[str, str]:
        failure = state.failure
        repo = failure.repository
        self.settings.allow_repository(repo)
        if not state.approved or state.simulated or state.status != "approved":
            raise GitHubError("Publishing requires explicit approval of real local verification")
        from .verification import validate_repair

        validate_repair(state)
        check_scope(state.original_files, state.files)
        if set(failure.blobs) != set(state.original_files):
            raise GitHubError("Original workspace does not match the commit manifest")
        for path, content in state.original_files.items():
            data = content.encode()
            if hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() != failure.blobs[path]:
                raise GitHubError("Original workspace blob verification failed")
        prefix = f"/repos/{repo}"
        base_ref = self._json("GET", f"{prefix}/git/ref/heads/{failure.base_branch}")
        if base_ref["object"]["sha"] != failure.head_sha:
            raise GitHubError("Base branch moved since the tested commit; repair must be rerun")
        changed = [path for path in sorted(state.files) if state.files[path] != state.original_files[path]]
        tree = self._json("POST", f"{prefix}/git/trees", expected=(201,), json={
            "base_tree": failure.tree_sha,
            "tree": [{"path": path, "mode": failure.modes[path], "type": "blob", "content": state.files[path]} for path in changed],
        })
        identity = {"name": "PatchPilot", "email": "patchpilot@users.noreply.github.com", "date": failure.created_at}
        message = (
            f"🐛 fix(ci): 실패한 Workflow Run {failure.run_id}의 pytest 오류 수정\n\n"
            f"변경 이유:\n{state.diagnoses[-1].cause}\n\n"
            f"수정 내용:\n{state.diagnoses[-1].suggested_fix}\n"
            f"변경 파일: {', '.join(changed)}\n수정 시도: {state.attempt}/3\n\n"
            f"검증:\n- Docker 대상 및 전체 pytest 통과, 패치 정책 및 원본 내용 검증 완료\n"
            f"- 원본 SHA: {failure.head_sha}\n- 원본 Run: {failure.run_url}\n"
            "- 원격 CI는 발행 후 별도로 확인하며 자동 Merge는 수행하지 않음"
        )
        commit = self._json("POST", f"{prefix}/git/commits", expected=(201,), json={
            "message": message,
            "tree": tree["sha"], "parents": [failure.head_sha], "author": identity, "committer": identity,
        })
        # Deterministic tree, parent, message and identities make replay produce the same Git object.
        branch = f"patchpilot/repair-{failure.run_id}-{failure.head_sha[:12]}"
        ref_path = f"{prefix}/git/ref/heads/{branch}"
        existing = self._request("GET", ref_path, expected=(200, 404))
        if existing.status_code == 200:
            if existing.json()["object"]["sha"] != commit["sha"]:
                raise GitHubError("Repair branch collision; refusing to overwrite")
        else:
            created = self._request("POST", f"{prefix}/git/refs", expected=(201, 422), json={
                "ref": f"refs/heads/{branch}", "sha": commit["sha"],
            })
            if created.status_code == 422:
                if self._json("GET", ref_path)["object"]["sha"] != commit["sha"]:
                    raise GitHubError("Repair branch collision")
        owner = repo.split("/")[0]
        pulls = self._pages(f"{prefix}/pulls", state="all", head=f"{owner}:{branch}", base=failure.base_branch)
        if pulls:
            return {"pr_url": pulls[0]["html_url"], "published_sha": commit["sha"]}
        cause = state.diagnoses[-1].cause
        from .reporting import approval_summary

        body = (
            f"## Failure\n{cause}\n\nOriginal run: {failure.run_url}\nBase SHA: `{failure.head_sha}`\n\n"
            f"## Repair\n{state.diagnoses[-1].suggested_fix}\nAttempts: {state.attempt}/3\n\n"
            f"## Local verification\n{approval_summary(state)}\n\n"
            "Docker targeted and full pytest passed. Remote CI is pending. Tests are not a proof of semantic correctness.\n\n"
            f"```diff\n{final_diff(state.original_files, state.files)}\n```\n\n"
            "Human review is required. PatchPilot never merges automatically."
        )
        response = self._request("POST", f"{prefix}/pulls", expected=(201, 422), json={
            "title": f"[PatchPilot] Repair failed workflow run {failure.run_id}",
            "head": branch, "base": failure.base_branch, "draft": True, "body": body,
        })
        if response.status_code == 422:
            pulls = self._pages(f"{prefix}/pulls", state="all", head=f"{owner}:{branch}", base=failure.base_branch)
            if not pulls:
                raise GitHubError("Draft PR creation rejected")
            pull = pulls[0]
        else:
            pull = response.json()
        return {"pr_url": pull["html_url"], "published_sha": commit["sha"]}

    def ci_status(self, state: RepairState) -> str:
        if not state.published_sha:
            return state.status
        runs = self._pages(f"/repos/{state.failure.repository}/actions/runs", "workflow_runs", head_sha=state.published_sha)
        # Observe the source workflow and all other workflows on the exact published SHA.
        latest = {}
        for run in runs:
            if run["head_sha"] == state.published_sha:
                key = run["workflow_id"]
                if key not in latest or run["id"] > latest[key]["id"]:
                    latest[key] = run
        if state.failure.workflow_id not in latest:
            return "ci_pending"
        if any(run["status"] != "completed" for run in latest.values()):
            return "ci_pending"
        if any(run["conclusion"] != "success" for run in latest.values()):
            return "ci_failed"
        return "ci_verified"
