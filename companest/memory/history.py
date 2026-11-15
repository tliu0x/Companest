"""File memory revisions and recoverable, team-local commits.

All methods run under MemoryManager's process-shared lock. A durable pending
commit is replayed before any managed read/write; model calls never hold the lock.
"""

import copy
import json
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from ..exceptions import CompanestError

CATEGORIES = {"fact", "task", "experience", "unclassified"}
PROVENANCE = ContextVar("memory_provenance", default={})


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class MemoryConflict(CompanestError):
    """The memory changed since the caller read it."""


@contextmanager
def memory_provenance(**fields):
    token = PROVENANCE.set({**PROVENANCE.get(), **fields})
    try:
        yield
    finally:
        PROVENANCE.reset(token)


def validate_metadata(fields):
    allowed = {"category", "importance", "tier", "summary", "tags", "derived_from", "scored_at", "pinned"}
    if set(fields) - allowed:
        raise ValueError(f"Cannot update fields: {set(fields) - allowed}")
    if "category" in fields and fields["category"] not in CATEGORIES:
        raise ValueError("category must be fact, task, experience or unclassified")
    if "tier" in fields and fields["tier"] not in {"short", "long"}:
        raise ValueError("tier must be short or long")
    if "pinned" in fields and type(fields["pinned"]) is not bool:
        raise ValueError("pinned must be boolean")
    if "importance" in fields:
        value = fields["importance"]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("importance must be between 0 and 1")
    if "summary" in fields and (not isinstance(fields["summary"], str) or len(fields["summary"]) > 4000):
        raise ValueError("summary must be text of at most 4000 characters")
    if "tags" in fields and (
        not isinstance(fields["tags"], list) or len(fields["tags"]) > 30
        or any(not isinstance(t, str) or len(t) > 100 for t in fields["tags"])
    ):
        raise ValueError("tags must be at most 30 strings of at most 100 characters")
    if "derived_from" in fields and (
        not isinstance(fields["derived_from"], list)
        or any(not isinstance(ref, dict) or set(ref) != {"key", "version"}
               or not isinstance(ref["key"], str) or type(ref["version"]) is not int or ref["version"] < 1
               for ref in fields["derived_from"])
    ):
        raise ValueError("derived_from must contain key/version references within this team")


class MemoryHistory:
    def __init__(self, manager):
        self.manager = manager

    def path(self, team_id, key, version=None):
        path = self.manager._memory_path(team_id, key, ".memory-history")
        if version is not None:
            if type(version) is not int or version < 1:
                raise ValueError("version must be a positive integer")
            path = path / f"{version}.json"
            self.manager._contained_path(self.manager.team_path(team_id), path)
        return path

    def _load(self, path):
        # History/journals fail closed on corruption; never silently lose revisions.
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def versions(self, team_id, key):
        path = self.path(team_id, key)
        return sorted(int(p.stem) for p in path.glob("*.json") if p.stem.isdecimal())

    def record(self, team_id, key, version):
        record = self._load(self.path(team_id, key, version))
        if record is not None and (record.get("team_id") != team_id or record.get("key") != key
                                   or record.get("version") != version):
            raise ValueError("Memory history identity mismatch")
        return record

    def file_metadata(self, team_id, key, source, path):
        """Reconstruct lost metadata from the last revision, or mark legacy data."""
        versions = self.versions(team_id, key)
        if versions:
            record = self.record(team_id, key, versions[-1])
            if record["state"] != source or record["data"] != self._load(path):
                raise MemoryConflict(f"Memory {key} was changed outside the versioned writer")
            return copy.deepcopy(record["metadata"])
        st = path.stat()
        when = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        meta = self.manager._new_meta(when, when, when, st.st_size)
        meta["version"] = 2 if source == "active" and self.manager._memory_path(team_id, key, "archive").exists() else 1
        return meta

    def current(self, team_id, key, source="active"):
        self.recover(team_id)
        area = "memory" if source == "active" else "archive"
        path = self.manager._memory_path(team_id, key, area)
        if not path.exists():
            return None
        index = self.manager._read_index(team_id) if source == "active" else self.manager._read_archive_index(team_id)
        meta = copy.deepcopy(index.get(key, {}))
        st = path.stat()
        now = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        meta = {**self.manager._new_meta(now, now, now, st.st_size), **meta}
        # Two pre-existing active/archive files with the same key are both retained.
        baseline = 2 if source == "active" and self.manager._memory_path(team_id, key, "archive").exists() else 1
        meta.setdefault("version", baseline)
        return {"team_id": team_id, "key": key, "version": meta["version"],
                "state": source, "metadata": meta, "data": self._load(path)}

    def _baselines(self, team_id, key):
        if self.versions(team_id, key):
            return []
        records = []
        for source in ("archive", "active"):
            record = self.current(team_id, key, source)
            if record:
                record.update(operation="import", recorded_at=record["metadata"]["updated_at"])
                record["metadata"]["provenance"] = {"kind": "legacy"}
                records.append(record)
        return records

    def prepare(self, team_id, key, *, operation, data=None, metadata=None, expected_version=None):
        self.recover(team_id)
        active = self.current(team_id, key)
        archived = self.current(team_id, key, "archive")
        versions = self.versions(team_id, key)
        current = active or archived or (self.record(team_id, key, versions[-1]) if versions else None)
        version = max(versions + [current["version"] if current else 0])
        if expected_version is not None and (type(expected_version) is not int or expected_version != version):
            raise MemoryConflict(f"Memory {key} changed: expected version {expected_version}, current {version}")
        if operation in {"archive", "delete", "metadata"} and not active:
            if operation == "metadata":
                raise ValueError(f"Key not in index: {key}")
            return []
        fields = dict(metadata or {})
        validate_metadata(fields)
        now = datetime.now(timezone.utc).isoformat()
        meta = copy.deepcopy(current["metadata"]) if current else self.manager._new_meta(now, now, now, 0)
        if operation in {"archive", "delete", "metadata"}:
            data = active["data"]
        else:
            meta.update(updated_at=now, summary="", tags=[], importance=0.0)
            if not current:
                meta["category"] = "task"
            # Content replacements invalidate old derived claims.
            meta.pop("derived_from", None)
            meta.pop("scored_at", None)
        meta.update(fields)
        if "category" in fields and "tier" not in fields:
            meta["tier"] = "long" if fields["category"] in {"fact", "experience"} else "short"
        meta.update(version=version + 1, modified_at=now,
                    provenance={"kind": "system", **PROVENANCE.get()},
                    size_bytes=len(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")))
        meta.pop("archived_at", None)
        state = "archive" if operation == "archive" else "deleted" if operation == "delete" else "active"
        if state == "archive":
            meta["archived_at"] = now
        record = {"team_id": team_id, "key": key, "version": version + 1, "state": state,
                  "operation": operation, "recorded_at": now, "metadata": meta, "data": data}
        return [*self._baselines(team_id, key), record]

    def commit(self, team_id, records):
        if not records:
            return
        self.recover(team_id)
        pending = self.manager._contained_path(self.manager.team_path(team_id),
                                               self.manager.team_path(team_id) / ".memory-pending.json")
        self.manager._write_json(pending, {"team_id": team_id, "records": records})
        self.recover(team_id)

    def recover(self, team_id):
        root = self.manager.team_path(team_id)
        pending = self.manager._contained_path(root, root / ".memory-pending.json")
        journal = self._load(pending)
        if journal is None:
            return
        if journal.get("team_id") != team_id or not isinstance(journal.get("records"), list):
            raise ValueError("Invalid memory commit journal")
        records = journal["records"]
        latest = {}
        # Validate all paths and identities before changing projections.
        for record in records:
            if record["team_id"] != team_id or record["state"] not in {"active", "archive", "deleted"}:
                raise ValueError("Invalid memory revision")
            self.path(team_id, record["key"], record["version"])
            for area in ("memory", "archive"):
                self.manager._memory_path(team_id, record["key"], area)
            latest[record["key"]] = record
        indexes = {}
        for area in ("memory", "archive"):
            index_path = self.manager._contained_path(root, root / area / ".index.json")
            indexes[area] = self._load(index_path) or {}
        for record in records:
            path = self.path(team_id, record["key"], record["version"])
            existing = self._load(path)
            if existing is not None and existing != record:
                raise MemoryConflict("Attempted to overwrite an immutable memory revision")
            if existing is None:
                self.manager._write_json(path, record)
        for key, record in latest.items():
            for area, state in (("memory", "active"), ("archive", "archive")):
                path = self.manager._memory_path(team_id, key, area)
                if record["state"] == state:
                    self.manager._write_json(path, record["data"])
                    indexes[area][key] = record["metadata"]
                else:
                    if path.exists():
                        path.unlink()
                        _sync_directory(path.parent)
                    indexes[area].pop(key, None)
        for area, index in indexes.items():
            self.manager._write_json(root / area / ".index.json", index)
        # Overview is derived data: don't inject stale claims after a revision.
        (root / "memory" / ".overview.md").unlink(missing_ok=True)
        _sync_directory(root / "memory")
        self.manager.clear_cache()
        pending.unlink()
        _sync_directory(root)
