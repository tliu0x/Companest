import pytest


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
            "/api/teams/{team_id}",
            "/api/teams/{team_id}/run",
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
