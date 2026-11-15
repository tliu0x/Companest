import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .manager import MemoryManager
from ..exceptions import CompanestError
from ..agents.model_routing import detect_provider, resolve_model_endpoint

if TYPE_CHECKING:
    from ..config import ProxyConfig

logger = logging.getLogger(__name__)

_SCORE_PROMPT = """\
Score each memory entry's importance (0.0-1.0) and write a detailed summary (3-5 sentences).
The summary should capture key facts, decisions, and context  enough for someone to understand
the entry without reading the full content.
Higher scores for: actionable insights, user preferences, recurring patterns, key decisions.
Lower scores for: transient data, one-off queries, stale information.

Context: Team "{team_id}".
Entries:
{entries_json}

Return ONLY valid JSON (no markdown fences): {{"key": {{"importance": 0.85, "summary": "3-5 sentence summary"}}, ...}}"""

_COMPACT_PROMPT = """\
Merge these related memory entries into a single consolidated summary.
Preserve all important facts, decisions, and patterns. Drop redundant or outdated info.
The consolidated entry should be self-contained.

Entries:
{entries_json}

Return ONLY valid JSON (no markdown fences):
{{"content": <merged content object>, "summary": "one-line summary of the consolidated entry", "tags": ["tag1", "tag2"]}}"""

_DEEP_PROMPT = """\
These are long-term memory entries that have already been consolidated once.
Merge them into a denser, more structured summary. Preserve key facts and decisions.
Remove any remaining redundancy.

Entries:
{entries_json}

Return ONLY valid JSON (no markdown fences):
{{"content": <merged content object>, "summary": "one-line summary", "tags": ["tag1", "tag2"]}}"""

_OVERVIEW_PROMPT = """\
You are summarizing the long-term memory of team "{team_id}".
Given these memory entries with their summaries, write a concise overview (~500 tokens)
that captures the key facts, patterns, decisions, and user preferences.
This will be injected into the agent's system prompt as context.
Write in a structured, scannable format using short paragraphs or bullet groups.

Entries:
{entries_json}

Write the overview directly (no JSON wrapping, no markdown fences):"""


class DreamerError(CompanestError):
    pass


class Dreamer:
    SCORE_BATCH_SIZE = 15   # Max entries per LLM scoring call
    COMPACT_BATCH_SIZE = 10  # Max entries per compaction call

    def __init__(
        self,
        memory: MemoryManager,
        proxy_config: Optional["ProxyConfig"] = None,
        model: str = "deepseek-chat",
        short_tier_max_age_hours: int = 24,
        min_importance_to_keep: float = 0.3,
        max_access_for_gc: int = 2,
    ):
        self.memory = memory
        self.proxy_config = proxy_config
        self.model = model
        self.short_tier_max_age_hours = short_tier_max_age_hours
        self.min_importance_to_keep = min_importance_to_keep
        self.max_access_for_gc = max_access_for_gc

    async def run_all_dreams(self, dry_run: bool = False) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        for team_id in self.memory.list_teams():
            try:
                stats[team_id] = await self.run_team_dream(team_id, dry_run=dry_run)
            except Exception as e:
                logger.error(f"[Dreamer] Dream failed for {team_id}: {e}")
                stats[team_id] = {"error": str(e)}

        if not dry_run:
            self._write_dream_log(stats, "nightly")
        return stats

    async def run_team_dream(self, team_id: str, dry_run: bool = False) -> Dict[str, Any]:
        index = self.memory.get_memory_index(team_id)
        short_entries = {
            k: v for k, v in index.items()
            if v.get("tier", "short") == "short"
        }
        if not short_entries:
            return {"skipped": True, "reason": "no short-tier entries"}

        unscored = {
            k: v for k, v in short_entries.items()
            if v.get("importance", 0.0) == 0.0
        }

        cutoff = self._cutoff()
        expired_important_keys = []
        gc_candidate_keys = []
        for k, v in short_entries.items():
            if _expired(v, cutoff):
                if v.get("importance", 0.0) >= self.min_importance_to_keep:
                    expired_important_keys.append(k)
                elif v.get("access_count", 0) < self.max_access_for_gc:
                    gc_candidate_keys.append(k)

        if dry_run:
            return {
                "dry_run": True,
                "would_score": list(unscored.keys()),
                "would_compact": expired_important_keys,
                "would_gc": gc_candidate_keys,
                "score_batches": (len(unscored) + self.SCORE_BATCH_SIZE - 1) // self.SCORE_BATCH_SIZE if unscored else 0,
            }

        snapshot = self.memory.get_memory_index(team_id)
        try:
            stats: Dict[str, Any] = {"scored": 0, "compacted": 0, "gc_deleted": 0}

            if unscored:
                entry_contents = {}
                for key in unscored:
                    content = self.memory.read_team_memory(team_id, key)
                    if content is not None:
                        entry_contents[key] = content
                if entry_contents:
                    all_scores: Dict[str, float] = {}
                    for batch in _batch_dict(entry_contents, self.SCORE_BATCH_SIZE):
                        batch_scores = await self._score_importance(team_id, batch)
                        all_scores.update(batch_scores)
                    stats["scored"] = len(all_scores)

            # Re-read: scoring updated the index
            index = self.memory.get_memory_index(team_id)
            expired_important = {}
            for k, v in index.items():
                if v.get("tier") != "short":
                    continue
                if _expired(v, cutoff) and v.get("importance", 0.0) >= self.min_importance_to_keep:
                    content = self.memory.read_team_memory(team_id, k)
                    if content is not None:
                        expired_important[k] = content

            if expired_important:
                all_compacted = []
                for batch in _batch_dict(expired_important, self.COMPACT_BATCH_SIZE):
                    compacted = await self._compact_entries(team_id, batch)
                    all_compacted.extend(compacted)
                stats["compacted"] = len(all_compacted)

            gc_deleted = self._gc_expired(team_id)
            stats["gc_deleted"] = len(gc_deleted)
            stats["gc_keys"] = gc_deleted

            try:
                overview = await self._generate_overview(team_id)
                stats["overview_generated"] = overview is not None
            except Exception as e:
                logger.warning(f"[Dreamer] Overview generation failed for {team_id}: {e}")
                stats["overview_generated"] = False

            return stats

        except Exception:
            self._restore_index(team_id, snapshot)
            raise

    async def run_deep_consolidation(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        for team_id in self.memory.list_teams():
            try:
                index = self.memory.get_memory_index(team_id)
                long_entries = {
                    k: v for k, v in index.items()
                    if v.get("tier") == "long"
                }
                if len(long_entries) < 3:
                    stats[team_id] = {"skipped": True, "reason": "fewer than 3 long-tier entries"}
                    continue

                entry_contents = {}
                for key in long_entries:
                    content = self.memory.read_team_memory(team_id, key)
                    if content is not None:
                        entry_contents[key] = content

                if len(entry_contents) < 3:
                    stats[team_id] = {"skipped": True, "reason": "not enough readable entries"}
                    continue

                snapshot = self.memory.get_memory_index(team_id)
                try:
                    all_compacted = []
                    for batch in _batch_dict(entry_contents, self.COMPACT_BATCH_SIZE):
                        compacted = await self._compact_entries(team_id, batch, deep=True)
                        all_compacted.extend(compacted)
                    try:
                        await self._generate_overview(team_id)
                    except Exception as e:
                        logger.warning(f"[Dreamer] Overview generation failed for {team_id}: {e}")
                    stats[team_id] = {"merged": len(entry_contents), "into": len(all_compacted)}
                except Exception:
                    self._restore_index(team_id, snapshot)
                    raise

            except Exception as e:
                logger.error(f"[Dreamer] Deep consolidation failed for {team_id}: {e}")
                stats[team_id] = {"error": str(e)}
        self._write_dream_log(stats, "deep")
        return stats

    def _write_dream_log(self, stats: Dict[str, Any], kind: str) -> None:
        fields = {"scored", "compacted", "gc_deleted", "merged", "into",
                  "overview_generated", "skipped", "reason", "error"}
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "stats": {
                team: {key: value for key, value in result.items() if key in fields}
                for team, result in stats.items()
            },
        }
        path = self.memory.base_path / "dream_log.json"
        try:
            with self.memory._append_lock:
                history = self.memory._read_json(path)
                if not isinstance(history, list):
                    history = []
                self.memory._write_json(path, [*history[-99:], entry])
        except Exception as e:
            logger.warning("Failed to write dream log: %s", e)

    def _cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.short_tier_max_age_hours)

    def _restore_index(self, team_id: str, snapshot: Dict[str, dict]) -> None:
        logger.warning(f"[Dreamer] Restoring index snapshot for {team_id}")
        self.memory._write_index(team_id, snapshot)

    async def _score_importance(
        self, team_id: str, entries: Dict[str, Any],
    ) -> Dict[str, float]:
        prompt = _SCORE_PROMPT.format(team_id=team_id, entries_json=_entries_json(entries))

        response_text = await self._call_llm(prompt)
        try:
            scores = json.loads(response_text)
        except json.JSONDecodeError:
            logger.warning(f"[Dreamer] Failed to parse scoring response: {response_text[:200]}")
            return {}

        result: Dict[str, float] = {}
        for key, meta in scores.items():
            if not isinstance(meta, dict):
                continue
            importance = float(meta.get("importance", 0.0))
            summary = str(meta.get("summary", ""))
            result[key] = importance
            try:
                self.memory.update_entry_meta(
                    team_id, key,
                    importance=importance,
                    summary=summary,
                )
            except Exception as e:
                logger.debug(f"[Dreamer] Failed to update meta for {key}: {e}")

        return result

    async def _compact_entries(
        self, team_id: str, entries: Dict[str, Any], deep: bool = False,
    ) -> List[Dict[str, Any]]:
        """Merge entries into one long-tier entry and archive the originals."""
        template = _DEEP_PROMPT if deep else _COMPACT_PROMPT
        prompt = template.format(entries_json=_entries_json(entries))

        response_text = await self._call_llm(prompt)
        try:
            compacted = json.loads(response_text)
        except json.JSONDecodeError:
            logger.warning(
                f"[Dreamer] Failed to parse {'deep ' if deep else ''}compaction response: {response_text[:200]}"
            )
            return []

        if not isinstance(compacted, dict) or "content" not in compacted:
            return []

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        new_key = f"{'deep-' if deep else ''}consolidated-{ts}.json"

        self.memory.write_team_memory(team_id, new_key, compacted["content"])
        self.memory.update_entry_meta(
            team_id, new_key,
            tier="long",
            summary=compacted.get("summary", ""),
            tags=compacted.get("tags", []),
            importance=1.0,
        )

        for key in entries:
            self.memory.archive_team_memory(team_id, key)

        return [compacted]

    def _gc_expired(self, team_id: str) -> List[str]:
        """Archive expired short-tier entries with low importance and few accesses."""
        index = self.memory.get_memory_index(team_id)
        cutoff = self._cutoff()
        deleted: List[str] = []

        for key, meta in index.items():
            if meta.get("tier") != "short":
                continue
            if (
                _expired(meta, cutoff)
                and meta.get("importance", 0.0) < self.min_importance_to_keep
                and meta.get("access_count", 0) < self.max_access_for_gc
            ):
                self.memory.archive_team_memory(team_id, key)
                deleted.append(key)

        return deleted

    async def _generate_overview(self, team_id: str) -> Optional[str]:
        index = self.memory.get_memory_index(team_id)
        long_entries = {
            k: v for k, v in index.items()
            if v.get("tier") == "long" and v.get("summary")
        }
        if not long_entries:
            return None

        entries_json = json.dumps(
            {k: {"summary": v["summary"], "tags": v.get("tags", [])}
             for k, v in long_entries.items()},
            ensure_ascii=False, indent=2,
        )
        prompt = _OVERVIEW_PROMPT.format(team_id=team_id, entries_json=entries_json)
        overview = await self._call_llm(prompt)

        overview_path = self.memory.team_path(team_id) / "memory" / ".overview.md"
        self.memory._write_file(overview_path, overview)
        self.memory._invalidate_prompt_cache_for_team(team_id)
        return overview

    async def _call_llm(self, prompt: str) -> str:
        proxy_enabled = bool(self.proxy_config and self.proxy_config.enabled)
        if detect_provider(self.model, proxy_enabled) == "anthropic":
            return await self._call_llm_anthropic(prompt)
        return await self._call_llm_openai(prompt)

    async def _call_llm_anthropic(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError:
            raise DreamerError(
                "anthropic SDK not installed. Run: pip install anthropic"
            )

        kwargs: Dict[str, Any] = {}
        if self.proxy_config and self.proxy_config.enabled:
            kwargs["base_url"] = self.proxy_config.base_url.rstrip("/")
            kwargs["api_key"] = (
                self.proxy_config.default_key or self.proxy_config.master_key
            )

        client = anthropic.AsyncAnthropic(**kwargs)
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            parts = []
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    parts.append(block.text)
            return "\n".join(parts)
        finally:
            await client.close()

    async def _call_llm_openai(self, prompt: str) -> str:
        try:
            import openai
        except ImportError:
            raise DreamerError(
                "openai SDK not installed. Run: pip install openai"
            )

        endpoint = resolve_model_endpoint(self.model, self.proxy_config)

        kwargs: Dict[str, Any] = {}
        if endpoint.base_url:
            kwargs["base_url"] = endpoint.base_url
        if endpoint.api_key:
            kwargs["api_key"] = endpoint.api_key

        client = openai.AsyncOpenAI(**kwargs)
        try:
            response = await client.chat.completions.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            if not response.choices:
                raise DreamerError(
                    f"LLM returned empty choices for model '{self.model}' "
                    f"(provider: openai-compatible). "
                    f"The model may be overloaded or the request was filtered.",
                    details={"model": self.model},
                )
            return response.choices[0].message.content or ""
        finally:
            await client.close()


def _expired(meta: dict, cutoff: datetime) -> bool:
    try:
        updated = datetime.fromisoformat(meta.get("updated_at", ""))
    except (ValueError, TypeError):
        return False
    return updated < cutoff


def _entries_json(entries: Dict[str, Any]) -> str:
    return json.dumps(
        {k: _truncate_content(v) for k, v in entries.items()},
        ensure_ascii=False, indent=2,
    )


def _batch_dict(d: Dict[str, Any], batch_size: int) -> List[Dict[str, Any]]:
    items = list(d.items())
    return [
        dict(items[i:i + batch_size])
        for i in range(0, len(items), batch_size)
    ]


def _truncate_content(content: Any, max_chars: int = 2000) -> Any:
    if isinstance(content, str):
        return content[:max_chars] + ("..." if len(content) > max_chars else "")
    s = json.dumps(content, ensure_ascii=False)
    if len(s) <= max_chars:
        return content
    return s[:max_chars] + "..."
