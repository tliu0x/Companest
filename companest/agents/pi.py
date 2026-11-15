"""Companest Pi Agent"""

import asyncio
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]

from ..memory import FileBackend, MemoryManager, MemorySearchService
from .tools import ToolRegistry, ToolContext, DEFAULT_TOOLS_DENY
from .cascade import CascadeEngine, CascadeMetrics
from ..exceptions import PiError
from .model_routing import build_chat_model, detect_provider

if TYPE_CHECKING:
    from ..config import ProxyConfig

logger = logging.getLogger(__name__)
_run_tool_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("run_tool_context", default=None)


@dataclass
class PiConfig:
    id: str
    model: str = "deepseek-chat"
    tools: List[str] = field(default_factory=list)  # empty = all tools
    tools_deny: List[str] = field(default_factory=list)
    max_turns: int = 10


class Pi:
    """Minimal agent unit, built on LangChain's create_agent."""

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
        self.user_scheduler = None  # set by orchestrator
        self.last_usage: Optional[Dict[str, Any]] = None
        self._user_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("user_context", default=None)
        self._extra_tool_context: Dict[str, Any] = {}  # set by orchestrator

    @staticmethod
    def configure_proxy(proxy_config: "ProxyConfig") -> None:
        import os
        base_url = proxy_config.base_url.rstrip("/")
        api_key = proxy_config.default_key or proxy_config.master_key
        os.environ["ANTHROPIC_BASE_URL"] = base_url
        os.environ["OPENAI_BASE_URL"] = base_url + "/v1"
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
            os.environ["OPENAI_API_KEY"] = api_key

    async def run(
        self, task: str, timeout: float = 300.0, cascade: bool = False,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        context_token = self._user_context.set(user_context)
        try:
            system = self.memory.build_system_prompt(
                self.team_id, self.id,
                company_context=user_context.get("company_context") if user_context else None,
            )
            retrieval_task = self._resolve_memory_task_hint(task, user_context)
            relevant_memory = self._build_relevant_memory_section(retrieval_task)
            if relevant_memory:
                system = system + "\n\n---\n\n" + relevant_memory if system else relevant_memory
            if not cascade:
                return await self._run_single(task, system, timeout)

            engine = self.cascade_engine or CascadeEngine()
            metrics = CascadeMetrics.load(self.memory, self.team_id)
            chain = engine.get_effective_chain(self.model, task, metrics)

            if len(chain) <= 1:
                return await self._run_single(task, system, timeout)

            def short(m: str) -> str:
                return m.rsplit("-", 1)[0] if "-20" in m else m

            last_error: Optional[PiError] = None
            last_result: Optional[str] = None

            for i, model in enumerate(chain):
                is_last = (i == len(chain) - 1)
                if on_progress:
                    await on_progress(
                        f"\U0001f916 {self.team_id}/{self.id} ({short(model)}) "
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
                except PiError as e:
                    metrics.record(model, succeeded=False, quality=0.0)
                    logger.info(
                        f"[Pi:{self.team_id}/{self.id}] Cascade: "
                        f"{model} failed ({e}), escalating"
                    )
                    last_error = e
                if on_progress and not is_last:
                    await on_progress(f"\u2b06\ufe0f \u2192 {short(chain[i + 1])} [{i+2}/{len(chain)}]")

            metrics.save(self.memory, self.team_id)
            if last_result:
                return last_result
            raise last_error
        finally:
            self._user_context.reset(context_token)

    @staticmethod
    def _resolve_memory_task_hint(
        task: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        if user_context:
            raw_hint = user_context.get("memory_task_hint")
            if isinstance(raw_hint, str) and raw_hint.strip():
                return raw_hint.strip()
        return task.strip()

    def _build_relevant_memory_section(self, task_hint: str) -> str:
        if not task_hint:
            return ""

        try:
            entries = self._get_memory_search_service().retrieve_for_task(
                self.team_id, task_hint, limit=5, budget_chars=1800, include_archive=False,
            )
        except Exception as e:
            logger.debug("Task-aware memory retrieval failed for %s/%s: %s", self.team_id, self.id, e)
            return ""

        if not entries:
            return ""
        return "\n".join(["## Relevant Memory"] + [self._format_relevant_memory_entry(e) for e in entries])

    def _get_memory_search_service(self) -> MemorySearchService:
        backend = self.tool_registry.memory_backend if self.tool_registry else None
        return MemorySearchService(backend if backend is not None else FileBackend(self.memory))

    @staticmethod
    def _format_relevant_memory_entry(entry: Dict[str, Any]) -> str:
        prefix = f"- {entry.get('key', '')}"
        source = str(entry.get("source", "") or "")
        if source and source != "active":
            prefix += " [archive]"
        text = str(entry.get("text", "") or "").strip()
        return f"{prefix}: {text}".rstrip()

    async def _run_single(self, task: str, system: str, timeout: float) -> str:
        return await self._run_with_model(task, system, self.model, timeout)

    async def _run_with_model(
        self, task: str, system: str, model: str, timeout: float,
    ) -> str:
        if self.tool_registry:
            skill_instructions = self.tool_registry.get_skill_instructions(self.tools_config)
            if skill_instructions:
                system = system + "\n\n---\n\n" + skill_instructions
        ws_context = self._get_extra_tool_context().get("workspace_context")
        if ws_context:
            system = system + "\n\n---\n\n" + ws_context

        provider = detect_provider(model, bool(self.proxy_config and self.proxy_config.enabled))
        details = {"model": model, "provider": provider}
        logger.info(f"[Pi:{self.team_id}/{self.id}] Running task (model={model}, provider={provider})")
        try:
            return await asyncio.wait_for(self._run_agent(task, system, model), timeout=timeout)
        except asyncio.TimeoutError:
            raise PiError(f"Pi {self.team_id}/{self.id} timed out after {timeout}s", details=details)
        except PiError:
            raise
        except Exception as e:
            raise PiError(f"Pi {self.team_id}/{self.id} failed: {e}", details=details)

    async def _run_agent(self, task: str, system: str, model: str) -> str:
        from langchain.agents import create_agent
        from langgraph.errors import GraphRecursionError

        tools = await self._build_tools()
        agent = create_agent(
            build_chat_model(model, self.proxy_config),
            tools,
            system_prompt=system or None,
            name=re.sub(r"[^A-Za-z0-9_-]", "_", f"{self.team_id}_{self.id}"),
        )
        try:
            # Model and tool steps alternate, so 2 * max_turns allows exactly max_turns model calls.
            state = await agent.ainvoke(
                {"messages": [{"role": "user", "content": task}]},
                config={"recursion_limit": 2 * self.max_turns},
            )
        except GraphRecursionError:
            raise PiError(
                f"Pi {self.team_id}/{self.id} exceeded max_turns={self.max_turns}",
                details={"model": model},
            )

        ai_messages = [m for m in state["messages"] if m.type == "ai"]
        self.last_usage = self._sum_usage(ai_messages, model)
        logger.info(f"[Pi:{self.team_id}/{self.id}] usage={self.last_usage}")
        result_text = ai_messages[-1].text.strip() if ai_messages else ""
        if not result_text:
            raise PiError(
                f"Pi {self.team_id}/{self.id} returned empty response",
                details={"model": model},
            )
        return result_text

    async def _build_tools(self) -> list:
        if not self.tool_registry:
            return []
        ctx = self._build_tool_context()
        tools = self.tool_registry.get_tools(ctx)
        tools.extend(await self.tool_registry.get_external_mcp_tools(
            company_id=ctx.company_id, tools_deny=ctx.tools_deny,
        ))
        return tools

    @staticmethod
    def _sum_usage(ai_messages: list, model: str) -> Dict[str, Any]:
        usage = {"model": model, "input_tokens": 0, "output_tokens": 0}
        for message in ai_messages:
            reported = message.usage_metadata or {}
            usage["input_tokens"] += reported.get("input_tokens", 0)
            usage["output_tokens"] += reported.get("output_tokens", 0)
        return usage

    def _build_deny_set(self) -> set:
        if not self.tools_deny:
            return set(DEFAULT_TOOLS_DENY)
        if self.tools_deny == ["none"]:
            return set()
        return set(self.tools_deny)

    def _build_tool_context(self) -> ToolContext:
        uc = self._user_context.get() or {}
        return ToolContext(
            memory=self.memory,
            team_id=self.team_id,
            pi_id=self.id,
            tools_config=self.tools_config,
            user_context=uc or None,
            user_scheduler=self.user_scheduler,
            tools_deny=self._build_deny_set(),
            extra=self._get_extra_tool_context(),
            memory_backend=self.tool_registry.memory_backend,
            company_id=uc.get("company_id"),
        )

    def _get_extra_tool_context(self) -> Dict[str, Any]:
        return self._extra_tool_context | (_run_tool_context.get() or {})
