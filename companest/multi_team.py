import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from .router import RoutingDecision
from .exceptions import OrchestratorError

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[str], Coroutine[Any, Any, None]]]


def _with_memory_task_hint(
    user_context: Optional[Dict[str, Any]],
    task_hint: str,
) -> Dict[str, Any]:
    """Attach the per-team instruction for downstream memory retrieval."""
    ctx = dict(user_context) if user_context else {}
    if task_hint:
        ctx["memory_task_hint"] = task_hint
    return ctx


async def run_multi_team(
    run_team_fn: Callable,
    decision: RoutingDecision,
    mode: str = "default",
    on_progress: ProgressCallback = None,
    user_context: Optional[Dict[str, Any]] = None,
) -> str:
    assignments = sorted(decision.teams, key=lambda a: a.priority)

    if decision.strategy == "parallel":
        return await _run_parallel(run_team_fn, assignments, mode, on_progress, user_context)
    return await _run_sequential(run_team_fn, assignments, mode, on_progress, user_context)


async def _run_parallel(
    run_team_fn: Callable,
    assignments: List,
    mode: str,
    on_progress: ProgressCallback = None,
    user_context: Optional[Dict[str, Any]] = None,
) -> str:
    async def _run_one(assignment):
        result = await run_team_fn(
            assignment.instruction, assignment.team_id,
            mode=mode, on_progress=on_progress,
            user_context=_with_memory_task_hint(user_context, assignment.instruction),
        )
        return assignment.team_id, result

    tasks = [_run_one(a) for a in assignments]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    team_results: List[Tuple[str, str]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"[MultiTeam] Parallel team failed: {r}")
        else:
            team_results.append(r)

    if not team_results:
        raise OrchestratorError("All parallel teams failed")

    if len(team_results) == 1:
        return team_results[0][1]

    return synthesize_results(team_results)


async def _run_sequential(
    run_team_fn: Callable,
    assignments: List,
    mode: str,
    on_progress: ProgressCallback = None,
    user_context: Optional[Dict[str, Any]] = None,
) -> str:
    team_results: List[Tuple[str, str]] = []

    for assignment in assignments:
        instruction = assignment.instruction

        if team_results:
            context_parts = []
            for tid, res in team_results:
                context_parts.append(f"### Output from {tid}\n{res}")
            context = "\n\n".join(context_parts)
            instruction = f"{instruction}\n\n## Context from previous teams\n{context}"

        result = await run_team_fn(
            instruction, assignment.team_id,
            mode=mode, on_progress=on_progress,
            user_context=_with_memory_task_hint(user_context, assignment.instruction),
        )
        team_results.append((assignment.team_id, result))

    return team_results[-1][1]


def synthesize_results(team_results: List[Tuple[str, str]]) -> str:
    parts = []
    for team_id, result in team_results:
        parts.append(f"## {team_id}\n\n{result}")
    return "\n\n---\n\n".join(parts)
