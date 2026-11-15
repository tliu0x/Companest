import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from companest.jobs.jobs import JobManager, JobStatus


@pytest.mark.asyncio
async def test_start_recovers_legacy_running_job_and_processes_each_job_once(tmp_path):
    with sqlite3.connect(tmp_path / "jobs.db") as db:
        db.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                context TEXT, subtasks TEXT, result TEXT, error TEXT,
                created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, submitted_by TEXT
            )
        """)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO jobs (id, task, status, context, created_at, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", "recover", "running", "null", now, now),
        )

    calls = []

    class Orchestrator:
        async def run_auto(self, task, user_context):
            calls.append(task)
            await asyncio.sleep(0)
            return "done", None

    manager = JobManager(Orchestrator(), data_dir=tmp_path)
    queued_id = await manager.submit("queued before startup", company_id="acme")
    await manager.start(num_workers=2)
    try:
        async with asyncio.timeout(5):
            while any(
                job.status != JobStatus.COMPLETED
                for job in [await manager.get_job("legacy"), await manager.get_job(queued_id)]
            ):
                await asyncio.sleep(0.01)
        assert sorted(calls) == ["queued before startup", "recover"]
        assert manager.get_company_activity_snapshot()["acme"]["total_jobs"] == 1
    finally:
        await manager.stop()
