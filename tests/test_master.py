"""Tests for the MasterConnection."""

import asyncio
import pytest
from unittest.mock import ANY, AsyncMock, MagicMock

from companest.master import MasterConnection
from companest.config import MasterConfig
from companest.router import RoutingDecision, TeamAssignment


@pytest.fixture
def master_config():
    return MasterConfig(
        enabled=True,
        host="127.0.0.1",
        port=18789,
        max_concurrent_tasks=2,
        task_timeout=10,
    )


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.get_status.return_value = {"status": "idle", "teams_initialized": True}

    # Mock run_team()
    orch.run_team = AsyncMock(return_value="Team result")

    # Mock run_auto()  returns (result, RoutingDecision)
    auto_decision = RoutingDecision(
        teams=[TeamAssignment(team_id="engineering", instruction="Fix the Python bug")],
        strategy="single",
        reasoning="Code task",
        confidence=0.85,
    )
    orch.run_auto = AsyncMock(return_value=("Team result", auto_decision))

    # Mock team_registry for auto-routing
    orch.team_registry = MagicMock()
    orch.team_registry.list_teams.return_value = [
        "engineering", "stock", "finance"
    ]

    return orch


@pytest.fixture
def master_conn(master_config, mock_orchestrator):
    return MasterConnection(master_config, mock_orchestrator)


# ---- Tests ----

class TestPing:
    @pytest.mark.asyncio
    async def test_ping_responds_with_pong(self, master_conn):
        """Ping should return {pong: true}."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request("req-1", "ping", {})

        assert len(sent) == 1
        args = sent[0][0]
        assert args[0] == "req-1"  # request_id
        assert args[1] is True     # ok
        assert args[2] == {"pong": True}  # payload


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_returns_orchestrator_and_master(self, master_conn):
        """Status should include orchestrator and master info."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request("req-2", "status", {})

        assert len(sent) == 1
        payload = sent[0][0][2]
        assert "orchestrator" in payload
        assert "master" in payload
        assert payload["master"]["max_concurrent_tasks"] == 2


class TestTaskTeam:
    @pytest.mark.asyncio
    async def test_task_team_calls_run_team(self, master_conn, mock_orchestrator):
        """task.team should call orchestrator.run_team() and return result."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request(
            "req-3", "task.team", {"task": "Analyze TSLA", "team_id": "stock"}
        )

        mock_orchestrator.run_team.assert_awaited_once_with(
            "Analyze TSLA", "stock", mode="default",
            on_progress=ANY,
            user_context={"user_id": "", "chat_id": "", "channel": "telegram"},
            priority="normal",
        )

        assert len(sent) == 1
        args = sent[0][0]
        assert args[1] is True  # ok
        assert args[2]["result"] == "Team result"
        assert args[2]["team_id"] == "stock"

    @pytest.mark.asyncio
    async def test_task_team_missing_params_returns_error(self, master_conn):
        """task.team without task or team_id should return error."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request("req-4", "task.team", {"task": "hello"})

        assert len(sent) == 1
        args = sent[0][0]
        assert args[1] is False  # ok=False
        assert args[3]["code"] == "invalid_params"


class TestTaskAuto:
    @pytest.mark.asyncio
    async def test_task_auto_routes_and_executes(self, master_conn, mock_orchestrator):
        """task.auto should auto-route via run_auto() and return result."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request(
            "req-5", "task.auto", {"task": "Fix the Python bug"}
        )

        mock_orchestrator.run_auto.assert_awaited_once()

        assert len(sent) == 1
        args = sent[0][0]
        assert args[1] is True
        assert args[2]["result"] == "Team result"
        assert "team_ids" in args[2]
        assert args[2]["team_ids"] == ["engineering"]
        assert "confidence" in args[2]
        assert "strategy" in args[2]

    @pytest.mark.asyncio
    async def test_task_execute_redirects_to_auto(self, master_conn, mock_orchestrator):
        """task.execute (deprecated) should redirect to task.auto."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request(
            "req-6", "task.execute", {"task": "Write hello world"}
        )

        # Should still work via auto-routing
        mock_orchestrator.run_auto.assert_awaited_once()
        assert len(sent) == 1
        assert sent[0][0][1] is True  # ok

    @pytest.mark.asyncio
    async def test_task_auto_missing_task_returns_error(self, master_conn):
        """task.auto without 'task' param should return error."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request("req-7", "task.auto", {})

        assert len(sent) == 1
        args = sent[0][0]
        assert args[1] is False
        assert args[3]["code"] == "invalid_params"


class TestUnknownMethod:
    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, master_conn):
        """Unknown methods should return an error response."""
        sent = []
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock(side_effect=lambda *a, **kw: sent.append((a, kw)))

        await master_conn._handle_inbound_request("req-9", "foo.bar", {})

        assert len(sent) == 1
        args = sent[0][0]
        assert args[1] is False
        assert args[3]["code"] == "unknown_method"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_tasks(self, master_config):
        """Max concurrent tasks should be enforced by semaphore."""
        master_config.max_concurrent_tasks = 1

        # Orchestrator that takes 0.3s per task
        running = []
        peak = [0]

        async def slow_run_team(task, team_id, **kwargs):
            running.append(1)
            peak[0] = max(peak[0], len(running))
            await asyncio.sleep(0.3)
            running.pop()
            return "done"

        mock_orch = MagicMock()
        mock_orch.run_team = slow_run_team
        mock_orch.get_status.return_value = {}

        conn = MasterConnection(master_config, mock_orch)
        conn._client = MagicMock()
        conn._client.is_connected = True
        conn._client.send_response = AsyncMock()

        # Fire 3 tasks concurrently via task.team
        await asyncio.gather(
            conn._handle_inbound_request("r1", "task.team", {"task": "a", "team_id": "eng"}),
            conn._handle_inbound_request("r2", "task.team", {"task": "b", "team_id": "eng"}),
            conn._handle_inbound_request("r3", "task.team", {"task": "c", "team_id": "eng"}),
        )

        # With semaphore=1, peak concurrency should be 1
        assert peak[0] == 1


class TestOnInboundRequest:
    def test_spawns_async_task(self, master_conn):
        """_on_inbound_request should create an asyncio.Task."""
        master_conn._client = MagicMock()
        master_conn._client.is_connected = True
        master_conn._client.send_response = AsyncMock()

        loop = asyncio.new_event_loop()
        try:
            # Run inside event loop so create_task works
            async def _test():
                master_conn._on_inbound_request({
                    "type": "req", "id": "t1", "method": "ping", "params": {}
                })
                assert "t1" in master_conn._active_tasks
                # Let the task finish
                await master_conn._active_tasks["t1"]

            loop.run_until_complete(_test())
        finally:
            loop.close()

    def test_missing_id_is_ignored(self, master_conn):
        """Frames without id should be silently ignored."""
        master_conn._on_inbound_request({"type": "req", "method": "ping"})
        assert len(master_conn._active_tasks) == 0


class TestSecretRedaction:
    """Verify _sanitize_output redacts all supported secret types."""

    def test_redacts_anthropic_key(self):
        text = "Key is sk-ant-api03-abcdefghijklmnop"
        assert "[REDACTED]" in MasterConnection._sanitize_output(text)
        assert "sk-ant-api" not in MasterConnection._sanitize_output(text)

    def test_redacts_openai_project_key(self):
        text = "Key is sk-proj-abcdefghij1234567890"
        assert "[REDACTED]" in MasterConnection._sanitize_output(text)

    def test_redacts_generic_openai_key(self):
        text = "Key is sk-abcdefghijklmnopqrstuvwx"
        assert "[REDACTED]" in MasterConnection._sanitize_output(text)

    def test_redacts_aws_access_key(self):
        text = "Key is AKIAIOSFODNN7EXAMPLE"
        assert "[REDACTED]" in MasterConnection._sanitize_output(text)

    def test_redacts_deepseek_api_key(self):
        text = 'DEEPSEEK_API_KEY="dk-abcdefghij12345678"'
        result = MasterConnection._sanitize_output(text)
        assert "dk-abcdefghij12345678" not in result
        assert "[REDACTED]" in result

    def test_redacts_moonshot_api_key(self):
        text = "MOONSHOT_API_KEY=ms-key-abcdefghij"
        result = MasterConnection._sanitize_output(text)
        assert "ms-key-abcdefghij" not in result
        assert "[REDACTED]" in result

    def test_redacts_litellm_master_key(self):
        text = "LITELLM_MASTER_KEY: 'sk-litellm-master-12345678'"
        result = MasterConnection._sanitize_output(text)
        assert "sk-litellm-master-12345678" not in result
        assert "[REDACTED]" in result

    def test_redacts_companest_api_token(self):
        text = "COMPANEST_API_TOKEN=my-secret-token-value"
        result = MasterConnection._sanitize_output(text)
        assert "my-secret-token-value" not in result
        assert "[REDACTED]" in result

    def test_redacts_brave_api_key(self):
        text = "BRAVE_API_KEY=BSAbcdefghij12345678"
        result = MasterConnection._sanitize_output(text)
        assert "BSAbcdefghij12345678" not in result
        assert "[REDACTED]" in result

    def test_preserves_normal_text(self):
        text = "The deployment was successful. All 5 teams are running."
        assert MasterConnection._sanitize_output(text) == text

    def test_redacts_multiple_secrets_in_same_text(self):
        text = "Keys: sk-ant-api03-abcdefghijklmnop and DEEPSEEK_API_KEY=dk-abcdefghij12345678"
        result = MasterConnection._sanitize_output(text)
        assert "sk-ant-api" not in result
        assert "dk-abcdefghij12345678" not in result
        assert result.count("[REDACTED]") >= 2
