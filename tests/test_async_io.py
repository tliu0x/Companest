import asyncio
import json
import tarfile
import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from companest.agents.builtin_tools import create_builtin_tool_defs
from companest.agents.tools import create_memory_tool_defs
from companest.agents.workspace import Workspace
from companest.knowledge.digests import DigestIngestor, DigestS3Store, create_digest_router
from companest.knowledge.public import IndexStore, create_public_knowledge_router
from companest.memory import MemoryManager
from companest.memory.archiver import MemoryArchiver
from companest.utils import run_sync


class SlowIO:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def block(self):
        self.started.set()
        assert self.release.wait(3), "I/O blocked the event loop"

    async def wait_started(self):
        async def poll():
            while not self.started.is_set():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
async def test_offload_preserves_context_and_waits_for_cleanup_on_cancel():
    context = ContextVar("test_company", default=None)
    context.set("acme")
    slow = SlowIO()
    finished = threading.Event()

    def work():
        assert context.get() == "acme"
        try:
            slow.block()
        finally:
            finished.set()

    task = asyncio.create_task(run_sync(work))
    try:
        await slow.wait_started()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        slow.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["backup", "restore"])
async def test_snapshot_transport_leaves_event_loop_responsive(tmp_path, operation):
    memory = MemoryManager(str(tmp_path / "active"))
    memory.write_team_memory("alpha", "notes.json", {"text": "saved"})
    archiver = MemoryArchiver(memory, "test-bucket")
    archiver._s3 = MagicMock()
    slow = SlowIO()

    def upload(*args):
        slow.block()

    def download(bucket, key, path):
        slow.block()
        with tarfile.open(path, "w:gz") as tar:
            member = tarfile.TarInfo(".companest")
            member.type = tarfile.DIRTYPE
            tar.addfile(member)

    archiver._s3.upload_file.side_effect = upload
    archiver._s3.download_file.side_effect = download
    coro = archiver.backup_snapshot() if operation == "backup" else archiver.restore_snapshot("key", str(tmp_path / "restore"))
    task = asyncio.create_task(coro)
    try:
        await slow.wait_started()
        assert not task.done()
        await asyncio.wait_for(asyncio.sleep(0), 0.5)
    finally:
        slow.release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("subsystem", ["knowledge", "digests"])
async def test_health_responds_while_s3_route_is_blocked(monkeypatch, subsystem):
    slow = SlowIO()
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    if subsystem == "knowledge":
        def load_index(self):
            slow.block()
            return []
        monkeypatch.setattr(IndexStore, "load_index", load_index)
        app.include_router(create_public_knowledge_router())
        path = "/api/public-knowledge/search"
    else:
        store = DigestS3Store("test-bucket")
        def get_latest(*args):
            slow.block()
            return {"company_id": "acme"}
        monkeypatch.setattr(store, "get_latest_digest", get_latest)
        app.include_router(create_digest_router(store))
        path = "/api/companies/acme/digests/positions/latest"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        task = asyncio.create_task(client.get(path))
        try:
            await slow.wait_started()
            assert not task.done()
            response = await asyncio.wait_for(client.get("/health"), 0.5)
            assert response.json() == {"ok": True}
        finally:
            slow.release.set()
            result = await task
        assert result.status_code == 200


@pytest.mark.asyncio
async def test_workspace_read_is_offloaded(tmp_path, monkeypatch):
    slow = SlowIO()
    def read(self, path):
        slow.block()
        return "contents"
    monkeypatch.setattr(Workspace, "read_file", read)
    tools = create_builtin_tool_defs(SimpleNamespace(extra={"workspace_path": str(tmp_path)}))
    tool = next(t for t in tools if t.name == "Read")
    task = asyncio.create_task(tool.handler({"path": "notes.txt"}))
    try:
        await slow.wait_started()
        assert not task.done()
    finally:
        slow.release.set()
        result = await task
    assert result == "contents"


@pytest.mark.asyncio
async def test_threaded_memory_writes_preserve_index_and_appends(tmp_path, monkeypatch):
    memory = MemoryManager(str(tmp_path))
    read_index = memory._read_index
    def delayed_index(team_id):
        result = read_index(team_id)
        time.sleep(0.001)
        return result
    monkeypatch.setattr(memory, "_read_index", delayed_index)
    tools = {t.name: t.handler for t in create_memory_tool_defs(memory, "alpha", "lead")}
    await asyncio.gather(*[
        tools["memory_write"]({"key": f"entry-{n}.json", "value": json.dumps({"n": n})})
        for n in range(25)
    ])
    await asyncio.gather(*[
        run_sync(memory.append_team_memory, "alpha", "inbox.json", n)
        for n in range(30)
    ])
    assert set(memory.get_memory_index("alpha")) == {"inbox.json", *[f"entry-{n}.json" for n in range(25)]}
    assert sorted(memory.read_team_memory("alpha", "inbox.json")) == list(range(30))


@pytest.mark.asyncio
async def test_concurrent_digest_duplicate_is_written_once():
    from datetime import datetime, timezone
    from companest.knowledge.digests import MarketSnapshotDigest

    store = MagicMock(spec=DigestS3Store)
    store.put_digest.side_effect = lambda **kwargs: time.sleep(0.02)
    ingestor = DigestIngestor(store)
    digest = MarketSnapshotDigest(company_id="acme", snapshot_timestamp=datetime.now(timezone.utc), idempotency_key="same", markets=[])
    results = await asyncio.gather(*[run_sync(ingestor.ingest, digest) for _ in range(8)])
    assert all(result.accepted for result in results)
    assert sum(result.reason == "accepted" for result in results) == 1
    store.put_digest.assert_called_once()


@pytest.mark.asyncio
async def test_parallel_router_prompts_keep_company_context_separate():
    from companest.orchestration.router import SmartRouter

    barrier = threading.Barrier(2)
    registry = SimpleNamespace(list_teams=lambda: ["alpha/research", "beta/research"], list_meta_teams=lambda: [])
    def soul(team_id):
        barrier.wait(timeout=2)
        return f"# {team_id}\nPrivate research for {team_id}"
    router = SmartRouter(registry, SimpleNamespace(read_team_soul=soul))
    alpha, beta = await asyncio.gather(
        run_sync(router._build_system_prompt, "alpha"),
        run_sync(router._build_system_prompt, "beta"),
    )
    assert "alpha/research" in alpha and "beta/research" not in alpha
    assert "beta/research" in beta and "alpha/research" not in beta


@pytest.mark.asyncio
async def test_router_invalidation_during_read_does_not_repopulate_stale_cache():
    from companest.orchestration.router import SmartRouter

    slow = SlowIO()
    registry = SimpleNamespace(list_teams=lambda: ["research"], list_meta_teams=lambda: [])
    def soul(team_id):
        slow.block()
        return "# Research\nOld description"
    router = SmartRouter(registry, SimpleNamespace(read_team_soul=soul))
    task = asyncio.create_task(run_sync(router._build_system_prompt))
    try:
        await slow.wait_started()
        router.invalidate_cache()
    finally:
        slow.release.set()
        await task
    assert router._cached_prompt is None


@pytest.mark.asyncio
async def test_status_keeps_runtime_reads_on_loop_and_offloads_finance():
    from companest.orchestration.orchestrator import CompanestOrchestrator

    loop_thread = threading.get_ident()
    slow = SlowIO()
    def runtime_status():
        assert threading.get_ident() == loop_thread
        return {"running": True}
    def finance_status():
        assert threading.get_ident() != loop_thread
        slow.block()
        return {"spending": 0}
    orchestrator = CompanestOrchestrator.__new__(CompanestOrchestrator)
    orchestrator.team_registry = SimpleNamespace(get_fleet_status=runtime_status)
    orchestrator.scheduler = SimpleNamespace(get_status=runtime_status)
    orchestrator.cost_gate = SimpleNamespace(get_spending_summary=finance_status)
    task = asyncio.create_task(orchestrator.get_status_async())
    try:
        await slow.wait_started()
        assert not task.done()
    finally:
        slow.release.set()
        result = await task
    assert result == {
        "teams_initialized": True, "teams": {"running": True},
        "scheduler": {"running": True}, "finance": {"spending": 0},
    }
