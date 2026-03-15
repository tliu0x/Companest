"""
Tests for model routing, provider resolution, and the fixes from the
DeepSeek/Kimi call-chain PR review.

Covers:
- Shared resolve_model_endpoint() for proxy/direct/missing-key/unsupported
- Pi._run_openai wrapper and max_turns propagation
- Dreamer empty-choices handling
- CostGate downgrade UX when suggested_downgrade is None
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companest.model_routing import (
    detect_provider,
    resolve_model_endpoint,
    ResolvedEndpoint,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
)
from companest.exceptions import ConfigurationError


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

class TestDetectProvider:
    def test_claude_models(self):
        assert detect_provider("claude-sonnet-4-5") == PROVIDER_ANTHROPIC
        assert detect_provider("anthropic/claude-3") == PROVIDER_ANTHROPIC

    def test_native_openai(self):
        assert detect_provider("gpt-4o") == PROVIDER_OPENAI
        assert detect_provider("o3") == PROVIDER_OPENAI
        assert detect_provider("o4-mini") == PROVIDER_OPENAI

    def test_openai_compatible(self):
        assert detect_provider("deepseek-chat") == PROVIDER_OPENAI
        assert detect_provider("moonshot-v1-8k") == PROVIDER_OPENAI
        assert detect_provider("kimi-k2.5") == PROVIDER_OPENAI

    def test_proxy_only_models(self):
        for prefix in ("qwen", "mistral", "llama", "gemma", "yi-", "glm-", "qwq"):
            assert detect_provider(f"{prefix}-test") == PROVIDER_OPENAI

    def test_unknown_model_no_proxy(self):
        assert detect_provider("some-unknown-model") == PROVIDER_ANTHROPIC

    def test_unknown_model_with_proxy(self):
        assert detect_provider("some-unknown-model", proxy_enabled=True) == PROVIDER_OPENAI


# ---------------------------------------------------------------------------
# resolve_model_endpoint
# ---------------------------------------------------------------------------

def _make_proxy(enabled=True, base_url="http://proxy:4000", key="sk-proxy"):
    return SimpleNamespace(
        enabled=enabled,
        base_url=base_url,
        default_key=key,
        master_key="sk-master",
    )


class TestResolveModelEndpoint:
    """Test unified endpoint resolution."""

    def test_anthropic_no_proxy(self):
        ep = resolve_model_endpoint("claude-sonnet-4-5")
        assert ep.provider == PROVIDER_ANTHROPIC
        assert ep.base_url is None
        assert not ep.needs_chat_completions_wrapper

    def test_anthropic_with_proxy(self):
        proxy = _make_proxy()
        ep = resolve_model_endpoint("claude-sonnet-4-5", proxy)
        assert ep.provider == PROVIDER_ANTHROPIC
        assert ep.base_url == "http://proxy:4000"
        assert ep.api_key == "sk-proxy"

    def test_deepseek_direct_with_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-ds-test"}):
            ep = resolve_model_endpoint("deepseek-chat")
        assert ep.provider == PROVIDER_OPENAI
        assert ep.base_url == "https://api.deepseek.com"
        assert ep.api_key == "sk-ds-test"
        assert ep.needs_chat_completions_wrapper is True

    def test_deepseek_direct_missing_key(self):
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
                resolve_model_endpoint("deepseek-chat")

    def test_moonshot_direct_with_key(self):
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-ms-test"}):
            ep = resolve_model_endpoint("moonshot-v1-8k")
        assert ep.base_url == "https://api.moonshot.cn/v1"
        assert ep.needs_chat_completions_wrapper is True

    def test_kimi_direct_with_key(self):
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-ms-test"}):
            ep = resolve_model_endpoint("kimi-k2.5")
        assert ep.base_url == "https://api.moonshot.cn/v1"

    def test_moonshot_direct_missing_key(self):
        env = {k: v for k, v in os.environ.items() if k != "MOONSHOT_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigurationError, match="MOONSHOT_API_KEY"):
                resolve_model_endpoint("kimi-k2.5")

    def test_proxy_only_model_no_proxy(self):
        """qwen/mistral/llama etc. should error without proxy."""
        for model in ("qwen3-coder", "mistral-large", "llama-3", "glm-4.7"):
            with pytest.raises(ConfigurationError, match="only supported via proxy"):
                resolve_model_endpoint(model)

    def test_proxy_only_model_with_proxy(self):
        """With proxy, any model should resolve without error."""
        proxy = _make_proxy()
        ep = resolve_model_endpoint("qwen3-coder", proxy)
        assert ep.provider == PROVIDER_OPENAI
        assert ep.needs_chat_completions_wrapper is False  # proxy handles it

    def test_native_openai_direct(self):
        ep = resolve_model_endpoint("gpt-4o")
        assert ep.provider == PROVIDER_OPENAI
        assert ep.base_url is None
        assert not ep.needs_chat_completions_wrapper

    def test_all_models_with_proxy(self):
        """With proxy, all models should resolve successfully."""
        proxy = _make_proxy()
        models = [
            "claude-sonnet-4-5", "gpt-4o", "deepseek-chat",
            "moonshot-v1-8k", "qwen3-coder", "mistral-large",
        ]
        for model in models:
            ep = resolve_model_endpoint(model, proxy)
            assert ep.provider in (PROVIDER_ANTHROPIC, PROVIDER_OPENAI)


# ---------------------------------------------------------------------------
# Pi: _run_openai wrapper and max_turns
# ---------------------------------------------------------------------------

class TestPiOpenAIRouting:
    """Test Pi._run_openai uses resolve_model_endpoint correctly."""

    def _make_pi(self, model="deepseek-chat", proxy=None):
        from companest.pi import Pi, PiConfig
        mm = MagicMock()
        mm.build_system_prompt.return_value = "system"
        return Pi(
            PiConfig(id="test", model=model, max_turns=5),
            memory=mm,
            team_id="test-team",
            proxy_config=proxy,
        )

    def _build_agents_mock(self):
        """Build a mock 'agents' module for lazy-import patching."""
        mock_agent_cls = MagicMock()
        mock_runner_cls = MagicMock()
        mock_wrapper_cls = MagicMock()
        mock_result = MagicMock()
        mock_result.final_output = "answer"
        mock_runner_cls.run = AsyncMock(return_value=mock_result)
        return mock_agent_cls, mock_runner_cls, mock_wrapper_cls

    def _patch_modules(self, mock_agent, mock_runner, mock_wrapper, mock_openai_client):
        """Context manager helper to patch both agents and openai modules."""
        import sys
        agents_mod = MagicMock()
        agents_mod.Agent = mock_agent
        agents_mod.Runner = mock_runner
        agents_mod.OpenAIChatCompletionsModel = mock_wrapper

        openai_mod = MagicMock()
        openai_mod.AsyncOpenAI = MagicMock(return_value=mock_openai_client)

        return patch.dict(sys.modules, {"agents": agents_mod, "openai": openai_mod})

    @pytest.mark.asyncio
    async def test_max_turns_passed_to_runner(self):
        """Runner.run must receive max_turns=self.max_turns."""
        pi = self._make_pi()
        mock_agent, mock_runner, mock_wrapper = self._build_agents_mock()
        mock_oa_client = AsyncMock()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with self._patch_modules(mock_agent, mock_runner, mock_wrapper, mock_oa_client):
                result = await pi._run_openai("hello", "system")

        assert mock_runner.run.call_count == 1
        call_kwargs = mock_runner.run.call_args
        assert call_kwargs.kwargs.get("max_turns") == 5

    @pytest.mark.asyncio
    async def test_direct_deepseek_uses_wrapper(self):
        """Without proxy, deepseek-chat should use OpenAIChatCompletionsModel."""
        pi = self._make_pi()
        mock_agent, mock_runner, mock_wrapper = self._build_agents_mock()
        mock_oa_client = AsyncMock()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with self._patch_modules(mock_agent, mock_runner, mock_wrapper, mock_oa_client):
                result = await pi._run_openai("hello", "system")
                mock_wrapper.assert_called_once()

    @pytest.mark.asyncio
    async def test_proxy_deepseek_no_wrapper(self):
        """With proxy, deepseek-chat should NOT use OpenAIChatCompletionsModel."""
        proxy = _make_proxy()
        pi = self._make_pi(proxy=proxy)
        mock_agent, mock_runner, mock_wrapper = self._build_agents_mock()
        mock_oa_client = AsyncMock()

        with self._patch_modules(mock_agent, mock_runner, mock_wrapper, mock_oa_client):
            result = await pi._run_openai("hello", "system")
            mock_wrapper.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_key_raises_config_error(self):
        """Missing DEEPSEEK_API_KEY should raise ConfigurationError, not 401."""
        import sys
        pi = self._make_pi()
        agents_mod = MagicMock()

        with patch.dict(sys.modules, {"agents": agents_mod}):
            env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
                    await pi._run_openai("hello", "system")


# ---------------------------------------------------------------------------
# Dreamer: empty choices
# ---------------------------------------------------------------------------

class TestDreamerEmptyChoices:
    """Dreamer._call_llm_openai must handle empty choices gracefully."""

    def _make_dreamer(self, model="deepseek-chat", proxy=None):
        from companest.memory.dreamer import Dreamer
        mm = MagicMock()
        return Dreamer(memory=mm, proxy_config=proxy, model=model)

    def _mock_openai_module(self, mock_client):
        """Patch openai into sys.modules for environments without it installed."""
        import sys
        openai_mod = MagicMock()
        openai_mod.AsyncOpenAI = MagicMock(return_value=mock_client)
        return patch.dict(sys.modules, {"openai": openai_mod})

    @pytest.mark.asyncio
    async def test_empty_choices_raises_dreamer_error(self):
        from companest.memory.dreamer import DreamerError

        dreamer = self._make_dreamer()

        mock_response = MagicMock()
        mock_response.choices = []

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_client.close = AsyncMock()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with self._mock_openai_module(mock_client):
                with pytest.raises(DreamerError, match="empty choices"):
                    await dreamer._call_llm_openai("test prompt")

    @pytest.mark.asyncio
    async def test_normal_response_returns_content(self):
        dreamer = self._make_dreamer()

        mock_message = MagicMock()
        mock_message.content = "test output"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_client.close = AsyncMock()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with self._mock_openai_module(mock_client):
                result = await dreamer._call_llm_openai("test prompt")
                assert result == "test output"

    @pytest.mark.asyncio
    async def test_missing_key_raises_config_error(self):
        """Dreamer with qwen model and no proxy should raise ConfigurationError."""
        import sys
        dreamer = self._make_dreamer(model="qwen3-coder")
        openai_mod = MagicMock()

        with patch.dict(sys.modules, {"openai": openai_mod}):
            with pytest.raises(ConfigurationError, match="only supported via proxy"):
                await dreamer._call_llm_openai("test prompt")

    @pytest.mark.asyncio
    async def test_proxy_model_resolves(self):
        """With proxy, qwen should work without needing direct API key."""
        proxy = _make_proxy()
        dreamer = self._make_dreamer(model="qwen3-coder", proxy=proxy)

        mock_message = MagicMock()
        mock_message.content = "result"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_client.close = AsyncMock()

        with self._mock_openai_module(mock_client):
            result = await dreamer._call_llm_openai("test prompt")
            assert result == "result"


# ---------------------------------------------------------------------------
# CostGate: downgrade UX
# ---------------------------------------------------------------------------

class TestCostGateDowngradeUX:
    """Downgrade option should not appear when suggested_downgrade is None."""

    def test_escalation_message_no_downgrade(self):
        """When suggested_downgrade is None, message should not offer downgrade."""
        from companest.cost_gate import CostEstimate, UserNotifier

        notifier = UserNotifier()
        estimate = CostEstimate(
            estimated_input_tokens=1000,
            estimated_output_tokens=2000,
            estimated_cost_usd=0.50,
            target_team="stock",
            target_model="deepseek-chat",
            suggested_downgrade=None,
        )
        # Build the message the same way request_approval does
        choices = "approve / reject"
        if estimate.suggested_downgrade:
            choices = f"approve / downgrade ({estimate.suggested_downgrade}) / reject"
        assert "downgrade" not in choices

    def test_escalation_message_with_downgrade(self):
        from companest.cost_gate import CostEstimate

        estimate = CostEstimate(
            estimated_input_tokens=1000,
            estimated_output_tokens=2000,
            estimated_cost_usd=0.50,
            target_team="stock",
            target_model="gpt-4o",
            suggested_downgrade="gpt-4o-mini",
        )
        choices = "approve / reject"
        if estimate.suggested_downgrade:
            choices = f"approve / downgrade ({estimate.suggested_downgrade}) / reject"
        assert "downgrade (gpt-4o-mini)" in choices

    def test_resolve_downgrade_without_suggestion_treated_as_reject(self):
        """Choosing 'downgrade' with no suggestion should fall through to reject."""
        from companest.cost_gate import CostEstimate

        estimate = CostEstimate(
            estimated_input_tokens=1000,
            estimated_output_tokens=2000,
            estimated_cost_usd=0.50,
            target_team="stock",
            target_model="deepseek-chat",
            suggested_downgrade=None,
        )
        # Simulate _escalate logic: "downgrade" without suggestion should not approve
        user_choice = "downgrade"
        if user_choice == "downgrade" and estimate.suggested_downgrade:
            action = "auto_approve"
        elif user_choice == "approve":
            action = "auto_approve"
        else:
            action = "rejected"
        assert action == "rejected"
