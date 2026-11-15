import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

from ..modes import ProgressCallback, _lead_pi, _with_memory_task_hint
from ..exceptions import PiError, TeamError
from ..utils import extract_json_object

if TYPE_CHECKING:
    from ..agents.team import AgentTeam

logger = logging.getLogger(__name__)

RunTeamFn = Callable[..., Coroutine[Any, Any, str]]


class Delegation:
    """Conditional branching: lead Pi evaluates task -picks branch team -executes."""

    def __init__(self, run_team_fn: RunTeamFn, max_steps: int = 5, allowed_teams=None):
        self._run_team_fn = run_team_fn
        self._max_steps = max_steps
        self._allowed_teams = allowed_teams

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
        pi = _lead_pi(team)
        accumulated_results: List[str] = []

        for step in range(self._max_steps):
            progress = ""
            if accumulated_results:
                progress = "\n\n## Previous Steps\n" + "\n---\n".join(
                    f"Step {i+1}: {r}" for i, r in enumerate(accumulated_results)
                )

            decision_prompt = (
                "You are a routing coordinator. Analyze the task and decide the next action.\n\n"
                f"## Task\n{task}\n"
                f"## Allowed teams\n{self._allowed_teams or []}\n"
                f"{progress}\n\n"
                "## Instructions\n"
                "Return ONLY valid JSON (no markdown fencing):\n"
                '{"team": "<team_id>", "mode": "default", "sub_task": "<refined task for that team>", "done": false}\n\n'
                'Set "done": true when the task is fully handled (include a "summary" field with the final answer).\n'
                'Set "done": false to route to a team for the next step.\n'
                "Available modes: default, collaborative, council"
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

            if not decision.get("done") and not decision.get("team"):
                logger.warning(f"[Team:{team.id}] Decision missing 'team' and not done, returning raw")
                return raw

            if decision.get("done"):
                summary = decision.get("summary", "")
                if summary:
                    return summary
                if accumulated_results:
                    return accumulated_results[-1]
                return raw

            branch_team = decision["team"]
            if self._allowed_teams is not None and branch_team not in self._allowed_teams:
                raise TeamError("Delegation target is outside the allowed teams")
            branch_mode = decision.get("mode", "default")
            if branch_mode not in ("default", "collaborative", "council", "cascade"):
                raise TeamError("Delegation cannot start a nested workflow")
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
                    user_context=_with_memory_task_hint(
                        {k: v for k, v in (user_context or {}).items() if k != "workflow"}, sub_task),
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

        logger.warning(
            f"[Team:{team.id}] Delegation hit max_steps={self._max_steps}"
        )
        if accumulated_results:
            return accumulated_results[-1]
        raise TeamError(f"Delegation produced no results after {self._max_steps} steps")


_parse_decision = extract_json_object
