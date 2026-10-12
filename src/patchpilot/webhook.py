"""Optional signed GitHub webhook receiver. Jobs are executed by a separate worker."""

import hashlib
import hmac
import json
import re

from .config import Settings
from .storage import JobStore


def create_app(settings: Settings | None = None):
    from fastapi import FastAPI, HTTPException, Request

    settings = settings or Settings.from_env()
    secret = settings.webhook_secret.get_secret_value()
    if not secret or not settings.allowed_repos:
        raise ValueError("Webhook requires PATCHPILOT_WEBHOOK_SECRET and PATCHPILOT_ALLOWED_REPOS")
    store = JobStore(settings.data_dir)
    app = FastAPI(title="PatchPilot Webhook", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request):
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 1_000_000:
                raise HTTPException(413, "Webhook payload exceeds limit")
        body = bytes(chunks)
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), expected):
            raise HTTPException(401, "Invalid webhook signature")
        delivery = request.headers.get("x-github-delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
            raise HTTPException(400, "Missing or invalid delivery ID")
        if request.headers.get("x-github-event") != "workflow_run":
            return {"ignored": True, "reason": "unsupported event"}
        try:
            event = json.loads(body)
            run = event["workflow_run"]
            repo = event["repository"]["full_name"]
            settings.allow_repository(repo)
            if event.get("action") != "completed" or run.get("status") != "completed" or run.get("conclusion") != "failure":
                return {"ignored": True, "reason": "not a completed failure"}
            if run.get("head_branch", "").startswith("patchpilot/"):
                return {"ignored": True, "reason": "repair branch excluded"}
            if run.get("head_repository", {}).get("full_name", "").casefold() != repo.casefold():
                return {"ignored": True, "reason": "fork run excluded"}
            run_id, attempt, sha = str(run["id"]), run.get("run_attempt", 1), run["head_sha"]
            if not run_id.isdecimal() or type(attempt) is not int or attempt < 1 or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("Invalid run metadata")
            job_id, created = store.enqueue(
                repo, run_id, attempt, sha, delivery_id=delivery, body_hash=hashlib.sha256(body).hexdigest()
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid event payload or disallowed repository") from None
        return {"job_id": job_id, "queued": created, "duplicate": not created}

    return app
