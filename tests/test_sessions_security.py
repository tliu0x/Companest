import asyncio
import json

from companest.memory import MemoryManager
from companest.tools import create_sessions_tool_defs


def _write_team(base, team_id: str) -> None:
    team_dir = base / "teams" / team_id / "memory"
    team_dir.mkdir(parents=True, exist_ok=True)


def _get_handler(defs, name: str):
    for tool in defs:
        if tool.name == name:
            return tool.handler
    raise AssertionError(f"Missing tool: {name}")


def test_sessions_without_company_context_cannot_touch_private_team(tmp_path):
    _write_team(tmp_path, "general")
    _write_team(tmp_path, "other-team/private")
    memory = MemoryManager(str(tmp_path))
    registry = type("Registry", (), {
        "list_teams": lambda self: ["general", "other-team/private"],
        "list_meta_teams": lambda self: [],
    })()
    defs = create_sessions_tool_defs(
        memory,
        team_id="general",
        pi_id="assistant",
        team_registry=registry,
    )

    send = _get_handler(defs, "sessions_send")
    history = _get_handler(defs, "sessions_history")
    listed = _get_handler(defs, "sessions_list")

    send_result = asyncio.run(send({"target_team": "other-team/private", "message": "hi"}))
    history_result = asyncio.run(history({"team_id": "other-team/private"}))
    list_result = json.loads(asyncio.run(listed({})))

    assert "Access denied" in send_result
    assert "Access denied" in history_result
    assert "other-team/private" not in list_result
    assert "general" in list_result


def test_sessions_respect_shared_team_whitelist_for_company(tmp_path):
    _write_team(tmp_path, "general")
    _write_team(tmp_path, "finance")
    _write_team(tmp_path, "acme-team/private")
    memory = MemoryManager(str(tmp_path))
    registry = type("Registry", (), {
        "list_teams": lambda self: ["general", "finance", "acme-team/private"],
        "list_meta_teams": lambda self: [],
    })()
    defs = create_sessions_tool_defs(
        memory,
        team_id="acme-team/private",
        pi_id="analyst",
        team_registry=registry,
        company_id="acme-team",
        shared_teams=[],
    )

    send = _get_handler(defs, "sessions_send")
    listed = _get_handler(defs, "sessions_list")

    send_result = asyncio.run(send({"target_team": "general", "message": "hello"}))
    list_result = json.loads(asyncio.run(listed({})))

    assert "Access denied" in send_result
    assert "general" not in list_result
    assert "finance" not in list_result
