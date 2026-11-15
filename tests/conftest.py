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
    from companest.config import CompanestConfig
    from companest.jobs import JobManager
    from companest.orchestrator import CompanestOrchestrator
    from companest.server import CompanestAPIServer

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

        config = CompanestConfig(debug=True)
        if auth_token:
            config.api.auth_token = auth_token
        orchestrator = CompanestOrchestrator(config)
        orchestrator.init_teams(str(tmp_path))
        server = CompanestAPIServer(config, JobManager(orchestrator, data_dir=tmp_path), orchestrator)
        return config, orchestrator, server

    return build
