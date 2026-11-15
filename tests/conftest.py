import asyncio

import pytest


@pytest.fixture(autouse=True)
def ensure_event_loop():
    """Provide a default event loop for sync tests using asyncio.get_event_loop()."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def api_runtime(tmp_path):
    """Factory for (config, orchestrator, server) on a minimal one-team runtime in tmp_path."""
    from companest.config import CompanestConfig, SubstrateConfig, ActorBinding
    from unittest.mock import AsyncMock
    from datetime import datetime, timezone
    import httpx
    from companest.jobs.jobs import JobManager
    from companest.orchestration.orchestrator import CompanestOrchestrator
    from companest.server.server import CompanestAPIServer

    def build(auth_token=None):
        general_pi = tmp_path / "teams" / "general" / "pis" / "assistant"
        general_pi.mkdir(parents=True)
        (tmp_path / "teams" / "general" / "team.md").write_text(
            "# Team: general\n"
            "- role: General-purpose assistant\n"
            "- lead_pi: assistant\n"
            "- enabled: true\n"
            "- mode: default\n\n"
            "#### Pi: assistant\n"
            "- model: claude-sonnet-4-5-20250929\n"
            "- tools: memory_read\n"
            "- max_turns: 5\n",
            encoding="utf-8",
        )
        (general_pi / "soul.md").write_text("You are a general assistant.", encoding="utf-8")
        (tmp_path / "soul.md").write_text("Master soul", encoding="utf-8")
        (tmp_path / "user.md").write_text("# User\n- Language: English", encoding="utf-8")

        (tmp_path / ".companest-storage-id").write_text("test-storage")
        config = CompanestConfig(debug=True, data_dir=str(tmp_path), storage_lock_dir=str(tmp_path),
            substrate=SubstrateConfig(storage_id="test-storage", scopes={
                key: ActorBinding(atespace="test", template="sample")
                for key in ("@shared", "prediction-market", "acme")
            }))
        if auth_token:
            config.api.auth_token = auth_token
        orchestrator = CompanestOrchestrator(config)
        orchestrator.init_teams(str(tmp_path))
        # API contract tests replace only the external Substrate transport.
        client = AsyncMock()
        resources, records = {}, {}
        async def create(ref, template):
            resources[ref.name] = {"metadata": {"name": ref.name, "atespace": ref.atespace, "uid": ref.name},
                                   "actorTemplate": {"name": template}}
            return resources[ref.name]
        async def get(ref):
            return resources[ref.name]
        async def request(ref, method, path, *, payload=None, port=8080):
            if path == "/readyz":
                return httpx.Response(200, json={"ready": True, "lock_protocol": "hardlink-v1"})
            if payload:
                records[ref.name] = {"job_id": payload["job_id"], "company_id": payload["company_id"],
                    "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": None, "result": None, "error": None}
            return httpx.Response(200, json=records[ref.name])
        client.create_actor.side_effect = create
        client.get_actor.side_effect = get
        client.request.side_effect = request
        server = CompanestAPIServer(config, JobManager(orchestrator, data_dir=tmp_path, client=client), orchestrator)
        return config, orchestrator, server

    return build
