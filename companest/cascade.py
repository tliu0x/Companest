"""
Companest Cascade Engine  Dynamic Model Cascade System

Replaces the hardcoded CASCADE_CHAINS in pi.py with a dynamic, configurable,
learning cascade engine.

Components:
- ModelTier: (model_id, provider, cost_rank) derived from MODEL_PRICES
- build_cascade_chain(): auto-derive cascade chain for any target model
- CascadeStrategy: task complexity  starting tier selection
- CascadeMetrics: per-(team, model) success/fail/escalation rates
- AdequacyChecker: pluggable quality gate (heuristic  semantic)
- CascadeEngine: orchestrates everything
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from .cost_gate import MODEL_PRICES

if TYPE_CHECKING:
    from .memory import MemoryManager

logger = logging.getLogger(__name__)


#  Language Detection & Affinity 

def detect_language(text: str) -> str:
    """Detect dominant language from Unicode ranges. Samples first 200 chars.

    Returns: "zh", "ja", "ko", "en", or "multi" (mixed CJK + Latin).
    """
    sample = text[:200]
    if not sample.strip():
        return "en"

    cjk = 0
    hiragana_katakana = 0
    hangul = 0
    total = 0

    for ch in sample:
        if ch.isspace():
            continue
        total += 1
        cp = ord(ch)
        # CJK Unified Ideographs (shared by zh/ja/ko but primarily zh)
        if 0x4E00 <= cp <= 0x9FFF:
            cjk += 1
        # Hiragana + Katakana  Japanese
        elif 0x3040 <= cp <= 0x30FF:
            hiragana_katakana += 1
        # Hangul  Korean
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            hangul += 1

    if total == 0:
        return "en"

    # Japanese: has hiragana/katakana (unique to Japanese)
    if hiragana_katakana / total > 0.1:
        return "ja"

    # Korean: has hangul
    if hangul / total > 0.1:
        return "ko"

    # Chinese: significant CJK without Japanese/Korean markers
    cjk_ratio = cjk / total
    if cjk_ratio > 0.3:
        return "zh"
    elif cjk_ratio > 0.1:
        return "multi"  # mixed CJK + Latin

    return "en"


def _lang_penalty(model_id: str, lang: str) -> float:
    """Language affinity penalty multiplier (>= 1.0).

    - lang == "en"  always 1.0 (backward compatible)
    - Model has exact lang match with strength s  1.0 + (1-s) * 0.5
    - Model has "multi" tag  use multi strength as fallback
    - No tag at all  1.5
    - Complete mismatch  2.0
    """
    if lang == "en":
        return 1.0

    prices = MODEL_PRICES.get(model_id)
    if not prices:
        return 1.5  # unknown model

    langs = prices.get("langs", {})
    if not langs:
        return 1.5

    # Exact language match
    if lang in langs:
        strength = langs[lang]
        return 1.0 + (1.0 - strength) * 0.5

    # Fallback to "multi" tag
    if "multi" in langs:
        strength = langs["multi"]
        return 1.0 + (1.0 - strength) * 0.5

    # No match at all
    return 2.0


#  ModelTier & auto-derived chains 

@dataclass
class ModelTier:
    """A model with its provider and cost ranking."""
    model_id: str
    provider: str
    cost_rank: float  # avg of input+output price per 1M tokens


def _detect_provider(model_id: str) -> str:
    """Detect provider from model ID. Pure function."""
    if model_id.startswith(("claude-", "anthropic/")):
        return "anthropic"
    elif model_id.startswith(("gpt-", "o3", "o4", "openai/")):
        return "openai"
    elif model_id.startswith("deepseek"):
        return "deepseek"
    elif model_id.startswith(("moonshot", "kimi")):
        return "moonshot"
    elif model_id.startswith(("glm-", "chatglm")):
        return "zhipu"
    elif model_id.startswith(("qwen", "qwq")):
        return "alibaba"
    elif model_id.startswith(("mistral", "llama", "gemma", "yi-")):
        return "openai"
    return "unknown"


def build_model_tiers() -> List[ModelTier]:
    """Build sorted tier list from MODEL_PRICES. Cheapest first."""
    tiers = []
    for model_id, prices in MODEL_PRICES.items():
        provider = _detect_provider(model_id)
        cost_rank = (prices["input"] + prices["output"]) / 2
        tiers.append(ModelTier(model_id=model_id, provider=provider, cost_rank=cost_rank))
    tiers.sort(key=lambda t: t.cost_rank)
    return tiers


def build_cascade_chain(
    target_model: str,
    tiers: List[ModelTier],
    same_provider_only: bool = True,
    lang: str = "en",
) -> List[str]:
    """Auto-derive cascade chain for a target model.

    Returns models from cheapest to target, filtered to same provider.
    With same_provider_only=False, allows cross-provider mixing.
    When lang != "en", models are sorted by cost_rank * lang_penalty
    so language-matching models rank cheaper (earlier in the chain).
    """
    # Find target tier
    target_tier = None
    for t in tiers:
        if t.model_id == target_model:
            target_tier = t
            break

    if target_tier is None:
        return [target_model]

    chain = []
    for t in tiers:
        if t.cost_rank > target_tier.cost_rank:
            continue
        if same_provider_only and t.provider != target_tier.provider:
            continue
        chain.append(t.model_id)

    # Sort by language-adjusted cost (lang penalty acts as multiplier)
    tier_map = {t.model_id: t.cost_rank for t in tiers}
    chain.sort(key=lambda m: tier_map.get(m, 0) * _lang_penalty(m, lang))

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for m in chain:
        if m not in seen:
            seen.add(m)
            deduped.append(m)

    # Ensure target is always last
    if target_model not in deduped:
        deduped.append(target_model)
    elif deduped[-1] != target_model:
        deduped.remove(target_model)
        deduped.append(target_model)

    return deduped


def get_downgrade(model_id: str, tiers: Optional[List[ModelTier]] = None) -> Optional[str]:
    """Get the next cheaper model (same provider). Replaces DOWNGRADE_MAP."""
    if tiers is None:
        tiers = build_model_tiers()

    target_tier = None
    for t in tiers:
        if t.model_id == model_id:
            target_tier = t
            break

    if target_tier is None:
        return None

    # Find the most expensive model that's cheaper than target, same provider
    best = None
    for t in tiers:
        if t.provider != target_tier.provider:
            continue
        if t.cost_rank >= target_tier.cost_rank:
            continue
        if t.model_id == model_id:
            continue
        if best is None or t.cost_rank > best.cost_rank:
            best = t

    return best.model_id if best else None


#  CascadeStrategy 

@dataclass
class CascadeStrategy:
    """Decides which tier to start from based on task analysis."""

    COMPLEXITY_PATTERNS = {
        "trivial": (re.compile(r"^(hi|hello|hey|thanks|ok|yes|no)\b", re.I), 0.0),
        "analytical": (re.compile(r"analy[sz]e|compar[ei]|evaluat|assess|review", re.I), 0.6),
        "creative": (re.compile(r"write|draft|create|design|plan|strateg", re.I), 0.5),
        "reasoning": (re.compile(r"why|explain|reason|cause|proof|derive", re.I), 0.7),
        "coding": (re.compile(r"implement|refactor|debug|fix.*bug|code", re.I), 0.7),
        "multi_step": (re.compile(r"first.*then|step.by.step|and also|additionally", re.I), 0.8),
    }

    def estimate_complexity(self, task: str) -> float:
        """0.0-1.0 complexity score. Combines pattern + length signals."""
        scores = []

        for _name, (pattern, score) in self.COMPLEXITY_PATTERNS.items():
            if pattern.search(task):
                scores.append(score)

        # Length signal: longer tasks tend to be more complex
        length = len(task)
        if length > 500:
            scores.append(0.5)
        elif length > 200:
            scores.append(0.3)
        elif length < 20:
            scores.append(0.0)

        if not scores:
            return 0.3  # default medium-low

        return min(max(scores), 1.0)

    def pick_start_tier(
        self,
        task: str,
        chain: List[str],
        metrics: Optional["CascadeMetrics"] = None,
    ) -> int:
        """Return index into chain to start from.

        - complexity < 0.2  start at index 0 (cheapest)
        - complexity 0.2-0.5  start at index 0
        - complexity > 0.5  skip to index 1+ (skip cheapest)
        - If metrics show cheapest always fails  skip it
        """
        if len(chain) <= 1:
            return 0

        complexity = self.estimate_complexity(task)

        # Check if metrics say to skip the cheapest model
        if metrics and metrics.should_skip(chain[0]):
            start = min(1, len(chain) - 1)
        elif complexity > 0.5 and len(chain) > 2:
            start = 1
        else:
            start = 0

        return start


#  CascadeMetrics 

@dataclass
class CascadeMetrics:
    """Tracks cascade performance per team. Stored in team memory."""

    # Key: model_id  {attempts, successes, total_quality}
    model_stats: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def record(self, model: str, succeeded: bool, quality: float = 1.0) -> None:
        """Record a cascade attempt result."""
        if model not in self.model_stats:
            self.model_stats[model] = {"attempts": 0, "successes": 0, "total_quality": 0.0}

        stats = self.model_stats[model]
        stats["attempts"] += 1
        if succeeded:
            stats["successes"] += 1
        stats["total_quality"] += quality

    def success_rate(self, model: str) -> float:
        """Get success rate for a model. Returns 1.0 if no data."""
        stats = self.model_stats.get(model)
        if not stats or stats["attempts"] == 0:
            return 1.0
        return stats["successes"] / stats["attempts"]

    def should_skip(self, model: str, threshold: float = 0.15) -> bool:
        """Skip model if success rate < threshold (historically inadequate).

        Requires at least 5 attempts before skipping to avoid premature decisions.
        """
        stats = self.model_stats.get(model)
        if not stats or stats["attempts"] < 5:
            return False
        return self.success_rate(model) < threshold

    @classmethod
    def load(cls, memory: "MemoryManager", team_id: str) -> "CascadeMetrics":
        """Load from teams/{team_id}/memory/cascade-metrics.json"""
        data = memory.read_team_memory(team_id, "cascade-metrics.json")
        if data and isinstance(data, dict) and "model_stats" in data:
            return cls(model_stats=data["model_stats"])
        return cls()

    def save(self, memory: "MemoryManager", team_id: str) -> None:
        """Persist to teams/{team_id}/memory/cascade-metrics.json"""
        memory.write_team_memory(team_id, "cascade-metrics.json", {
            "model_stats": self.model_stats,
        })


#  AdequacyChecker 

# Patterns that indicate the model refused or punted on the task.
REFUSAL_PATTERNS = re.compile(
    r"(?i)^(I can\'t|I cannot|I\'m unable|I am unable|"
    r"I apologize but I cannot|I\'m not able|"
    r"I don\'t have the ability|Sorry, I can\'t)",
)

# Patterns that indicate hedging / low confidence
HEDGING_PATTERNS = re.compile(
    r"(?i)(I\'m not sure|I don\'t have access|"
    r"I cannot verify|I\'m not able to confirm|"
    r"I don\'t have enough information)",
)


class AdequacyChecker:
    """Pluggable quality check  default heuristic, optional semantic."""

    def __init__(self):
        pass

    def check(self, result: str, task: str) -> Tuple[bool, float]:
        """Returns (is_adequate, quality_score 0.0-1.0)."""
        return self._heuristic_check(result, task)

    def _heuristic_check(self, result: str, task: str) -> Tuple[bool, float]:
        """Enhanced heuristic: length + refusal + relevance + hedging."""
        stripped = result.strip()
        quality = 1.0

        # Empty or near-empty response
        if len(stripped) < 5:
            return False, 0.0

        # Model refused the task
        if REFUSAL_PATTERNS.search(stripped[:300]):
            return False, 0.1

        # Suspiciously short for a non-trivial task
        if len(stripped) < 20 and len(task) > 50:
            return False, 0.2

        # Hedging detection  penalize but don't auto-fail
        if HEDGING_PATTERNS.search(stripped[:500]):
            quality -= 0.3

        # Length ratio: result should be meaningful relative to task
        if len(task) > 100 and len(stripped) < len(task) * 0.3:
            quality -= 0.2

        # Crude relevance: check if any significant task words appear in result
        task_words = set(
            w.lower() for w in re.findall(r'\b\w{4,}\b', task)
        )
        if task_words:
            result_lower = stripped.lower()
            overlap = sum(1 for w in task_words if w in result_lower)
            relevance = overlap / len(task_words) if task_words else 1.0
            if relevance < 0.1 and len(task) > 50:
                quality -= 0.2

        quality = max(quality, 0.0)
        is_adequate = quality >= 0.5
        return is_adequate, round(quality, 2)


#  CascadeEngine 

class CascadeEngine:
    """Replaces the cascade logic previously embedded in Pi.run().

    Auto-derives cascade chains from MODEL_PRICES (single source of truth).
    Supports task complexity estimation, historical learning, and pluggable
    adequacy checking.
    """

    def __init__(
        self,
        strategy: Optional[CascadeStrategy] = None,
        adequacy: Optional[AdequacyChecker] = None,
        cross_provider: bool = True,
    ):
        self.strategy = strategy or CascadeStrategy()
        self.adequacy = adequacy or AdequacyChecker()
        self.cross_provider = cross_provider
        self._tiers = build_model_tiers()

    def get_chain(self, target_model: str, lang: str = "en") -> List[str]:
        """Get full cascade chain for target model (auto-derived)."""
        return build_cascade_chain(
            target_model, self._tiers,
            same_provider_only=not self.cross_provider,
            lang=lang,
        )

    def get_effective_chain(
        self,
        target_model: str,
        task: str,
        metrics: Optional[CascadeMetrics] = None,
        skip_models: Optional[List[str]] = None,
        lang: Optional[str] = None,
    ) -> List[str]:
        """Chain with complexity-based start + historically-bad models skipped.

        If lang is None, auto-detects from task text.
        """
        if lang is None:
            lang = detect_language(task)
        chain = self.get_chain(target_model, lang=lang)

        # Remove explicitly skipped models (but never remove the target)
        if skip_models:
            chain = [m for m in chain if m not in skip_models or m == target_model]

        if len(chain) <= 1:
            return chain

        # Determine start index based on complexity + metrics
        start = self.strategy.pick_start_tier(task, chain, metrics)

        return chain[start:]

    def get_downgrade(self, model_id: str) -> Optional[str]:
        """Get the next cheaper model for a given model."""
        return get_downgrade(model_id, self._tiers)

    def check_adequate(self, result: str, task: str) -> Tuple[bool, float]:
        """Delegate to AdequacyChecker."""
        return self.adequacy.check(result, task)

    def estimate_cascade_cost(
        self,
        task: str,
        target_model: str,
        metrics: Optional[CascadeMetrics] = None,
        lang: Optional[str] = None,
    ) -> float:
        """Estimate expected cost across cascade chain.

        Uses success rates from metrics to weight expected cost per tier.
        Without metrics, assumes each tier has 50% chance of adequacy
        (except the last tier which always runs if reached).
        """
        chain = self.get_effective_chain(target_model, task, metrics, lang=lang)
        if not chain:
            return 0.0

        # Conservative token estimate (same as CostGate.estimate_cost)
        task_tokens = max(len(task) // 2, 10)
        est_input = task_tokens + 2000
        est_output = min(task_tokens * 3, 8192)

        total_cost = 0.0
        remaining_prob = 1.0

        for i, model in enumerate(chain):
            is_last = (i == len(chain) - 1)
            prices = MODEL_PRICES.get(model)
            if not prices:
                prices = {"input": 15.0, "output": 75.0}

            model_cost = (est_input * prices["input"] + est_output * prices["output"]) / 1_000_000

            if is_last:
                # Last tier always runs if reached
                total_cost += remaining_prob * model_cost
            else:
                # Probability of this tier being adequate
                if metrics:
                    success_rate = metrics.success_rate(model)
                else:
                    success_rate = 0.5

                total_cost += remaining_prob * model_cost
                remaining_prob *= (1 - success_rate)

        return round(total_cost, 6)
