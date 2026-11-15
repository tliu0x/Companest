"""Companest Cascade Engine Tests"""

import pytest

from companest.agents.cascade import (
    build_model_tiers,
    build_cascade_chain,
    get_downgrade,
    detect_language,
    _lang_penalty,
    CascadeStrategy,
    CascadeMetrics,
    AdequacyChecker,
    CascadeEngine,
    _detect_provider,
)
from companest.orchestration.cost_gate import MODEL_PRICES
from companest.memory import MemoryManager

OPUS = "claude-opus-4-6"
HAIKU = "claude-haiku-4-5-20251001"
ZH_TASK = "分析一下 TSLA 走势"


class TestModelTier:
    def test_build_model_tiers_sorted_and_complete(self):
        tiers = build_model_tiers()
        costs = [t.cost_rank for t in tiers]
        assert costs == sorted(costs)
        assert {t.model_id for t in tiers} == set(MODEL_PRICES)

    @pytest.mark.parametrize("model_id,provider", [
        ("claude-sonnet-4-5-20250929", "anthropic"),
        ("gpt-4o-mini", "openai"),
        ("o3", "openai"),
        ("deepseek-reasoner", "deepseek"),
        ("moonshot-v1-8k", "moonshot"),
        ("kimi-k2.5", "moonshot"),
        ("glm-4.7", "zhipu"),
        ("chatglm-something", "zhipu"),
        ("qwen3-coder", "alibaba"),
        ("qwq-something", "alibaba"),
    ])
    def test_detect_provider(self, model_id, provider):
        assert _detect_provider(model_id) == provider


class TestBuildCascadeChain:
    def setup_method(self):
        self.tiers = build_model_tiers()

    def test_opus_chain_same_provider(self):
        chain = build_cascade_chain(OPUS, self.tiers, same_provider_only=True)
        assert chain[-1] == OPUS
        assert HAIKU in chain or "claude-haiku-4-5" in chain
        assert "claude-sonnet-4-5-20250929" in chain or "claude-sonnet-4-5" in chain
        for m in chain:
            assert _detect_provider(m) == "anthropic"

    @pytest.mark.parametrize("target,cheaper", [
        ("claude-sonnet-4-5-20250929", [HAIKU]),
        ("moonshot-v1-128k", ["moonshot-v1-8k", "kimi-k2.5"]),
        ("gpt-4o", ["gpt-4o-mini"]),
        ("o3", ["gpt-4o-mini", "gpt-4o"]),
    ])
    def test_chain_includes_cheaper_same_provider_models(self, target, cheaper):
        chain = build_cascade_chain(target, self.tiers)
        assert chain[-1] == target
        assert set(cheaper) <= set(chain)

    def test_unknown_model_returns_single(self):
        assert build_cascade_chain("unknown-model-xyz", self.tiers) == ["unknown-model-xyz"]

    def test_chain_deduplicates_custom_tiers(self):
        tiers = [tier for tier in self.tiers if tier.model_id in {HAIKU, OPUS}]
        assert build_cascade_chain(OPUS, tiers + tiers) == [HAIKU, OPUS]

    def test_chain_is_sorted_cheapest_first(self):
        chain = build_cascade_chain(OPUS, self.tiers)
        tier_map = {t.model_id: t.cost_rank for t in self.tiers}
        costs = [tier_map.get(m, float('inf')) for m in chain]
        assert costs == sorted(costs)

    def test_engine_cross_provider_flag(self):
        same = CascadeEngine(cross_provider=False).get_chain("deepseek-reasoner")
        assert same == ["deepseek-chat", "deepseek-reasoner"]
        cross = CascadeEngine().get_chain(OPUS)
        assert len({_detect_provider(m) for m in cross}) > 1

    def test_cheapest_model_chain_is_itself(self):
        assert CascadeEngine().get_chain("deepseek-chat") == ["deepseek-chat"]


class TestGetDowngrade:
    @pytest.mark.parametrize("model,expected", [
        (OPUS, ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5")),
        ("claude-sonnet-4-5-20250929", (HAIKU, "claude-haiku-4-5")),
        ("gpt-4o", ("gpt-4o-mini",)),
        ("deepseek-reasoner", ("deepseek-chat",)),
        ("moonshot-v1-128k", ("kimi-k2.5",)),
        ("deepseek-chat", (None,)),
        ("unknown-xyz", (None,)),
    ])
    def test_get_downgrade(self, model, expected):
        assert get_downgrade(model) in expected


class TestCascadeStrategy:
    def setup_method(self):
        self.strategy = CascadeStrategy()

    @pytest.mark.parametrize("task,score", [
        ("hi", 0.0),
        ("analyze the 10-K filing for TSLA", 0.6),
        ("implement a new feature for the API", 0.7),
        ("First research the topic, then analyze the data, and additionally create a summary", 0.8),
        ("What is the weather like?" + " " * 200, 0.3),
        ("lorem ipsum " * 50, 0.5),
    ])
    def test_estimate_complexity(self, task, score):
        assert self.strategy.estimate_complexity(task) == score

    @pytest.mark.parametrize("task,chain,start", [
        ("hi", ["haiku", "sonnet", "opus"], 0),
        ("analyze the complex financial derivatives portfolio", ["haiku", "sonnet", "opus"], 1),
        ("anything", ["haiku"], 0),
        # complex task, but skipping the cheapest needs more than two tiers
        ("analyze the quarterly earnings report in detail", ["haiku", "sonnet"], 0),
    ])
    def test_pick_start_tier(self, task, chain, start):
        assert self.strategy.pick_start_tier(task, chain) == start

    def test_pick_start_tier_metrics_skip(self):
        metrics = CascadeMetrics()
        for _ in range(10):
            metrics.record("haiku", succeeded=False, quality=0.1)
        assert self.strategy.pick_start_tier("hi", ["haiku", "sonnet", "opus"], metrics=metrics) == 1


def _metrics(model, successes, failures):
    metrics = CascadeMetrics()
    for _ in range(successes):
        metrics.record(model, succeeded=True)
    for _ in range(failures):
        metrics.record(model, succeeded=False)
    return metrics


class TestCascadeMetrics:
    def test_empty_metrics(self):
        metrics = CascadeMetrics()
        assert metrics.success_rate("any-model") == 1.0
        assert not metrics.should_skip("any-model")

    def test_success_rate_calculation(self):
        assert _metrics("haiku", 7, 3).success_rate("haiku") == pytest.approx(0.7)

    @pytest.mark.parametrize("successes,failures,skip", [
        (0, 4, False),  # below the 5-attempt minimum
        (0, 10, True),
        (5, 5, False),  # 50% > 15% threshold
    ])
    def test_should_skip(self, successes, failures, skip):
        assert _metrics("haiku", successes, failures).should_skip("haiku") is skip

    def test_persistence_roundtrip(self, tmp_path):
        (tmp_path / "teams" / "stock" / "memory").mkdir(parents=True)
        mm = MemoryManager(str(tmp_path))
        assert CascadeMetrics.load(mm, "stock").model_stats == {}

        _metrics("haiku", 1, 1).save(mm, "stock")

        loaded = CascadeMetrics.load(mm, "stock")
        assert loaded.model_stats["haiku"]["attempts"] == 2
        assert loaded.model_stats["haiku"]["successes"] == 1


class TestAdequacyChecker:
    def setup_method(self):
        self.checker = AdequacyChecker()

    def test_empty_response_inadequate(self):
        assert self.checker.check("", "What is AI?") == (False, 0.0)

    @pytest.mark.parametrize("refusal", [
        "I can't help with that request.",
        "I cannot provide that information.",
        "I'm unable to assist with this.",
        "I apologize but I cannot do that.",
        "I'm not able to help here.",
        "Sorry, I can't do that.",
    ])
    def test_refusal_inadequate(self, refusal):
        adequate, quality = self.checker.check(refusal, "Do something")
        assert not adequate
        assert quality <= 0.2

    def test_short_response_for_complex_task(self):
        adequate, _ = self.checker.check(
            "Yes.", "Analyze the quarterly earnings report for TSLA including revenue, margins, and guidance"
        )
        assert not adequate

    @pytest.mark.parametrize("prefix", ["", "I can help you with that. "])
    def test_good_response_adequate(self, prefix):
        result = prefix + (
            "Python is a high-level, interpreted programming language known for its simplicity "
            "and readability. It supports multiple programming paradigms and has a large standard library."
        )
        adequate, quality = self.checker.check(result, "What is Python?")
        assert adequate
        assert quality >= 0.5

    @pytest.mark.parametrize("hedge", ["I'm not sure about this", "I don't have access to that"])
    def test_hedging_penalized(self, hedge):
        _, quality_hedging = self.checker.check(
            f"{hedge}, but I think it might be related to something.", "short task",
        )
        _, quality_confident = self.checker.check(
            "This is definitely related to the topic at hand and here is why.", "short task",
        )
        assert quality_hedging < quality_confident

    def test_engine_check_adequate_delegates(self):
        adequate, quality = CascadeEngine().check_adequate(
            "Here is a good answer about Python programming.", "What is Python?",
        )
        assert adequate
        assert quality > 0.5


class TestCascadeEngine:
    def setup_method(self):
        self.engine = CascadeEngine()

    def test_get_effective_chain_complex_task_skips_cheapest(self):
        full_chain = self.engine.get_chain(OPUS)
        effective = self.engine.get_effective_chain(
            OPUS, "analyze the complex financial derivatives portfolio and provide recommendations",
        )
        assert len(full_chain) > 2
        assert effective == full_chain[1:]

    def test_skip_models_removes_all_but_target(self):
        assert HAIKU in self.engine.get_effective_chain(OPUS, "hi")
        chain = self.engine.get_effective_chain(OPUS, "hi", skip_models=[HAIKU, OPUS])
        assert HAIKU not in chain
        assert OPUS in chain


class TestDetectLanguage:
    @pytest.mark.parametrize("text,lang", [
        ("Hello, how are you?", "en"),
        ("", "en"),
        ("请帮我分析一下 TSLA 的走势", "zh"),
        ("こんにちは、元気ですか", "ja"),
        ("안녕하세요, 잘 지내세요?", "ko"),
        # CJK ratio below the threshold stays English
        ("This has some 中文 mixed in the English text here and there", "en"),
    ])
    def test_detect_language(self, text, lang):
        assert detect_language(text) == lang


class TestLangPenalty:
    @pytest.mark.parametrize("model,lang,penalty", [
        ("glm-4.7", "en", 1.0),
        ("glm-4.7", "zh", 1.0),
        ("gpt-4o-mini", "zh", 1.1),  # multi:0.8 -> 1.0 + (1-0.8)*0.5
        ("claude-opus-4-6", "zh", 1.025),  # multi:0.95 fallback
        ("unknown-xyz", "zh", 1.5),
    ])
    def test_lang_penalty(self, model, lang, penalty):
        assert _lang_penalty(model, lang) == pytest.approx(penalty)


class TestLanguageAwareCascade:
    def setup_method(self):
        self.engine = CascadeEngine(cross_provider=True)

    def test_zh_task_reorders_cross_provider_chain(self):
        assert self.engine.get_chain(OPUS, lang="zh") != self.engine.get_chain(OPUS, lang="en")

    def test_zh_deepseek_before_gpt4o_mini(self):
        chain = self.engine.get_chain(OPUS, lang="zh")
        assert chain.index("deepseek-chat") < chain.index("gpt-4o-mini"), chain

    def test_same_provider_chain_unaffected_by_lang(self):
        tiers = build_model_tiers()
        chain_en = build_cascade_chain(OPUS, tiers, same_provider_only=True, lang="en")
        chain_zh = build_cascade_chain(OPUS, tiers, same_provider_only=True, lang="zh")
        assert chain_en == chain_zh

    def test_effective_chain_auto_detects_zh(self):
        chain = self.engine.get_effective_chain(OPUS, ZH_TASK)
        assert chain == self.engine.get_effective_chain(OPUS, ZH_TASK, lang="zh")
        assert chain != self.engine.get_effective_chain(OPUS, ZH_TASK, lang="en")

    @pytest.mark.parametrize("lang", ["en", "zh", "ja"])
    def test_target_always_last(self, lang):
        assert self.engine.get_chain(OPUS, lang=lang)[-1] == OPUS
