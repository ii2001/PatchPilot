"""SQLite jobs/delivery records, with per-job cross-process execution locks."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class JobBusyError(RuntimeError):
    pass


class JobStore:
    def __init__(self, directory: Path):
        self.directory = directory
        if directory.is_symlink():
            raise ValueError("State directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "jobs.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, repo TEXT NOT NULL, run_id TEXT NOT NULL,
                    run_attempt INTEGER NOT NULL, status TEXT NOT NULL,
                    request TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(repo, run_id, run_attempt)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, body_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_cache (
                    cache_key TEXT PRIMARY KEY, result TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(self, repo, run_id, run_attempt=1, head_sha="", *, delivery_id=None, body_hash=""):
        repo = repo.casefold()
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"patchpilot:{repo}:{run_id}:{run_attempt}").hex
        request = {"repo": repo, "run_id": str(run_id), "run_attempt": run_attempt, "head_sha": head_sha}
        now = time.time()
        with self.connect() as connection:
            if delivery_id:
                existing = connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                if existing:
                    if existing["body_hash"] != body_hash:
                        raise ValueError("Delivery ID reused with a different payload")
                    return existing["job_id"], False
            result = connection.execute(
                "INSERT OR IGNORE INTO jobs(id,repo,run_id,run_attempt,status,request,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, repo, str(run_id), run_attempt, "QUEUED", json.dumps(request), now, now),
            )
            created = result.rowcount == 1
            if delivery_id:
                connection.execute("INSERT INTO deliveries VALUES(?,?,?)", (delivery_id, job_id, body_hash))
        return job_id, created

    def get(self, job_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown job ID")
        data = dict(row)
        data["request"], data["result"] = json.loads(data["request"]), json.loads(data["result"])
        return data

    def save(self, job_id, status, result):
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status=?,result=?,updated=? WHERE id=?",
                               (status, json.dumps(result, ensure_ascii=False), time.time(), job_id))

    def pending(self):
        with self.connect() as connection:
            return [row["id"] for row in connection.execute(
                "SELECT id FROM jobs WHERE status IN ('QUEUED','RUNNING') ORDER BY created"
            )]

    def get_llm(self, key):
        with self.connect() as connection:
            row = connection.execute("SELECT result FROM llm_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row["result"]) if row else None

    def save_llm(self, key, result):
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO llm_cache VALUES(?,?)", (key, json.dumps(result)))

    @contextmanager
    def lock(self, job_id):
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("Invalid job ID")
        descriptor = os.open(self.directory / f"{job_id}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JobBusyError("Job is currently executing in another process") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
