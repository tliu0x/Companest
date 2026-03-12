"""
Dreamer + Memory Index Tests

Tests for:
- Index CRUD (rebuild, write/read/delete updates, meta update)
- Dreaming (mock LLM: scoring, compaction, GC)
- CoW snapshot (restore on failure, no restore on success)
- Integration (run_team_dream, run_all_dreams, prompt long-term section)
"""

import copy
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from companest.memory import MemoryManager, INDEX_FILENAME, Dreamer


#  Fixtures 


@pytest.fixture
def mem_dir(tmp_path):
    """Create a minimal .companest directory structure for testing."""
    base = tmp_path / ".companest"
    base.mkdir()
    (base / "soul.md").write_text("master soul")
    (base / "user.md").write_text("user context")

    # Team: alpha
    alpha = base / "teams" / "alpha"
    (alpha / "pis" / "lead").mkdir(parents=True)
    (alpha / "memory").mkdir(parents=True)
    (alpha / "team.md").write_text("# Team: alpha\n- role: general\n- lead_pi: lead")
    (alpha / "soul.md").write_text("alpha team soul")
    (alpha / "pis" / "lead" / "soul.md").write_text("alpha lead soul")

    # Team: beta (for multi-team tests)
    beta = base / "teams" / "beta"
    (beta / "pis" / "worker").mkdir(parents=True)
    (beta / "memory").mkdir(parents=True)
    (beta / "team.md").write_text("# Team: beta\n- role: analysis")
    (beta / "soul.md").write_text("beta team soul")
    (beta / "pis" / "worker" / "soul.md").write_text("beta worker soul")

    # Team: research (enrichment source)
    research = base / "teams" / "research"
    (research / "pis" / "analyst").mkdir(parents=True)
    (research / "memory").mkdir(parents=True)
    (research / "team.md").write_text("# Team: research\n- role: research")
    (research / "soul.md").write_text("research team soul")
    (research / "pis" / "analyst" / "soul.md").write_text("research analyst soul")

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


#  Index CRUD Tests 


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
        # Clear any cached state
        mm.clear_cache()

        index = mm._read_index("alpha")
        assert "notes.json" in index
        assert "data.json" in index
        assert INDEX_FILENAME not in index
        # Verify metadata structure
        meta = index["notes.json"]
        assert "created_at" in meta
        assert "updated_at" in meta
        assert meta["tier"] == "short"
        assert meta["importance"] == 0.0
        assert meta["access_count"] == 0

    def test_write_updates_index(self, mm):
        """write_team_memory creates/updates index entry."""
        mm.write_team_memory("alpha", "test.json", {"key": "value"})
        index = mm.get_memory_index("alpha")
        assert "test.json" in index
        meta = index["test.json"]
        assert meta["tier"] == "short"
        assert meta["size_bytes"] > 0

    def test_read_increments_access_count(self, mm):
        """read_team_memory increments access_count."""
        mm.write_team_memory("alpha", "counter.json", {"x": 1})
        index = mm.get_memory_index("alpha")
        initial_count = index["counter.json"]["access_count"]

        mm.read_team_memory("alpha", "counter.json")
        index = mm.get_memory_index("alpha")
        assert index["counter.json"]["access_count"] == initial_count + 1

        mm.read_team_memory("alpha", "counter.json")
        index = mm.get_memory_index("alpha")
        assert index["counter.json"]["access_count"] == initial_count + 2

    def test_delete_removes_from_index(self, mm):
        """delete_team_memory removes file and index entry."""
        mm.write_team_memory("alpha", "temp.json", {"tmp": True})
        assert "temp.json" in mm.get_memory_index("alpha")

        mm.delete_team_memory("alpha", "temp.json")
        assert "temp.json" not in mm.get_memory_index("alpha")
        assert not (mm.team_path("alpha") / "memory" / "temp.json").exists()

    def test_update_entry_meta(self, mm):
        """update_entry_meta updates specific fields."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})

        mm.update_entry_meta(
            "alpha", "entry.json",
            importance=0.8,
            tier="long",
            summary="Important entry",
            tags=["critical"],
        )

        meta = mm.get_entry_meta("alpha", "entry.json")
        assert meta["importance"] == 0.8
        assert meta["tier"] == "long"
        assert meta["summary"] == "Important entry"
        assert meta["tags"] == ["critical"]

    def test_update_entry_meta_invalid_field(self, mm):
        """update_entry_meta rejects invalid field names."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})
        with pytest.raises(Exception, match="Cannot update"):
            mm.update_entry_meta("alpha", "entry.json", created_at="bad")

    def test_update_entry_meta_missing_key(self, mm):
        """update_entry_meta raises on missing key."""
        with pytest.raises(Exception, match="Key not in index"):
            mm.update_entry_meta("alpha", "nonexistent.json", importance=0.5)

    def test_get_entry_meta_none_for_missing(self, mm):
        """get_entry_meta returns None for unknown key."""
        assert mm.get_entry_meta("alpha", "nope.json") is None

    def test_list_team_memory_excludes_index(self, mm):
        """list_team_memory should not include .index.json."""
        mm.write_team_memory("alpha", "visible.json", {"v": 1})
        keys = mm.list_team_memory("alpha")
        assert "visible.json" in keys
        assert INDEX_FILENAME not in keys

    def test_append_updates_index(self, mm):
        """append_team_memory updates index entry."""
        mm.write_team_memory("alpha", "log.json", [])
        mm.append_team_memory("alpha", "log.json", {"entry": 1})
        index = mm.get_memory_index("alpha")
        assert "log.json" in index
        # Size should reflect the appended content
        assert index["log.json"]["size_bytes"] > 2  # more than "[]"


#  Dreaming Tests (mock LLM) 


class TestDreamingScoring:

    @pytest.mark.asyncio
    async def test_score_importance(self, dreamer, mm):
        """Mock LLM returns scores; index gets updated."""
        mm.write_team_memory("alpha", "notes.json", {"note": "important insight"})
        mm.write_team_memory("alpha", "trash.json", {"junk": "data"})

        mock_response = json.dumps({
            "notes.json": {"importance": 0.9, "summary": "Critical insight about user"},
            "trash.json": {"importance": 0.1, "summary": "Low-value junk data"},
        })

        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value=mock_response):
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
        """If LLM returns invalid JSON, scoring returns empty dict gracefully."""
        mm.write_team_memory("alpha", "notes.json", {"note": "test"})

        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value="not json"):
            scores = await dreamer._score_importance("alpha", {
                "notes.json": {"note": "test"},
            })

        assert scores == {}


class TestDreamingCompaction:

    @pytest.mark.asyncio
    async def test_compact_entries(self, dreamer, mm):
        """Mock compaction merges entries into a long-tier consolidated entry and archives originals."""
        mm.write_team_memory("alpha", "old1.json", {"fact": "A"})
        mm.write_team_memory("alpha", "old2.json", {"fact": "B"})

        mock_response = json.dumps({
            "content": {"facts": ["A", "B"]},
            "summary": "Consolidated facts A and B",
            "tags": ["facts"],
        })

        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value=mock_response):
            result = await dreamer._compact_entries("alpha", {
                "old1.json": {"fact": "A"},
                "old2.json": {"fact": "B"},
            })

        assert len(result) == 1
        # Original entries should be removed from memory index
        assert mm.get_entry_meta("alpha", "old1.json") is None
        assert mm.get_entry_meta("alpha", "old2.json") is None

        # Originals should be archived (not deleted)
        archive_dir = mm.team_path("alpha") / "archive"
        assert (archive_dir / "old1.json").exists()
        assert (archive_dir / "old2.json").exists()

        # Archive index should contain metadata
        archive_index = mm._read_archive_index("alpha")
        assert "old1.json" in archive_index
        assert "old2.json" in archive_index
        assert "archived_at" in archive_index["old1.json"]

        # New consolidated entry should exist in long tier
        index = mm.get_memory_index("alpha")
        consolidated_keys = [k for k in index if k.startswith("consolidated-")]
        assert len(consolidated_keys) == 1
        meta = index[consolidated_keys[0]]
        assert meta["tier"] == "long"
        assert meta["summary"] == "Consolidated facts A and B"

    @pytest.mark.asyncio
    async def test_compact_bad_json(self, dreamer, mm):
        """If LLM returns invalid JSON, compaction returns empty list."""
        mm.write_team_memory("alpha", "old1.json", {"fact": "A"})

        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value="bad"):
            result = await dreamer._compact_entries("alpha", {
                "old1.json": {"fact": "A"},
            })

        assert result == []
        # Original should NOT be deleted on failure
        assert mm.get_entry_meta("alpha", "old1.json") is not None


class TestDreamingGC:

    def test_gc_expired(self, dreamer, mm):
        """GC archives low-importance expired entries."""
        mm.write_team_memory("alpha", "old.json", {"stale": True})
        # Set metadata: old, low importance, low access
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        index = mm.get_memory_index("alpha")
        index["old.json"]["updated_at"] = old_time
        index["old.json"]["importance"] = 0.1
        index["old.json"]["access_count"] = 0
        mm._write_index("alpha", index)

        deleted = dreamer._gc_expired("alpha")
        assert "old.json" in deleted
        assert mm.get_entry_meta("alpha", "old.json") is None

        # Should be archived, not deleted
        archive_dir = mm.team_path("alpha") / "archive"
        assert (archive_dir / "old.json").exists()
        archive_index = mm._read_archive_index("alpha")
        assert "old.json" in archive_index

    def test_gc_preserves_important(self, dreamer, mm):
        """GC does NOT delete expired entries with high importance."""
        mm.write_team_memory("alpha", "important.json", {"key": "data"})
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        index = mm.get_memory_index("alpha")
        index["important.json"]["updated_at"] = old_time
        index["important.json"]["importance"] = 0.9
        index["important.json"]["access_count"] = 0
        mm._write_index("alpha", index)

        deleted = dreamer._gc_expired("alpha")
        assert "important.json" not in deleted
        assert mm.get_entry_meta("alpha", "important.json") is not None

    def test_gc_preserves_recent(self, dreamer, mm):
        """GC does NOT delete recent entries even with low importance."""
        mm.write_team_memory("alpha", "recent.json", {"fresh": True})
        # Importance is 0.0 by default, but it's recent
        deleted = dreamer._gc_expired("alpha")
        assert "recent.json" not in deleted

    def test_gc_preserves_high_access(self, dreamer, mm):
        """GC does NOT delete expired low-importance entries with high access count."""
        mm.write_team_memory("alpha", "popular.json", {"used": True})
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        index = mm.get_memory_index("alpha")
        index["popular.json"]["updated_at"] = old_time
        index["popular.json"]["importance"] = 0.1
        index["popular.json"]["access_count"] = 10  # above threshold
        mm._write_index("alpha", index)

        deleted = dreamer._gc_expired("alpha")
        assert "popular.json" not in deleted


#  CoW Snapshot Tests 


class TestCoWSnapshot:

    @pytest.mark.asyncio
    async def test_snapshot_and_restore(self, dreamer, mm):
        """On dream failure, index is restored from snapshot."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})
        original_index = copy.deepcopy(mm.get_memory_index("alpha"))

        # Make _score_importance raise an error
        with patch.object(
            dreamer, "_score_importance",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM down"),
        ):
            with pytest.raises(RuntimeError, match="LLM down"):
                await dreamer.run_team_dream("alpha")

        # Index should be restored
        restored_index = mm.get_memory_index("alpha")
        assert restored_index == original_index

    @pytest.mark.asyncio
    async def test_dream_success_no_restore(self, dreamer, mm):
        """On success, index is NOT restored (changes persist)."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})

        mock_scores = json.dumps({
            "entry.json": {"importance": 0.5, "summary": "Test entry"},
        })

        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value=mock_scores):
            stats = await dreamer.run_team_dream("alpha")

        assert stats.get("scored", 0) > 0
        meta = mm.get_entry_meta("alpha", "entry.json")
        assert meta["importance"] == 0.5
        assert meta["summary"] == "Test entry"


#  Integration Tests 


class TestDreamerIntegration:

    @pytest.mark.asyncio
    async def test_run_team_dream_empty(self, dreamer, mm):
        """Dream with no short-tier entries returns skipped."""
        result = await dreamer.run_team_dream("alpha")
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_run_all_dreams(self, dreamer, mm):
        """run_all_dreams processes all teams."""
        mm.write_team_memory("alpha", "a.json", {"a": 1})
        mm.write_team_memory("beta", "b.json", {"b": 2})

        mock_scores_alpha = json.dumps({
            "a.json": {"importance": 0.5, "summary": "Alpha data"},
        })
        mock_scores_beta = json.dumps({
            "b.json": {"importance": 0.7, "summary": "Beta data"},
        })
        call_count = 0

        async def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            if "a.json" in prompt:
                return mock_scores_alpha
            return mock_scores_beta

        with patch.object(dreamer, "_call_llm", side_effect=mock_llm):
            stats = await dreamer.run_all_dreams()

        # All three teams should appear in stats
        assert "alpha" in stats
        assert "beta" in stats

    def test_build_prompt_includes_long_term(self, mm):
        """Long-tier summaries appear in build_system_prompt."""
        mm.write_team_memory("alpha", "consolidated.json", {"merged": True})
        mm.update_entry_meta(
            "alpha", "consolidated.json",
            tier="long",
            summary="Key consolidated insights about user preferences",
        )

        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Long-term Memory" in prompt
        assert "Key consolidated insights" in prompt

    def test_build_prompt_no_long_term_for_short_tier(self, mm):
        """Short-tier entries with summaries do NOT appear in long-term section."""
        mm.write_team_memory("alpha", "short.json", {"temp": True})
        mm.update_entry_meta(
            "alpha", "short.json",
            summary="A short-tier summary",
        )

        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Long-term Memory" not in prompt

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

        # _call_llm is called twice: once for deep compact, once for overview
        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value=mock_response):
            stats = await dreamer.run_deep_consolidation()

        assert stats["alpha"]["merged"] == 3
        assert stats["alpha"]["into"] == 1

        # Original entries should be archived (not in memory index)
        index = mm.get_memory_index("alpha")
        for i in range(3):
            assert f"long{i}.json" not in index

        # Originals should be in archive
        archive_index = mm._read_archive_index("alpha")
        for i in range(3):
            assert f"long{i}.json" in archive_index

        # Deep consolidated entry should exist
        deep_keys = [k for k in index if k.startswith("deep-consolidated-")]
        assert len(deep_keys) == 1


#  Memory Index in get_all_memory_stats 


class TestOverviewGeneration:

    @pytest.mark.asyncio
    async def test_generate_overview(self, dreamer, mm):
        """_generate_overview writes .overview.md from long-tier summaries."""
        mm.write_team_memory("alpha", "entry1.json", {"data": 1})
        mm.update_entry_meta("alpha", "entry1.json", tier="long", summary="Key insight about user preferences")
        mm.write_team_memory("alpha", "entry2.json", {"data": 2})
        mm.update_entry_meta("alpha", "entry2.json", tier="long", summary="Important pattern in trading")

        mock_overview = "The team has learned about user preferences and trading patterns."

        with patch.object(dreamer, "_call_llm", new_callable=AsyncMock, return_value=mock_overview):
            result = await dreamer._generate_overview("alpha")

        assert result == mock_overview
        overview_path = mm.team_path("alpha") / "memory" / ".overview.md"
        assert overview_path.exists()
        assert overview_path.read_text() == mock_overview

    @pytest.mark.asyncio
    async def test_generate_overview_no_long_entries(self, dreamer, mm):
        """_generate_overview returns None when no long-tier entries exist."""
        result = await dreamer._generate_overview("alpha")
        assert result is None

    def test_read_overview(self, mm):
        """read_overview reads .overview.md file."""
        overview_path = mm.team_path("alpha") / "memory" / ".overview.md"
        overview_path.write_text("Test overview content")
        assert mm.read_overview("alpha") == "Test overview content"

    def test_read_overview_missing(self, mm):
        """read_overview returns empty string when file doesn't exist."""
        assert mm.read_overview("alpha") == ""

    def test_build_prompt_includes_overview(self, mm):
        """build_system_prompt includes overview when present."""
        overview_path = mm.team_path("alpha") / "memory" / ".overview.md"
        overview_path.write_text("Team overview: key facts and patterns.")

        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Memory Overview" in prompt
        assert "Team overview: key facts and patterns." in prompt


class TestArchive:

    def test_archive_team_memory(self, mm):
        """archive_team_memory moves file to archive/ and updates indices."""
        mm.write_team_memory("alpha", "entry.json", {"data": 1})
        mm.update_entry_meta("alpha", "entry.json", importance=0.7, summary="Test entry")

        mm.archive_team_memory("alpha", "entry.json")

        # Should be gone from memory
        assert mm.get_entry_meta("alpha", "entry.json") is None
        assert not (mm.team_path("alpha") / "memory" / "entry.json").exists()

        # Should be in archive
        assert (mm.team_path("alpha") / "archive" / "entry.json").exists()
        archive_index = mm._read_archive_index("alpha")
        assert "entry.json" in archive_index
        assert archive_index["entry.json"]["importance"] == 0.7
        assert "archived_at" in archive_index["entry.json"]

    def test_search_archive(self, mm):
        """search_archive finds entries by summary and tags."""
        mm.write_team_memory("alpha", "a.json", {"data": 1})
        mm.update_entry_meta("alpha", "a.json", importance=0.8, summary="User trading preferences", tags=["trading"])
        mm.write_team_memory("alpha", "b.json", {"data": 2})
        mm.update_entry_meta("alpha", "b.json", importance=0.5, summary="Weather data from last week")

        mm.archive_team_memory("alpha", "a.json")
        mm.archive_team_memory("alpha", "b.json")

        # Search by summary
        results = mm.search_archive("alpha", "trading")
        assert len(results) == 1
        assert results[0]["key"] == "a.json"

        # Search by tag
        results = mm.search_archive("alpha", "trading")
        assert len(results) == 1

        # Search with no results
        results = mm.search_archive("alpha", "nonexistent")
        assert len(results) == 0

    def test_search_archive_empty(self, mm):
        """search_archive returns empty list when no archive exists."""
        results = mm.search_archive("alpha", "anything")
        assert results == []

    def test_search_archive_sorted_by_importance(self, mm):
        """search_archive results are sorted by importance descending."""
        mm.write_team_memory("alpha", "low.json", {"data": 1})
        mm.update_entry_meta("alpha", "low.json", importance=0.3, summary="data analysis")
        mm.write_team_memory("alpha", "high.json", {"data": 2})
        mm.update_entry_meta("alpha", "high.json", importance=0.9, summary="data patterns")

        mm.archive_team_memory("alpha", "low.json")
        mm.archive_team_memory("alpha", "high.json")

        results = mm.search_archive("alpha", "data")
        assert len(results) == 2
        assert results[0]["key"] == "high.json"
        assert results[1]["key"] == "low.json"


class TestTokenBudget:

    def test_long_term_respects_budget(self, mm):
        """build_system_prompt respects token budget for long-term summaries."""
        # Create many long-tier entries with long summaries
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
        assert "Long-term Memory" in prompt

        # Extract the long-term section and verify it's bounded
        parts = prompt.split("## Long-term Memory\n")
        if len(parts) > 1:
            long_section = parts[1].split("\n\n---\n\n")[0]
            # Should not include all 50 entries (budget would be ~8000 chars)
            entry_lines = [l for l in long_section.split("\n") if l.startswith("- ")]
            assert len(entry_lines) < 50

    def test_overview_consumes_budget(self, mm):
        """Overview text consumes part of the token budget, leaving less room for summaries."""
        # Write a large overview that consumes most of the budget (~7500 of 8000 chars)
        large_overview = "x" * 7500
        overview_path = mm.team_path("alpha") / "memory" / ".overview.md"
        overview_path.write_text(large_overview)

        # Create long-tier entries with long summaries (~100 chars each)
        for i in range(20):
            key = f"entry{i}.json"
            mm.write_team_memory("alpha", key, {"data": i})
            mm.update_entry_meta(
                "alpha", key,
                tier="long",
                importance=0.9,
                summary=f"This is a detailed summary for entry number {i} with significant content " * 2,
            )

        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Memory Overview" in prompt

        # With large overview consuming most budget, fewer long-term entries should fit
        if "Long-term Memory" in prompt:
            parts = prompt.split("## Long-term Memory\n")
            long_section = parts[1].split("\n\n---\n\n")[0]
            entry_lines = [l for l in long_section.split("\n") if l.startswith("- ")]
            assert len(entry_lines) < 20


class TestMemoryStats:

    def test_stats_exclude_index_file(self, mm, mem_dir):
        """get_all_memory_stats does not count .index.json."""
        mm.write_team_memory("alpha", "data.json", {"x": 1})
        # This creates .index.json as a side effect

        stats = mm.get_all_memory_stats()
        alpha_keys = stats.get("alpha", {}).get("keys", [])
        assert "data.json" in alpha_keys
        assert INDEX_FILENAME not in alpha_keys
