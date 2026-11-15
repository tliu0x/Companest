"""Companest Cascade Engine  Dynamic Model Cascade System"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..orchestration.cost_gate import MODEL_PRICES

if TYPE_CHECKING:
    from ..memory import MemoryManager


def detect_language(text: str) -> str:
    """Dominant language of the first 200 chars: "en", "zh", "ja", "ko" or "multi"."""
    cjk = 0
    hiragana_katakana = 0
    hangul = 0
    total = 0

    for ch in text[:200]:
        if ch.isspace():
            continue
        total += 1
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            cjk += 1
        elif 0x3040 <= cp <= 0x30FF:
            hiragana_katakana += 1
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            hangul += 1

    if total == 0:
        return "en"
    if hiragana_katakana / total > 0.1:
        return "ja"
    if hangul / total > 0.1:
        return "ko"
    cjk_ratio = cjk / total
    if cjk_ratio > 0.3:
        return "zh"
    if cjk_ratio > 0.1:
        return "multi"
    return "en"


def _lang_penalty(model_id: str, lang: str) -> float:
    """Cost multiplier (>= 1.0) for running a `lang` task on this model."""
    if lang == "en":
        return 1.0
    langs = MODEL_PRICES.get(model_id, {}).get("langs")
    if not langs:
        return 1.5
    strength = langs.get(lang, langs.get("multi"))
    if strength is None:
        return 2.0
    return 1.0 + (1.0 - strength) * 0.5


@dataclass
class ModelTier:
    model_id: str
    provider: str
    cost_rank: float  # avg of input+output price per 1M tokens


def _detect_provider(model_id: str) -> str:
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
    """Tiers from MODEL_PRICES, cheapest first."""
    tiers = [
        ModelTier(model_id, _detect_provider(model_id), (prices["input"] + prices["output"]) / 2)
        for model_id, prices in MODEL_PRICES.items()
    ]
    tiers.sort(key=lambda t: t.cost_rank)
    return tiers


def build_cascade_chain(
    target_model: str,
    tiers: List[ModelTier],
    same_provider_only: bool = True,
    lang: str = "en",
) -> List[str]:
    """Models no pricier than the target, by language-adjusted cost, target last."""
    target = next((t for t in tiers if t.model_id == target_model), None)
    if target is None:
        return [target_model]
    cheaper = [
        t for t in tiers
        if t.model_id != target_model and t.cost_rank <= target.cost_rank
        and (not same_provider_only or t.provider == target.provider)
    ]
    ranks = {tier.model_id: tier.cost_rank for tier in tiers}
    cheaper.sort(key=lambda t: ranks[t.model_id] * _lang_penalty(t.model_id, lang))
    return [*dict.fromkeys(t.model_id for t in cheaper), target_model]


def get_downgrade(model_id: str, tiers: Optional[List[ModelTier]] = None) -> Optional[str]:
    """Priciest same-provider model that is cheaper than `model_id`."""
    if tiers is None:
        tiers = build_model_tiers()
    target = next((t for t in tiers if t.model_id == model_id), None)
    if target is None:
        return None
    cheaper = [t for t in tiers if t.provider == target.provider and t.cost_rank < target.cost_rank]
    return max(cheaper, key=lambda t: t.cost_rank).model_id if cheaper else None


@dataclass
class CascadeStrategy:
    COMPLEXITY_PATTERNS = {
        "trivial": (re.compile(r"^(hi|hello|hey|thanks|ok|yes|no)\b", re.I), 0.0),
        "analytical": (re.compile(r"analy[sz]e|compar[ei]|evaluat|assess|review", re.I), 0.6),
        "creative": (re.compile(r"write|draft|create|design|plan|strateg", re.I), 0.5),
        "reasoning": (re.compile(r"why|explain|reason|cause|proof|derive", re.I), 0.7),
        "coding": (re.compile(r"implement|refactor|debug|fix.*bug|code", re.I), 0.7),
        "multi_step": (re.compile(r"first.*then|step.by.step|and also|additionally", re.I), 0.8),
    }

    def estimate_complexity(self, task: str) -> float:
        """0.0-1.0 score from keyword patterns and task length."""
        scores = [score for pattern, score in self.COMPLEXITY_PATTERNS.values() if pattern.search(task)]
        length = len(task)
        if length > 500:
            scores.append(0.5)
        elif length > 200:
            scores.append(0.3)
        elif length < 20:
            scores.append(0.0)
        return max(scores, default=0.3)

    def pick_start_tier(
        self,
        task: str,
        chain: List[str],
        metrics: Optional["CascadeMetrics"] = None,
    ) -> int:
        if len(chain) <= 1:
            return 0
        if metrics and metrics.should_skip(chain[0]):
            return 1
        return 1 if self.estimate_complexity(task) > 0.5 and len(chain) > 2 else 0


@dataclass
class CascadeMetrics:
    """Per-team cascade stats, stored in team memory as cascade-metrics.json."""

    model_stats: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def record(self, model: str, succeeded: bool, quality: float = 1.0) -> None:
        stats = self.model_stats.setdefault(
            model, {"attempts": 0, "successes": 0, "total_quality": 0.0},
        )
        stats["attempts"] += 1
        if succeeded:
            stats["successes"] += 1
        stats["total_quality"] += quality

    def success_rate(self, model: str) -> float:
        """1.0 when there is no data."""
        stats = self.model_stats.get(model)
        if not stats or stats["attempts"] == 0:
            return 1.0
        return stats["successes"] / stats["attempts"]

    def should_skip(self, model: str, threshold: float = 0.15) -> bool:
        """True once a model has 5+ attempts and a success rate below threshold."""
        stats = self.model_stats.get(model)
        if not stats or stats["attempts"] < 5:
            return False
        return self.success_rate(model) < threshold

    @classmethod
    def load(cls, memory: "MemoryManager", team_id: str) -> "CascadeMetrics":
        data = memory.read_team_memory(team_id, "cascade-metrics.json")
        if data and isinstance(data, dict) and "model_stats" in data:
            return cls(model_stats=data["model_stats"])
        return cls()

    def save(self, memory: "MemoryManager", team_id: str) -> None:
        memory.write_team_memory(team_id, "cascade-metrics.json", {
            "model_stats": self.model_stats,
        }, category="task", pinned=True)


REFUSAL_PATTERNS = re.compile(
    r"(?i)^(I can\'t|I cannot|I\'m unable|I am unable|"
    r"I apologize but I cannot|I\'m not able|"
    r"I don\'t have the ability|Sorry, I can\'t)",
)

HEDGING_PATTERNS = re.compile(
    r"(?i)(I\'m not sure|I don\'t have access|"
    r"I cannot verify|I\'m not able to confirm|"
    r"I don\'t have enough information)",
)


class AdequacyChecker:
    def check(self, result: str, task: str) -> Tuple[bool, float]:
        """Returns (is_adequate, quality_score 0.0-1.0)."""
        stripped = result.strip()
        quality = 1.0

        if len(stripped) < 5:
            return False, 0.0
        if REFUSAL_PATTERNS.search(stripped[:300]):
            return False, 0.1
        if len(stripped) < 20 and len(task) > 50:
            return False, 0.2

        if HEDGING_PATTERNS.search(stripped[:500]):
            quality -= 0.3
        if len(task) > 100 and len(stripped) < len(task) * 0.3:
            quality -= 0.2

        task_words = set(w.lower() for w in re.findall(r'\b\w{4,}\b', task))
        if task_words and len(task) > 50:
            result_lower = stripped.lower()
            overlap = sum(1 for w in task_words if w in result_lower)
            if overlap / len(task_words) < 0.1:
                quality -= 0.2

        return quality >= 0.5, round(quality, 2)


class CascadeEngine:
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
        """Chain minus skip_models (never the target), started at the tier the strategy picks."""
        if lang is None:
            lang = detect_language(task)
        chain = self.get_chain(target_model, lang=lang)
        if skip_models:
            chain = [m for m in chain if m not in skip_models or m == target_model]
        return chain[self.strategy.pick_start_tier(task, chain, metrics):]

    def check_adequate(self, result: str, task: str) -> Tuple[bool, float]:
        return self.adequacy.check(result, task)
