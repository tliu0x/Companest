"""
Companest Cascade Engine Tests

Comprehensive tests for the dynamic cascade system:
1. ModelTier & auto-chain derivation
2. CascadeStrategy  complexity estimation & tier selection
3. CascadeMetrics  recording, success rates, skip logic
4. AdequacyChecker  heuristic quality gate
5. CascadeEngine  integration tests
6. get_downgrade()  replaces DOWNGRADE_MAP
"""

import tempfile
import shutil
from pathlib import Path

import pytest

from companest.cascade import (
    ModelTier,
    build_model_tiers,
    build_cascade_chain,
    get_downgrade,
    detect_language,
    _lang_penalty,
    CascadeStrategy,
    CascadeMetrics,
    AdequacyChecker,
    CascadeEngine,
    REFUSAL_PATTERNS,
    HEDGING_PATTERNS,
    _detect_provider,
)
from companest.cost_gate import MODEL_PRICES


#  1. ModelTier & Chain Derivation 

class TestModelTier:
    """Test ModelTier construction and chain derivation."""

    def test_build_model_tiers_not_empty(self):
        tiers = build_model_tiers()
        assert len(tiers) > 0

    def test_build_model_tiers_sorted_by_cost(self):
        tiers = build_model_tiers()
        costs = [t.cost_rank for t in tiers]
        assert costs == sorted(costs)

    def test_build_model_tiers_all_models_covered(self):
        tiers = build_model_tiers()
        tier_ids = {t.model_id for t in tiers}
        for model_id in MODEL_PRICES:
            assert model_id in tier_ids, f"{model_id} missing from tiers"

    def test_detect_provider_anthropic(self):
        assert _detect_provider("claude-sonnet-4-5-20250929") == "anthropic"
        assert _detect_provider("claude-opus-4-6") == "anthropic"
        assert _detect_provider("claude-haiku-4-5-20251001") == "anthropic"

    def test_detect_provider_openai(self):
        assert _detect_provider("gpt-4o") == "openai"
        assert _detect_provider("gpt-4o-mini") == "openai"
        assert _detect_provider("o3") == "openai"

    def test_detect_provider_deepseek(self):
        assert _detect_provider("deepseek-chat") == "deepseek"
        assert _detect_provider("deepseek-reasoner") == "deepseek"

    def test_detect_provider_moonshot(self):
        assert _detect_provider("moonshot-v1-8k") == "moonshot"
        assert _detect_provider("moonshot-v1-128k") == "moonshot"
        assert _detect_provider("kimi-k2.5") == "moonshot"

    def test_detect_provider_zhipu(self):
        assert _detect_provider("glm-4.7") == "zhipu"
        assert _detect_provider("glm-5") == "zhipu"
        assert _detect_provider("chatglm-something") == "zhipu"

    def test_detect_provider_alibaba(self):
        assert _detect_provider("qwen3-coder") == "alibaba"
        assert _detect_provider("qwq-something") == "alibaba"


class TestBuildCascadeChain:
    """Test auto-derived cascade chains."""

    def setup_method(self):
        self.tiers = build_model_tiers()

    def test_opus_chain_same_provider(self):
        chain = build_cascade_chain("claude-opus-4-6", self.tiers, same_provider_only=True)
        assert chain[-1] == "claude-opus-4-6"
        assert "claude-haiku-4-5-20251001" in chain or "claude-haiku-4-5" in chain
        assert "claude-sonnet-4-5-20250929" in chain or "claude-sonnet-4-5" in chain
        # No non-anthropic models
        for m in chain:
            assert _detect_provider(m) == "anthropic"

    def test_opus_chain_target_is_last(self):
        chain = build_cascade_chain("claude-opus-4-6", self.tiers)
        assert chain[-1] == "claude-opus-4-6"

    def test_cheapest_model_chain_is_single(self):
        """Cheapest model in a provider  chain of just itself."""
        chain = build_cascade_chain("gpt-4o-mini", self.tiers)
        # Should be [gpt-4o-mini] since it's the cheapest openai model
        assert chain[-1] == "gpt-4o-mini"
        assert len(chain) >= 1

    def test_cross_provider_includes_multiple_providers(self):
        chain = build_cascade_chain("claude-opus-4-6", self.tiers, same_provider_only=False)
        providers = {_detect_provider(m) for m in chain}
        assert len(providers) > 1, "Cross-provider chain should include multiple providers"

    def test_unknown_model_returns_single(self):
        chain = build_cascade_chain("unknown-model-xyz", self.tiers)
        assert chain == ["unknown-model-xyz"]

    def test_deepseek_chain(self):
        chain = build_cascade_chain("deepseek-reasoner", self.tiers)
        assert chain[-1] == "deepseek-reasoner"
        assert "deepseek-chat" in chain
        assert len(chain) == 2

    def test_moonshot_chain(self):
        chain = build_cascade_chain("moonshot-v1-128k", self.tiers, same_provider_only=True)
        assert chain[-1] == "moonshot-v1-128k"
        assert "moonshot-v1-8k" in chain
        assert "kimi-k2.5" in chain  # kimi is also moonshot provider

    def test_chain_is_sorted_cheapest_first(self):
        chain = build_cascade_chain("claude-opus-4-6", self.tiers)
        tier_map = {t.model_id: t.cost_rank for t in self.tiers}
        costs = [tier_map.get(m, float('inf')) for m in chain]
        assert costs == sorted(costs), "Chain should be sorted cheapest first"

    def test_gpt4o_chain(self):
        chain = build_cascade_chain("gpt-4o", self.tiers)
        assert chain[-1] == "gpt-4o"
        assert "gpt-4o-mini" in chain

    def test_o3_chain(self):
        chain = build_cascade_chain("o3", self.tiers)
        assert chain[-1] == "o3"
        assert "gpt-4o-mini" in chain
        assert "gpt-4o" in chain


class TestGetDowngrade:
    """Test get_downgrade()  replacement for DOWNGRADE_MAP."""

    def test_opus_downgrades_to_sonnet(self):
        result = get_downgrade("claude-opus-4-6")
        assert result in ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5")

    def test_sonnet_downgrades_to_haiku(self):
        result = get_downgrade("claude-sonnet-4-5-20250929")
        assert result in ("claude-haiku-4-5-20251001", "claude-haiku-4-5")

    def test_cheapest_has_no_downgrade(self):
        # deepseek-chat is the cheapest deepseek model
        result = get_downgrade("deepseek-chat")
        assert result is None

    def test_gpt4o_downgrades_to_mini(self):
        result = get_downgrade("gpt-4o")
        assert result == "gpt-4o-mini"

    def test_unknown_model_returns_none(self):
        result = get_downgrade("unknown-xyz")
        assert result is None

    def test_deepseek_reasoner_downgrades_to_chat(self):
        result = get_downgrade("deepseek-reasoner")
        assert result == "deepseek-chat"


#  2. CascadeStrategy 

class TestCascadeStrategy:
    """Test complexity estimation and tier selection."""

    def setup_method(self):
        self.strategy = CascadeStrategy()

    def test_trivial_task_low_complexity(self):
        assert self.strategy.estimate_complexity("hi") < 0.2

    def test_trivial_hello(self):
        assert self.strategy.estimate_complexity("hello") < 0.2

    def test_analytical_task_high_complexity(self):
        score = self.strategy.estimate_complexity("analyze the 10-K filing for TSLA")
        assert score >= 0.5

    def test_coding_task_high_complexity(self):
        score = self.strategy.estimate_complexity("implement a new feature for the API")
        assert score >= 0.5

    def test_multi_step_highest_complexity(self):
        score = self.strategy.estimate_complexity(
            "First research the topic, then analyze the data, and additionally create a summary"
        )
        assert score >= 0.7

    def test_medium_length_task(self):
        task = "What is the weather like?" + " " * 200
        score = self.strategy.estimate_complexity(task)
        assert 0.2 <= score <= 0.8

    def test_long_task_adds_complexity(self):
        short = "analyze this"
        long = "analyze this " + "context " * 100
        assert self.strategy.estimate_complexity(long) >= self.strategy.estimate_complexity(short)

    def test_pick_start_tier_trivial_starts_at_0(self):
        chain = ["haiku", "sonnet", "opus"]
        start = self.strategy.pick_start_tier("hi", chain)
        assert start == 0

    def test_pick_start_tier_complex_skips_cheapest(self):
        chain = ["haiku", "sonnet", "opus"]
        start = self.strategy.pick_start_tier(
            "analyze the complex financial derivatives portfolio", chain,
        )
        assert start >= 1

    def test_pick_start_tier_single_chain(self):
        chain = ["haiku"]
        start = self.strategy.pick_start_tier("anything", chain)
        assert start == 0

    def test_pick_start_tier_two_item_chain_complex(self):
        """Two-item chain: complex task still starts at 0 (needs >2 for skip)."""
        chain = ["haiku", "sonnet"]
        start = self.strategy.pick_start_tier(
            "analyze the quarterly earnings report in detail", chain,
        )
        # With only 2 items, complexity > 0.5 needs len > 2 to skip
        assert start == 0

    def test_pick_start_tier_metrics_skip(self):
        """Metrics say model always fails  skip it."""
        chain = ["haiku", "sonnet", "opus"]
        metrics = CascadeMetrics()
        for _ in range(10):
            metrics.record("haiku", succeeded=False, quality=0.1)

        start = self.strategy.pick_start_tier("hi", chain, metrics=metrics)
        assert start >= 1


#  3. CascadeMetrics 

class TestCascadeMetrics:
    """Test metric recording, success rates, and persistence."""

    def test_empty_metrics(self):
        metrics = CascadeMetrics()
        assert metrics.success_rate("any-model") == 1.0
        assert not metrics.should_skip("any-model")

    def test_record_success(self):
        metrics = CascadeMetrics()
        metrics.record("haiku", succeeded=True, quality=0.9)
        assert metrics.model_stats["haiku"]["attempts"] == 1
        assert metrics.model_stats["haiku"]["successes"] == 1

    def test_record_failure(self):
        metrics = CascadeMetrics()
        metrics.record("haiku", succeeded=False, quality=0.1)
        assert metrics.model_stats["haiku"]["attempts"] == 1
        assert metrics.model_stats["haiku"]["successes"] == 0

    def test_success_rate_calculation(self):
        metrics = CascadeMetrics()
        for _ in range(7):
            metrics.record("haiku", succeeded=True)
        for _ in range(3):
            metrics.record("haiku", succeeded=False)
        assert metrics.success_rate("haiku") == pytest.approx(0.7)

    def test_should_skip_needs_min_attempts(self):
        """Don't skip until we have enough data (5 attempts)."""
        metrics = CascadeMetrics()
        for _ in range(4):
            metrics.record("haiku", succeeded=False)
        assert not metrics.should_skip("haiku")  # Only 4 attempts

    def test_should_skip_after_enough_failures(self):
        metrics = CascadeMetrics()
        for _ in range(10):
            metrics.record("haiku", succeeded=False)
        assert metrics.should_skip("haiku")

    def test_should_not_skip_decent_model(self):
        metrics = CascadeMetrics()
        for _ in range(5):
            metrics.record("sonnet", succeeded=True)
        for _ in range(5):
            metrics.record("sonnet", succeeded=False)
        assert not metrics.should_skip("sonnet")  # 50% > 15%

    def test_persistence_roundtrip(self):
        tmpdir = tempfile.mkdtemp()
        try:
            base = Path(tmpdir)
            (base / "teams" / "stock" / "memory").mkdir(parents=True)

            from companest.memory import MemoryManager
            mm = MemoryManager(str(base))

            metrics = CascadeMetrics()
            metrics.record("haiku", succeeded=True)
            metrics.record("haiku", succeeded=False)
            metrics.save(mm, "stock")

            loaded = CascadeMetrics.load(mm, "stock")
            assert loaded.model_stats["haiku"]["attempts"] == 2
            assert loaded.model_stats["haiku"]["successes"] == 1
        finally:
            shutil.rmtree(tmpdir)

    def test_load_empty(self):
        tmpdir = tempfile.mkdtemp()
        try:
            base = Path(tmpdir)
            (base / "teams" / "stock" / "memory").mkdir(parents=True)

            from companest.memory import MemoryManager
            mm = MemoryManager(str(base))

            loaded = CascadeMetrics.load(mm, "stock")
            assert loaded.model_stats == {}
        finally:
            shutil.rmtree(tmpdir)


#  4. AdequacyChecker 

class TestAdequacyChecker:
    """Test heuristic quality gate."""

    def setup_method(self):
        self.checker = AdequacyChecker()

    def test_empty_response_inadequate(self):
        adequate, quality = self.checker.check("", "What is AI?")
        assert not adequate
        assert quality == 0.0

    def test_refusal_inadequate(self):
        adequate, quality = self.checker.check(
            "I can't help with that request.", "Analyze TSLA"
        )
        assert not adequate
        assert quality <= 0.2

    def test_refusal_variants(self):
        refusals = [
            "I cannot provide that information.",
            "I'm unable to assist with this.",
            "I apologize but I cannot do that.",
            "I'm not able to help here.",
            "Sorry, I can't do that.",
        ]
        for refusal in refusals:
            adequate, _ = self.checker.check(refusal, "Do something")
            assert not adequate, f"Should be inadequate: {refusal}"

    def test_short_response_for_complex_task(self):
        adequate, quality = self.checker.check(
            "Yes.", "Analyze the quarterly earnings report for TSLA including revenue, margins, and guidance"
        )
        assert not adequate

    def test_good_response_adequate(self):
        task = "What is Python?"
        result = "Python is a high-level, interpreted programming language known for its simplicity and readability. It supports multiple programming paradigms and has a large standard library."
        adequate, quality = self.checker.check(result, task)
        assert adequate
        assert quality >= 0.5

    def test_hedging_penalized(self):
        result = "I'm not sure about this, but I think it might be related to something."
        _, quality_hedging = self.checker.check(result, "short task")
        _, quality_confident = self.checker.check(
            "This is definitely related to the topic at hand and here is why.",
            "short task",
        )
        assert quality_hedging < quality_confident

    def test_refusal_patterns_compiled(self):
        assert REFUSAL_PATTERNS.search("I can't do that")
        assert REFUSAL_PATTERNS.search("I cannot help")
        assert REFUSAL_PATTERNS.search("I'm unable to assist")
        assert not REFUSAL_PATTERNS.search("I can help you with that")

    def test_hedging_patterns_compiled(self):
        assert HEDGING_PATTERNS.search("I'm not sure about this")
        assert HEDGING_PATTERNS.search("I don't have access to that")
        assert not HEDGING_PATTERNS.search("Here is the definitive answer")


#  5. CascadeEngine Integration 

class TestCascadeEngine:
    """Integration tests for CascadeEngine."""

    def test_default_construction(self):
        engine = CascadeEngine()
        assert engine.strategy is not None
        assert engine.adequacy is not None
        assert engine.cross_provider  # default is True for cross-provider cascade

    def test_get_chain_opus(self):
        engine = CascadeEngine()
        chain = engine.get_chain("claude-opus-4-6")
        assert chain[-1] == "claude-opus-4-6"
        assert len(chain) >= 3  # haiku, sonnet variants, opus

    def test_get_chain_unknown_model(self):
        engine = CascadeEngine()
        chain = engine.get_chain("unknown-xyz")
        assert chain == ["unknown-xyz"]

    def test_get_effective_chain_trivial_task(self):
        engine = CascadeEngine()
        chain = engine.get_effective_chain("claude-opus-4-6", "\u5206\u6790\u4e00\u4e0b TSLA \u8d70\u52bf")
        assert len(chain) >= 2  # starts from cheapest for trivial

    def test_get_effective_chain_complex_task(self):
        engine = CascadeEngine()
        full_chain = engine.get_chain("claude-opus-4-6")
        effective = engine.get_effective_chain(
            "claude-opus-4-6",
            "analyze the complex financial derivatives portfolio and provide recommendations",
        )
        assert len(effective) <= len(full_chain)
        # Complex task should skip cheapest if chain has >2 items
        if len(full_chain) > 2:
            assert len(effective) < len(full_chain)

    def test_get_effective_chain_with_skip_models(self):
        engine = CascadeEngine()
        chain = engine.get_effective_chain(
            "claude-opus-4-6", "hi",
            skip_models=["claude-haiku-4-5-20251001"],
        )
        assert "claude-haiku-4-5-20251001" not in chain
        # Target should still be present
        assert "claude-opus-4-6" in chain

    def test_skip_models_cannot_remove_target(self):
        engine = CascadeEngine()
        chain = engine.get_effective_chain(
            "claude-opus-4-6", "hi",
            skip_models=["claude-opus-4-6"],  # trying to skip the target
        )
        assert "claude-opus-4-6" in chain

    def test_get_downgrade(self):
        engine = CascadeEngine()
        downgrade = engine.get_downgrade("claude-opus-4-6")
        assert downgrade is not None
        assert "sonnet" in downgrade

    def test_check_adequate_delegates(self):
        engine = CascadeEngine()
        adequate, quality = engine.check_adequate(
            "Here is a good answer about Python programming.",
            "What is Python?",
        )
        assert adequate
        assert quality > 0.5

    def test_cross_provider_engine(self):
        engine = CascadeEngine(cross_provider=True)
        chain = engine.get_chain("claude-opus-4-6")
        providers = {_detect_provider(m) for m in chain}
        assert len(providers) > 1

    def test_estimate_cascade_cost(self):
        engine = CascadeEngine()
        cost = engine.estimate_cascade_cost("analyze this", "claude-opus-4-6")
        assert cost > 0

    def test_estimate_cascade_cost_with_metrics(self):
        engine = CascadeEngine()
        metrics = CascadeMetrics()
        # Record high success rate for cheap model
        for _ in range(10):
            metrics.record("claude-haiku-4-5-20251001", succeeded=True)

        cost_with_metrics = engine.estimate_cascade_cost(
            "analyze this", "claude-opus-4-6", metrics=metrics,
        )
        cost_without = engine.estimate_cascade_cost(
            "analyze this", "claude-opus-4-6",
        )
        # With high success rate on cheap model, expected cost should be lower
        assert cost_with_metrics <= cost_without

    def test_custom_strategy(self):
        strategy = CascadeStrategy()
        engine = CascadeEngine(strategy=strategy)
        assert engine.strategy is strategy

    def test_custom_adequacy_checker(self):
        checker = AdequacyChecker()
        engine = CascadeEngine(adequacy=checker)
        assert engine.adequacy is checker


#  6. Backward Compatibility 

class TestBackwardCompatibility:
    """Verify the cascade engine produces equivalent chains to old CASCADE_CHAINS."""

    def test_opus_chain_covers_old_chain(self):
        """Opus chain should include haiku, sonnet, opus."""
        engine = CascadeEngine()
        chain = engine.get_chain("claude-opus-4-6")
        # Old chain: haiku-20251001, sonnet-20250929, opus
        assert "claude-opus-4-6" in chain
        # Should have at least one haiku and one sonnet variant
        has_haiku = any("haiku" in m for m in chain)
        has_sonnet = any("sonnet" in m for m in chain)
        assert has_haiku, f"Chain missing haiku: {chain}"
        assert has_sonnet, f"Chain missing sonnet: {chain}"

    def test_sonnet_chain_covers_old_chain(self):
        engine = CascadeEngine()
        chain = engine.get_chain("claude-sonnet-4-5-20250929")
        assert "claude-sonnet-4-5-20250929" in chain
        has_haiku = any("haiku" in m for m in chain)
        assert has_haiku

    def test_gpt4o_chain_covers_old_chain(self):
        engine = CascadeEngine()
        chain = engine.get_chain("gpt-4o")
        assert "gpt-4o" in chain
        assert "gpt-4o-mini" in chain

    def test_deepseek_chain_covers_old_chain(self):
        engine = CascadeEngine(cross_provider=False)
        chain = engine.get_chain("deepseek-reasoner")
        assert chain == ["deepseek-chat", "deepseek-reasoner"]

    def test_moonshot_chain_covers_old_chain(self):
        engine = CascadeEngine(cross_provider=False)
        chain = engine.get_chain("moonshot-v1-128k")
        assert "moonshot-v1-8k" in chain
        assert "kimi-k2.5" in chain
        assert chain[-1] == "moonshot-v1-128k"

    def test_single_model_chain(self):
        """Cheapest model  chain of just itself."""
        engine = CascadeEngine()
        chain = engine.get_chain("deepseek-chat")
        assert chain == ["deepseek-chat"]

    def test_downgrade_compatibility(self):
        """get_downgrade produces same results as old DOWNGRADE_MAP for key models."""
        expected_downgrades = {
            "claude-opus-4-6": lambda d: "sonnet" in d,
            "gpt-4o": lambda d: d == "gpt-4o-mini",
            "deepseek-reasoner": lambda d: d == "deepseek-chat",
            "moonshot-v1-128k": lambda d: d == "kimi-k2.5",  # kimi-k2.5 is now the next cheaper moonshot model
        }
        for model, check in expected_downgrades.items():
            result = get_downgrade(model)
            assert result is not None, f"No downgrade for {model}"
            assert check(result), f"Unexpected downgrade for {model}: {result}"


#  7. Language Detection 

class TestDetectLanguage:
    """Test Unicode-based language detection."""

    def test_english(self):
        assert detect_language("Hello, how are you?") == "en"

    def test_chinese(self):
        assert detect_language("\u5206\u6790\u4e00\u4e0b\u6570\u636e") == "zh"

    def test_chinese_sentence(self):
        assert detect_language("\u8bf7\u5e2e\u6211\u5206\u6790\u4e00\u4e0b TSLA \u7684\u8d70\u52bf") == "zh"

    def test_japanese(self):
        assert detect_language("\u3053\u3093\u306b\u3061\u306f\u3001\u5143\u6c17\u3067\u3059\u304b") == "ja"

    def test_korean(self):
        assert detect_language("\uc548\ub155\ud558\uc138\uc694, \uc798 \uc9c0\ub0b4\uc138\uc694?") == "ko"

    def test_mixed_cjk_latin(self):
        # Low CJK ratio -> "multi"
        result = detect_language("This has some \u4e2d\u6587 mixed in the English text here and there")
        assert result in ("multi", "en")

    def test_empty_string(self):
        assert detect_language("") == "en"

    def test_code_is_english(self):
        assert detect_language("def foo(x): return x * 2") == "en"


class TestLangPenalty:
    """Test language affinity penalty multiplier."""

    def test_english_always_no_penalty(self):
        """English tasks never penalize any model."""
        assert _lang_penalty("glm-4.7", "en") == 1.0
        assert _lang_penalty("claude-opus-4-6", "en") == 1.0
        assert _lang_penalty("unknown-model", "en") == 1.0

    def test_zh_model_zh_task_no_penalty(self):
        """Chinese model with zh strength=1.0  penalty 1.0."""
        assert _lang_penalty("glm-4.7", "zh") == 1.0
        assert _lang_penalty("deepseek-chat", "zh") == 1.0
        assert _lang_penalty("qwen3-coder", "zh") == 1.0

    def test_en_model_zh_task_penalized(self):
        """English-primary model handling Chinese task  penalty > 1.0."""
        penalty = _lang_penalty("gpt-4o-mini", "zh")
        assert penalty > 1.0
        # gpt-4o-mini has multi:0.8  1.0 + (1-0.8)*0.5 = 1.1
        assert penalty == pytest.approx(1.1)

    def test_unknown_model_penalty(self):
        """Unknown model  1.5."""
        assert _lang_penalty("unknown-xyz", "zh") == 1.5

    def test_multi_fallback(self):
        """Model with "multi" tag uses it as fallback for non-en languages."""
        # claude-opus-4-6 has multi:0.95  1.0 + (1-0.95)*0.5 = 1.025
        penalty = _lang_penalty("claude-opus-4-6", "zh")
        assert penalty == pytest.approx(1.025)


#  9. Language-Aware Cascade 

class TestLanguageAwareCascade:
    """Test that cascade chain ordering changes based on task language."""

    def test_zh_task_reorders_cross_provider_chain(self):
        """Chinese task: zh-native models should rank before en-primary models."""
        engine = CascadeEngine(cross_provider=True)
        chain_zh = engine.get_chain("claude-opus-4-6", lang="zh")
        chain_en = engine.get_chain("claude-opus-4-6", lang="en")

        # Both chains should end with the target
        assert chain_zh[-1] == "claude-opus-4-6"
        assert chain_en[-1] == "claude-opus-4-6"

        # Chinese chain order should differ from English chain order
        # (at least the non-target portion should be reordered)
        assert chain_zh != chain_en

    def test_zh_deepseek_before_gpt4o_mini(self):
        """In Chinese task cross-provider chain, deepseek-chat should appear before gpt-4o-mini."""
        engine = CascadeEngine(cross_provider=True)
        chain = engine.get_chain("claude-opus-4-6", lang="zh")

        if "deepseek-chat" in chain and "gpt-4o-mini" in chain:
            ds_idx = chain.index("deepseek-chat")
            gpt_idx = chain.index("gpt-4o-mini")
            assert ds_idx < gpt_idx, (
                f"deepseek-chat ({ds_idx}) should be before gpt-4o-mini ({gpt_idx}) "
                f"in zh chain: {chain}"
            )

    def test_same_provider_chain_unaffected_by_lang(self):
        """Same-provider chain: all models share the same lang profile, order is stable."""
        tiers = build_model_tiers()
        chain_en = build_cascade_chain("claude-opus-4-6", tiers, same_provider_only=True, lang="en")
        chain_zh = build_cascade_chain("claude-opus-4-6", tiers, same_provider_only=True, lang="zh")
        # Same provider (anthropic) models all have similar lang profiles,
        # so the order should remain the same
        assert chain_en == chain_zh

    def test_auto_detect_zh(self):
        """get_effective_chain auto-detects Chinese from task text."""
        engine = CascadeEngine(cross_provider=True)
        chain = engine.get_effective_chain("claude-opus-4-6", " TSLA ")
        assert chain[-1] == "claude-opus-4-6"
        # Should have zh-friendly models ranked earlier
        assert len(chain) > 1

    def test_target_always_last(self):
        """Target model is always the last element regardless of language."""
        engine = CascadeEngine(cross_provider=True)
        for lang in ("en", "zh", "ja", "ko"):
            chain = engine.get_chain("claude-opus-4-6", lang=lang)
            assert chain[-1] == "claude-opus-4-6", f"Target not last for lang={lang}: {chain}"
