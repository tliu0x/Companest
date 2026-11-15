"""Tests for the LangChain-based Pi runtime, driven by a scripted fake model."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from companest.exceptions import PiError
from companest.memory import MemoryManager
from companest.agents.pi import Pi, PiConfig
from companest.agents.tools import ToolContext, ToolDefinition, ToolProvider, ToolRegistry, _to_mcp_connection
from companest.companies.company import CompanyConfig


class ScriptedModel(BaseChatModel):
    """Chat model that replays a fixed list of responses and records its inputs."""

    responses: List[Any]
    seen_messages: List[Any] = []
    bound_tool_names: List[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names.extend(t.name for t in tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages.append(list(messages))
        step = len(self.seen_messages) - 1
        response = self.responses[min(step, len(self.responses) - 1)]
        if response.tool_calls:
            # A real provider issues a fresh id for every tool call.
            calls = [{**call, "id": f"call_{step}_{i}"} for i, call in enumerate(response.tool_calls)]
            response = response.model_copy(update={"tool_calls": calls})
        return ChatResult(generations=[ChatGeneration(message=response)])


def _tool_call(name, args, call_id="call_1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def _answer(text):
    return AIMessage(content=text, usage_metadata={"input_tokens": 20, "output_tokens": 7, "total_tokens": 27})


@pytest.fixture
def memory(tmp_path):
    (tmp_path / "teams" / "alpha" / "memory").mkdir(parents=True)
    (tmp_path / "teams" / "alpha" / "pis" / "lead").mkdir(parents=True)
    return MemoryManager(str(tmp_path))


def _make_pi(memory, tools=None, tools_deny=None, max_turns=5, registry=None):
    return Pi(
        PiConfig(id="lead", model="deepseek-chat", tools=tools or [], tools_deny=tools_deny or [], max_turns=max_turns),
        memory=memory,
        team_id="alpha",
        tool_registry=registry or ToolRegistry(),
    )


async def _run(pi, model, task="do the task"):
    with patch("companest.agents.pi.build_chat_model", return_value=model):
        return await pi.run(task)


@pytest.mark.asyncio
async def test_plain_answer_and_usage(memory):
    pi = _make_pi(memory)
    model = ScriptedModel(responses=[_answer("final answer")])

    assert await _run(pi, model) == "final answer"
    assert pi.last_usage == {"model": "deepseek-chat", "input_tokens": 20, "output_tokens": 7}


@pytest.mark.asyncio
async def test_tool_call_loop_writes_memory(memory):
    pi = _make_pi(memory, tools=["memory_write", "memory_read"])
    model = ScriptedModel(responses=[
        _tool_call("memory_write", {"key": "note.json", "value": json.dumps({"x": 1})}),
        _answer("saved"),
    ])

    assert await _run(pi, model) == "saved"
    assert memory.read_team_memory("alpha", "note.json") == {"x": 1}
    assert model.seen_messages[1][-1].type == "tool"
    assert pi.last_usage["input_tokens"] == 30 and pi.last_usage["output_tokens"] == 12


@pytest.mark.asyncio
async def test_only_configured_tools_are_offered(memory):
    pi = _make_pi(memory, tools=["memory_read"])
    model = ScriptedModel(responses=[_answer("ok")])
    await _run(pi, model)
    assert model.bound_tool_names == ["memory_read"]


@pytest.mark.asyncio
async def test_system_prompt_reaches_the_model(memory, tmp_path):
    (tmp_path / "teams" / "alpha" / "pis" / "lead" / "soul.md").write_text("You are the alpha lead.")
    memory.clear_cache()
    pi = _make_pi(memory)
    model = ScriptedModel(responses=[_answer("ok")])
    await _run(pi, model)
    first = model.seen_messages[0]
    assert first[0].type == "system" and "You are the alpha lead." in first[0].text
    assert first[-1].type == "human" and first[-1].text == "do the task"


@pytest.mark.asyncio
async def test_max_turns_exceeded_raises_pi_error(memory):
    pi = _make_pi(memory, tools=["memory_list"], max_turns=2)
    model = ScriptedModel(responses=[_tool_call("memory_list", {})])  # never stops calling tools

    with pytest.raises(PiError, match="max_turns=2"):
        await _run(pi, model)
    assert len(model.seen_messages) == 2


@pytest.mark.asyncio
async def test_answer_on_the_last_allowed_turn_succeeds(memory):
    pi = _make_pi(memory, tools=["memory_list"], max_turns=2)
    model = ScriptedModel(responses=[_tool_call("memory_list", {}), _answer("done")])
    assert await _run(pi, model) == "done"


@pytest.mark.asyncio
async def test_empty_response_raises_pi_error(memory):
    pi = _make_pi(memory)
    with pytest.raises(PiError, match="empty response"):
        await _run(pi, ScriptedModel(responses=[_answer("  ")]))


@pytest.mark.asyncio
async def test_model_failure_is_wrapped_in_pi_error(memory):
    pi = _make_pi(memory)
    with patch("companest.agents.pi.build_chat_model", side_effect=RuntimeError("boom")):
        with pytest.raises(PiError, match="boom"):
            await pi.run("task")


def _ctx(memory, tools=None, deny=None, extra=None):
    return ToolContext(
        memory=memory, team_id="alpha", pi_id="lead", tools_config=tools or [],
        tools_deny=deny if deny is not None else {"Bash"}, extra=extra or {},
    )


def _names(registry, ctx):
    return {d.name for d in registry.get_tool_defs(ctx)}


def test_default_all_excludes_denied_and_opt_in_tools(memory, tmp_path):
    names = _names(ToolRegistry(), _ctx(memory, extra={"workspace_path": str(tmp_path), "run_team_fn": object()}))
    assert {"Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "memory_read", "brave_search"} <= names
    assert "Bash" not in names
    # Orchestrator and git tools require explicit opt-in.
    assert not names & {"run_team", "run_auto", "git_status"}


def test_file_tools_require_a_workspace(memory):
    names = _names(ToolRegistry(), _ctx(memory, tools=["pi-core"]))
    assert names == set()


def test_coder_preset_with_bash_opt_in(memory, tmp_path):
    names = _names(ToolRegistry(), _ctx(memory, tools=["coder"], deny=set(), extra={"workspace_path": str(tmp_path)}))
    assert {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "git_status", "memory_read"} <= names
    assert "WebSearch" not in names


def test_ceo_preset_gets_orchestrator_tools(memory):
    names = _names(ToolRegistry(), _ctx(memory, tools=["ceo"], extra={"run_team_fn": object()}))
    assert names == {"run_team", "run_auto", "memory_read", "memory_write", "memory_list"}


def test_custom_provider_tools_are_exposed(memory):
    async def handler(args):
        return "placed"

    registry = ToolRegistry()
    registry.register(ToolProvider(
        name="kalshi",
        tool_names={"place_order"},
        factory=lambda ctx: [ToolDefinition("place_order", "Place an order", {"ticker": {"type": "string"}}, handler)],
    ))
    assert "place_order" in _names(registry, _ctx(memory, tools=["place_order"]))
    assert "place_order" in _names(registry, _ctx(memory))
    assert "place_order" not in _names(registry, _ctx(memory, tools=["memory_read"]))


@pytest.mark.asyncio
async def test_workspace_tools_are_confined_to_the_workspace(memory, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("outside")
    registry = ToolRegistry()
    defs = {d.name: d for d in registry.get_tool_defs(_ctx(memory, deny=set(), extra={"workspace_path": str(workspace)}))}

    assert "Wrote" in await defs["Write"].handler({"path": "src/a.py", "content": "x = 1\ny = 2\n"})
    assert await defs["Read"].handler({"path": "src/a.py"}) == "x = 1\ny = 2\n"
    assert await defs["Edit"].handler({"path": "src/a.py", "old_string": "x = 1", "new_string": "x = 3"}) == "Edited src/a.py"
    assert "found 0 matches" in await defs["Edit"].handler({"path": "src/a.py", "old_string": "nope", "new_string": "z"})
    assert json.loads(await defs["Glob"].handler({"pattern": "**/*.py"})) == ["src/a.py"]
    assert await defs["Grep"].handler({"pattern": r"y = \d"}) == "src/a.py:2: y = 2"
    assert (await defs["Bash"].handler({"command": "pwd"})).strip().endswith(str(workspace))

    assert "outside workspace" in await defs["Read"].handler({"path": "../secret.txt"})
    assert "outside workspace" in await defs["Write"].handler({"path": "../evil.txt", "content": "x"})
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.asyncio
async def test_web_fetch_refuses_non_public_addresses(memory):
    defs = {d.name: d for d in ToolRegistry().get_tool_defs(_ctx(memory))}
    for url in ("http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
        assert (await defs["WebFetch"].handler({"url": url})).startswith("Error")


def test_mcp_connection_conversion():
    assert _to_mcp_connection({"type": "stdio", "command": "npx", "args": ["-y", "srv"], "env": {"K": "v"}}) == {
        "transport": "stdio", "command": "npx", "args": ["-y", "srv"], "env": {"K": "v"},
    }
    assert _to_mcp_connection({"type": "http", "url": "https://mcp.example.com", "headers": {"A": "b"}}) == {
        "transport": "streamable_http", "url": "https://mcp.example.com", "headers": {"A": "b"},
    }
    assert _to_mcp_connection({"type": "sse", "url": "https://mcp.example.com/sse"})["transport"] == "sse"


@pytest.mark.asyncio
async def test_external_tools_honor_plain_and_legacy_deny_names(monkeypatch):
    class MCPClient:
        def __init__(self, configs):
            self.server = next(iter(configs))

        async def get_tools(self):
            return [
                SimpleNamespace(name=name, server=self.server)
                for name in ("write", "read", "inspect")
            ]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", MCPClient)
    registry = ToolRegistry()
    config = {"command": "unused"}
    registry.register_external_mcp("global", config)
    registry.register_company_mcp("alpha", "private", config)
    registry.register_company_mcp("beta", "private", config)

    tools = await registry.get_external_mcp_tools("alpha", {"inspect", "mcp__private__write"})

    assert {(tool.server, tool.name) for tool in tools} == {
        ("global", "read"), ("global", "write"), ("alpha/private", "read"),
    }


@pytest.mark.asyncio
async def test_shared_pi_uses_each_runs_company_and_workspace(api_runtime, monkeypatch, tmp_path):
    _, orch, _ = api_runtime()
    from companest.runtime.harness import ActorOrchestrator
    orch.company_registry.save(CompanyConfig(id="alpha", name="Alpha", shared_teams=["general"]))
    orch.company_registry.save(
        CompanyConfig(id="beta", name="Beta", shared_teams=["general", "other"]),
    )
    (tmp_path / "workspaces.json").write_text(json.dumps({"demo": {"path": str(tmp_path)}}))
    orch.workspace_registry.load()
    both_running = asyncio.Event()
    contexts = {}
    entered = 0

    async def run_agent(pi, task, system, model):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_running.set()
        await both_running.wait()
        ctx = pi._build_tool_context()
        contexts[task] = (
            ctx.company_id, ctx.extra["company_shared_teams"], ctx.extra.get("workspace_path"),
        )
        return "done"

    monkeypatch.setattr(Pi, "_run_agent", run_agent)
    await asyncio.wait_for(asyncio.gather(
        ActorOrchestrator.run_team(orch, "first", "general", skip_cost_check=True,
                      user_context={"company_id": "alpha", "workspace_id": "demo"}),
        ActorOrchestrator.run_team(orch, "second", "general", skip_cost_check=True,
                      user_context={"company_id": "beta"}),
    ), timeout=5)

    assert contexts == {
        "first": ("alpha", ["general"], str(tmp_path)),
        "second": ("beta", ["general", "other"], None),
    }
