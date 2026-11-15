"""Tests for model routing and provider resolution."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companest.config import CompanestConfig
from companest.model_routing import (
    build_chat_model,
    detect_provider,
    resolve_model_endpoint,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
)
from companest.exceptions import ConfigurationError
from companest.memory.dreamer import Dreamer, DreamerError


@pytest.mark.parametrize("model, proxy_enabled, expected", [
    ("claude-sonnet-4-5", False, PROVIDER_ANTHROPIC),
    ("anthropic/claude-3", False, PROVIDER_ANTHROPIC),
    ("gpt-4o", False, PROVIDER_OPENAI),
    ("o3", False, PROVIDER_OPENAI),
    ("o4-mini", False, PROVIDER_OPENAI),
    ("deepseek-chat", False, PROVIDER_OPENAI),
    ("moonshot-v1-8k", False, PROVIDER_OPENAI),
    ("kimi-k2.5", False, PROVIDER_OPENAI),
    *[(f"{prefix}-test", False, PROVIDER_OPENAI)
      for prefix in ("qwen", "mistral", "llama", "gemma", "yi-", "glm-", "qwq")],
    ("some-unknown-model", False, PROVIDER_ANTHROPIC),
    ("some-unknown-model", True, PROVIDER_OPENAI),
])
def test_detect_provider(model, proxy_enabled, expected):
    assert detect_provider(model, proxy_enabled=proxy_enabled) == expected


def _make_proxy(enabled=True, base_url="http://proxy:4000", key="sk-proxy"):
    return SimpleNamespace(
        enabled=enabled,
        base_url=base_url,
        default_key=key,
        master_key="sk-master",
    )


def _env_without(name):
    env = {k: v for k, v in os.environ.items() if k != name}
    return patch.dict(os.environ, env, clear=True)


class TestResolveModelEndpoint:
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

    @pytest.mark.parametrize("model, key_env, base_url", [
        ("deepseek-chat", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
        ("moonshot-v1-8k", "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1"),
        ("kimi-k2.5", "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1"),
    ])
    def test_openai_compatible_direct(self, model, key_env, base_url):
        with patch.dict(os.environ, {key_env: "sk-test"}):
            ep = resolve_model_endpoint(model)
        assert ep.provider == PROVIDER_OPENAI
        assert ep.base_url == base_url
        assert ep.api_key == "sk-test"
        assert ep.needs_chat_completions_wrapper is True

        with _env_without(key_env):
            with pytest.raises(ConfigurationError, match=key_env):
                resolve_model_endpoint(model)

    def test_proxy_only_model_no_proxy(self):
        for model in ("qwen3-coder", "mistral-large", "llama-3", "glm-4.7"):
            with pytest.raises(ConfigurationError, match="only supported via proxy"):
                resolve_model_endpoint(model)

    def test_proxy_only_model_with_proxy(self):
        proxy = _make_proxy()
        ep = resolve_model_endpoint("qwen3-coder", proxy)
        assert ep.provider == PROVIDER_OPENAI
        assert ep.needs_chat_completions_wrapper is False  # proxy handles it

    def test_native_openai_direct(self):
        ep = resolve_model_endpoint("gpt-4o")
        assert ep.provider == PROVIDER_OPENAI
        assert ep.base_url is None
        assert not ep.needs_chat_completions_wrapper


class TestRemovedProxyConfig:
    def test_subscription_mode_is_rejected(self):
        with pytest.raises(ValueError, match="proxy.subscription_mode has been removed"):
            CompanestConfig(proxy={"enabled": True, "subscription_mode": True})


class TestBuildChatModel:
    def test_direct_deepseek_uses_openai_protocol_endpoint(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            model = build_chat_model("deepseek-chat")
        assert type(model).__name__ == "ChatOpenAI"
        assert model.model_name == "deepseek-chat"
        assert str(model.openai_api_base).rstrip("/") == "https://api.deepseek.com"
        assert model.openai_api_key.get_secret_value() == "sk-test"

    def test_proxy_routes_openai_protocol_models_through_proxy(self):
        proxy = _make_proxy()
        model = build_chat_model("deepseek-chat", proxy)
        assert type(model).__name__ == "ChatOpenAI"
        assert str(model.openai_api_base) == proxy.base_url.rstrip("/") + "/v1"

    def test_claude_uses_anthropic_client(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            model = build_chat_model("claude-sonnet-5-5")
        assert type(model).__name__ == "ChatAnthropic"
        assert model.model == "claude-sonnet-5-5"

    def test_proxy_routes_claude_through_proxy(self):
        proxy = _make_proxy()
        model = build_chat_model("claude-sonnet-5-5", proxy)
        assert type(model).__name__ == "ChatAnthropic"
        assert str(model.anthropic_api_url).rstrip("/") == proxy.base_url.rstrip("/")


def _openai_client(choices):
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=MagicMock(choices=choices))
    client.close = AsyncMock()
    return client


def _mock_openai_module(client=None):
    """Patch openai into sys.modules for environments without it installed."""
    openai_mod = MagicMock()
    openai_mod.AsyncOpenAI = MagicMock(return_value=client)
    return patch.dict(sys.modules, {"openai": openai_mod})


def _make_dreamer(model="deepseek-chat", proxy=None):
    return Dreamer(memory=MagicMock(), proxy_config=proxy, model=model)


class TestDreamerOpenAICall:
    @pytest.mark.asyncio
    async def test_empty_choices_raises_dreamer_error(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with _mock_openai_module(_openai_client([])):
                with pytest.raises(DreamerError, match="empty choices"):
                    await _make_dreamer()._call_llm_openai("test prompt")

    @pytest.mark.asyncio
    async def test_normal_response_returns_content(self):
        choice = MagicMock()
        choice.message.content = "test output"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with _mock_openai_module(_openai_client([choice])):
                assert await _make_dreamer()._call_llm_openai("test prompt") == "test output"

    @pytest.mark.asyncio
    async def test_proxy_only_model_without_proxy_raises_config_error(self):
        with _mock_openai_module():
            with pytest.raises(ConfigurationError, match="only supported via proxy"):
                await _make_dreamer(model="qwen3-coder")._call_llm_openai("test prompt")

    @pytest.mark.asyncio
    async def test_proxy_only_model_resolves_through_proxy(self):
        choice = MagicMock()
        choice.message.content = "result"
        with _mock_openai_module(_openai_client([choice])):
            dreamer = _make_dreamer(model="qwen3-coder", proxy=_make_proxy())
            assert await dreamer._call_llm_openai("test prompt") == "result"
