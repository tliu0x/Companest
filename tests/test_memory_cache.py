"""MemoryManager Three-Layer Cache Tests"""

import json
import time

import pytest

from companest.memory import MemoryManager


@pytest.fixture
def mem_dir(tmp_path):
    base = tmp_path / ".companest"
    base.mkdir()
    (base / "soul.md").write_text("master soul")
    (base / "user.md").write_text("user context")

    alpha = base / "teams" / "alpha"
    (alpha / "pis" / "lead").mkdir(parents=True)
    (alpha / "memory").mkdir(parents=True)
    (alpha / "team.md").write_text("# Team: alpha\n- role: general\n- lead_pi: lead")
    (alpha / "soul.md").write_text("alpha team soul")
    (alpha / "pis" / "lead" / "soul.md").write_text("alpha lead soul")

    # research is the enrichment source for other teams
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


class TestFileCache:

    def test_file_cache_hit(self, mm, mem_dir):
        path = mem_dir / "user.md"
        assert mm._read_file(path) == "user context"

        entry = mm._file_cache[path.resolve()]
        assert entry.content == "user context"
        assert not entry.is_json
        assert mm._read_file(path) == "user context"
        assert mm._file_cache[path.resolve()] is entry

    def test_file_cache_invalidation_on_mtime(self, mm, mem_dir):
        path = mem_dir / "user.md"
        mm._read_file(path)

        # Ensure mtime differs (some filesystems have coarse resolution)
        time.sleep(0.05)
        path.write_text("updated user context")

        assert mm._read_file(path) == "updated user context"

    def test_file_cache_nonexistent(self, mm, mem_dir):
        path = mem_dir / "nonexistent.md"
        assert mm._read_file(path) == ""
        assert path.resolve() not in mm._file_cache

    def test_json_cache_parsed(self, mm, mem_dir):
        """JSON cache stores parsed object, not raw string."""
        path = mem_dir / "teams" / "alpha" / "memory" / "data.json"
        path.write_text(json.dumps({"key": "value"}))

        assert mm._read_json(path) == {"key": "value"}

        entry = mm._file_cache[path.resolve()]
        assert entry.is_json
        assert entry.content == {"key": "value"}

    def test_file_and_json_use_separate_slots(self, mm, mem_dir):
        """Reading same path as file then JSON replaces cache entry."""
        path = mem_dir / "teams" / "alpha" / "memory" / "test.json"
        path.write_text(json.dumps({"a": 1}))
        resolved = path.resolve()

        assert mm._read_file(path) == '{"a": 1}'
        assert not mm._file_cache[resolved].is_json

        assert mm._read_json(path) == {"a": 1}
        assert mm._file_cache[resolved].is_json


class TestPromptCache:

    def test_prompt_cache_hit(self, mm):
        p1 = mm.build_system_prompt("alpha", "lead")
        assert "alpha lead soul" in p1
        assert ("alpha", "lead") in mm._prompt_cache
        assert mm.build_system_prompt("alpha", "lead") is p1

    @pytest.mark.parametrize("rel_path,new_text", [
        ("teams/alpha/pis/lead/soul.md", "updated alpha lead soul"),
        ("user.md", "new user context"),
    ])
    def test_prompt_cache_invalidation_on_source_change(self, mm, mem_dir, rel_path, new_text):
        p1 = mm.build_system_prompt("alpha", "lead")

        time.sleep(0.05)
        (mem_dir / rel_path).write_text(new_text)

        p2 = mm.build_system_prompt("alpha", "lead")
        assert new_text in p2
        assert p1 != p2

    def test_prompt_cache_invalidation_on_write(self, mm):
        mm.build_system_prompt("alpha", "lead")
        assert ("alpha", "lead") in mm._prompt_cache

        mm.write_team_memory("alpha", "notes.json", {"note": "hello"})
        assert ("alpha", "lead") not in mm._prompt_cache

    def test_prompt_cache_cross_team_enrichment(self, mm):
        """Writing research memory invalidates alpha's prompt: research is an enrichment source."""
        mm.write_team_memory("research", "briefing.json", {
            "items": [{"headline": "Test news", "category": "test"}],
            "updated_at": "2026-01-01",
        })

        assert "Test news" in mm.build_system_prompt("alpha", "lead")
        assert ("alpha", "lead") in mm._prompt_cache

        mm.write_team_memory("research", "briefing.json", {
            "items": [{"headline": "Updated news", "category": "test"}],
            "updated_at": "2026-01-02",
        })

        assert ("alpha", "lead") not in mm._prompt_cache
        assert "Updated news" in mm.build_system_prompt("alpha", "lead")

    def test_prompt_cache_research_self_excluded(self, mm):
        """Research team's own prompt doesn't include its own briefing."""
        mm.write_team_memory("research", "briefing.json", {
            "items": [{"headline": "News item"}],
            "updated_at": "2026-01-01",
        })
        assert "News item" not in mm.build_system_prompt("research", "analyst")

    def test_prompt_includes_memory_keys(self, mm):
        mm.write_team_memory("alpha", "notes.json", {"note": "test"})
        prompt = mm.build_system_prompt("alpha", "lead")
        assert "Available Memory" in prompt
        assert "notes.json" in prompt


class TestDirCache:

    def test_dir_cache_invalidation_on_write(self, mm):
        mm.write_team_memory("alpha", "first.json", {"a": 1})
        r1 = mm.list_team_memory("alpha")

        mm.write_team_memory("alpha", "second.json", {"b": 2})
        r2 = mm.list_team_memory("alpha")

        assert "second.json" in r2
        assert len(r2) > len(r1)

    def test_dir_cache_missing_memory_dir(self, mm, mem_dir):
        team_dir = mem_dir / "teams" / "empty"
        (team_dir / "pis" / "pi1").mkdir(parents=True)
        (team_dir / "team.md").write_text("# Team: empty")

        assert mm.list_team_memory("empty") == []


class TestCacheInvalidation:

    def test_clear_cache_empties_all_layers(self, mm, mem_dir):
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

    def test_append_team_memory_invalidates(self, mm):
        mm.write_team_memory("alpha", "log.json", [])
        mm.build_system_prompt("alpha", "lead")
        mm.list_team_memory("alpha")
        assert ("alpha", "lead") in mm._prompt_cache

        mm.append_team_memory("alpha", "log.json", {"entry": 1})

        assert ("alpha", "lead") not in mm._prompt_cache
        assert mm.read_team_memory("alpha", "log.json") == [{"entry": 1}]

    def test_delete_team_memory_invalidates(self, mm):
        mm.write_team_memory("alpha", "del.json", {"d": 1})
        mm.build_system_prompt("alpha", "lead")
        mm.list_team_memory("alpha")
        assert ("alpha", "lead") in mm._prompt_cache

        mm.delete_team_memory("alpha", "del.json")
        assert ("alpha", "lead") not in mm._prompt_cache

    def test_index_persists_across_instances(self, mm, mem_dir):
        mm.write_team_memory("alpha", "persist.json", {"p": 1})
        mm.update_entry_meta("alpha", "persist.json", importance=0.7)

        meta = MemoryManager(str(mem_dir)).get_entry_meta("alpha", "persist.json")
        assert meta is not None
        assert meta["importance"] == 0.7
