import logging

import pytest

from companest.config import CompanestConfig
from companest.memory import FileBackend, MemoryManager, MemorySearchService, QdrantBackend
from companest.orchestrator import CompanestOrchestrator
from companest.pi import Pi, PiConfig


def _make_memory(tmp_path):
    (tmp_path / "teams" / "alpha" / "memory").mkdir(parents=True, exist_ok=True)
    return MemoryManager(str(tmp_path))


def test_orchestrator_falls_back_to_file_backend_for_unknown_backend(tmp_path, caplog):
    (tmp_path / "teams").mkdir()
    orchestrator = CompanestOrchestrator(CompanestConfig(memory_backend="viking"))

    with caplog.at_level(logging.WARNING):
        orchestrator.init_teams(str(tmp_path))

    assert isinstance(orchestrator.memory_backend, FileBackend)
    assert "Unknown memory backend" in caplog.text


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_semantic_modes_downgrade_to_exact_for_file_backend(tmp_path, mode):
    memory = _make_memory(tmp_path)
    memory.write_team_memory("alpha", "roadmap.json", {"milestone": "launch"})

    service = MemorySearchService(FileBackend(memory))
    results = service.search("alpha", "roadmap", mode=mode, explain=True)

    assert [result["key"] for result in results] == ["roadmap.json"]
    assert all(result["_mode"] == "exact" for result in results)


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


async def _run_pi_with_roadmap_memory(tmp_path, monkeypatch, task, **run_kwargs):
    """Run a Pi whose team memory holds a launch roadmap; return (result, captured _run_single args)."""
    team_dir = tmp_path / "teams" / "alpha"
    (team_dir / "memory").mkdir(parents=True)
    (team_dir / "pis" / "lead").mkdir(parents=True)
    (team_dir / "pis" / "lead" / "soul.md").write_text("alpha lead soul", encoding="utf-8")

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
        captured.update(task=task, system=system)
        return "ok"

    monkeypatch.setattr(pi, "_run_single", fake_run_single)
    result = await pi.run(task, timeout=1, **run_kwargs)
    return result, captured


@pytest.mark.asyncio
async def test_pi_run_appends_relevant_memory_section(tmp_path, monkeypatch):
    result, captured = await _run_pi_with_roadmap_memory(
        tmp_path, monkeypatch, "Review the launch roadmap",
    )

    assert result == "ok"
    assert captured["task"] == "Review the launch roadmap"
    assert "## Relevant Memory" in captured["system"]
    assert "launch-roadmap.json" in captured["system"]
    assert "Launch roadmap for the release." in captured["system"]
    assert "score=" not in captured["system"]


@pytest.mark.asyncio
async def test_pi_run_prefers_memory_task_hint(tmp_path, monkeypatch):
    _, captured = await _run_pi_with_roadmap_memory(
        tmp_path, monkeypatch,
        "## Independent Perspectives\nA long synthesis prompt",
        user_context={"memory_task_hint": "Review the launch roadmap"},
    )

    assert "launch-roadmap.json" in captured["system"]
    assert "Launch roadmap for the release." in captured["system"]


def test_qdrant_backend_file_delegation(tmp_path):
    backend = QdrantBackend(manager=_make_memory(tmp_path), in_memory=True)

    backend.write("alpha", "notes.json", {"topic": "memory research"})
    assert backend.read("alpha", "notes.json") == {"topic": "memory research"}
    assert "notes.json" in backend.list_keys("alpha")


def test_qdrant_backend_search_service_resolves_auto_mode(tmp_path):
    service = MemorySearchService(QdrantBackend(manager=_make_memory(tmp_path), in_memory=True))

    assert service._choose_mode("auto", "roadmap") == "semantic"
    assert service._choose_mode("auto", "what is the project roadmap status") == "hybrid"


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
