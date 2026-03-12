"""
Companest Memory Manager

Hierarchical memory system for Pi Agent Teams:
- Master level: soul.md, identity.md, user.md, global memory/
- Shared level: shared/ (read-only for all teams)
- Team level: teams/{id}/memory/ (shared within team)
- Pi level: teams/{id}/pis/{pi_id}/ (soul.md per pi)

Responsibilities:
- Read/write memory files (JSON, Markdown)
- Build system prompts by composing soul + memory + context
- Provide MCP server for Pi memory tools
"""

import copy
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..exceptions import CompanestError

logger = logging.getLogger(__name__)


class MemoryError(CompanestError):
    """Raised for memory operation failures."""
    pass

# Keep MemoryError local since it shadows builtin  not in exceptions.py


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
ARCHIVE_INDEX_FILENAME = ".index.json"

# Token budget for long-term memory in system prompts
_MEMORY_TOKEN_BUDGET = 2000  # ~2000 tokens
_CHARS_PER_TOKEN = 4  # conservative estimate


@dataclass
class MemoryEntryMeta:
    """inode-like metadata for a single memory file."""
    created_at: str          # ISO 8601
    updated_at: str          # ISO 8601
    access_count: int = 0
    last_accessed: str = ""  # ISO 8601
    size_bytes: int = 0
    importance: float = 0.0  # 0.0-1.0, dreamer scores this
    tier: str = "short"      # "short" | "long"
    tags: List[str] = field(default_factory=list)
    summary: str = ""        # one-line summary from dreaming


@dataclass
class EnrichmentSource:
    """
    A data source that enriches system prompts.

    External projects can register custom enrichment sources
    to inject domain-specific context into Pi system prompts.
    """
    source_team_id: str
    memory_key: str
    section_title: str
    formatter: Callable[[Any], Optional[str]]
    exclude_teams: Optional[Set[str]] = field(default_factory=set)


class MemoryManager:
    """
    Hierarchical memory manager for Companest.

    Directory layout:
        base_path/
         soul.md
         identity.md
         user.md
         memory/
         shared/
         teams/{team_id}/
             soul.md
             memory/
             pis/{pi_id}/soul.md
    """

    # Allowed characters for path components (team_id, pi_id, key)
    _SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_./-]*$")

    def __init__(self, base_path: str = ".companest"):
        self.base_path = Path(base_path).resolve()
        self._append_lock = threading.Lock()
        self._enrichment_sources: List[EnrichmentSource] = []
        # Three-layer mtime-based cache
        self._file_cache: Dict[Path, _FileCacheEntry] = {}
        self._prompt_cache: Dict[Tuple[str, str], _PromptCacheEntry] = {}
        self._dir_cache: Dict[Path, _DirCacheEntry] = {}
        self._register_default_enrichments()
        if not self.base_path.exists():
            logger.warning(f"Memory base path does not exist: {self.base_path}")

    def _register_default_enrichments(self) -> None:
        """Register the two built-in enrichment sources."""
        self._enrichment_sources.append(EnrichmentSource(
            source_team_id="research",
            memory_key="briefing.json",
            section_title="World Briefing",
            formatter=self._format_research_briefing,
            exclude_teams={"research"},
        ))
        self._enrichment_sources.append(EnrichmentSource(
            source_team_id="info-collection",
            memory_key="feed.json",
            section_title="Recent Feed",
            formatter=self._format_info_feed,
            exclude_teams={"info-collection"},
        ))

    def register_enrichment(self, source: EnrichmentSource) -> None:
        """Register a custom enrichment source."""
        self._enrichment_sources.append(source)

    #  Master level 

    def read_master_soul(self) -> str:
        return self._read_file(self.base_path / "soul.md")

    def read_master_identity(self) -> str:
        return self._read_file(self.base_path / "identity.md")

    def read_master_user(self) -> str:
        return self._read_file(self.base_path / "user.md")

    def read_global_memory(self, key: str) -> Any:
        self._validate_path_component(key, "key")
        return self._read_json(self.base_path / "memory" / key)

    def write_global_memory(self, key: str, data: Any) -> None:
        self._validate_path_component(key, "key")
        path = self.base_path / "memory" / key
        self._write_json(path, data)
        self._file_cache.pop(path.resolve(), None)

    #  Path validation 

    def _validate_path_component(self, value: str, label: str) -> str:
        """Validate a path component to prevent path traversal."""
        if not value:
            raise MemoryError(f"Empty {label}")
        if ".." in value or value.startswith("/"):
            raise MemoryError(f"Invalid {label}: path traversal not allowed: {value!r}")
        if not self._SAFE_PATH_RE.match(value):
            raise MemoryError(f"Invalid {label}: {value!r} (alphanumeric, dash, underscore, dot only)")
        return value

    def _validate_resolved_path(self, path: Path) -> Path:
        """Ensure resolved path stays within base_path."""
        resolved = path.resolve()
        base_str = str(self.base_path)
        if not (resolved == self.base_path or str(resolved).startswith(base_str + os.sep)):
            raise MemoryError(f"Path escapes base directory: {path}")
        return resolved

    #  Shared level (read-only for teams) 

    def read_shared(self, key: str) -> Any:
        self._validate_path_component(key, "key")
        return self._read_json(self.base_path / "shared" / key)

    def write_shared(self, key: str, data: Any) -> None:
        self._validate_path_component(key, "key")
        path = self.base_path / "shared" / key
        self._write_json(path, data)
        self._file_cache.pop(path.resolve(), None)

    #  Company shared memory 

    def company_shared_path(self, company_id: str) -> Path:
        """Path to a company's shared memory directory."""
        self._validate_path_component(company_id, "company_id")
        p = self.base_path / "companies" / company_id / "shared"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def read_company_shared(self, company_id: str, key: str) -> Any:
        """Read from a company's shared memory namespace."""
        self._validate_path_component(key, "key")
        return self._read_json(self.company_shared_path(company_id) / key)

    def write_company_shared(self, company_id: str, key: str, data: Any) -> None:
        """Write to a company's shared memory namespace."""
        self._validate_path_component(key, "key")
        path = self.company_shared_path(company_id) / key
        self._write_json(path, data)
        self._file_cache.pop(path.resolve(), None)

    def list_company_shared(self, company_id: str) -> List[str]:
        """List keys in a company's shared memory."""
        d = self.company_shared_path(company_id)
        if not d.exists():
            return []
        return sorted(
            f.name for f in d.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    #  Team level 

    def team_path(self, team_id: str) -> Path:
        self._validate_path_component(team_id, "team_id")
        return self.base_path / "teams" / team_id

    def read_team_soul(self, team_id: str) -> str:
        return self._read_file(self.team_path(team_id) / "soul.md")

    def read_team_memory(self, team_id: str, key: str) -> Any:
        self._validate_path_component(key, "key")
        data = self._read_json(self.team_path(team_id) / "memory" / key)
        self._update_index_on_read(team_id, key)
        return data

    def write_team_memory(self, team_id: str, key: str, data: Any) -> None:
        self._validate_path_component(key, "key")
        path = self.team_path(team_id) / "memory" / key
        self._write_json(path, data)
        self._file_cache.pop(path.resolve(), None)
        self._dir_cache.pop(path.parent.resolve(), None)
        self._invalidate_prompt_cache_for_team(team_id)
        self._update_index_on_write(team_id, key, data)

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

    def team_exists(self, team_id: str) -> bool:
        return (self.team_path(team_id) / "team.md").exists()

    def list_teams(self) -> List[str]:
        teams_dir = self.base_path / "teams"
        if not teams_dir.exists():
            return []
        return [
            d.name for d in teams_dir.iterdir()
            if d.is_dir() and (d / "team.md").exists()
        ]

    #  Index management (inode-like metadata) 

    def _index_path(self, team_id: str) -> Path:
        """Return path to teams/{team_id}/memory/.index.json."""
        return self.team_path(team_id) / "memory" / INDEX_FILENAME

    def _read_index(self, team_id: str) -> Dict[str, dict]:
        """Read .index.json; rebuild from files if missing (lazy init)."""
        path = self._index_path(team_id)
        if not path.exists():
            mem_dir = self.team_path(team_id) / "memory"
            if not mem_dir.exists():
                return {}
            return self._rebuild_index(team_id)
        data = self._read_json(path)
        return data if isinstance(data, dict) else {}

    def _write_index(self, team_id: str, index: Dict[str, dict]) -> None:
        """Write .index.json and invalidate related caches."""
        path = self._index_path(team_id)
        self._write_json(path, index)
        self._file_cache.pop(path.resolve(), None)

    def _rebuild_index(self, team_id: str) -> Dict[str, dict]:
        """Rebuild index from actual memory files (migration path)."""
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
            index[f.name] = {
                "created_at": datetime.fromtimestamp(
                    st.st_ctime, tz=timezone.utc,
                ).isoformat(),
                "updated_at": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc,
                ).isoformat(),
                "access_count": 0,
                "last_accessed": now,
                "size_bytes": st.st_size,
                "importance": 0.0,
                "tier": "short",
                "tags": [],
                "summary": "",
            }
        self._write_index(team_id, index)
        return index

    def _update_index_on_write(self, team_id: str, key: str, data: Any) -> None:
        """Update index after a write operation."""
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
                index[key] = {
                    "created_at": now,
                    "updated_at": now,
                    "access_count": 0,
                    "last_accessed": now,
                    "size_bytes": size,
                    "importance": 0.0,
                    "tier": "short",
                    "tags": [],
                    "summary": "",
                }
            self._write_index(team_id, index)
        except Exception as e:
            logger.debug(f"Index update on write failed (non-blocking): {e}")

    def _update_index_on_read(self, team_id: str, key: str) -> None:
        """Increment access_count on read (best-effort)."""
        try:
            index = self._read_index(team_id)
            if key in index:
                index[key]["access_count"] = index[key].get("access_count", 0) + 1
                index[key]["last_accessed"] = datetime.now(timezone.utc).isoformat()
                self._write_index(team_id, index)
        except Exception:
            pass  # best-effort

    def _remove_from_index(self, team_id: str, key: str) -> None:
        """Remove a key from the index."""
        index = self._read_index(team_id)
        if key in index:
            del index[key]
            self._write_index(team_id, index)

    #  Public index API 

    def get_memory_index(self, team_id: str) -> Dict[str, dict]:
        """Return the full memory index for a team."""
        return self._read_index(team_id)

    def get_entry_meta(self, team_id: str, key: str) -> Optional[dict]:
        """Return inode metadata for a single memory entry."""
        index = self._read_index(team_id)
        return index.get(key)

    def delete_team_memory(self, team_id: str, key: str) -> None:
        """Delete a memory file and its index entry."""
        self._validate_path_component(key, "key")
        path = self.team_path(team_id) / "memory" / key
        if path.exists():
            path.unlink()
        self._file_cache.pop(path.resolve(), None)
        self._dir_cache.pop(path.parent.resolve(), None)
        self._remove_from_index(team_id, key)
        self._invalidate_prompt_cache_for_team(team_id)

    def archive_team_memory(self, team_id: str, key: str) -> None:
        """Move a memory file to archive/ and update indices."""
        self._validate_path_component(key, "key")
        src = self.team_path(team_id) / "memory" / key
        dst_dir = self.team_path(team_id) / "archive"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / key

        # Copy metadata to archive index
        meta = self.get_entry_meta(team_id, key)
        if meta:
            archive_index = self._read_archive_index(team_id)
            meta["archived_at"] = datetime.now(timezone.utc).isoformat()
            archive_index[key] = meta
            self._write_archive_index(team_id, archive_index)

        # Move file
        if src.exists():
            shutil.move(str(src), str(dst))

        # Clean up memory index + caches
        self._remove_from_index(team_id, key)
        self._file_cache.pop(src.resolve(), None)
        self._dir_cache.pop(src.parent.resolve(), None)
        self._invalidate_prompt_cache_for_team(team_id)

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
        # Sort by importance DESC, then by archived_at DESC
        results.sort(
            key=lambda r: (-r.get("importance", 0), r.get("archived_at", "")),
        )
        return results[:limit]

    def read_overview(self, team_id: str) -> str:
        """Read the .overview.md file for a team. Returns empty string if missing."""
        path = self.team_path(team_id) / "memory" / ".overview.md"
        return self._read_file(path)

    def _read_archive_index(self, team_id: str) -> Dict[str, dict]:
        """Read archive/.index.json for a team."""
        path = self.team_path(team_id) / "archive" / ARCHIVE_INDEX_FILENAME
        if not path.exists():
            return {}
        data = self._read_json(path)
        return data if isinstance(data, dict) else {}

    def _write_archive_index(self, team_id: str, index: Dict[str, dict]) -> None:
        """Write archive/.index.json for a team."""
        path = self.team_path(team_id) / "archive" / ARCHIVE_INDEX_FILENAME
        self._write_json(path, index)
        self._file_cache.pop(path.resolve(), None)

    def update_entry_meta(self, team_id: str, key: str, **fields) -> None:
        """Update specific fields of an index entry (importance, tier, summary, tags)."""
        allowed = {"importance", "tier", "summary", "tags"}
        invalid = set(fields) - allowed
        if invalid:
            raise MemoryError(f"Cannot update fields: {invalid}")
        index = self._read_index(team_id)
        if key not in index:
            raise MemoryError(f"Key not in index: {key}")
        index[key].update(fields)
        self._write_index(team_id, index)

    #  Pi level 

    def read_pi_soul(self, team_id: str, pi_id: str) -> str:
        self._validate_path_component(pi_id, "pi_id")
        return self._read_file(
            self.team_path(team_id) / "pis" / pi_id / "soul.md"
        )

    #  System prompt builder 

    def build_system_prompt(
        self, team_id: str, pi_id: str, company_context: Optional[str] = None,
    ) -> str:
        """
        Compose a system prompt for a Pi by layering:
        1. Pi's own soul.md
        2. Team's soul.md
        3. Company context (if provided  domain knowledge from CompanyConfig)
        4. Master's user.md (user context)
        5. Research briefing (if available, skipped for research team itself)
        6. Team memory summary (key names)

        Uses Layer 2 prompt cache: stat-checks all dependency files,
        returns cached prompt if none have changed.
        """
        # Include company_context in cache key to differentiate prompts
        cache_key = (team_id, pi_id)
        current_stats = self._collect_prompt_dependency_stats(team_id, pi_id)

        # When company_context is present, skip prompt cache (company context is dynamic)
        if not company_context:
            cached = self._prompt_cache.get(cache_key)
            if cached is not None and cached.dependency_mtimes == current_stats:
                return cached.prompt

        parts = []

        # Pi soul
        pi_soul = self.read_pi_soul(team_id, pi_id)
        if pi_soul:
            parts.append(pi_soul)

        # Team soul
        team_soul = self.read_team_soul(team_id)
        if team_soul:
            parts.append(f"## Team Context\n{team_soul}")

        # Company context (domain knowledge injected by orchestrator)
        if company_context:
            parts.append(f"## Company Context\n{company_context}")

        # User context
        user_md = self.read_master_user()
        if user_md:
            parts.append(f"## User\n{user_md}")

        # Enrichment sources (research briefing, info feed, custom)
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

        # Team memory keys (let Pi know what's available)
        mem_keys = self.list_team_memory(team_id)
        if mem_keys:
            keys_str = ", ".join(mem_keys)
            parts.append(
                f"## Available Memory\n"
                f"Team memory keys: {keys_str}\n"
                f"Use memory_read/memory_write tools to access."
            )

        # Memory overview + long-term summaries with token budget
        try:
            budget_chars = _MEMORY_TOKEN_BUDGET * _CHARS_PER_TOKEN

            # 1. Overview (always included if present, pre-budgeted ~500 tokens)
            overview = self.read_overview(team_id)
            if overview:
                parts.append(f"## Memory Overview\n{overview}")
                budget_chars -= len(overview)

            # 2. Individual long-tier summaries, ranked by importance
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

        prompt = "\n\n---\n\n".join(parts) if parts else ""
        # Re-collect stats after build: _read_index may have created
        # .index.json (lazy rebuild), changing the memory dir mtime.
        final_stats = self._collect_prompt_dependency_stats(team_id, pi_id)
        self._prompt_cache[cache_key] = _PromptCacheEntry(
            prompt=prompt,
            dependency_mtimes=final_stats,
        )
        return prompt

    #  Enrichment formatters 

    _MAX_BRIEFING_ITEMS = 15

    def _format_research_briefing(self, data: Any) -> Optional[str]:
        """Format research briefing data as a World Briefing section."""
        if not isinstance(data, dict):
            return None

        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None

        updated_at = data.get("updated_at", "unknown")

        # Cap at max items
        capped = items[: self._MAX_BRIEFING_ITEMS]

        lines = []
        for item in capped:
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

    def _format_info_feed(self, data: Any) -> Optional[str]:
        """Format info-collection feed data as a Recent Feed section."""
        if not isinstance(data, dict):
            return None

        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None

        updated_at = data.get("updated_at", "unknown")

        capped = items[: self._MAX_FEED_ITEMS]

        lines = []
        for item in capped:
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

    #  Scan all teams for archiver 

    def get_all_memory_stats(self) -> Dict[str, Any]:
        """Get memory stats for all teams (used by archiver)."""
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

    #  Cache management 

    def clear_cache(self) -> None:
        """Clear all three cache layers. Called on hot-reload and registry reload."""
        self._file_cache.clear()
        self._prompt_cache.clear()
        self._dir_cache.clear()

    def _stat_safe(self, path: Path) -> Optional[os.stat_result]:
        """stat() that returns None on missing/inaccessible files."""
        try:
            return path.stat()
        except (FileNotFoundError, OSError):
            return None

    def _collect_prompt_dependency_stats(
        self, team_id: str, pi_id: str,
    ) -> Dict[str, float]:
        """Stat all files that build_system_prompt depends on.

        Returns {resolved_path_str: mtime} for cache comparison.
        Missing files get mtime 0.0 so their appearance triggers invalidation.
        """
        paths: List[Path] = []

        # Pi soul, team soul, user.md
        self._validate_path_component(pi_id, "pi_id")
        team_dir = self.team_path(team_id)
        paths.append(team_dir / "pis" / pi_id / "soul.md")
        paths.append(team_dir / "soul.md")
        paths.append(self.base_path / "user.md")

        # Enrichment source files
        for source in self._enrichment_sources:
            if source.exclude_teams and team_id in source.exclude_teams:
                continue
            try:
                src_team_dir = self.team_path(source.source_team_id)
                paths.append(src_team_dir / "memory" / source.memory_key)
            except MemoryError:
                pass

        # Team memory directory (for key listing) + overview
        paths.append(team_dir / "memory")
        paths.append(team_dir / "memory" / ".overview.md")

        stats: Dict[str, float] = {}
        for p in paths:
            resolved = p.resolve()
            st = self._stat_safe(resolved)
            stats[str(resolved)] = st.st_mtime if st else 0.0
        return stats

    def _invalidate_prompt_cache_for_team(self, team_id: str) -> None:
        """Invalidate prompt cache entries affected by a team's memory write.

        Direct: invalidates all prompt cache entries for this team.
        Indirect: if this team is an enrichment source for other teams,
        invalidates those consuming teams' prompt caches too.
        """
        # Direct: remove all entries for this team_id
        keys_to_remove = [k for k in self._prompt_cache if k[0] == team_id]

        # Indirect: check if this team is an enrichment source
        for source in self._enrichment_sources:
            if source.source_team_id == team_id:
                # All teams that consume this source need invalidation
                # (i.e., all teams NOT in exclude_teams)
                keys_to_remove.extend(
                    k for k in self._prompt_cache
                    if k[0] != team_id
                    and (not source.exclude_teams or k[0] not in source.exclude_teams)
                )

        for k in set(keys_to_remove):
            self._prompt_cache.pop(k, None)

    #  Internal helpers 

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
            # Return a deep copy to prevent callers from mutating cached data
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

    def _write_json(self, path: Path, data: Any) -> None:
        self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))

    def _write_file(self, path: Path, content: str) -> None:
        self._atomic_write(path, content)

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write content to path atomically via temp file + os.replace().

        Ensures that concurrent readers never see a partially-written file.
        os.replace() is atomic on POSIX (same filesystem).
        """
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
                # Clean up temp file on any error
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            raise MemoryError(f"Failed to write {path}: {e}")
