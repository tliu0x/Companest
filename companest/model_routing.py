"""Companest Model Routing — Unified provider/endpoint resolution"""

import os
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from .exceptions import ConfigurationError

if TYPE_CHECKING:
    from .config import ProxyConfig

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"

# OpenAI-compatible chat completions endpoints: prefix -> (base_url, api_key_env_var)
_OPENAI_COMPATIBLE_ENDPOINTS = {
    "deepseek":  ("https://api.deepseek.com",      "DEEPSEEK_API_KEY"),
    "moonshot":  ("https://api.moonshot.cn/v1",     "MOONSHOT_API_KEY"),
    "kimi":      ("https://api.moonshot.cn/v1",     "MOONSHOT_API_KEY"),
}

# Only reachable via proxy/LiteLLM
_PROXY_ONLY_PREFIXES = ("qwen", "mistral", "llama", "gemma", "yi-", "glm-", "qwq")

_NATIVE_OPENAI_PREFIXES = ("gpt-", "o3", "o4", "openai/")


@dataclass(frozen=True)
class ResolvedEndpoint:
    provider: str
    base_url: Optional[str] = None  # None means use SDK default
    api_key: Optional[str] = None   # None means use SDK default (env var)


def detect_provider(model: str, proxy_enabled: bool = False) -> str:
    if model.startswith(("claude-", "anthropic/")):
        return PROVIDER_ANTHROPIC
    if model.startswith(_NATIVE_OPENAI_PREFIXES + tuple(_OPENAI_COMPATIBLE_ENDPOINTS) + _PROXY_ONLY_PREFIXES):
        return PROVIDER_OPENAI
    # Unknown model: a proxy can route anything via the OpenAI protocol
    return PROVIDER_OPENAI if proxy_enabled else PROVIDER_ANTHROPIC


def resolve_model_endpoint(
    model: str,
    proxy_config: Optional["ProxyConfig"] = None,
) -> ResolvedEndpoint:
    proxy_enabled = bool(proxy_config and proxy_config.enabled)
    provider = detect_provider(model, proxy_enabled)

    if provider == PROVIDER_ANTHROPIC:
        if not proxy_enabled:
            return ResolvedEndpoint(PROVIDER_ANTHROPIC)
        return ResolvedEndpoint(
            PROVIDER_ANTHROPIC,
            base_url=proxy_config.base_url.rstrip("/"),
            api_key=proxy_config.default_key or proxy_config.master_key,
        )

    # Behind a proxy raw SDK clients read OPENAI_BASE_URL / OPENAI_API_KEY set by Pi.configure_proxy().
    if proxy_enabled or model.startswith(_NATIVE_OPENAI_PREFIXES):
        return ResolvedEndpoint(PROVIDER_OPENAI)

    for prefix, (base_url, key_env) in _OPENAI_COMPATIBLE_ENDPOINTS.items():
        if model.startswith(prefix):
            api_key = os.environ.get(key_env)
            if not api_key:
                raise ConfigurationError(
                    f"API key not configured for model '{model}'. "
                    f"Set the {key_env} environment variable, "
                    f"or enable the LiteLLM proxy.",
                    details={"model": model, "env_var": key_env},
                )
            return ResolvedEndpoint(PROVIDER_OPENAI, base_url=base_url, api_key=api_key)

    if model.startswith(_PROXY_ONLY_PREFIXES):
        raise ConfigurationError(
            f"Model '{model}' is only supported via proxy/LiteLLM. "
            f"Enable the proxy in your configuration, or choose a model "
            f"with a direct API endpoint (e.g. deepseek-chat, moonshot-v1-8k).",
            details={"model": model},
        )

    raise ConfigurationError(
        f"Unknown model '{model}'. Cannot determine provider or endpoint. "
        f"Enable the LiteLLM proxy for custom model routing.",
        details={"model": model},
    )


def build_chat_model(model: str, proxy_config: Optional["ProxyConfig"] = None):
    endpoint = resolve_model_endpoint(model, proxy_config)
    base_url, api_key = endpoint.base_url, endpoint.api_key
    if proxy_config and proxy_config.enabled and endpoint.provider == PROVIDER_OPENAI:
        base_url = proxy_config.base_url.rstrip("/") + "/v1"
        api_key = proxy_config.default_key or proxy_config.master_key
    kwargs = {"model": model}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key

    if endpoint.provider == PROVIDER_ANTHROPIC:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(**kwargs)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(**kwargs)
