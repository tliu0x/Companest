import logging

import pytest

from companest.config import CompanestConfig
from companest.memory import FileBackend, MemoryManager, MemorySearchService, QdrantBackend, VikingBackend
from companest.orchestrator import CompanestOrchestrator
from companest.pi import Pi, PiConfig
from companest.tools import create_memory_tool_defs


def _make_memory(tmp_path):
    (tmp_path / "teams" / "alpha" / "memory").mkdir(parents=True, exist_ok=True)
    return MemoryManager(str(tmp_path))


def test_viking_backend_does_not_claim_unimplemented_capabilities(caplog):
    with caplog.at_level(logging.WARNING):
        backend = VikingBackend()

    assert backend.supports_semantic_search is False
    assert backend.supports_native_compaction is False
    assert backend.supports_snapshot is False
    assert "not yet implemented" in caplog.text


def test_orchestrator_falls_back_to_file_backend_for_viking(tmp_path, caplog):
    (tmp_path / "teams").mkdir()
    config = CompanestConfig(memory_backend="viking")
    orchestrator = CompanestOrchestrator(config)

    with caplog.at_level(logging.WARNING):
        orchestrator.init_teams(str(tmp_path))

    assert isinstance(orchestrator.memory_backend, FileBackend)
    assert "deprecated" in caplog.text


def test_semantic_mode_downgrades_to_exact_for_file_backend(tmp_path):
    memory = _make_memory(tmp_path)
    memory.write_team_memory("alpha", "roadmap.json", {"milestone": "launch"})

    service = MemorySearchService(FileBackend(memory))
    results = service.search("alpha", "roadmap", mode="semantic", explain=True)

    assert [result["key"] for result in results] == ["roadmap.json"]
    assert all(result["_mode"] == "exact" for result in results)


def test_hybrid_mode_downgrades_to_exact_for_file_backend(tmp_path):
    memory = _make_memory(tmp_path)
    memory.write_team_memory("alpha", "roadmap.json", {"milestone": "launch"})

    service = MemorySearchService(FileBackend(memory))
    results = service.search("alpha", "roadmap", mode="hybrid", explain=True)

    assert [result["key"] for result in results] == ["roadmap.json"]
    assert all(result["_mode"] == "exact" for result in results)


def test_memory_search_tool_describes_semantic_fallback(tmp_path):
    memory = _make_memory(tmp_path)
    tool_defs = create_memory_tool_defs(memory, "alpha", "analyst")
    memory_search = next(tool for tool in tool_defs if tool.name == "memory_search")

    assert "fall back to exact search" in memory_search.description
    assert "downgrade to exact" in memory_search.parameters["mode"]["description"]


def test_retrieve_for_task_uses_keywords_and_content_fallback(tmp_path):
    memory = _make_memory(tmp_path)
    memory.write_team_memory("alpha", "launch-roadmap.json", {"milestone": "launch"})
    memory.update_entry_meta(
        "alpha",
        "launch-roadmap.json",
        summary="Launch roadmap and milestones for the release.",
        importance=1.0,
        tags=["planning"],
    )
    memory.write_team_memory(
        "alpha",
        "launch-notes.json",
        {"note": "launch checklist and rollout plan"},
    )

    service = MemorySearchService(FileBackend(memory))
    results = service.retrieve_for_task(
        "alpha",
        "Review the launch roadmap",
        limit=3,
        budget_chars=1000,
    )

    assert results[0]["key"] == "launch-roadmap.json"
    assert set(results[0]["matched_terms"]) >= {"launch", "roadmap"}
    notes = next(result for result in results if result["key"] == "launch-notes.json")
    assert "launch checklist" in notes["text"]


def test_retrieve_for_task_respects_budget(tmp_path):
    memory = _make_memory(tmp_path)
    memory.write_team_memory("alpha", "launch-roadmap.json", {"milestone": "launch"})
    memory.update_entry_meta(
        "alpha",
        "launch-roadmap.json",
        summary="Launch roadmap " * 30,  # ~450 chars
        importance=1.0,
        tags=["planning"],
    )
    memory.write_team_memory("alpha", "notes.json", {"note": "secondary context"})
    memory.update_entry_meta(
        "alpha",
        "notes.json",
        summary="Secondary launch notes.",
        importance=0.4,
    )

    service = MemorySearchService(FileBackend(memory))

    # Budget of 200: enough for one entry with truncated text, not both
    results = service.retrieve_for_task(
        "alpha",
        "launch roadmap",
        limit=5,
        budget_chars=200,
    )

    assert len(results) == 1
    assert results[0]["key"] == "launch-roadmap.json"
    assert results[0]["text"].endswith("...")


@pytest.mark.asyncio
async def test_pi_run_appends_relevant_memory_section(tmp_path, monkeypatch):
    team_dir = tmp_path / "teams" / "alpha"
    (team_dir / "memory").mkdir(parents=True)
    (team_dir / "pis" / "lead").mkdir(parents=True)
    (team_dir / "pis" / "lead" / "soul.md").write_text(
        "alpha lead soul",
        encoding="utf-8",
    )

    memory = MemoryManager(str(tmp_path))
    memory.write_team_memory("alpha", "launch-roadmap.json", {"milestone": "launch"})
    memory.update_entry_meta(
        "alpha",
        "launch-roadmap.json",
        summary="Launch roadmap for the release.",
        importance=0.9,
        tags=["launch"],
    )

    pi = Pi(PiConfig(id="lead"), memory, team_id="alpha")
    captured = {}

    async def fake_run_single(task, system, timeout):
        captured["task"] = task
        captured["system"] = system
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(pi, "_run_single", fake_run_single)

    result = await pi.run("Review the launch roadmap", timeout=1)

    assert result == "ok"
    assert captured["task"] == "Review the launch roadmap"
    assert "## Relevant Memory" in captured["system"]
    assert "launch-roadmap.json" in captured["system"]
    assert "Launch roadmap for the release." in captured["system"]
    assert "score=" not in captured["system"]


@pytest.mark.asyncio
async def test_pi_run_prefers_memory_task_hint(tmp_path, monkeypatch):
    team_dir = tmp_path / "teams" / "alpha"
    (team_dir / "memory").mkdir(parents=True)
    (team_dir / "pis" / "lead").mkdir(parents=True)
    (team_dir / "pis" / "lead" / "soul.md").write_text(
        "alpha lead soul",
        encoding="utf-8",
    )

    memory = MemoryManager(str(tmp_path))
    memory.write_team_memory("alpha", "launch-roadmap.json", {"milestone": "launch"})
    memory.update_entry_meta(
        "alpha",
        "launch-roadmap.json",
        summary="Launch roadmap for the release.",
        importance=0.9,
    )

    pi = Pi(PiConfig(id="lead"), memory, team_id="alpha")
    captured = {}

    async def fake_run_single(task, system, timeout):
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(pi, "_run_single", fake_run_single)

    await pi.run(
        "## Independent Perspectives\nA long synthesis prompt",
        timeout=1,
        user_context={"memory_task_hint": "Review the launch roadmap"},
    )

    assert "launch-roadmap.json" in captured["system"]
    assert "Launch roadmap for the release." in captured["system"]


def test_qdrant_backend_claims_semantic_search(tmp_path):
    memory = _make_memory(tmp_path)
    backend = QdrantBackend(manager=memory, in_memory=True)

    assert backend.supports_semantic_search is True
    assert backend.supports_snapshot is True
    assert backend.supports_native_compaction is False


def test_qdrant_backend_delegates_read_write_to_file(tmp_path):
    memory = _make_memory(tmp_path)
    backend = QdrantBackend(manager=memory, in_memory=True)

    backend.write("alpha", "notes.json", {"topic": "memory research"})
    result = backend.read("alpha", "notes.json")
    assert result == {"topic": "memory research"}

    keys = backend.list_keys("alpha")
    assert "notes.json" in keys


def test_qdrant_backend_search_service_uses_semantic_mode(tmp_path):
    """When QdrantBackend is used, MemorySearchService should resolve
    'auto' to 'semantic' or 'hybrid' instead of always 'exact'."""
    memory = _make_memory(tmp_path)
    backend = QdrantBackend(manager=memory, in_memory=True)
    service = MemorySearchService(backend)

    resolved = service._choose_mode("auto", "roadmap")
    assert resolved == "semantic"

    resolved = service._choose_mode("auto", "what is the project roadmap status")
    assert resolved == "hybrid"


def test_orchestrator_initializes_qdrant_backend(tmp_path):
    (tmp_path / "teams").mkdir()
    config = CompanestConfig(
        memory_backend="qdrant",
        memory_config={"in_memory": True},
    )
    orchestrator = CompanestOrchestrator(config)
    orchestrator.init_teams(str(tmp_path))

    assert isinstance(orchestrator.memory_backend, QdrantBackend)
    assert orchestrator.memory_backend.supports_semantic_search is True

