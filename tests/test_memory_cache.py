"""
MemoryManager Three-Layer Cache Tests

Layer 1: _read_file / _read_json mtime+size cache
Layer 2: build_system_prompt dependency-tracking cache
Layer 3: list_team_memory directory mtime cache
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from companest.memory import MemoryManager


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


#  Layer 1: File Cache 


class TestFileCache:

    def test_file_cache_hit(self, mm, mem_dir):
        """Second read of same unchanged file should return cached content."""
        path = mem_dir / "user.md"
        result1 = mm._read_file(path)
        assert result1 == "user context"

        # Verify cache entry exists
        resolved = path.resolve()
        assert resolved in mm._file_cache
        entry = mm._file_cache[resolved]
        assert entry.content == "user context"
        assert not entry.is_json

        # Second read  same content, from cache
        result2 = mm._read_file(path)
        assert result2 == "user context"

    def test_file_cache_invalidation_on_mtime(self, mm, mem_dir):
        """Modifying a file should cause a cache miss and return new content."""
        path = mem_dir / "user.md"
        mm._read_file(path)

        # Ensure mtime differs (some filesystems have 1s resolution)
        time.sleep(0.05)
        path.write_text("updated user context")

        result = mm._read_file(path)
        assert result == "updated user context"

    def test_file_cache_nonexistent(self, mm, mem_dir):
        """Reading a nonexistent file returns empty string and doesn't cache."""
        path = mem_dir / "nonexistent.md"
        result = mm._read_file(path)
        assert result == ""
        assert path.resolve() not in mm._file_cache

    def test_json_cache_parsed(self, mm, mem_dir):
        """JSON cache stores parsed object, not raw string."""
        path = mem_dir / "teams" / "alpha" / "memory" / "data.json"
        path.write_text(json.dumps({"key": "value"}))

        result = mm._read_json(path)
        assert result == {"key": "value"}

        resolved = path.resolve()
        assert resolved in mm._file_cache
        entry = mm._file_cache[resolved]
        assert entry.is_json
        assert entry.content == {"key": "value"}

    def test_json_cache_hit(self, mm, mem_dir):
        """Second JSON read returns cached parsed object."""
        path = mem_dir / "teams" / "alpha" / "memory" / "data.json"
        path.write_text(json.dumps([1, 2, 3]))

        result1 = mm._read_json(path)
        result2 = mm._read_json(path)
        assert result1 == [1, 2, 3]
        # Both reads return equal data (deep-copied from cache to prevent mutation)
        assert result1 == result2

    def test_json_cache_nonexistent(self, mm, mem_dir):
        """Reading nonexistent JSON returns None."""
        path = mem_dir / "teams" / "alpha" / "memory" / "missing.json"
        result = mm._read_json(path)
        assert result is None

    def test_file_and_json_use_separate_slots(self, mm, mem_dir):
        """Reading same path as file then JSON replaces cache entry."""
        path = mem_dir / "teams" / "alpha" / "memory" / "test.json"
        path.write_text(json.dumps({"a": 1}))

        # Read as text first
        text = mm._read_file(path)
        assert text == '{"a": 1}'
        resolved = path.resolve()
        assert not mm._file_cache[resolved].is_json

        # Read as JSON  should replace cache entry
        parsed = mm._read_json(path)
        assert parsed == {"a": 1}
        assert mm._file_cache[resolved].is_json


#  Layer 2: Prompt Cache 


class TestPromptCache:

    def test_prompt_cache_hit(self, mm, mem_dir):
        """Consecutive calls with unchanged files return cached prompt."""
        p1 = mm.build_system_prompt("alpha", "lead")
        assert "alpha lead soul" in p1
        assert ("alpha", "lead") in mm._prompt_cache

        p2 = mm.build_system_prompt("alpha", "lead")
        assert p1 == p2
        # Same string object (cache hit)
        assert p1 is p2

    def test_prompt_cache_invalidation_on_soul_change(self, mm, mem_dir):
        """Modifying soul.md invalidates prompt cache."""
        p1 = mm.build_system_prompt("alpha", "lead")

        time.sleep(0.05)
        (mem_dir / "teams" / "alpha" / "pis" / "lead" / "soul.md").write_text(
            "updated alpha lead soul"
        )

        p2 = mm.build_system_prompt("alpha", "lead")
        assert "updated alpha lead soul" in p2
        assert p1 != p2

    def test_prompt_cache_invalidation_on_user_change(self, mm, mem_dir):
        """Modifying user.md invalidates prompt cache."""
        p1 = mm.build_system_prompt("alpha", "lead")
        assert "user context" in p1

        time.sleep(0.05)
        (mem_dir / "user.md").write_text("new user context")

        p2 = mm.build_system_prompt("alpha", "lead")
        assert "new user context" in p2
        assert p1 != p2

    def test_prompt_cache_invalidation_on_write(self, mm, mem_dir):
        """write_team_memory invalidates prompt cache for that team."""
        p1 = mm.build_system_prompt("alpha", "lead")
        assert ("alpha", "lead") in mm._prompt_cache

        mm.write_team_memory("alpha", "notes.json", {"note": "hello"})

        # Cache should be invalidated
        assert ("alpha", "lead") not in mm._prompt_cache

    def test_prompt_cache_cross_team_enrichment(self, mm, mem_dir):
        """Writing to research team memory invalidates alpha's prompt cache
        because research is an enrichment source for alpha."""
        # Write initial briefing
        mm.write_team_memory("research", "briefing.json", {
            "items": [{"headline": "Test news", "category": "test"}],
            "updated_at": "2026-01-01",
        })

        p1 = mm.build_system_prompt("alpha", "lead")
        assert "Test news" in p1
        assert ("alpha", "lead") in mm._prompt_cache

        # Update research briefing  should invalidate alpha's cache
        mm.write_team_memory("research", "briefing.json", {
            "items": [{"headline": "Updated news", "category": "test"}],
            "updated_at": "2026-01-02",
        })

        assert ("alpha", "lead") not in mm._prompt_cache

        p2 = mm.build_system_prompt("alpha", "lead")
        assert "Updated news" in p2

    def test_prompt_cache_research_self_excluded(self, mm, mem_dir):
        """Research team's own prompt doesn't include its own briefing."""
        mm.write_team_memory("research", "briefing.json", {
            "items": [{"headline": "News item"}],
            "updated_at": "2026-01-01",
        })
        prompt = mm.build_system_prompt("research", "analyst")
        assert "News item" not in prompt

    def test_prompt_includes_memory_keys(self, mm, mem_dir):
        """Prompt includes Available Memory section listing team memory keys."""
        mm.write_team_memory("alpha", "notes.json", {"note": "test"})
        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Available Memory" in prompt
        assert "notes.json" in prompt


#  Layer 3: Directory Cache 


class TestDirCache:

    def test_dir_cache_hit(self, mm, mem_dir):
        """Consecutive list_team_memory calls use cached result."""
        # Create a file first
        mm.write_team_memory("alpha", "data.json", [1, 2])

        r1 = mm.list_team_memory("alpha")
        assert "data.json" in r1

        r2 = mm.list_team_memory("alpha")
        assert r1 == r2

    def test_dir_cache_invalidation_on_write(self, mm, mem_dir):
        """write_team_memory invalidates dir cache."""
        mm.write_team_memory("alpha", "first.json", {"a": 1})
        r1 = mm.list_team_memory("alpha")

        mm.write_team_memory("alpha", "second.json", {"b": 2})
        r2 = mm.list_team_memory("alpha")

        assert "second.json" in r2
        assert len(r2) > len(r1)

    def test_dir_cache_empty_dir(self, mm, mem_dir):
        """list_team_memory for nonexistent memory dir returns empty list."""
        # Team with no memory dir
        team_dir = mem_dir / "teams" / "empty"
        team_dir.mkdir(parents=True)
        (team_dir / "team.md").write_text("# Team: empty")
        (team_dir / "pis" / "pi1").mkdir(parents=True)

        result = mm.list_team_memory("empty")
        assert result == []


#  Integration: clear_cache 


class TestClearCache:

    def test_clear_cache_empties_all_layers(self, mm, mem_dir):
        """clear_cache() empties all three cache dicts."""
        # Warm up all caches
        mm._read_file(mem_dir / "user.md")
        mm.write_team_memory("alpha", "data.json", [1])
        mm.list_team_memory("alpha")
        mm.build_system_prompt("alpha", "lead")

        assert len(mm._file_cache) > 0
        assert len(mm._prompt_cache) > 0
        assert len(mm._dir_cache) > 0

        mm.clear_cache()

        assert len(mm._file_cache) == 0
        assert len(mm._prompt_cache) == 0
        assert len(mm._dir_cache) == 0

    def test_cache_works_after_clear(self, mm, mem_dir):
        """Cache repopulates correctly after clear_cache()."""
        p1 = mm.build_system_prompt("alpha", "lead")
        mm.clear_cache()
        p2 = mm.build_system_prompt("alpha", "lead")
        assert p1 == p2


#  Edge cases 


class TestCacheEdgeCases:

    def test_append_team_memory_invalidates(self, mm, mem_dir):
        """append_team_memory invalidates file, dir, and prompt caches."""
        mm.write_team_memory("alpha", "log.json", [])
        mm.build_system_prompt("alpha", "lead")
        mm.list_team_memory("alpha")

        assert ("alpha", "lead") in mm._prompt_cache

        mm.append_team_memory("alpha", "log.json", {"entry": 1})

        # All relevant caches invalidated
        assert ("alpha", "lead") not in mm._prompt_cache

        # Data is correct
        data = mm.read_team_memory("alpha", "log.json")
        assert data == [{"entry": 1}]

    def test_write_global_memory_invalidates_file_cache(self, mm, mem_dir):
        """write_global_memory invalidates Layer 1 cache."""
        (mem_dir / "memory").mkdir(exist_ok=True)
        path = mem_dir / "memory" / "global.json"
        path.write_text(json.dumps({"old": True}))

        result1 = mm.read_global_memory("global.json")
        assert result1 == {"old": True}

        mm.write_global_memory("global.json", {"new": True})
        result2 = mm.read_global_memory("global.json")
        assert result2 == {"new": True}

    def test_write_shared_invalidates_file_cache(self, mm, mem_dir):
        """write_shared invalidates Layer 1 cache."""
        (mem_dir / "shared").mkdir(exist_ok=True)
        path = mem_dir / "shared" / "common.json"
        path.write_text(json.dumps({"version": 1}))

        result1 = mm.read_shared("common.json")
        assert result1 == {"version": 1}

        mm.write_shared("common.json", {"version": 2})
        result2 = mm.read_shared("common.json")
        assert result2 == {"version": 2}


#  Index integration with cache 


class TestIndexCache:

    def test_list_team_memory_excludes_dot_files(self, mm, mem_dir):
        """list_team_memory excludes .index.json (dot-prefixed files)."""
        mm.write_team_memory("alpha", "visible.json", {"v": 1})
        keys = mm.list_team_memory("alpha")
        assert "visible.json" in keys
        assert ".index.json" not in keys

    def test_index_persists_across_instances(self, mm, mem_dir):
        """Index written by one MemoryManager is readable by another."""
        mm.write_team_memory("alpha", "persist.json", {"p": 1})
        mm.update_entry_meta("alpha", "persist.json", importance=0.7)

        mm2 = MemoryManager(str(mem_dir))
        meta = mm2.get_entry_meta("alpha", "persist.json")
        assert meta is not None
        assert meta["importance"] == 0.7

    def test_delete_team_memory_invalidates_caches(self, mm, mem_dir):
        """delete_team_memory invalidates file, dir, and prompt caches."""
        mm.write_team_memory("alpha", "del.json", {"d": 1})
        mm.build_system_prompt("alpha", "lead")
        mm.list_team_memory("alpha")

        assert ("alpha", "lead") in mm._prompt_cache

        mm.delete_team_memory("alpha", "del.json")
        assert ("alpha", "lead") not in mm._prompt_cache
