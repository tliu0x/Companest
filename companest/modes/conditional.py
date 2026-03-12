"""
Conditional execution mode -lead Pi evaluates -branch decision -execute.

ConditionalMode: lead Pi receives the task and decides which team/mode
to route to. Optionally loops back for multi-step decisions.
"""

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from . import ExecutionMode, ProgressCallback
from ..exceptions import PiError, TeamError
from ..utils import extract_json_object

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..team import AgentTeam

logger = logging.getLogger(__name__)


def _with_memory_task_hint(
    user_context: Optional[Dict[str, Any]],
    task_hint: str,
) -> Dict[str, Any]:
    """Attach a retrieval hint for internal conditional prompts."""
    ctx = dict(user_context) if user_context else {}
    if task_hint:
        ctx["memory_task_hint"] = task_hint
    return ctx

# Type for the injected run_team function
RunTeamFn = Callable[..., Coroutine[Any, Any, str]]


class ConditionalMode(ExecutionMode):
    """Conditional branching: lead Pi evaluates task -picks branch team -executes.

    Flow:
        1. Lead Pi evaluates task -returns branch decision (JSON)
        2. Based on decision, call run_team_fn(branch_team, ...)
        3. Optional: loop back to lead Pi for next-step decision (max_steps)
    """

    def __init__(self, run_team_fn: RunTeamFn, max_steps: int = 5):
        self._run_team_fn = run_team_fn
        self._max_steps = max_steps

    @property
    def name(self) -> str:
        return "conditional"

    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not team.lead_pi_id or team.lead_pi_id not in team.pis:
            raise TeamError(
                f"Team '{team.id}' has no valid lead_pi",
                details={"lead_pi": team.lead_pi_id, "available": list(team.pis.keys())},
            )

        pi = team.pis[team.lead_pi_id]
        accumulated_results: List[str] = []

        for step in range(self._max_steps):
            # Build decision prompt with accumulated context
            progress = ""
            if accumulated_results:
                progress = "\n\n## Previous Steps\n" + "\n---\n".join(
                    f"Step {i+1}: {r}" for i, r in enumerate(accumulated_results)
                )

            decision_prompt = (
                "You are a routing coordinator. Analyze the task and decide the next action.\n\n"
                f"## Task\n{task}\n"
                f"{progress}\n\n"
                "## Instructions\n"
                "Return ONLY valid JSON (no markdown fencing):\n"
                '{"team": "<team_id>", "mode": "default", "sub_task": "<refined task for that team>", "done": false}\n\n'
                'Set "done": true when the task is fully handled (include a "summary" field with the final answer).\n'
                'Set "done": false to route to a team for the next step.\n'
                "Available modes: default, cascade, loop, council"
            )

            if on_progress:
                await on_progress(f"\U0001f500 Conditional step {step + 1}: evaluating...")

            try:
                raw = await pi.run(
                    decision_prompt,
                    cascade=True,
                    user_context=_with_memory_task_hint(user_context, task),
                )
            except PiError as e:
                logger.error(f"[Team:{team.id}] Conditional decision failed at step {step + 1}: {e}")
                if accumulated_results:
                    return accumulated_results[-1]
                raise

            decision = _parse_decision(raw)

            # Retry once if JSON parsing failed
            if not decision:
                logger.info(f"[Team:{team.id}] Decision parse failed, retrying with correction prompt")
                try:
                    retry_prompt = (
                        "Your previous response was not valid JSON. "
                        "Please respond with ONLY a valid JSON object, no other text:\n"
                        '{"team": "<team_id>", "mode": "default", "sub_task": "<task>", "done": false}\n'
                        'Or: {"done": true, "summary": "<final answer>"}'
                    )
                    raw = await pi.run(
                        retry_prompt,
                        cascade=True,
                        user_context=_with_memory_task_hint(user_context, task),
                    )
                    decision = _parse_decision(raw)
                except PiError:
                    pass

            if not decision:
                logger.warning(f"[Team:{team.id}] Could not parse decision after retry, returning raw LLM output")
                return raw

            # Validate required fields
            if not decision.get("done") and not decision.get("team"):
                logger.warning(f"[Team:{team.id}] Decision missing 'team' and not done, returning raw")
                return raw

            # Done -return summary
            if decision.get("done"):
                summary = decision.get("summary", "")
                if summary:
                    return summary
                if accumulated_results:
                    return accumulated_results[-1]
                return raw

            # Route to branch team
            branch_team = decision.get("team", "")
            branch_mode = decision.get("mode", "default")
            sub_task = decision.get("sub_task", task)

            if on_progress:
                await on_progress(
                    f"\U0001f500 Routing to {branch_team} (mode={branch_mode})"
                )

            logger.info(
                f"[Team:{team.id}] Conditional step {step + 1}: "
                f"routing to {branch_team} (mode={branch_mode})"
            )

            try:
                result = await self._run_team_fn(
                    task=sub_task,
                    team_id=branch_team,
                    mode=branch_mode,
                    on_progress=on_progress,
                    user_context=_with_memory_task_hint(user_context, sub_task),
                    priority=decision.get("priority", "normal"),
                )
                accumulated_results.append(result)
            except Exception as e:
                logger.error(
                    f"[Team:{team.id}] Branch team '{branch_team}' failed: {e}"
                )
                if accumulated_results:
                    return accumulated_results[-1]
                raise TeamError(
                    f"Conditional branch '{branch_team}' failed: {e}",
                    details={"step": step + 1, "team": branch_team},
                )

        # Max steps reached -return last result
        logger.warning(
            f"[Team:{team.id}] Conditional mode hit max_steps={self._max_steps}"
        )
        if accumulated_results:
            return accumulated_results[-1]
        raise TeamError(f"Conditional mode produced no results after {self._max_steps} steps")


def _parse_decision(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON decision from LLM output using robust balanced-brace extraction."""
    return extract_json_object(raw)

