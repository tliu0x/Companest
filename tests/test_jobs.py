import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from companest.jobs.jobs import JobManager, JobStatus


@pytest.mark.asyncio
async def test_start_marks_legacy_running_job_unknown_without_replaying(tmp_path, api_runtime):
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

    _, _, server = api_runtime()
    manager = server.job_manager
    await manager.start()
    try:
        job = await manager.get_job("legacy")
        assert job.status == JobStatus.FAILED
        assert "not replayed" in job.error
        manager.client.create_actor.assert_not_awaited()
    finally:
        await manager.stop()
