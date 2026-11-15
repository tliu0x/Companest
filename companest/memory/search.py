import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .backend import MemoryBackend
from .history import CATEGORIES

logger = logging.getLogger(__name__)

_COMMON_QUERY_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "please",
    "review",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "with",
}

_WEIGHT_KEY = 0.95
_WEIGHT_TAG = 0.85
_WEIGHT_SUMMARY = 0.70


@dataclass
class SearchResult:
    key: str
    source: str  # "active" | "archive"
    score: float  # 0.0-1.0
    mode: str  # which mode produced this result
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, explain: bool = False) -> dict:
        data = {"key": self.key, "source": self.source, **self.meta}
        if explain:
            data["_score"] = round(self.score, 4)
            data["_mode"] = self.mode
        return data


class MemorySearchService:
    def __init__(self, backend: MemoryBackend) -> None:
        self._backend = backend

    def search(
        self,
        team_id: str,
        query: str,
        *,
        limit: int = 20,
        include_archive: bool = True,
        explain: bool = False,
        category: str | None = None,
    ) -> List[dict]:
        self.validate_category(category)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        logger.debug("memory_search team=%s query=%r limit=%d", team_id, query, limit)
        results = self._exact_search(team_id, query, include_archive=include_archive, category=category)

        seen: Dict[tuple, SearchResult] = {}
        for result in results:
            if category is not None and result.meta.get("category", "unclassified") != category:
                continue
            pair = (result.key, result.source)
            if pair not in seen or result.score > seen[pair].score:
                seen[pair] = result
        deduped = list(seen.values())
        deduped.sort(key=lambda result: result.score, reverse=True)

        return [result.to_dict(explain=explain) for result in deduped[:limit]]

    @staticmethod
    def validate_category(category):
        if category is not None and category not in CATEGORIES:
            raise ValueError("Unknown memory category")

    def retrieve_for_task(
        self,
        team_id: str,
        task: str,
        *,
        limit: int = 6,
        budget_chars: int = 2400,
        include_archive: bool = False,
    ) -> List[dict]:
        """Select prompt-ready memory snippets relevant to the current task."""
        task = task.strip()
        if not task or limit <= 0 or budget_chars <= 0:
            return []

        search_limit = min(max(limit * 3, 12), 100)
        candidates: Dict[tuple, dict] = {}

        for result in self.search(
            team_id,
            task,
            limit=search_limit,
            include_archive=include_archive,
            explain=True,
        ):
            self._merge_retrieval_candidate(candidates, result)

        term_limit = min(max(limit * 2, 8), 100)
        for term in self._task_terms(task):
            for result in self.search(
                team_id,
                term,
                limit=term_limit,
                include_archive=include_archive,
                explain=True,
            ):
                self._merge_retrieval_candidate(
                    candidates,
                    result,
                    matched_term=term,
                )

        ranked = sorted(
            candidates.values(),
            key=self._retrieval_rank,
            reverse=True,
        )

        selected: List[dict] = []
        used = 0
        for result in ranked:
            snippet = self._result_snippet(team_id, result)
            if not snippet:
                continue

            entry = {
                "key": result.get("key", ""),
                "source": result.get("source", "active"),
                "text": snippet,
                "summary": result.get("summary", ""),
                "tags": list(result.get("tags", []))
                if isinstance(result.get("tags"), list) else [],
                "importance": float(result.get("importance", 0.0) or 0.0),
                "score": float(result.get("_score", 0.0) or 0.0),
                "mode": result.get("_mode", "exact"),
                "matched_terms": list(result.get("_matched_terms", [])),
                "version": result.get("version", 1),
                "category": result.get("category", "unclassified"),
            }

            entry_size = self._estimate_prompt_size(entry)
            remaining = budget_chars - used
            if entry_size > remaining:
                if selected or remaining < 80:
                    break
                tags_len = sum(len(str(t)) for t in entry["tags"])
                fixed_overhead = len(entry["key"]) + tags_len + 48
                max_text = max(remaining - fixed_overhead, 0)
                entry["text"] = self._truncate_text(entry["text"], max_text)
                entry_size = self._estimate_prompt_size(entry)
                if not entry["text"] or entry_size > remaining:
                    break

            selected.append(entry)
            used += entry_size
            if len(selected) >= limit:
                break

        return selected

    def _exact_search(
        self,
        team_id: str,
        query: str,
        *,
        include_archive: bool = True,
        category: str | None = None,
    ) -> List[SearchResult]:
        """Keyword matching against key names, tags, and summaries."""
        query_lower = query.lower()
        results: List[SearchResult] = []

        index = self._backend.get_index(team_id)
        for key, meta in index.items():
            score = self._score_entry(query_lower, key, meta)
            if score <= 0:
                continue
            results.append(SearchResult(
                key=key,
                source="active",
                score=score,
                mode="exact",
                meta=dict(meta),
            ))

        if include_archive:
            for result in self._backend.search_archive(team_id, query, limit=100, category=category):
                key = result.pop("key", "")
                importance = result.get("importance", 0.0)
                base = max(
                    _WEIGHT_TAG if any(
                        query_lower in tag.lower() for tag in result.get("tags", [])
                    ) else 0,
                    _WEIGHT_SUMMARY if query_lower in result.get("summary", "").lower() else 0,
                )
                if base == 0:
                    base = _WEIGHT_SUMMARY
                score = base * (0.5 + 0.5 * importance)
                results.append(SearchResult(
                    key=key,
                    source="archive",
                    score=score,
                    mode="exact",
                    meta=result,
                ))

        return results

    def _score_entry(self, query_lower: str, key: str, meta: dict) -> float:
        base = 0.0

        key_lower = key.lower()
        key_stem = re.sub(r"\.[^.]+$", "", key_lower)
        if query_lower in key_lower or query_lower in key_stem:
            base = max(base, _WEIGHT_KEY)

        tags = meta.get("tags", [])
        if any(query_lower in tag.lower() for tag in tags):
            base = max(base, _WEIGHT_TAG)

        summary = meta.get("summary", "").lower()
        if query_lower in summary:
            base = max(base, _WEIGHT_SUMMARY)

        if base == 0.0:
            return 0.0

        importance = meta.get("importance", 0.0)
        return base * (0.5 + 0.5 * importance)

    def _task_terms(self, task: str) -> List[str]:
        """Extract a compact set of meaningful keywords from a task string."""
        terms: List[str] = []
        seen = set()
        for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", task.lower()):
            if (len(raw) < 3 and raw.isascii()) or raw in _COMMON_QUERY_TERMS or raw in seen:
                continue
            seen.add(raw)
            terms.append(raw)
            if len(terms) >= 8:
                break
        return terms

    def _merge_retrieval_candidate(
        self,
        candidates: Dict[tuple, dict],
        result: dict,
        *,
        matched_term: str = "",
    ) -> None:
        """Merge repeated hits from full-query and term-level retrieval."""
        pair = (result.get("key", ""), result.get("source", "active"))
        incoming = dict(result)
        incoming_terms = []
        if matched_term:
            incoming_terms.append(matched_term)
        incoming["_matched_terms"] = incoming_terms

        existing = candidates.get(pair)
        if existing is None:
            candidates[pair] = incoming
            return

        if float(incoming.get("_score", 0.0) or 0.0) > float(existing.get("_score", 0.0) or 0.0):
            existing["_score"] = incoming.get("_score", existing.get("_score", 0.0))
            existing["_mode"] = incoming.get("_mode", existing.get("_mode", "exact"))

        if not existing.get("summary") and incoming.get("summary"):
            existing["summary"] = incoming["summary"]
        if not existing.get("tags") and incoming.get("tags"):
            existing["tags"] = incoming["tags"]
        if not existing.get("importance") and incoming.get("importance"):
            existing["importance"] = incoming["importance"]

        terms = existing.setdefault("_matched_terms", [])
        for term in incoming_terms:
            if term not in terms:
                terms.append(term)

    @staticmethod
    def _retrieval_rank(result: dict) -> tuple:
        """Sort retrieval candidates by query-term coverage, score, and importance."""
        term_hits = len(result.get("_matched_terms", []))
        score = float(result.get("_score", 0.0) or 0.0)
        importance = float(result.get("importance", 0.0) or 0.0)
        return (score + (0.08 * min(term_hits, 5)), term_hits, importance)

    def _result_snippet(self, team_id: str, result: dict, *, max_chars: int = 420) -> str:
        """Return a prompt-friendly snippet for a search result."""
        summary = str(result.get("summary", "") or "").strip()
        if summary:
            return self._truncate_text(summary, max_chars)

        if result.get("source") != "active":
            return ""

        key = str(result.get("key", "") or "").strip()
        if not key:
            return ""

        try:
            content = self._backend.read(team_id, key)
        except Exception:
            return ""
        return self._stringify_for_prompt(content, max_chars=max_chars)

    @staticmethod
    def _stringify_for_prompt(value: Any, *, max_chars: int) -> str:
        """Convert arbitrary memory content into a compact single-line snippet."""
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except TypeError:
                text = str(value)
        text = re.sub(r"\s+", " ", text).strip()
        return MemorySearchService._truncate_text(text, max_chars)

    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        return text[: max_chars - 3].rstrip() + "..."

    @staticmethod
    def _estimate_prompt_size(entry: dict) -> int:
        """Rough char estimate for a formatted retrieval entry."""
        tags_len = sum(len(str(tag)) for tag in entry["tags"])
        return len(entry["key"]) + len(entry["text"]) + tags_len + 48
