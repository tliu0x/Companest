"""
Companest Model Routing — Unified provider/endpoint resolution

Single source of truth for mapping model names to providers, base URLs,
and API key environment variables. Used by both Pi and Dreamer to avoid
duplicated prefix tables and inconsistent fallback logic.
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from .exceptions import ConfigurationError

if TYPE_CHECKING:
    from .config import ProxyConfig

logger = logging.getLogger(__name__)

# Provider constants
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"

# Non-OpenAI models that speak the OpenAI-compatible chat completions protocol.
# Maps prefix -> (base_url, api_key_env_var)
_OPENAI_COMPATIBLE_ENDPOINTS = {
    "deepseek":  ("https://api.deepseek.com",      "DEEPSEEK_API_KEY"),
    "moonshot":  ("https://api.moonshot.cn/v1",     "MOONSHOT_API_KEY"),
    "kimi":      ("https://api.moonshot.cn/v1",     "MOONSHOT_API_KEY"),
}

# Models that are only reachable via proxy/LiteLLM (no public direct endpoint
# configured).  When proxy is off these should error clearly.
_PROXY_ONLY_PREFIXES = ("qwen", "mistral", "llama", "gemma", "yi-", "glm-", "qwq")

# Native OpenAI model prefixes
_NATIVE_OPENAI_PREFIXES = ("gpt-", "o3", "o4", "openai/")


@dataclass(frozen=True)
class ResolvedEndpoint:
    """Result of resolve_model_endpoint()."""
    provider: str           # "anthropic" | "openai"
    base_url: Optional[str]  # None means use SDK default
    api_key: Optional[str]   # None means use SDK default (env var)
    needs_chat_completions_wrapper: bool  # True for non-native OpenAI models
    api_key_env: Optional[str] = None  # env var name, for error messages


def detect_provider(model: str, proxy_enabled: bool = False) -> str:
    """Determine provider from model name.  Shared by Pi and Dreamer."""
    if model.startswith(("claude-", "anthropic/")):
        return PROVIDER_ANTHROPIC
    if model.startswith(_NATIVE_OPENAI_PREFIXES):
        return PROVIDER_OPENAI
    if model.startswith(tuple(_OPENAI_COMPATIBLE_ENDPOINTS.keys())):
        return PROVIDER_OPENAI
    if model.startswith(_PROXY_ONLY_PREFIXES):
        return PROVIDER_OPENAI
    # Unknown model: if proxy is on assume it can route anything via OpenAI protocol
    return PROVIDER_OPENAI if proxy_enabled else PROVIDER_ANTHROPIC


def resolve_model_endpoint(
    model: str,
    proxy_config: Optional["ProxyConfig"] = None,
) -> ResolvedEndpoint:
    """Resolve provider, base_url, api_key, and wrapper need for a model.

    Raises ConfigurationError when a required API key is missing.
    """
    proxy_enabled = bool(proxy_config and proxy_config.enabled)
    provider = detect_provider(model, proxy_enabled)

    # -- Anthropic models --------------------------------------------------
    if provider == PROVIDER_ANTHROPIC:
        if proxy_enabled:
            return ResolvedEndpoint(
                provider=PROVIDER_ANTHROPIC,
                base_url=proxy_config.base_url.rstrip("/"),
                api_key=proxy_config.default_key or proxy_config.master_key,
                needs_chat_completions_wrapper=False,
            )
        return ResolvedEndpoint(
            provider=PROVIDER_ANTHROPIC,
            base_url=None,
            api_key=None,
            needs_chat_completions_wrapper=False,
        )

    # -- OpenAI-protocol models --------------------------------------------
    if proxy_enabled:
        # Proxy handles routing; SDK reads OPENAI_BASE_URL / OPENAI_API_KEY
        # set once at startup by Pi.configure_proxy().
        return ResolvedEndpoint(
            provider=PROVIDER_OPENAI,
            base_url=None,  # already in env
            api_key=None,
            needs_chat_completions_wrapper=False,
        )

    # Direct connection (no proxy) -----------------------------------------

    # Native OpenAI models — use SDK defaults (OPENAI_API_KEY)
    if model.startswith(_NATIVE_OPENAI_PREFIXES):
        return ResolvedEndpoint(
            provider=PROVIDER_OPENAI,
            base_url=None,
            api_key=None,
            needs_chat_completions_wrapper=False,
        )

    # OpenAI-compatible third-party models with known endpoints
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
            return ResolvedEndpoint(
                provider=PROVIDER_OPENAI,
                base_url=base_url,
                api_key=api_key,
                needs_chat_completions_wrapper=True,
                api_key_env=key_env,
            )

    # Proxy-only models without direct endpoint
    if model.startswith(_PROXY_ONLY_PREFIXES):
        raise ConfigurationError(
            f"Model '{model}' is only supported via proxy/LiteLLM. "
            f"Enable the proxy in your configuration, or choose a model "
            f"with a direct API endpoint (e.g. deepseek-chat, moonshot-v1-8k).",
            details={"model": model},
        )

    # Completely unknown model — refuse to guess
    raise ConfigurationError(
        f"Unknown model '{model}'. Cannot determine provider or endpoint. "
        f"Enable the LiteLLM proxy for custom model routing.",
        details={"model": model},
    )
