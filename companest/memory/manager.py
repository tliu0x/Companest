import copy
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..utils import synchronized
from ..exceptions import CompanestError
from ..runtime.storage import FileLock

logger = logging.getLogger(__name__)


class MemoryError(CompanestError):
    pass


@dataclass
class _FileCacheEntry:
    mtime: float
    size: int
    content: Any       # str for md, parsed obj for json
    is_json: bool


@dataclass
class _PromptCacheEntry:
    prompt: str
    dependency_mtimes: Dict[str, float]  # {path_str: mtime}


@dataclass
class _DirCacheEntry:
    mtime: float
    entries: List[str]


INDEX_FILENAME = ".index.json"

_MEMORY_TOKEN_BUDGET = 2000
_CHARS_PER_TOKEN = 4


@dataclass
class EnrichmentSource:
    """A data source that enriches system prompts."""
    source_team_id: str
    memory_key: str
    section_title: str
    formatter: Callable[[Any], Optional[str]]
    exclude_teams: Optional[Set[str]] = field(default_factory=set)
    company_id: Optional[str] = None


class MemoryManager:
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_./-]*$")

    def __init__(self, base_path: str = ".companest", lock_dir: Optional[str] = None):
        self.base_path = Path(base_path).resolve()
        locks = Path(lock_dir) if lock_dir else self.base_path / ".locks"
        self._io_lock = self._append_lock = FileLock(locks / "memory.lock")
        self._file_cache: Dict[Path, _FileCacheEntry] = {}
        self._prompt_cache: Dict[Tuple[str, str], _PromptCacheEntry] = {}
        self._dir_cache: Dict[Path, _DirCacheEntry] = {}
        self._enrichment_sources: List[EnrichmentSource] = [
            EnrichmentSource(
                source_team_id="research",
                memory_key="briefing.json",
                section_title="World Briefing",
                formatter=self._format_research_briefing,
                exclude_teams={"research"},
            ),
            EnrichmentSource(
                source_team_id="info-collection",
                memory_key="feed.json",
                section_title="Recent Feed",
                formatter=self._format_info_feed,
                exclude_teams={"info-collection"},
            ),
        ]
        self._team_path_overrides: Dict[str, Path] = {}
        if not self.base_path.exists():
            logger.warning(f"Memory base path does not exist: {self.base_path}")

    @synchronized
    def register_enrichment(self, source: EnrichmentSource) -> None:
        self._enrichment_sources.append(source)

    @synchronized
    def remove_enrichments_by_company(self, company_id: str) -> int:
        before = len(self._enrichment_sources)
        self._enrichment_sources = [s for s in self._enrichment_sources if s.company_id != company_id]
        return before - len(self._enrichment_sources)

    @synchronized
    def read_master_user(self) -> str:
        return self._read_file(self.base_path / "user.md")

    @synchronized
    def _validate_path_component(self, value: str, label: str) -> str:
        """Validate a path component to prevent path traversal."""
        if not value:
            raise MemoryError(f"Empty {label}")
        if ".." in value or value.startswith("/"):
            raise MemoryError(f"Invalid {label}: path traversal not allowed: {value!r}")
        if not self._SAFE_PATH_RE.match(value):
            raise MemoryError(f"Invalid {label}: {value!r} (alphanumeric, dash, underscore, dot only)")
        return value

    @synchronized
    def register_team_path(self, team_id: str, path: Path) -> None:
        """Register a physical path override for a namespaced team."""
        self._team_path_overrides[team_id] = path.resolve()

    @synchronized
    def unregister_company_paths(self, company_id: str) -> None:
        to_remove = [k for k in self._team_path_overrides if k.startswith(f"{company_id}/")]
        for k in to_remove:
            del self._team_path_overrides[k]

    @synchronized
    def team_path(self, team_id: str) -> Path:
        if team_id in self._team_path_overrides:
            return self._team_path_overrides[team_id]
        self._validate_path_component(team_id, "team_id")
        return self.base_path / "teams" / team_id

    @synchronized
    def read_team_soul(self, team_id: str) -> str:
        return self._read_file(self.team_path(team_id) / "soul.md")

    @synchronized
    def read_team_memory(self, team_id: str, key: str) -> Any:
        self._validate_path_component(key, "key")
        data = self._read_json(self.team_path(team_id) / "memory" / key)
        self._update_index_on_read(team_id, key)
        return data

    @synchronized
    def write_team_memory(self, team_id: str, key: str, data: Any) -> None:
        self._validate_path_component(key, "key")
        path = self.team_path(team_id) / "memory" / key
        self._write_json(path, data)
        self._file_cache.pop(path.resolve(), None)
        self._dir_cache.pop(path.parent.resolve(), None)
        self._invalidate_prompt_cache_for_team(team_id)
        self._update_index_on_write(team_id, key, data)

    @synchronized
    def append_team_memory(self, team_id: str, key: str, entry: Any) -> None:
        """Append an entry to a JSON array in team memory (thread-safe)."""
        self._validate_path_component(key, "key")
        path = self.team_path(team_id) / "memory" / key
        with self._append_lock:
            existing = self._read_json(path) if path.exists() else []
            if not isinstance(existing, list):
                existing = [existing]
            existing.append(entry)
            self._write_json(path, existing)
            self._file_cache.pop(path.resolve(), None)
            self._dir_cache.pop(path.parent.resolve(), None)
            self._invalidate_prompt_cache_for_team(team_id)
            self._update_index_on_write(team_id, key, existing)

    @synchronized
    def list_team_memory(self, team_id: str) -> List[str]:
        mem_dir = self.team_path(team_id) / "memory"
        resolved = mem_dir.resolve()
        st = self._stat_safe(resolved)
        if st is None:
            return []
        cached = self._dir_cache.get(resolved)
        if cached is not None and cached.mtime == st.st_mtime:
            return cached.entries
        entries = [
            f.name for f in mem_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ]
        self._dir_cache[resolved] = _DirCacheEntry(mtime=st.st_mtime, entries=entries)
        return entries

    @synchronized
    def list_teams(self) -> List[str]:
        teams_dir = self.base_path / "teams"
        if not teams_dir.exists():
            return []
        return [
            d.name for d in teams_dir.iterdir()
            if d.is_dir() and (d / "team.md").exists()
        ]

    @synchronized
    def _index_path(self, team_id: str) -> Path:
        return self.team_path(team_id) / "memory" / INDEX_FILENAME

    @synchronized
    def _read_index(self, team_id: str) -> Dict[str, dict]:
        path = self._index_path(team_id)
        if not path.exists():
            return self._rebuild_index(team_id)
        data = self._read_json(path)
        return data if isinstance(data, dict) else {}

    @synchronized
    def _write_index(self, team_id: str, index: Dict[str, dict]) -> None:
        path = self._index_path(team_id)
        self._write_json(path, index)
        self._file_cache.pop(path.resolve(), None)

    @staticmethod
    def _new_meta(created_at: str, updated_at: str, last_accessed: str, size: int) -> dict:
        return {
            "created_at": created_at,
            "updated_at": updated_at,
            "access_count": 0,
            "last_accessed": last_accessed,
            "size_bytes": size,
            "importance": 0.0,  # 0.0-1.0, scored by the dreamer
            "tier": "short",    # "short" | "long"
            "tags": [],
            "summary": "",
        }

    @synchronized
    def _rebuild_index(self, team_id: str) -> Dict[str, dict]:
        """Rebuild the index from the memory files on disk."""
        mem_dir = self.team_path(team_id) / "memory"
        if not mem_dir.exists():
            return {}
        now = datetime.now(timezone.utc).isoformat()
        index: Dict[str, dict] = {}
        for f in mem_dir.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            index[f.name] = self._new_meta(
                datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                now,
                st.st_size,
            )
        self._write_index(team_id, index)
        return index

    @synchronized
    def _update_index_on_write(self, team_id: str, key: str, data: Any) -> None:
        try:
            index = self._read_index(team_id)
            now = datetime.now(timezone.utc).isoformat()
            path = self.team_path(team_id) / "memory" / key
            try:
                size = path.stat().st_size
            except OSError:
                size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))

            if key in index:
                index[key]["updated_at"] = now
                index[key]["size_bytes"] = size
            else:
                index[key] = self._new_meta(now, now, now, size)
            self._write_index(team_id, index)
        except Exception as e:
            logger.debug(f"Index update on write failed (non-blocking): {e}")

    @synchronized
    def _update_index_on_read(self, team_id: str, key: str) -> None:
        try:
            index = self._read_index(team_id)
            if key in index:
                index[key]["access_count"] = index[key].get("access_count", 0) + 1
                index[key]["last_accessed"] = datetime.now(timezone.utc).isoformat()
                self._write_index(team_id, index)
        except Exception:
            pass  # best-effort

    @synchronized
    def _remove_from_index(self, team_id: str, key: str) -> None:
        index = self._read_index(team_id)
        if key in index:
            del index[key]
            self._write_index(team_id, index)

    @synchronized
    def get_memory_index(self, team_id: str) -> Dict[str, dict]:
        return self._read_index(team_id)

    @synchronized
    def get_entry_meta(self, team_id: str, key: str) -> Optional[dict]:
        return self._read_index(team_id).get(key)

    @synchronized
    def delete_team_memory(self, team_id: str, key: str) -> None:
        self._validate_path_component(key, "key")
        path = self.team_path(team_id) / "memory" / key
        if path.exists():
            path.unlink()
        self._file_cache.pop(path.resolve(), None)
        self._dir_cache.pop(path.parent.resolve(), None)
        self._remove_from_index(team_id, key)
        self._invalidate_prompt_cache_for_team(team_id)

    @synchronized
    def archive_team_memory(self, team_id: str, key: str) -> None:
        """Move a memory file to archive/ and update indices."""
        self._validate_path_component(key, "key")
        src = self.team_path(team_id) / "memory" / key
        dst_dir = self.team_path(team_id) / "archive"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / key

        meta = self.get_entry_meta(team_id, key)
        if meta:
            archive_index = self._read_archive_index(team_id)
            meta["archived_at"] = datetime.now(timezone.utc).isoformat()
            archive_index[key] = meta
            self._write_archive_index(team_id, archive_index)

        if src.exists():
            shutil.move(str(src), str(dst))

        self._remove_from_index(team_id, key)
        self._file_cache.pop(src.resolve(), None)
        self._dir_cache.pop(src.parent.resolve(), None)
        self._invalidate_prompt_cache_for_team(team_id)

    @synchronized
    def search_archive(self, team_id: str, query: str, limit: int = 10) -> List[dict]:
        """Search archive entries by summary and tags (case-insensitive substring)."""
        archive_index = self._read_archive_index(team_id)
        query_lower = query.lower()
        results = []
        for key, meta in archive_index.items():
            summary = meta.get("summary", "").lower()
            tags = [t.lower() for t in meta.get("tags", [])]
            if query_lower in summary or any(query_lower in t for t in tags):
                results.append({"key": key, **meta})
        results.sort(
            key=lambda r: (-r.get("importance", 0), r.get("archived_at", "")),
        )
        return results[:limit]

    @synchronized
    def read_overview(self, team_id: str) -> str:
        return self._read_file(self.team_path(team_id) / "memory" / ".overview.md")

    @synchronized
    def _read_archive_index(self, team_id: str) -> Dict[str, dict]:
        path = self.team_path(team_id) / "archive" / INDEX_FILENAME
        if not path.exists():
            return {}
        data = self._read_json(path)
        return data if isinstance(data, dict) else {}

    @synchronized
    def _write_archive_index(self, team_id: str, index: Dict[str, dict]) -> None:
        path = self.team_path(team_id) / "archive" / INDEX_FILENAME
        self._write_json(path, index)
        self._file_cache.pop(path.resolve(), None)

    @synchronized
    def update_entry_meta(self, team_id: str, key: str, **fields) -> None:
        allowed = {"importance", "tier", "summary", "tags"}
        invalid = set(fields) - allowed
        if invalid:
            raise MemoryError(f"Cannot update fields: {invalid}")
        index = self._read_index(team_id)
        if key not in index:
            raise MemoryError(f"Key not in index: {key}")
        index[key].update(fields)
        self._write_index(team_id, index)

    @synchronized
    def read_pi_soul(self, team_id: str, pi_id: str) -> str:
        self._validate_path_component(pi_id, "pi_id")
        return self._read_file(
            self.team_path(team_id) / "pis" / pi_id / "soul.md"
        )

    @synchronized
    def build_system_prompt(
        self, team_id: str, pi_id: str, company_context: Optional[str] = None,
    ) -> str:
        cache_key = (team_id, pi_id)
        current_stats = self._collect_prompt_dependency_stats(team_id, pi_id)

        if not company_context:
            cached = self._prompt_cache.get(cache_key)
            if cached is not None and cached.dependency_mtimes == current_stats:
                return cached.prompt

        parts = []

        pi_soul = self.read_pi_soul(team_id, pi_id)
        if pi_soul:
            parts.append(pi_soul)

        team_soul = self.read_team_soul(team_id)
        if team_soul:
            parts.append(f"## Team Context\n{team_soul}")

        if company_context:
            parts.append(f"## Company Context\n{company_context}")

        user_md = self.read_master_user()
        if user_md:
            parts.append(f"## User\n{user_md}")

        for source in self._enrichment_sources:
            if source.exclude_teams and team_id in source.exclude_teams:
                continue
            try:
                data = self.read_team_memory(source.source_team_id, source.memory_key)
            except Exception:
                continue
            if data is not None:
                section = source.formatter(data)
                if section:
                    parts.append(section)

        mem_keys = self.list_team_memory(team_id)
        if mem_keys:
            keys_str = ", ".join(mem_keys)
            parts.append(
                f"## Available Memory\n"
                f"Team memory keys: {keys_str}\n"
                f"Use memory_read/memory_write tools to access."
            )

        try:
            budget_chars = _MEMORY_TOKEN_BUDGET * _CHARS_PER_TOKEN

            overview = self.read_overview(team_id)
            if overview:
                parts.append(f"## Memory Overview\n{overview}")
                budget_chars -= len(overview)

            index = self._read_index(team_id)
            long_entries = [
                (key, meta) for key, meta in index.items()
                if meta.get("tier") == "long" and meta.get("summary")
            ]
            long_entries.sort(
                key=lambda x: x[1].get("importance", 0), reverse=True,
            )
            used = 0
            lines = []
            for key, meta in long_entries:
                line = f"- {key}: {meta['summary']}"
                used += len(line)
                if used > budget_chars:
                    break
                lines.append(line)
            if lines:
                parts.append(
                    "## Long-term Memory\n" + "\n".join(lines)
                )
        except Exception:
            pass  # non-blocking

        prompt = "\n\n---\n\n".join(parts)
        # Re-collect: _read_index may have lazily created .index.json, changing the memory dir mtime.
        final_stats = self._collect_prompt_dependency_stats(team_id, pi_id)
        self._prompt_cache[cache_key] = _PromptCacheEntry(
            prompt=prompt,
            dependency_mtimes=final_stats,
        )
        return prompt

    _MAX_BRIEFING_ITEMS = 15

    @synchronized
    def _format_research_briefing(self, data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None

        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None

        updated_at = data.get("updated_at", "unknown")

        lines = []
        for item in items[: self._MAX_BRIEFING_ITEMS]:
            if not isinstance(item, dict):
                continue
            headline = item.get("headline", "")
            if not headline:
                continue
            category = item.get("category", "")
            source = item.get("source", "")
            tag = f"[{category}]" if category else ""
            src = f"({source})" if source else ""
            lines.append(f"- {tag} {headline} {src}".strip())

        if not lines:
            return None

        return (
            f"## World Briefing (updated: {updated_at})\n"
            + "\n".join(lines)
        )

    _MAX_FEED_ITEMS = 20

    @synchronized
    def _format_info_feed(self, data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None

        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None

        updated_at = data.get("updated_at", "unknown")

        lines = []
        for item in items[: self._MAX_FEED_ITEMS]:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            if not title:
                continue
            source = item.get("source", "")
            src = f"[{source}]" if source else ""
            lines.append(f"- {src} {title}".strip())

        if not lines:
            return None

        return (
            f"## Recent Feed (updated: {updated_at})\n"
            + "\n".join(lines)
        )

    @synchronized
    def get_all_memory_stats(self) -> Dict[str, Any]:
        stats = {}
        for team_id in self.list_teams():
            mem_dir = self.team_path(team_id) / "memory"
            if mem_dir.exists():
                files = [
                    f for f in mem_dir.iterdir()
                    if f.is_file() and not f.name.startswith(".")
                ]
                total_size = sum(f.stat().st_size for f in files)
                stats[team_id] = {
                    "files": len(files),
                    "total_bytes": total_size,
                    "keys": [f.name for f in files],
                }
        return stats

    @synchronized
    def clear_cache(self) -> None:
        self._file_cache.clear()
        self._prompt_cache.clear()
        self._dir_cache.clear()

    @synchronized
    def _stat_safe(self, path: Path) -> Optional[os.stat_result]:
        try:
            return path.stat()
        except OSError:
            return None

    @synchronized
    def _collect_prompt_dependency_stats(
        self, team_id: str, pi_id: str,
    ) -> Dict[str, float]:
        """Stat all files that build_system_prompt depends on."""
        paths: List[Path] = []

        self._validate_path_component(pi_id, "pi_id")
        team_dir = self.team_path(team_id)
        paths.append(team_dir / "pis" / pi_id / "soul.md")
        paths.append(team_dir / "soul.md")
        paths.append(self.base_path / "user.md")

        for source in self._enrichment_sources:
            if source.exclude_teams and team_id in source.exclude_teams:
                continue
            try:
                src_team_dir = self.team_path(source.source_team_id)
                paths.append(src_team_dir / "memory" / source.memory_key)
            except MemoryError:
                pass

        paths.append(team_dir / "memory")
        paths.append(team_dir / "memory" / ".overview.md")

        stats: Dict[str, float] = {}
        for p in paths:
            resolved = p.resolve()
            st = self._stat_safe(resolved)
            stats[str(resolved)] = st.st_mtime if st else 0.0
        return stats

    @synchronized
    def _invalidate_prompt_cache_for_team(self, team_id: str) -> None:
        keys_to_remove = [k for k in self._prompt_cache if k[0] == team_id]

        # A team that is an enrichment source also invalidates every team consuming it.
        for source in self._enrichment_sources:
            if source.source_team_id == team_id:
                keys_to_remove.extend(
                    k for k in self._prompt_cache
                    if k[0] != team_id
                    and (not source.exclude_teams or k[0] not in source.exclude_teams)
                )

        for k in set(keys_to_remove):
            self._prompt_cache.pop(k, None)

    @synchronized
    def _read_file(self, path: Path) -> str:
        resolved = path.resolve()
        st = self._stat_safe(resolved)
        if st is None:
            self._file_cache.pop(resolved, None)
            return ""
        cached = self._file_cache.get(resolved)
        if (cached is not None
                and not cached.is_json
                and cached.mtime == st.st_mtime
                and cached.size == st.st_size):
            return cached.content
        try:
            content = path.read_text(encoding="utf-8")
            self._file_cache[resolved] = _FileCacheEntry(
                mtime=st.st_mtime, size=st.st_size, content=content, is_json=False,
            )
            return content
        except Exception as e:
            logger.warning(f"Failed to read {path}: {e}")
            return ""

    @synchronized
    def _read_json(self, path: Path) -> Any:
        resolved = path.resolve()
        st = self._stat_safe(resolved)
        if st is None:
            self._file_cache.pop(resolved, None)
            return None
        cached = self._file_cache.get(resolved)
        if (cached is not None
                and cached.is_json
                and cached.mtime == st.st_mtime
                and cached.size == st.st_size):
            # Deep copy so callers cannot mutate cached data
            return copy.deepcopy(cached.content)
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            self._file_cache[resolved] = _FileCacheEntry(
                mtime=st.st_mtime, size=st.st_size, content=parsed, is_json=True,
            )
            return copy.deepcopy(parsed)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in {path}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Failed to read {path}: {e}")
            return None

    @synchronized
    def _write_json(self, path: Path, data: Any) -> None:
        self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))

    @synchronized
    def _write_file(self, path: Path, content: str) -> None:
        self._atomic_write(path, content)

    @synchronized
    def _atomic_write(self, path: Path, content: str) -> None:
        """Write content to path atomically via temp file + os.replace()."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            raise MemoryError(f"Failed to write {path}: {e}")
