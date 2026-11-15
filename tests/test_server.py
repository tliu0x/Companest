import pytest
from fastapi.testclient import TestClient

from companest.config import CompanestConfig, MasterConfig
from companest.jobs.jobs import JobManager
from companest.server.server import CompanestAPIServer


class TestServerEndpoints:
    """API endpoints exist on the FastAPI app and auth is enforced at startup."""

    @staticmethod
    def _server(debug):
        pytest.importorskip("fastapi")

        from companest.config import CompanestConfig
        from companest.jobs.jobs import JobManager
        from companest.server.server import CompanestAPIServer

        jm = JobManager.__new__(JobManager)
        jm.orchestrator = None
        jm._data_dir = None
        return CompanestAPIServer(CompanestConfig(debug=debug), jm)

    def test_endpoints_registered(self):
        app = self._server(debug=True).create_app()  # debug: no auth token required

        routes = [r.path for r in app.routes]
        for path in (
            "/api/teams",
            "/api/teams/{team_id:path}",
            "/api/teams/{team_id:path}/run",
            "/api/finance/summary",
            "/api/finance/approve/{approval_id}",
            "/api/finance/report",
            "/api/finance/circuit-breaker/reset",
            "/api/scheduler/status",
            "/api/scheduler/{task_name}/trigger",
            "/api/v2/status",
        ):
            assert path in routes

    def test_server_refuses_start_without_auth_in_production(self):
        """Server must refuse to start without COMPANEST_API_TOKEN when debug=False."""
        with pytest.raises(RuntimeError, match="COMPANEST_API_TOKEN is required"):
            self._server(debug=False).create_app()


def test_unresolved_production_token_is_not_a_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPANEST_API_TOKEN", raising=False)
    monkeypatch.delenv("COMPANEST_DEBUG", raising=False)
    config = CompanestConfig.from_markdown("deploy/config.prod.md")
    server = CompanestAPIServer(config, JobManager(data_dir=tmp_path))

    with pytest.raises(RuntimeError, match="COMPANEST_API_TOKEN is required"):
        server.create_app()

    monkeypatch.setenv("COMPANEST_API_TOKEN", "test-token")
    server.config = CompanestConfig.from_markdown("deploy/config.prod.md")
    with TestClient(server.create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/fleet/status").status_code == 401
        assert client.get(
            "/api/fleet/status", headers={"Authorization": "Bearer test-token"},
        ).status_code == 200


def test_master_config_resolves_token_from_environment(monkeypatch):
    monkeypatch.delenv("COMPANEST_MASTER_TOKEN", raising=False)
    assert MasterConfig(auth_token="${COMPANEST_MASTER_TOKEN}").auth_token is None
    monkeypatch.setenv("COMPANEST_MASTER_TOKEN", "test-master-token")
    assert MasterConfig(auth_token="${COMPANEST_MASTER_TOKEN}").auth_token == "test-master-token"
