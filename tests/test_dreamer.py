"""Dreamer + Memory Index Tests"""

import copy
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from companest.memory import MemoryManager, INDEX_FILENAME, Dreamer


@pytest.fixture
def mem_dir(tmp_path):
    base = tmp_path / ".companest"
    base.mkdir()
    (base / "soul.md").write_text("master soul")
    (base / "user.md").write_text("user context")

    for team, pi in (("alpha", "lead"), ("beta", "worker")):
        team_dir = base / "teams" / team
        (team_dir / "pis" / pi).mkdir(parents=True)
        (team_dir / "memory").mkdir(parents=True)
        (team_dir / "team.md").write_text(f"# Team: {team}\n- role: general\n- lead_pi: {pi}")
        (team_dir / "soul.md").write_text(f"{team} team soul")
        (team_dir / "pis" / pi / "soul.md").write_text(f"{team} {pi} soul")

    return base


@pytest.fixture
def mm(mem_dir):
    return MemoryManager(str(mem_dir))


@pytest.fixture
def dreamer(mm):
    return Dreamer(
        memory=mm,
        proxy_config=None,
        short_tier_max_age_hours=24,
        min_importance_to_keep=0.3,
        max_access_for_gc=2,
    )


def _llm(dreamer, response):
    return patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value=response)


def _write_aged(mm, key, importance, access_count):
    """Write an alpha entry last updated 48h ago (past the 24h short-tier max age)."""
    mm.write_team_memory("alpha", key, {"data": True})
    index = mm.get_memory_index("alpha")
    index[key]["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    index[key]["importance"] = importance
    index[key]["access_count"] = access_count
    mm._write_index("alpha", index)


@pytest.mark.asyncio
async def test_dream_log_persists_summaries_and_errors(dreamer, mm, monkeypatch):
    async def run_team(team_id, dry_run=False):
        if team_id == "beta":
            raise RuntimeError("scoring unavailable")
        return {"scored": 2, "compacted": 1, "gc_deleted": 1, "gc_keys": ["private.json"]}

    monkeypatch.setattr(dreamer, "run_team_dream", run_team)
    result = await dreamer.run_all_dreams()
    path = mm.base_path / "dream_log.json"
    history = json.loads(path.read_text())
    assert history[0]["kind"] == "nightly"
    assert datetime.fromisoformat(history[0]["timestamp"]).tzinfo is not None
    assert history[0]["stats"] == {
        "alpha": {"scored": 2, "compacted": 1, "gc_deleted": 1},
        "beta": {"error": "scoring unavailable"},
    }
    assert result["alpha"]["gc_keys"] == ["private.json"]
    before = path.read_bytes()
    await dreamer.run_all_dreams(dry_run=True)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_deep_dream_log_caps_history_and_survives_restart(dreamer, mm):
    path = mm.base_path / "dream_log.json"
    path.write_text(json.dumps([{"sequence": n} for n in range(100)]))
    await dreamer.run_deep_consolidation()
    restarted = Dreamer(MemoryManager(str(mm.base_path)))
    await restarted.run_all_dreams()
    history = json.loads(path.read_text())
    assert len(history) == 100
    assert history[0] == {"sequence": 2}
    assert [row["kind"] for row in history[-2:]] == ["deep", "nightly"]
    assert history[-2]["stats"]["alpha"]["skipped"] is True


@pytest.mark.asyncio
async def test_dream_log_failure_preserves_previous_log(dreamer, mm, monkeypatch, caplog):
    path = mm.base_path / "dream_log.json"
    path.write_text("[]")
    write_json = mm._write_json

    def fail_write(destination, data):
        if destination == path:
            raise OSError("disk full")
        write_json(destination, data)

    monkeypatch.setattr(mm, "_write_json", fail_write)
    result = await dreamer.run_all_dreams()
    assert result["alpha"]["skipped"] is True
    assert path.read_text() == "[]"
    assert "Failed to write dream log" in caplog.text


class TestIndexCRUD:

    def test_rebuild_index_from_files(self, mm, mem_dir):
        """When .index.json doesn't exist, _read_index rebuilds from files."""
        # Write files directly (not through MemoryManager to avoid auto-index)
        (mem_dir / "teams" / "alpha" / "memory" / "notes.json").write_text(
            json.dumps({"note": "hello"})
        )
        (mem_dir / "teams" / "alpha" / "memory" / "data.json").write_text(
            json.dumps([1, 2, 3])
        )
        mm.clear_cache()

        index = mm._read_index("alpha")
        assert "notes.json" in index
        assert "data.json" in index
        assert INDEX_FILENAME not in index
        meta = index["notes.json"]
        assert "created_at" in meta
        assert "updated_at" in meta
        assert meta["tier"] == "short"
        assert meta["importance"] == 0.0
        assert meta["access_count"] == 0


    def test_read_increments_access_count(self, mm):
        mm.write_team_memory("alpha", "counter.json", {"x": 1})
        initial_count = mm.get_memory_index("alpha")["counter.json"]["access_count"]

        mm.read_team_memory("alpha", "counter.json")
        assert mm.get_memory_index("alpha")["counter.json"]["access_count"] == initial_count + 1

        mm.read_team_memory("alpha", "counter.json")
        assert mm.get_memory_index("alpha")["counter.json"]["access_count"] == initial_count + 2

    def test_delete_removes_from_index(self, mm):
        mm.write_team_memory("alpha", "temp.json", {"tmp": True})
        assert "temp.json" in mm.get_memory_index("alpha")

        mm.delete_team_memory("alpha", "temp.json")
        assert "temp.json" not in mm.get_memory_index("alpha")
        assert not (mm.team_path("alpha") / "memory" / "temp.json").exists()


    def test_update_entry_meta_invalid_field(self, mm):
        mm.write_team_memory("alpha", "entry.json", {"data": 1})
        with pytest.raises(Exception, match="Cannot update"):
            mm.update_entry_meta("alpha", "entry.json", created_at="bad")

    def test_update_entry_meta_missing_key(self, mm):
        with pytest.raises(Exception, match="Key not in index"):
            mm.update_entry_meta("alpha", "nonexistent.json", importance=0.5)

    def test_listing_and_stats_exclude_index_file(self, mm):
        mm.write_team_memory("alpha", "visible.json", {"v": 1})

        keys = mm.list_team_memory("alpha")
        assert "visible.json" in keys
        assert INDEX_FILENAME not in keys

        stat_keys = mm.get_all_memory_stats().get("alpha", {}).get("keys", [])
        assert "visible.json" in stat_keys
        assert INDEX_FILENAME not in stat_keys


class TestDreamingScoring:

    @pytest.mark.asyncio
    async def test_score_importance(self, dreamer, mm):
        mm.write_team_memory("alpha", "notes.json", {"note": "important insight"})
        mm.write_team_memory("alpha", "trash.json", {"junk": "data"})

        mock_response = json.dumps({
            "notes.json": {"importance": 0.9, "summary": "Critical insight about user"},
            "trash.json": {"importance": 0.1, "summary": "Low-value junk data"},
        })

        with _llm(dreamer, mock_response):
            scores = await dreamer._score_importance("alpha", {
                "notes.json": {"note": "important insight"},
                "trash.json": {"junk": "data"},
            })

        assert scores["notes.json"] == 0.9
        assert scores["trash.json"] == 0.1

        meta = mm.get_entry_meta("alpha", "notes.json")
        assert meta["importance"] == 0.9
        assert meta["summary"] == "Critical insight about user"

    @pytest.mark.asyncio
    async def test_score_importance_bad_json(self, dreamer, mm):
        mm.write_team_memory("alpha", "notes.json", {"note": "test"})

        with _llm(dreamer, "not json"):
            scores = await dreamer._score_importance("alpha", {
                "notes.json": {"note": "test"},
            })

        assert scores == {}


class TestDreamingCompaction:

    @pytest.mark.asyncio
    async def test_compact_entries(self, dreamer, mm):
        """Compaction merges entries into a long-tier consolidated entry and archives originals."""
        mm.write_team_memory("alpha", "old1.json", {"fact": "A"})
        mm.write_team_memory("alpha", "old2.json", {"fact": "B"})

        mock_response = json.dumps({
            "content": {"facts": ["A", "B"]},
            "summary": "Consolidated facts A and B",
            "tags": ["facts"],
        })

        with _llm(dreamer, mock_response):
            result = await dreamer._compact_entries("alpha", {
                "old1.json": {"fact": "A"},
                "old2.json": {"fact": "B"},
            })

        assert len(result) == 1
        assert mm.get_entry_meta("alpha", "old1.json") is None
        assert mm.get_entry_meta("alpha", "old2.json") is None

        archive_dir = mm.team_path("alpha") / "archive"
        assert (archive_dir / "old1.json").exists()
        assert (archive_dir / "old2.json").exists()

        archive_index = mm._read_archive_index("alpha")
        assert "old1.json" in archive_index
        assert "old2.json" in archive_index
        assert "archived_at" in archive_index["old1.json"]

        index = mm.get_memory_index("alpha")
        consolidated_keys = [k for k in index if k.startswith("consolidated-")]
        assert len(consolidated_keys) == 1
        meta = index[consolidated_keys[0]]
        assert meta["tier"] == "long"
        assert meta["summary"] == "Consolidated facts A and B"

    @pytest.mark.asyncio
    async def test_compact_bad_json(self, dreamer, mm):
        mm.write_team_memory("alpha", "old1.json", {"fact": "A"})

        with _llm(dreamer, "bad"):
            result = await dreamer._compact_entries("alpha", {
                "old1.json": {"fact": "A"},
            })

        assert result == []
        # Original must survive a failed compaction
        assert mm.get_entry_meta("alpha", "old1.json") is not None


class TestDreamingGC:

    def test_gc_expired(self, dreamer, mm):
        """GC archives low-importance expired entries."""
        _write_aged(mm, "old.json", importance=0.1, access_count=0)

        deleted = dreamer._gc_expired("alpha")
        assert "old.json" in deleted
        assert mm.get_entry_meta("alpha", "old.json") is None

        assert (mm.team_path("alpha") / "archive" / "old.json").exists()
        assert "old.json" in mm._read_archive_index("alpha")

    @pytest.mark.parametrize("importance,access_count", [
        (0.9, 0),   # important
        (0.1, 10),  # frequently accessed
    ])
    def test_gc_preserves_valuable_expired_entries(self, dreamer, mm, importance, access_count):
        _write_aged(mm, "kept.json", importance=importance, access_count=access_count)

        assert "kept.json" not in dreamer._gc_expired("alpha")
        assert mm.get_entry_meta("alpha", "kept.json") is not None

    def test_gc_preserves_recent(self, dreamer, mm):
        mm.write_team_memory("alpha", "recent.json", {"fresh": True})
        assert "recent.json" not in dreamer._gc_expired("alpha")


class TestMaintenanceFailure:

    @pytest.mark.asyncio
    async def test_scoring_failure_preserves_memory(self, dreamer, mm):
        """A failed model call cannot alter content or overwrite concurrent metadata."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})
        original_index = copy.deepcopy(mm.get_memory_index("alpha"))

        with patch.object(
            dreamer, "_score_importance",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM down"),
        ):
            with pytest.raises(RuntimeError, match="LLM down"):
                await dreamer.run_team_dream("alpha")

        assert mm.get_memory_index("alpha") == original_index

    @pytest.mark.asyncio
    async def test_dream_success_no_restore(self, dreamer, mm):
        """On success, index is NOT restored (changes persist)."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})

        mock_scores = json.dumps({
            "entry.json": {"importance": 0.5, "summary": "Test entry"},
        })

        with _llm(dreamer, mock_scores):
            stats = await dreamer.run_team_dream("alpha")

        assert stats.get("scored", 0) > 0
        meta = mm.get_entry_meta("alpha", "entry.json")
        assert meta["importance"] == 0.5
        assert meta["summary"] == "Test entry"


class TestDreamerIntegration:


    @pytest.mark.asyncio
    async def test_run_all_dreams(self, dreamer, mm):
        mm.write_team_memory("alpha", "a.json", {"a": 1})
        mm.write_team_memory("beta", "b.json", {"b": 2})

        async def mock_llm(prompt):
            if "a.json" in prompt:
                return json.dumps({"a.json": {"importance": 0.5, "summary": "Alpha data"}})
            return json.dumps({"b.json": {"importance": 0.7, "summary": "Beta data"}})

        with patch.object(dreamer, "_call_llm", side_effect=mock_llm):
            stats = await dreamer.run_all_dreams()

        assert "alpha" in stats
        assert "beta" in stats


    def test_build_prompt_no_long_term_for_short_tier(self, mm):
        mm.write_team_memory("alpha", "short.json", {"temp": True})
        mm.update_entry_meta("alpha", "short.json", summary="A short-tier summary")

        assert "Long-term Memory" not in mm.build_system_prompt("alpha", "lead")

    @pytest.mark.asyncio
    async def test_run_deep_consolidation_skips_few(self, dreamer, mm):
        """Deep consolidation skips teams with < 3 long-tier entries."""
        mm.write_team_memory("alpha", "long1.json", {"a": 1})
        mm.update_entry_meta("alpha", "long1.json", tier="long")

        stats = await dreamer.run_deep_consolidation()
        assert stats["alpha"]["skipped"] is True

    @pytest.mark.asyncio
    async def test_run_deep_consolidation(self, dreamer, mm):
        """Deep consolidation merges 3+ long-tier entries and archives originals."""
        for i in range(3):
            key = f"long{i}.json"
            mm.write_team_memory("alpha", key, {"fact": f"fact_{i}"})
            mm.update_entry_meta("alpha", key, tier="long", summary=f"Fact {i}")

        mock_response = json.dumps({
            "content": {"all_facts": ["fact_0", "fact_1", "fact_2"]},
            "summary": "All facts merged",
            "tags": ["facts"],
        })

        with _llm(dreamer, mock_response):
            stats = await dreamer.run_deep_consolidation()

        assert stats["alpha"]["merged"] == 3
        assert stats["alpha"]["into"] == 1

        index = mm.get_memory_index("alpha")
        archive_index = mm._read_archive_index("alpha")
        for i in range(3):
            assert f"long{i}.json" not in index
            assert f"long{i}.json" in archive_index

        deep_keys = [k for k in index if k.startswith("deep-consolidated-")]
        assert len(deep_keys) == 1


class TestOverviewGeneration:

    @pytest.mark.asyncio
    async def test_generate_overview(self, dreamer, mm):
        """_generate_overview writes .overview.md from long-tier summaries."""
        mm.write_team_memory("alpha", "entry1.json", {"data": 1})
        mm.update_entry_meta("alpha", "entry1.json", tier="long", summary="Key insight about user preferences")
        mm.write_team_memory("alpha", "entry2.json", {"data": 2})
        mm.update_entry_meta("alpha", "entry2.json", tier="long", summary="Important pattern in trading")

        mock_overview = "The team has learned about user preferences and trading patterns."

        with _llm(dreamer, mock_overview):
            result = await dreamer._generate_overview("alpha")

        assert result == mock_overview
        assert (mm.team_path("alpha") / "memory" / ".overview.md").read_text() == mock_overview


    def test_read_overview_and_prompt_inclusion(self, mm):
        assert mm.read_overview("alpha") == ""

        overview_path = mm.team_path("alpha") / "memory" / ".overview.md"
        overview_path.write_text("Team overview: key facts and patterns.")
        assert mm.read_overview("alpha") == "Team overview: key facts and patterns."

        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Memory Overview" in prompt
        assert "Team overview: key facts and patterns." in prompt


class TestArchive:

    def test_archive_team_memory(self, mm):
        """archive_team_memory moves file to archive/ and updates indices."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})
        mm.update_entry_meta("alpha", "entry.json", importance=0.7, summary="Test entry")

        mm.archive_team_memory("alpha", "entry.json")

        assert mm.get_entry_meta("alpha", "entry.json") is None
        assert not (mm.team_path("alpha") / "memory" / "entry.json").exists()

        assert (mm.team_path("alpha") / "archive" / "entry.json").exists()
        archive_index = mm._read_archive_index("alpha")
        assert archive_index["entry.json"]["importance"] == 0.7
        assert "archived_at" in archive_index["entry.json"]

    def test_search_archive(self, mm):
        assert mm.search_archive("alpha", "anything") == []

        mm.write_team_memory("alpha", "a.json", {"data": 1})
        mm.update_entry_meta("alpha", "a.json", importance=0.8, summary="User trading preferences", tags=["trading"])
        mm.write_team_memory("alpha", "b.json", {"data": 2})
        mm.update_entry_meta("alpha", "b.json", importance=0.5, summary="Weather data from last week")
        mm.archive_team_memory("alpha", "a.json")
        mm.archive_team_memory("alpha", "b.json")

        assert [r["key"] for r in mm.search_archive("alpha", "trading")] == ["a.json"]
        assert mm.search_archive("alpha", "nonexistent") == []

    def test_search_archive_sorted_by_importance(self, mm):
        mm.write_team_memory("alpha", "low.json", {"data": 1})
        mm.update_entry_meta("alpha", "low.json", importance=0.3, summary="data analysis")
        mm.write_team_memory("alpha", "high.json", {"data": 2})
        mm.update_entry_meta("alpha", "high.json", importance=0.9, summary="data patterns")

        mm.archive_team_memory("alpha", "low.json")
        mm.archive_team_memory("alpha", "high.json")

        results = mm.search_archive("alpha", "data")
        assert [r["key"] for r in results] == ["high.json", "low.json"]


class TestTokenBudget:

    def test_long_term_details_are_loaded_on_demand(self, mm):
        for i in range(50):
            key = f"entry{i}.json"
            mm.write_team_memory("alpha", key, {"data": i})
            mm.update_entry_meta(
                "alpha", key,
                tier="long",
                importance=1.0 - i * 0.01,
                summary=f"Long summary for entry {i} with lots of detail " * 5,
            )

        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Long-term Memory" not in prompt
        assert "Long summary for entry" not in prompt
        assert "memory_search" in prompt
        assert len(prompt) < 1600

    def test_overview_is_bounded_and_invalidated_by_writes(self, mm):
        for i in range(20):
            key = f"entry{i}.json"
            mm.write_team_memory("alpha", key, {"data": i})
            mm.update_entry_meta(
                "alpha", key,
                tier="long",
                importance=0.9,
                summary=f"This is a detailed summary for entry number {i} with significant content " * 2,
            )

        (mm.team_path("alpha") / "memory" / ".overview.md").write_text("x" * 7500)
        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Memory Overview" in prompt
        assert "x" * 2400 in prompt
        assert "x" * 2401 not in prompt
        assert "Long-term Memory" not in prompt
        mm.write_team_memory("alpha", "changed.json", {"fact": "new"})
        assert "Memory Overview" not in mm.build_system_prompt("alpha", "lead")
