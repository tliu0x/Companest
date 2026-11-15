import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from companest.companies import company as company_module
from companest.companies.company import CompanyConfig, CompanyRegistry
from companest.config import CompanestConfig
from companest.events import EventType
from companest.orchestration.orchestrator import CompanestOrchestrator


class BlockedIO:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def block(self):
        self.started.set()
        assert self.release.wait(4), "event loop could not release blocking I/O"

    async def wait(self):
        async def poll():
            while not self.started.is_set():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(poll(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "update", "bind", "global", "delete", "replace_teams"])
async def test_company_management_keeps_health_responsive(api_runtime, monkeypatch, action):
    _, orchestrator, server = api_runtime()
    registry = orchestrator.company_registry
    registry.save(CompanyConfig(id="acme", name="Original", ceo={"enabled": False}))
    await orchestrator.apply_company("acme")
    stale = orchestrator.memory.base_path / "companies/acme/teams/stale"
    stale.mkdir(parents=True)
    (stale / "team.md").write_text("# Team: stale\n")
    blocker = BlockedIO()
    if action in {"delete", "replace_teams"}:
        original = company_module.shutil.rmtree
        def remove(*args, **kwargs):
            blocker.block()
            return original(*args, **kwargs)
        monkeypatch.setattr(company_module.shutil, "rmtree", remove)
    else:
        original = company_module._atomic_write_text
        def write(*args, **kwargs):
            blocker.block()
            return original(*args, **kwargs)
        monkeypatch.setattr(company_module, "_atomic_write_text", write)
    requests = {
        "create": ("POST", "/api/companies", {"id": "beta", "name": "Beta", "ceo": {"enabled": False}}),
        "update": ("PATCH", "/api/companies/acme", {"name": "Updated"}),
        "bind": ("POST", "/api/companies/acme/bind", {"channel": "api", "chat_id": "demo"}),
        "global": ("PUT", "/api/bindings", [{"channel": "api", "team_id": "general"}]),
        "delete": ("DELETE", "/api/companies/acme", None),
        "replace_teams": ("PATCH", "/api/companies/acme", {"teams": []}),
    }
    method, path, payload = requests[action]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()), base_url="http://test") as client:
        task = asyncio.create_task(client.request(method, path, json=payload))
        try:
            await blocker.wait()
            health = await asyncio.wait_for(client.get("/health"), 0.5)
            assert health.status_code == 200
            listing = await asyncio.wait_for(client.get("/api/companies"), 0.5)
            assert listing.status_code == 200
            assert not task.done()
            if action == "update":
                assert registry.get("acme").name == "Original"
        finally:
            blocker.release.set()
            response = await task
        assert response.status_code == 200, response.text
    if action == "update":
        assert registry.get("acme").name == "Updated"
    if action == "delete":
        assert registry.get("acme") is None
    if action == "replace_teams":
        assert not stale.exists()


@pytest.mark.asyncio
async def test_duplicate_create_is_serialized(api_runtime, monkeypatch):
    _, _, server = api_runtime()
    blocker = BlockedIO()
    original = company_module._atomic_write_text
    def write(*args, **kwargs):
        blocker.block()
        return original(*args, **kwargs)
    monkeypatch.setattr(company_module, "_atomic_write_text", write)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app()), base_url="http://test") as client:
        payload = {"id": "acme", "name": "Acme", "ceo": {"enabled": False}}
        first = asyncio.create_task(client.post("/api/companies", json=payload))
        second = None
        try:
            await blocker.wait()
            second = asyncio.create_task(client.post("/api/companies", json=payload))
            await asyncio.sleep(0.02)
            assert not second.done()
        finally:
            blocker.release.set()
            responses = await asyncio.gather(first, *([second] if second else []))
        assert sorted(r.status_code for r in responses) == [200, 409]


@pytest.mark.asyncio
async def test_registry_cancel_finishes_publication_and_serializes_next_write(tmp_path, monkeypatch):
    registry = CompanyRegistry(str(tmp_path))
    blocker = BlockedIO()
    original = company_module._atomic_write_text
    def write(*args, **kwargs):
        blocker.block()
        return original(*args, **kwargs)
    monkeypatch.setattr(company_module, "_atomic_write_text", write)
    first = asyncio.create_task(registry.save_async(CompanyConfig(id="alpha", name="Alpha")))
    second = None
    try:
        await blocker.wait()
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(registry.save_async(CompanyConfig(id="beta", name="Beta")))
        await asyncio.sleep(0.02)
        assert not first.done() and not second.done()
        assert registry.list_companies() == []
    finally:
        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second:
            await second
    assert sorted(registry.list_companies()) == ["alpha", "beta"]
    disk = CompanyRegistry(str(tmp_path))
    disk.scan()
    assert sorted(disk.list_companies()) == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_failed_registry_write_does_not_publish(tmp_path, monkeypatch):
    registry = CompanyRegistry(str(tmp_path))
    registry.save(CompanyConfig(id="acme", name="Original"))
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(company_module, "_atomic_write_text", fail)
    with pytest.raises(OSError):
        await registry.save_async(CompanyConfig(id="acme", name="Changed"))
    assert registry.get("acme").name == "Original"


@pytest.mark.asyncio
async def test_sync_component_hooks_offloaded_and_registrations_stay_on_loop(api_runtime, monkeypatch):
    _, orch, _ = api_runtime()
    owner = threading.get_ident()
    init_blocker, teardown_blocker = BlockedIO(), BlockedIO()
    registrations = []
    for target, method in [(orch.tool_registry, "register_company_mcp"), (orch.events, "on"), (orch._smart_router, "add_binding")]:
        original = getattr(target, method)
        def checked(*args, _original=original, _method=method, **kwargs):
            assert threading.get_ident() == owner
            registrations.append(_method)
            return _original(*args, **kwargs)
        monkeypatch.setattr(target, method, checked)
    async def listener(event):
        pass
    class Component:
        company_id = "acme"
        config = CompanyConfig(id="acme", name="Acme", ceo={"enabled": False})
        def on_init(self, ctx):
            assert threading.get_ident() != owner
            self.ctx = ctx
            init_blocker.block()
            ctx.memory.write_shared("ready.json", {"ready": True})
            ctx.tool_registry.register_company_mcp("acme", "demo", {"command": "demo"})
            ctx.event_bus.on(EventType.TASK_COMPLETED, listener)
            ctx.add_binding("demo", "general", "default")
        def on_teardown(self):
            assert threading.get_ident() != owner
            teardown_blocker.block()
            self.ctx.event_bus.off(EventType.TASK_COMPLETED, listener)
    component = Component()
    orch.company_registry.register_component(component)
    init = asyncio.create_task(orch.apply_company("acme"))
    teardown = None
    try:
        await init_blocker.wait()
        init.cancel()
        await asyncio.sleep(0)
        teardown = asyncio.create_task(orch.teardown_company("acme"))
        await asyncio.sleep(0.02)
        assert not teardown_blocker.started.is_set()
        assert not init.done()
    finally:
        init_blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await init
    try:
        await teardown_blocker.wait()
        assert set(registrations) == {"register_company_mcp", "on", "add_binding"}
        assert component.ctx.memory.read_shared("ready.json") == {"ready": True}
    finally:
        teardown_blocker.release.set()
        if teardown:
            await teardown
    assert not orch._is_company_initialized("acme")
    assert listener not in orch.events._listeners[EventType.TASK_COMPLETED]


@pytest.mark.asyncio
async def test_async_component_hooks_are_awaited(api_runtime):
    _, orch, _ = api_runtime()
    calls = []
    loop = asyncio.get_running_loop()
    async def init(ctx):
        assert asyncio.get_running_loop() is loop
        await asyncio.sleep(0)
        calls.append("init")
    async def teardown():
        calls.append("teardown")
    component = SimpleNamespace(company_id="acme", config=CompanyConfig(id="acme", name="Acme", ceo={"enabled": False}), on_init=init, on_teardown=teardown)
    orch.company_registry.register_component(component)
    await orch.apply_company("acme")
    await orch.apply_company("acme")
    assert calls == ["init", "teardown", "init"]


@pytest.mark.asyncio
async def test_reload_publishes_complete_registry_after_slow_read(api_runtime, monkeypatch):
    _, orch, _ = api_runtime()
    registry = orch.company_registry
    registry.save(CompanyConfig(id="acme", name="Original", ceo={"enabled": False}))
    await orch.apply_company("acme")
    path = orch.memory.base_path / "companies/acme/company.yaml"
    path.write_text(path.read_text().replace("Original", "Changed"))
    blocker = BlockedIO()
    original = CompanyRegistry._load_config
    def load(self, *args):
        blocker.block()
        return original(self, *args)
    monkeypatch.setattr(CompanyRegistry, "_load_config", load)
    task = asyncio.create_task(orch._check_company_changes())
    try:
        await blocker.wait()
        assert registry.get("acme").name == "Original"
        assert registry.list_companies() == ["acme"]
    finally:
        blocker.release.set()
        await task
    assert registry.get("acme").name == "Changed"
    assert orch._is_company_initialized("acme")


@pytest.mark.asyncio
async def test_async_bootstrap_offloads_disk_scan(tmp_path, monkeypatch):
    blocker = BlockedIO()
    original = CompanyRegistry.scan
    def scan(self):
        blocker.block()
        return original(self)
    monkeypatch.setattr(CompanyRegistry, "scan", scan)
    orch = CompanestOrchestrator(CompanestConfig(debug=True))
    task = asyncio.create_task(orch.init_teams_async(str(tmp_path)))
    try:
        await blocker.wait()
        assert not task.done()
    finally:
        blocker.release.set()
        await task
    assert orch.company_registry.list_companies() == []
