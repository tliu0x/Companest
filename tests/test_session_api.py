import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from companest.config import CompanestConfig
from companest.jobs.jobs import JobManager
from companest.server.server import CompanestAPIServer


@pytest.mark.asyncio
async def test_session_job_survives_client_disconnect_and_preserves_context(tmp_path, monkeypatch, api_runtime):
    monkeypatch.setenv("COMPANEST_API_TOKEN", "test-token")
    monkeypatch.delenv("COMPANEST_DEBUG", raising=False)
    release = asyncio.Event()
    full_result = "完整结果" * 1000

    from companest.companies.company import CompanyConfig
    from companest.agents.workspace import Workspace
    config, orchestrator, server = api_runtime(auth_token="test-token")
    orchestrator.company_registry.save(CompanyConfig(id="acme", name="Acme", shared_teams=["general"]))
    orchestrator.workspace_registry._workspaces["project"] = Workspace(
        "project", str(tmp_path), company_id="acme", storage_id="test-storage")
    manager = server.job_manager
    request = manager.client.request.side_effect
    async def response(ref, method, path, **kwargs):
        result = await request(ref, method, path, **kwargs)
        if release.is_set():
            data = result.json()
            data.update(status="completed", result=full_result, completed_at=data["started_at"])
            return httpx.Response(200, json=data)
        return result
    manager.client.request.side_effect = response
    app = server.create_app()
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-token"}
    await manager.start()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/jobs", json={"task": "research"})).status_code == 401
            response = await client.post("/api/jobs", headers=headers, json={
                "task": "research", "company_id": "acme", "submitted_by": "claude-code",
                "context": {"team_id": "general", "workspace_id": "project"},
            })
            assert response.status_code == 200
            job_id = response.json()["job_id"]
        release.set()
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
            async def poll():
                while True:
                    response = await client.get(f"/api/jobs/{job_id}")
                    data = response.json()
                    if data["status"] in {"completed", "failed", "cancelled"}:
                        return data
                    await asyncio.sleep(0.01)
            data = await asyncio.wait_for(poll(), 3)
            assert data["status"] == "completed"
            assert data["result"] == full_result
            assert data["company_id"] == "acme"
            assert data["submitted_by"] == "claude-code"
            assert (await client.get("/api/jobs", params={"company_id": "other"})).json()["jobs"] == []
        manager.client.create_actor.assert_awaited_once()
        submitted = next(call.kwargs["payload"] for call in manager.client.request.await_args_list if call.kwargs.get("payload"))
        assert submitted["context"] == {"company_id": "acme", "team_id": "general", "workspace_id": "project"}
    finally:
        release.set()
        await manager.stop()
