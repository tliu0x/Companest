"""
Companest Pi Agent

The minimal agent unit -SDK-native, no hand-written tool-use loop.
Wraps claude-agent-sdk (for Claude models) and openai-agents (for OpenAI models).

Pi is stateless: each run() = one SDK query call.
Memory persists on disk via MemoryManager, not in the Pi instance.

Supports model cascade: try cheap model first, escalate on failure/refusal.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

# Callback type: async fn(str) -> None
ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]

from .memory import FileBackend, MemoryManager, MemorySearchService
from .tools import (
    ToolRegistry,
    ToolContext,
    create_memory_mcp_server,
    create_memory_openai_tools,
    create_scheduler_mcp_server,
    create_scheduler_openai_tools,
    create_feed_mcp_server,
    create_feed_openai_tools,
    create_sessions_mcp_server,
    create_sessions_openai_tools,
    resolve_tool_names,
    CLAUDE_BUILTIN_TOOLS,
    CUSTOM_TOOL_NAMES,
    SCHEDULER_TOOL_NAMES,
    FEED_TOOL_NAMES,
    SESSIONS_TOOL_NAMES,
    DEFAULT_TOOLS_DENY,
)
from .cascade import CascadeEngine, CascadeMetrics
from .exceptions import PiError

if TYPE_CHECKING:
    from .config import ProxyConfig

logger = logging.getLogger(__name__)


@dataclass
class PiConfig:
    """Configuration for a single Pi agent."""
    id: str
    model: str = "claude-sonnet-4-5-20250929"
    tools: List[str] = field(default_factory=list)  # empty = all tools
    tools_deny: List[str] = field(default_factory=list)
    max_turns: int = 10


class Pi:
    """
    Minimal agent unit -SDK-native, dual provider.

    Usage:
        pi = Pi(config, memory, team_id="stock")
        result = await pi.run("Analyze TSLA stock movement today")
    """

    def __init__(
        self,
        config: PiConfig,
        memory: MemoryManager,
        team_id: str,
        proxy_config: Optional["ProxyConfig"] = None,
        tool_registry: Optional[ToolRegistry] = None,
        cascade_engine: Optional[CascadeEngine] = None,
    ):
        self.id = config.id
        self.model = config.model
        self.tools_config = config.tools
        self.tools_deny = config.tools_deny
        self.max_turns = config.max_turns
        self.memory = memory
        self.team_id = team_id
        self.proxy_config = proxy_config
        self.tool_registry = tool_registry
        self.cascade_engine = cascade_engine
        self._proxy_enabled = bool(proxy_config and proxy_config.enabled)
        self.provider = self._detect_provider(config.model, self._proxy_enabled)
        self.user_scheduler = None  # Set by orchestrator when available

    def _get_proxy_params(self) -> Optional[dict]:
        """Return proxy base_url and api_key if proxy is enabled, else None."""
        if self.proxy_config and self.proxy_config.enabled:
            return {
                "base_url": self.proxy_config.base_url.rstrip("/"),
                "api_key": self.proxy_config.default_key or self.proxy_config.master_key,
            }
        return None

    @staticmethod
    def _detect_provider(model: str, proxy_enabled: bool = False) -> str:
        if model.startswith(("claude-", "anthropic/")):
            return "anthropic"
        elif model.startswith(("gpt-", "o3", "o4", "openai/")):
            return "openai"
        elif model.startswith((
            "deepseek", "moonshot", "kimi", "qwen",
            "mistral", "llama", "gemma", "yi-", "glm-", "qwq",
        )):
            return "openai"
        return "openai" if proxy_enabled else "anthropic"

    @staticmethod
    def configure_proxy(proxy_config: "ProxyConfig") -> None:
        """Set proxy env vars once at startup for SDKs that read them.

        Called from orchestrator.init_teams() when proxy is enabled.
        Both claude-agent-sdk and openai-agents read env vars for
        base_url/api_key. Setting them once avoids per-call mutation races.
        """
        import os
        if not proxy_config or not proxy_config.enabled:
            return
        base_url = proxy_config.base_url.rstrip("/")
        api_key = proxy_config.default_key or proxy_config.master_key
        os.environ["ANTHROPIC_BASE_URL"] = base_url
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        os.environ["OPENAI_BASE_URL"] = base_url + "/v1"
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key

    async def run(
        self, task: str, timeout: float = 300.0, cascade: bool = False,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute a task using the appropriate SDK.

        Args:
            task: The prompt / instruction.
            timeout: Hard timeout in seconds.
            cascade: If True, try cheap model first and escalate on
                     failure or low-quality response.
            on_progress: Optional async callback for progress updates.
            user_context: Optional dict with user_id, chat_id, channel for scheduling tools.
        """
        self._user_context = user_context
        try:
            # Extract company context for system prompt injection
            company_context = None
            if user_context:
                company_context = user_context.get("company_context")
            system = self.memory.build_system_prompt(
                self.team_id, self.id, company_context=company_context,
            )
            retrieval_task = self._resolve_memory_task_hint(task, user_context)
            relevant_memory = self._build_relevant_memory_section(retrieval_task)
            if relevant_memory:
                system = system + "\n\n---\n\n" + relevant_memory if system else relevant_memory
            if not cascade:
                return await self._run_single(task, system, timeout)

            # -- Cascade mode (via CascadeEngine) --------------------
            engine = self.cascade_engine or CascadeEngine()
            metrics = CascadeMetrics.load(self.memory, self.team_id)
            chain = engine.get_effective_chain(self.model, task, metrics)

            if len(chain) <= 1:
                return await self._run_single(task, system, timeout)

            last_error: Optional[PiError] = None
            last_result: Optional[str] = None

            for i, model in enumerate(chain):
                is_last = (i == len(chain) - 1)
                # Extract short model name for display
                short_model = model.rsplit("-", 1)[0] if "-20" in model else model
                if on_progress:
                    await on_progress(
                        f"\U0001f916 {self.team_id}/{self.id} ({short_model}) "
                        f"[{i+1}/{len(chain)}]"
                    )
                try:
                    result = await self._run_with_model(task, system, model, timeout)
                    adequate, quality = engine.check_adequate(result, task)
                    if is_last or adequate:
                        metrics.record(model, succeeded=True, quality=quality)
                        metrics.save(self.memory, self.team_id)
                        logger.info(
                            f"[Pi:{self.team_id}/{self.id}] Cascade accepted at "
                            f"{model} (step {i+1}/{len(chain)}, quality={quality:.2f})"
                        )
                        return result
                    metrics.record(model, succeeded=False, quality=quality)
                    logger.info(
                        f"[Pi:{self.team_id}/{self.id}] Cascade: "
                        f"{model} inadequate (quality={quality:.2f}), escalating"
                    )
                    last_result = result
                    if on_progress and not is_last:
                        next_model = chain[i + 1]
                        short_next = next_model.rsplit("-", 1)[0] if "-20" in next_model else next_model
                        await on_progress(f"\u2b06\ufe0f \u2192 {short_next} [{i+2}/{len(chain)}]")
                except PiError as e:
                    metrics.record(model, succeeded=False, quality=0.0)
                    logger.info(
                        f"[Pi:{self.team_id}/{self.id}] Cascade: "
                        f"{model} failed ({e}), escalating"
                    )
                    last_error = e
                    if on_progress and not is_last:
                        next_model = chain[i + 1]
                        short_next = next_model.rsplit("-", 1)[0] if "-20" in next_model else next_model
                        await on_progress(f"\u2b06\ufe0f \u2192 {short_next} [{i+2}/{len(chain)}]")
                    continue

            metrics.save(self.memory, self.team_id)
            if last_result:
                return last_result
            raise last_error or PiError(
                f"Cascade exhausted for {self.team_id}/{self.id}",
                details={"model": self.model},
            )
        finally:
            self._user_context = None

    # -- Internal execution helpers ------------------------------

    @staticmethod
    def _resolve_memory_task_hint(
        task: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Resolve the task string that should drive task-aware retrieval."""
        if user_context:
            raw_hint = user_context.get("memory_task_hint")
            if isinstance(raw_hint, str) and raw_hint.strip():
                return raw_hint.strip()
        return task.strip()

    def _build_relevant_memory_section(self, task_hint: str) -> str:
        """Build a compact dynamic prompt section with task-relevant memory."""
        task_hint = task_hint.strip()
        if not task_hint:
            return ""

        try:
            entries = self._get_memory_search_service().retrieve_for_task(
                self.team_id,
                task_hint,
                limit=5,
                budget_chars=1800,
                include_archive=False,
            )
        except Exception as e:
            logger.debug(
                "Task-aware memory retrieval failed for %s/%s: %s",
                self.team_id,
                self.id,
                e,
            )
            return ""

        if not entries:
            return ""

        lines = ["## Relevant Memory"]
        for entry in entries:
            lines.append(self._format_relevant_memory_entry(entry))
        return "\n".join(lines)

    def _get_memory_search_service(self) -> MemorySearchService:
        """Use the orchestrator-configured backend when available."""
        backend = getattr(self.tool_registry, "memory_backend", None) if self.tool_registry else None
        if backend is None:
            backend = FileBackend(self.memory)
        return MemorySearchService(backend)

    @staticmethod
    def _format_relevant_memory_entry(entry: Dict[str, Any]) -> str:
        """Format a retrieved memory entry for compact system prompt injection."""
        prefix = f"- {entry.get('key', '')}"
        source = str(entry.get("source", "") or "")
        if source and source != "active":
            prefix += " [archive]"

        text = str(entry.get("text", "") or "").strip()
        return f"{prefix}: {text}".rstrip()

    async def _run_single(self, task: str, system: str, timeout: float) -> str:
        """Execute with the instance's current model (no cascade)."""
        logger.info(
            f"[Pi:{self.team_id}/{self.id}] Running task "
            f"(model={self.model}, provider={self.provider})"
        )
        # Inject skill instructions into system prompt
        if self.tool_registry:
            skill_instructions = self.tool_registry.get_skill_instructions(self.tools_config)
            if skill_instructions:
                system = system + "\n\n---\n\n" + skill_instructions
        # Inject workspace context into system prompt (for coding teams)
        extra_ctx = getattr(self, "_extra_tool_context", {})
        ws_context = extra_ctx.get("workspace_context")
        if ws_context:
            system = system + "\n\n---\n\n" + ws_context
        try:
            if self.provider == "anthropic":
                coro = self._run_claude(task, system)
            else:
                coro = self._run_openai(task, system)
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise PiError(
                f"Pi {self.team_id}/{self.id} timed out after {timeout}s",
                details={"model": self.model, "provider": self.provider},
            )
        except PiError:
            raise
        except Exception as e:
            raise PiError(
                f"Pi {self.team_id}/{self.id} failed: {e}",
                details={"model": self.model, "provider": self.provider},
            )

    async def _run_with_model(
        self, task: str, system: str, model: str, timeout: float,
    ) -> str:
        """Execute with a specific model without mutating instance state."""
        # Inject skill instructions and workspace context (same as _run_single)
        if self.tool_registry:
            skill_instructions = self.tool_registry.get_skill_instructions(self.tools_config)
            if skill_instructions:
                system = system + "\n\n---\n\n" + skill_instructions
        extra_ctx = getattr(self, "_extra_tool_context", {})
        ws_context = extra_ctx.get("workspace_context")
        if ws_context:
            system = system + "\n\n---\n\n" + ws_context

        provider = self._detect_provider(model, self._proxy_enabled)
        logger.info(
            f"[Pi:{self.team_id}/{self.id}] Running task "
            f"(model={model}, provider={provider})"
        )
        try:
            if provider == "anthropic":
                coro = self._run_claude(task, system, model_override=model)
            else:
                coro = self._run_openai(task, system, model_override=model)
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise PiError(
                f"Pi {self.team_id}/{self.id} timed out after {timeout}s",
                details={"model": model, "provider": provider},
            )
        except PiError:
            raise
        except Exception as e:
            raise PiError(
                f"Pi {self.team_id}/{self.id} failed: {e}",
                details={"model": model, "provider": provider},
            )

    # Class-level: sdk not importable (permanent, won't change at runtime)
    _agent_sdk_not_installed = False
    # Instance-level failure tracking -retry after 5 min
    _AGENT_SDK_RETRY_AFTER = 300  # seconds

    async def _run_claude(
        self, task: str, system: str, model_override: Optional[str] = None,
    ) -> str:
        """Run via Claude Agent SDK (preferred) or Anthropic SDK (fallback)."""
        effective_model = model_override or self.model
        if Pi._agent_sdk_not_installed:
            return await self._run_claude_direct(task, system, model_override=effective_model)

        # Instance-level backoff: skip SDK if it failed recently
        import time
        if hasattr(self, "_sdk_fail_time") and time.monotonic() - self._sdk_fail_time < self._AGENT_SDK_RETRY_AFTER:
            return await self._run_claude_direct(task, system, model_override=effective_model)

        try:
            from claude_agent_sdk import (
                query,
                ClaudeAgentOptions,
                AssistantMessage,
                ResultMessage,
            )
            return await self._run_claude_agent_sdk(
                task, system, query, ClaudeAgentOptions,
                AssistantMessage, ResultMessage,
                model_override=effective_model,
            )
        except ImportError:
            Pi._agent_sdk_not_installed = True
            logger.info("[Pi] claude-agent-sdk not available, disabled permanently")
            return await self._run_claude_direct(task, system, model_override=effective_model)
        except Exception as e:
            self._sdk_fail_time = time.monotonic()
            logger.warning(f"[Pi] claude-agent-sdk failed: {e}, backing off {self._AGENT_SDK_RETRY_AFTER}s")
            return await self._run_claude_direct(task, system, model_override=effective_model)

    def _build_deny_set(self) -> set:
        """Compute effective deny set from PiConfig.tools_deny + DEFAULT_TOOLS_DENY.

        - tools_deny=[] (default) -uses DEFAULT_TOOLS_DENY
        - tools_deny=["Bash", "Write"] -uses that set (overrides global)
        - tools_deny=["none"] -empty set (trusted Pi, no denials)
        """
        if not self.tools_deny:
            return set(DEFAULT_TOOLS_DENY)
        if self.tools_deny == ["none"]:
            return set()
        return set(self.tools_deny)

    def _build_tool_context(self) -> ToolContext:
        """Build a ToolContext from current Pi state."""
        uc = getattr(self, "_user_context", None) or {}
        return ToolContext(
            memory=self.memory,
            team_id=self.team_id,
            pi_id=self.id,
            tools_config=self.tools_config,
            user_context=uc or None,
            user_scheduler=self.user_scheduler,
            team_registry=getattr(self, "_team_registry", None),
            tools_deny=self._build_deny_set(),
            extra=getattr(self, "_extra_tool_context", {}),
            memory_backend=getattr(self.tool_registry, "memory_backend", None) if self.tool_registry else None,
            company_id=uc.get("company_id") if isinstance(uc, dict) else None,
        )

    async def _run_claude_agent_sdk(
        self, task, system, query, ClaudeAgentOptions,
        AssistantMessage, ResultMessage,
        model_override: Optional[str] = None,
    ) -> str:
        """Full agentic mode via claude-agent-sdk (Python 3.10+)."""
        deny_set = self._build_deny_set()

        if self.tool_registry:
            ctx = self._build_tool_context()
            mcp_servers = self.tool_registry.get_mcp_servers(ctx)
            # Merge external MCP servers (global + company-scoped)
            ext_servers = self.tool_registry.get_external_mcp_servers(
                company_id=ctx.company_id
            )
            mcp_servers.update(ext_servers)
            # Resolve tools (empty tools_config = all tools via registry)
            allowed = self.tool_registry.resolve_tool_names(self.tools_config, tools_deny=deny_set)
        else:
            # Fallback: hardcoded logic for backward compat
            mem_server = create_memory_mcp_server(
                self.memory, self.team_id, self.id
            )
            allowed = resolve_tool_names(self.tools_config)
            allowed = [t for t in allowed if t not in deny_set]
            mcp_servers = {}
            if mem_server:
                mcp_servers["mem"] = mem_server

            uc = getattr(self, "_user_context", None)
            has_sched_tools = any(t in SCHEDULER_TOOL_NAMES or t == "scheduler"
                                 for t in self.tools_config)
            if uc and self.user_scheduler and has_sched_tools:
                sched_server = create_scheduler_mcp_server(
                    self.user_scheduler,
                    user_id=uc.get("user_id", ""),
                    chat_id=uc.get("chat_id", ""),
                    channel=uc.get("channel", "telegram"),
                )
                if sched_server:
                    mcp_servers["sched"] = sched_server

            has_feed_tools = any(t in FEED_TOOL_NAMES or t == "collector"
                                for t in self.tools_config)
            if has_feed_tools:
                feed_server = create_feed_mcp_server()
                if feed_server:
                    mcp_servers["feed"] = feed_server

            has_sessions_tools = any(t in SESSIONS_TOOL_NAMES or t == "messenger"
                                    for t in self.tools_config)
            if has_sessions_tools:
                uc = getattr(self, "_user_context", None) or {}
                extra_ctx = getattr(self, "_extra_tool_context", {})
                sessions_server = create_sessions_mcp_server(
                    self.memory, self.team_id, self.id,
                    company_id=uc.get("company_id") if isinstance(uc, dict) else None,
                    shared_teams=extra_ctx.get("company_shared_teams"),
                )
                if sessions_server:
                    mcp_servers["sessions"] = sessions_server

        effective_model = model_override or self.model
        options = ClaudeAgentOptions(
            system_prompt=system,
            allowed_tools=allowed,
            mcp_servers=mcp_servers,
            max_turns=self.max_turns,
            permission_mode="default",
            model=effective_model,
        )

        # Proxy env vars are set once at startup via Pi.configure_proxy()
        result_parts = []
        async for message in query(prompt=task, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        result_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                break

        result_text = "\n\n".join(result_parts)
        if not result_text:
            raise PiError(
                f"Pi {self.team_id}/{self.id} returned empty response",
                details={"model": effective_model},
            )
        return result_text

    async def _run_claude_direct(
        self, task: str, system: str, model_override: Optional[str] = None,
    ) -> str:
        """Direct Anthropic SDK fallback -single-turn, no tool use.

        Used when claude-agent-sdk is unavailable (Python < 3.10).
        Memory context is injected into the system prompt instead of
        being available as tools.
        """
        effective_model = model_override or self.model
        try:
            import anthropic
        except ImportError:
            raise PiError(
                "Neither claude-agent-sdk nor anthropic is installed. "
                "Run: pip install anthropic"
            )

        proxy = self._get_proxy_params()
        if proxy:
            client = anthropic.AsyncAnthropic(
                base_url=proxy["base_url"],
                api_key=proxy["api_key"],
            )
        else:
            client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=effective_model,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": task}],
        )

        result_parts = []
        for block in response.content:
            if hasattr(block, "text") and block.text:
                result_parts.append(block.text)

        result_text = "\n\n".join(result_parts)
        if not result_text:
            raise PiError(
                f"Pi {self.team_id}/{self.id} returned empty response",
                details={"model": effective_model},
            )
        return result_text

    async def _run_openai(
        self, task: str, system: str, model_override: Optional[str] = None,
    ) -> str:
        """Run via OpenAI Agents SDK."""
        effective_model = model_override or self.model
        try:
            from agents import Agent, Runner
        except ImportError:
            raise PiError(
                "openai-agents not installed. Run: pip install openai-agents"
            )

        if self.tool_registry:
            ctx = self._build_tool_context()
            mem_tools = self.tool_registry.get_openai_tools(ctx)
        else:
            # Fallback: hardcoded logic for backward compat
            mem_tools = create_memory_openai_tools(
                self.memory, self.team_id, self.id
            )

            uc = getattr(self, "_user_context", None)
            has_sched_tools = any(t in SCHEDULER_TOOL_NAMES or t == "scheduler"
                                 for t in self.tools_config)
            if uc and self.user_scheduler and has_sched_tools:
                sched_tools = create_scheduler_openai_tools(
                    self.user_scheduler,
                    user_id=uc.get("user_id", ""),
                    chat_id=uc.get("chat_id", ""),
                    channel=uc.get("channel", "telegram"),
                )
                mem_tools.extend(sched_tools)

            has_feed_tools = any(t in FEED_TOOL_NAMES or t == "collector"
                                for t in self.tools_config)
            if has_feed_tools:
                feed_tools = create_feed_openai_tools()
                mem_tools.extend(feed_tools)

            has_sessions_tools = any(t in SESSIONS_TOOL_NAMES or t == "messenger"
                                    for t in self.tools_config)
            if has_sessions_tools:
                uc = getattr(self, "_user_context", None) or {}
                extra_ctx = getattr(self, "_extra_tool_context", {})
                sessions_tools = create_sessions_openai_tools(
                    self.memory, self.team_id, self.id,
                    company_id=uc.get("company_id") if isinstance(uc, dict) else None,
                    shared_teams=extra_ctx.get("company_shared_teams"),
                )
                mem_tools.extend(sessions_tools)

        agent = Agent(
            name=f"{self.team_id}/{self.id}",
            model=effective_model,
            instructions=system,
            tools=mem_tools,
        )

        # Proxy env vars are set once at startup via Pi.configure_proxy()
        result = await Runner.run(agent, task)
        result_text = result.final_output or ""
        if not result_text:
            raise PiError(
                f"Pi {self.team_id}/{self.id} returned empty response",
                details={"model": effective_model},
            )
        return result_text



