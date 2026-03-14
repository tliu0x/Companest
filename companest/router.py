"""
Companest Team Auto-Router

Maps inbound task content to the most appropriate team(s).
Two routing layers:
1. SmartRouter  LLM-powered routing (reads team soul.md, returns structured JSON)
2. TeamRouter  keyword regex fallback (used when LLM is unavailable)

Routing priority:
1. Explicit team tag (e.g. "@stock ...", "#engineering ...")
2. LLM routing via SmartRouter (Haiku, ~$0.001)
3. Keyword pattern matching (fallback)
4. Decline (no default team)
"""

import asyncio
import hashlib
import json
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .team import TeamRegistry
    from .memory import MemoryManager
    from .config import ProxyConfig
    from .events import EventBus

logger = logging.getLogger(__name__)

# Keyword  team_id mapping (checked in order)
# Each entry: (team_id, [keyword_patterns], priority)
DEFAULT_ROUTES: List[Tuple[str, List[str], int]] = [
    ("stock", [
        r"stock|ticker|etf|spy|qqq|tsla|nvda|aapl",
        r"candlestick|macd|rsi|moving[- ]average|technical[- ]analysis",
        r"market|crypto|btc|eth",
        r"earnings|pe[- ]ratio|market[- ]cap",
    ], 10),
    ("bizdev", [
        r"buy\w*\s+\w*\s*(?:car|house)|sell\w*\s+\w*\s*(?:car|house)|rent|moving",
        r"business|partnership|deal",
        r"negotiate|contract|quote",
        r"invest|insurance|loan|mortgage|real[- ]estate",
    ], 8),
    ("finance", [
        r"budget|spending|cost|balance",
        r"token[- ]cost|expense|reimbursement|account",
    ], 7),
    ("engineering", [
        r"code|bug|debug|programming",
        r"api|sdk|framework|deploy",
        r"python|javascript|typescript|rust|go\b",
        r"docker|k8s|kubernetes|ci[- ]cd|git\b",
        r"database|sql|redis|mongo",
    ], 6),
    ("science", [
        r"paper|research|experiment",
        r"data[- ]analysis|statistics",
        r"machine[- ]learning|ml\b|deep[- ]learning|neural",
        r"physics|chemistry|biology|math",
    ], 5),
    ("philosophy", [
        r"philosophy|metaphysics",
        r"existence|consciousness|free[- ]will",
        r"ethics|moral|meaning[- ]of[- ]life",
        r"epistemology|ontology",
    ], 4),
]

# Meta-teams that should never be routing targets
META_TEAM_ROLES = {"cost_gate", "memory", "scheduler", "research"}

# Valid execution modes the router can select (canonical source: companest.modes)
from .modes import VALID_MODES


#  Data Models 

@dataclass
class RoutingBinding:
    """A deterministic regex binding rule for fast routing (before LLM call)."""
    pattern: re.Pattern
    team_id: str
    mode: str = "cascade"
    priority: int = 1
    owner_company_id: Optional[str] = None


@dataclass
class TeamAssignment:
    """A single team assignment within a routing decision."""
    team_id: str          # e.g. "stock"
    instruction: str      # refined task for this specific team
    priority: int = 1     # 1=primary, 2+=supporting


@dataclass
class RoutingDecision:
    """Result of SmartRouter.route()  which team(s) and how."""
    teams: List[TeamAssignment] = field(default_factory=list)
    strategy: str = "single"      # "single" | "sequential" | "parallel"
    reasoning: str = ""           # one-line explanation
    confidence: float = 0.0
    declined: bool = False
    decline_reason: Optional[str] = None
    mode: str = "default"         # "default" | "cascade" | "loop" | "council"
    task_priority: str = "normal" # "critical" | "high" | "normal" | "low"


#  Keyword Router (fallback) 

class TeamRouter:
    """
    Auto-routes tasks to teams based on keyword pattern matching.

    Usage:
        router = TeamRouter(available_teams=["stock", "bizdev", ...])
        team_id = router.route("Analyze TSLA recent trends")
        #  "stock"
    """

    def __init__(
        self,
        available_teams: Optional[List[str]] = None,
        default_team: Optional[str] = "philosophy",
        custom_routes: Optional[List[Tuple[str, List[str], int]]] = None,
    ):
        self.default_team = default_team
        self.available_teams = set(available_teams) if available_teams else None

        # Build compiled patterns
        routes = custom_routes or DEFAULT_ROUTES
        self._routes: List[Tuple[str, re.Pattern, int]] = []
        for team_id, patterns, priority in routes:
            combined = "|".join(f"(?:{p})" for p in patterns)
            compiled = re.compile(combined, re.IGNORECASE)
            self._routes.append((team_id, compiled, priority))

        # Sort by priority descending
        self._routes.sort(key=lambda x: x[2], reverse=True)

    def route(self, task: str) -> Optional[str]:
        """
        Determine the best team for a task.

        Returns:
            team_id string, or None if default_team is None and nothing matched.
        """
        # 1. Check explicit team tag: @team_name or #team_name
        explicit = self._check_explicit_tag(task)
        if explicit:
            return explicit

        # 2. Keyword pattern matching
        best_team = None
        best_score = 0

        for team_id, pattern, priority in self._routes:
            if self.available_teams and team_id not in self.available_teams:
                continue
            matches = pattern.findall(task)
            if matches:
                score = len(matches) * priority
                if score > best_score:
                    best_score = score
                    best_team = team_id

        if best_team:
            logger.debug(f"[Router] '{task[:50]}...'  {best_team} (score={best_score})")
            return best_team

        # 3. Default fallback (may be None)
        if self.default_team:
            logger.debug(f"[Router] '{task[:50]}...'  {self.default_team} (default)")
        return self.default_team

    def _check_explicit_tag(self, task: str) -> Optional[str]:
        """Check for @team or #team prefix. Supports hyphenated and namespaced IDs."""
        match = re.match(r"^[@#]([\w][\w-]*(?:/[\w][\w-]*)?)\s+", task)
        if match:
            team_id = match.group(1).lower()
            if self.available_teams is None or team_id in self.available_teams:
                logger.debug(f"[Router] Explicit tag: @{team_id}")
                return team_id
        return None

    def route_with_confidence(self, task: str) -> Tuple[Optional[str], float]:
        """
        Route with confidence score (0.0-1.0).

        Confidence reflects how strongly the task matches a team:
        - 1.0: explicit @team tag
        - 0.7-0.9: multiple keyword matches with clear winner
        - 0.3-0.6: single match or weak signal
        - 0.0: no matches (default fallback)

        Returns:
            (team_id, confidence)  team_id may be None if no default
        """
        explicit = self._check_explicit_tag(task)
        if explicit:
            return explicit, 1.0

        scores: Dict[str, float] = {}
        for team_id, pattern, priority in self._routes:
            if self.available_teams and team_id not in self.available_teams:
                continue
            matches = pattern.findall(task)
            if matches:
                scores[team_id] = len(matches) * priority

        if not scores:
            return self.default_team, 0.0

        best_team = max(scores, key=scores.get)
        best_score = scores[best_team]

        # Confidence = combination of two signals:
        # 1. Dominance: how much better the winner is vs. runner-up
        # 2. Strength: absolute match count (more matches = more confident)
        sorted_scores = sorted(scores.values(), reverse=True)
        if len(sorted_scores) >= 2:
            dominance = 1.0 - (sorted_scores[1] / sorted_scores[0])
        else:
            dominance = 0.5  # Single match  moderate, not certain

        # Strength: diminishing returns  1 match=0.4, 2=0.6, 3+=0.8
        match_count = best_score / max(p for _, _, p in self._routes)
        strength = min(0.4 + match_count * 0.2, 1.0)

        confidence = round(min(dominance * 0.4 + strength * 0.6, 1.0), 2)

        return best_team, confidence


#  SmartRouter (LLM-powered) 

_ROUTING_SYSTEM_PROMPT_TEMPLATE = """\
You are a ROUTING ENGINE (not an assistant). You do NOT answer user questions. You ONLY output a JSON routing decision.

## Available Teams
{team_descriptions}

## Rules
1. ALWAYS route to at least one team. NEVER decline. Every message can be handled by some team.
2. Pick the MINIMUM number of teams needed. Most tasks need exactly 1 team.
3. Use multi-team (sequential/parallel) ONLY for genuinely cross-domain tasks.
4. "sequential" = teams run in order (output of one feeds the next). "parallel" = teams run simultaneously.
5. The "instruction" field should be a refined, specific instruction for that particular team.
6. For general conversation, casual chat, meta-questions, or anything that doesn't clearly fit another team, route to the most general/philosophical team.

## Decision Framework
Reason along three dimensions:
- **Logos**: Which team's domain expertise best matches the task's factual/technical content?
- **Pathos**: What does the user actually need? Is this high-stakes (prefer council) or routine?
- **Ethos**: Which team is most credible and reliable for this type of task?

If Pathos indicates high stakes or emotional sensitivity, prefer "council" mode.
If Logos identifies clearly multi-step analysis, prefer "loop" mode.

## Execution Mode
Choose the best execution mode:
- "cascade": Cost-optimized. Tries a cheap model first, escalates on failure. USE THIS FOR 90% OF TASKS.
- "default": Simple single-turn task. One Pi answers directly. Only for trivial greetings like "hi".
- "loop": Complex multi-step task requiring decomposition. Only when task has 3+ distinct steps.
- "council": EXPENSIVE  runs ALL Pis in parallel then synthesizes. ONLY use when the user EXPLICITLY requests multiple perspectives, or for life-changing decisions (career, ethics dilemmas). NEVER for casual questions or general conversation.
- "collaborative": Pipeline  Pis run sequentially, each building on the previous output. Use when the team has specialized Pis for distinct stages (e.g. research  analyze  write).

## Task Priority
Assess the task priority for budget allocation:
- "critical": Urgent, time-sensitive, or safety-related tasks
- "high": Important tasks that justify higher cost
- "normal": Standard tasks (default for most)
- "low": Background, nice-to-have, or exploratory tasks

## Keyword Signal
The user message may include a `[Keyword signal: team=X, confidence=Y]` line. This is an automated hint from pattern matching  consider it as one input but YOU make the final decision. Override it when your analysis disagrees.

## Response Format
Return ONLY valid JSON, no markdown fencing:
{{"teams": [{{"team_id": "...", "instruction": "...", "priority": 1}}], "strategy": "single", "mode": "cascade", "task_priority": "normal", "reasoning": "Logos: ... Pathos: ... Ethos: ...", "confidence": 0.85, "declined": false}}
"""


class SmartRouter:
    """
    LLM-powered team router. Uses a cheap Kimi K2.5 call to analyze tasks
    and decide which team(s) should handle them.

    Falls back to keyword routing when LLM is unavailable.
    Explicitly declines when no team fits (no random defaults).
    """

    # Routing LLM config
    ROUTING_MODEL = "kimi-k2.5"
    ROUTING_MAX_TOKENS = 512
    ROUTING_TIMEOUT = 15  # seconds (slightly longer for non-Anthropic models)

    def __init__(
        self,
        team_registry: "TeamRegistry",
        memory: "MemoryManager",
        proxy_config: Optional["ProxyConfig"] = None,
        event_bus: Optional["EventBus"] = None,
    ):
        self._registry = team_registry
        self._memory = memory
        self._proxy_config = proxy_config
        self._event_bus = event_bus

        # Keyword router as fallback (no default  decline if no match)
        available = [
            t for t in team_registry.list_teams()
            if t not in team_registry.list_meta_teams()
        ]
        self._keyword_router = TeamRouter(
            available_teams=available,
            default_team=None,
        )

        # Binding rules (fast path before LLM)
        self._bindings: List[RoutingBinding] = []

        # System prompt cache
        self._cached_prompt: Optional[str] = None
        self._cached_prompt_hash: Optional[str] = None

    def add_binding(
        self, pattern: str, team_id: str, mode: str = "cascade", priority: int = 1,
        owner_company_id: Optional[str] = None,
    ) -> None:
        """Add a deterministic regex binding rule for fast routing.

        Bindings are checked before the LLM call. First match wins.
        """
        compiled = re.compile(pattern, re.IGNORECASE)
        self._bindings.append(RoutingBinding(
            pattern=compiled, team_id=team_id, mode=mode, priority=priority,
            owner_company_id=owner_company_id,
        ))
        logger.info(f"[SmartRouter] Added binding: /{pattern}/  {team_id} ({mode})")

    def remove_bindings_by_owner(self, company_id: str) -> int:
        """Remove all bindings owned by a company."""
        before = len(self._bindings)
        self._bindings = [b for b in self._bindings if b.owner_company_id != company_id]
        removed = before - len(self._bindings)
        if removed:
            self.invalidate_cache()
        return removed

    def get_available_teams(
        self, company_id: Optional[str] = None,
        shared_teams: Optional[List[str]] = None,
    ) -> List[str]:
        """Return teams accessible by a company.

        - Global teams (no '/'): if company_id given, only include if in shared_teams whitelist
        - Private teams ('{owner}/'): included only if owner == company_id
        - No company_id: global teams only
        - shared_teams=None (not passed): backward compat, include all global teams
        """
        all_teams = self._registry.list_teams()
        meta = set(self._registry.list_meta_teams())
        result = []
        for t in all_teams:
            if t in meta:
                continue
            if "/" not in t:
                # Global team
                if company_id is None:
                    result.append(t)
                elif shared_teams is not None and t in shared_teams:
                    result.append(t)
                elif shared_teams is None:
                    result.append(t)  # backward compat: no whitelist = all global teams
            elif company_id and t.startswith(f"{company_id}/"):
                result.append(t)
        return result

    async def route(
        self, task: str, preferred_teams: Optional[List[str]] = None,
        company_id: Optional[str] = None,
        shared_teams: Optional[List[str]] = None,
    ) -> RoutingDecision:
        """
        Route a task to the best team(s).

        Priority:
        1. Explicit @team / #team tag  single team, confidence=1.0
        2. LLM routing (Haiku)  RoutingDecision
        3. Keyword fallback  RoutingDecision
        4. No match  declined=True

        Args:
            task: The user's task/prompt.
            preferred_teams: Optional hint from company preferences (not forced).
            company_id: Restricts routing to global + this company's private teams.
        """
        # Build the set of teams this caller is allowed to use.
        # When company_id is None, restrict to global teams only (no private teams).
        allowed_teams = set(self.get_available_teams(company_id, shared_teams=shared_teams))

        # 1. Check explicit tag
        explicit = self._keyword_router._check_explicit_tag(task)
        if explicit:
            if explicit not in allowed_teams:
                logger.warning(f"[SmartRouter] Explicit tag @{explicit} denied for company={company_id}")
                return RoutingDecision(
                    declined=True,
                    decline_reason=f"Team '{explicit}' is not accessible to company '{company_id}'.",
                    confidence=0.0,
                )
            logger.info(f"[SmartRouter] Explicit tag  {explicit}")
            decision = RoutingDecision(
                teams=[TeamAssignment(team_id=explicit, instruction=task, priority=1)],
                strategy="single",
                reasoning=f"Explicit @{explicit} tag",
                confidence=1.0,
            )
            await self._emit_routing_audit(task, decision, source="explicit_tag")
            return decision

        # 1.5. Check binding rules (fast path before LLM)
        for binding in self._bindings:
            if binding.pattern.search(task):
                # Validate team exists and is allowed
                registered = set(self._registry.list_teams())
                if binding.team_id in registered:
                    if binding.team_id not in allowed_teams:
                        continue  # skip binding for teams the caller cannot access
                    logger.info(f"[SmartRouter] Binding match  {binding.team_id}")
                    decision = RoutingDecision(
                        teams=[TeamAssignment(
                            team_id=binding.team_id, instruction=task, priority=binding.priority,
                        )],
                        strategy="single",
                        reasoning=f"Binding match: /{binding.pattern.pattern}/",
                        confidence=0.95,
                        mode=binding.mode,
                    )
                    await self._emit_routing_audit(task, decision, source="binding")
                    return decision

        # 2. Try LLM routing
        try:
            decision = await self._llm_route(task, preferred_teams=preferred_teams, company_id=company_id)
            if decision:
                # Filter out teams the caller cannot access
                decision.teams = [t for t in decision.teams if t.team_id in allowed_teams]
                if decision.teams:
                    logger.info(
                        f"[SmartRouter] LLM routed  "
                        f"{[t.team_id for t in decision.teams]} "
                        f"({decision.strategy}, conf={decision.confidence:.0%})"
                    )
                    await self._emit_routing_audit(task, decision, source="llm")
                    return decision
        except Exception as e:
            logger.warning(f"[SmartRouter] LLM routing failed: {e}, falling back to keywords")

        # 3. Keyword fallback
        decision = self._keyword_fallback(task)
        if decision.teams:
            decision.teams = [t for t in decision.teams if t.team_id in allowed_teams]
            if not decision.teams:
                decision.declined = True
                decision.decline_reason = "No accessible team matched."
        await self._emit_routing_audit(task, decision, source="keyword_fallback")
        return decision

    async def _llm_route(
        self, task: str, preferred_teams: Optional[List[str]] = None,
        company_id: Optional[str] = None,
    ) -> Optional[RoutingDecision]:
        """Single-turn Kimi K2.5 call to determine routing."""
        from openai import AsyncOpenAI

        system_prompt = self._build_system_prompt(company_id=company_id)
        if not system_prompt:
            return None

        # Run keyword router as a reference signal for the LLM
        keyword_team, keyword_conf = self._keyword_router.route_with_confidence(task)
        user_message = task
        if keyword_team and keyword_conf > 0:
            user_message = (
                f"{task}\n\n"
                f"[Keyword signal: team={keyword_team}, confidence={keyword_conf:.0%}]"
            )
        if preferred_teams:
            user_message += f"\n[Company preferred teams: {', '.join(preferred_teams)}]"

        # Build client  Kimi uses OpenAI-compatible API
        client_kwargs: Dict[str, Any] = {}
        if self._proxy_config and self._proxy_config.enabled:
            client_kwargs["base_url"] = self._proxy_config.base_url.rstrip("/") + "/v1"
            api_key = self._proxy_config.default_key or self._proxy_config.master_key
            if api_key:
                client_kwargs["api_key"] = api_key
        else:
            # Direct Moonshot API (no proxy)
            import os
            client_kwargs["base_url"] = "https://api.moonshot.cn/v1"
            client_kwargs["api_key"] = os.getenv("MOONSHOT_API_KEY", "")

        client = AsyncOpenAI(**client_kwargs)

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.ROUTING_MODEL,
                    max_tokens=self.ROUTING_MAX_TOKENS,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                ),
                timeout=self.ROUTING_TIMEOUT,
            )
        finally:
            await client.close()

        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return None

        return self._parse_response(raw)

    def _build_system_prompt(self, company_id: Optional[str] = None) -> Optional[str]:
        """Build routing system prompt from team soul.md files. Cached by team list + company hash."""
        teams = self._registry.list_teams()
        meta = set(self._registry.list_meta_teams())
        # Filter out meta-teams, and restrict to teams accessible to this company
        business_teams = []
        for t in teams:
            if t in meta:
                continue
            if "/" not in t:
                # Global team  always visible
                business_teams.append(t)
            elif company_id and t.startswith(f"{company_id}/"):
                # This company's private team
                business_teams.append(t)
            # else: another company's private team  hide from prompt

        if not business_teams:
            return None

        # Check cache (keyed by team list + company_id)
        hash_input = ",".join(sorted(business_teams)) + f"|{company_id or ''}"
        team_hash = hashlib.sha256(hash_input.encode()).hexdigest()
        if self._cached_prompt and self._cached_prompt_hash == team_hash:
            return self._cached_prompt

        # Build team descriptions
        descriptions = []
        for team_id in sorted(business_teams):
            soul = self._memory.read_team_soul(team_id)
            # Extract a concise description (first ~200 chars of soul.md)
            if soul:
                # Try to find a duties/responsibilities section
                summary = _extract_soul_summary(soul, team_id)
            else:
                summary = f"Team: {team_id}"
            descriptions.append(f"### {team_id}\n{summary}")

        team_block = "\n\n".join(descriptions)
        self._cached_prompt = _ROUTING_SYSTEM_PROMPT_TEMPLATE.format(
            team_descriptions=team_block
        )
        self._cached_prompt_hash = team_hash
        return self._cached_prompt

    def _parse_response(self, raw: str) -> Optional[RoutingDecision]:
        """Parse LLM JSON response into RoutingDecision.

        Handles: raw JSON, markdown-fenced JSON, and malformed output.
        Validates team_ids against registry.
        """
        # Try to extract JSON
        json_str = raw.strip()

        # Strip markdown code fences
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", json_str)
        if fence_match:
            json_str = fence_match.group(1).strip()

        # Try to find JSON object boundaries
        if not json_str.startswith("{"):
            brace_match = re.search(r"\{[\s\S]*\}", json_str)
            if brace_match:
                json_str = brace_match.group(0)
            else:
                logger.warning(f"[SmartRouter] Could not find JSON in response: {raw[:100]}")
                return None

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"[SmartRouter] JSON parse error: {e}, raw={raw[:200]}")
            return None

        # Handle declined
        if data.get("declined", False):
            return RoutingDecision(
                declined=True,
                decline_reason=data.get("decline_reason", data.get("reasoning", "")),
                reasoning=data.get("reasoning", ""),
                confidence=0.0,
            )

        # Parse team assignments
        raw_teams = data.get("teams", [])
        if not isinstance(raw_teams, list) or not raw_teams:
            logger.warning(f"[SmartRouter] No teams in response: {data}")
            return None

        # Validate team_ids against registry
        registered = set(self._registry.list_teams())
        meta = set(self._registry.list_meta_teams())
        valid_teams: List[TeamAssignment] = []

        for t in raw_teams:
            if not isinstance(t, dict):
                continue
            tid = t.get("team_id", "")
            if tid in registered and tid not in meta:
                valid_teams.append(TeamAssignment(
                    team_id=tid,
                    instruction=t.get("instruction", ""),
                    priority=t.get("priority", 1),
                ))
            else:
                logger.debug(f"[SmartRouter] Filtered unknown/meta team: {tid}")

        if not valid_teams:
            logger.warning(f"[SmartRouter] All teams filtered out from LLM response")
            return None

        strategy = data.get("strategy", "single")
        if strategy not in ("single", "sequential", "parallel"):
            strategy = "single"

        # Single team  force strategy to "single"
        if len(valid_teams) == 1:
            strategy = "single"

        confidence = float(data.get("confidence", 0.8))
        confidence = max(0.0, min(1.0, confidence))

        mode = data.get("mode", "default")
        if mode not in VALID_MODES:
            mode = "default"

        task_priority = data.get("task_priority", "normal")
        if task_priority not in ("critical", "high", "normal", "low"):
            task_priority = "normal"

        return RoutingDecision(
            teams=valid_teams,
            strategy=strategy,
            reasoning=data.get("reasoning", ""),
            confidence=confidence,
            mode=mode,
            task_priority=task_priority,
        )

    async def _emit_routing_audit(
        self, task: str, decision: RoutingDecision, source: str,
    ) -> None:
        """Emit a TASK_ROUTED event for audit/debugging. Best-effort."""
        if not self._event_bus:
            return
        try:
            from .events import EventType
            available = [
                t for t in self._registry.list_teams()
                if t not in self._registry.list_meta_teams()
            ]
            await self._event_bus.emit(EventType.TASK_ROUTED, {
                "input_summary": task[:200],
                "source": source,
                "candidate_teams": available,
                "selected_teams": [t.team_id for t in decision.teams],
                "strategy": decision.strategy,
                "mode": decision.mode,
                "confidence": decision.confidence,
                "reasoning": decision.reasoning,
                "declined": decision.declined,
            })
        except Exception as e:
            logger.debug(f"[SmartRouter] Routing audit emit failed: {e}")

    def _keyword_fallback(self, task: str) -> RoutingDecision:
        """Use keyword router as fallback when LLM is unavailable."""
        team_id, confidence = self._keyword_router.route_with_confidence(task)

        if team_id is None:
            return RoutingDecision(
                declined=True,
                decline_reason="No team matched and no LLM available for smart routing.",
                confidence=0.0,
            )

        return RoutingDecision(
            teams=[TeamAssignment(team_id=team_id, instruction=task, priority=1)],
            strategy="single",
            reasoning=f"Keyword match (fallback)",
            confidence=confidence,
            mode="cascade",  # fallback uses cascade  try cheap model first, escalate
        )

    def invalidate_cache(self) -> None:
        """Force rebuild of system prompt and keyword router on next route() call."""
        self._cached_prompt = None
        self._cached_prompt_hash = None
        # Rebuild keyword router with current team list
        available = [
            t for t in self._registry.list_teams()
            if t not in self._registry.list_meta_teams()
        ]
        self._keyword_router = TeamRouter(
            available_teams=available,
            default_team=None,
        )


def _extract_soul_summary(soul_text: str, team_id: str) -> str:
    """Extract a concise summary from a team's soul.md for the routing prompt."""
    lines = soul_text.strip().splitlines()
    summary_lines = []
    in_duties = False

    for line in lines:
        stripped = line.strip()
        # Look for duties/responsibilities section
        if re.match(r"^#{1,4}\s*(duties|responsibilities|capabilities)", stripped, re.I):
            in_duties = True
            continue
        # Stop at next heading
        if in_duties and re.match(r"^#{1,4}\s", stripped):
            break
        if in_duties and stripped:
            summary_lines.append(stripped)

    if summary_lines:
        return "\n".join(summary_lines[:8])  # Max 8 lines

    # Fallback: first meaningful lines (skip title)
    content_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        content_lines.append(stripped)
        if len(content_lines) >= 4:
            break

    return "\n".join(content_lines) if content_lines else f"Team: {team_id}"
