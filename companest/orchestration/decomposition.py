import hashlib
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..utils import run_sync, extract_json_array
from ..modes import ProgressCallback, _lead_pi, _with_memory_task_hint
from ..exceptions import PiError, TeamError

if TYPE_CHECKING:
    from ..agents.team import AgentTeam
    from ..agents.pi import Pi

logger = logging.getLogger(__name__)


class Decomposition:
    """Decompose -execute each subtask with fresh context -synthesize."""

    def __init__(self, max_subtasks: int = 10, cascade: bool = True):
        self._max_subtasks = max_subtasks
        self._cascade = cascade

    @property
    def name(self) -> str:
        return "loop"

    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        pi = _lead_pi(team)
        task_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
        progress_key = f"progress-{task_hash}.json"

        subtasks = await self._decompose_task(
            pi,
            task,
            self._max_subtasks,
            self._cascade,
            user_context,
        )
        logger.info(f"[Team:{team.id}] Decomposition: {len(subtasks)} subtask(s)")

        if on_progress:
            await on_progress(f"\U0001f504 Decomposition: {len(subtasks)} subtask(s)")

        if len(subtasks) <= 1:
            return await pi.run(
                task,
                cascade=self._cascade,
                on_progress=on_progress,
                user_context=_with_memory_task_hint(user_context, task),
            )

        results: List[Dict] = []
        for i, subtask in enumerate(subtasks):
            if on_progress:
                await on_progress(
                    f"\U0001f504 Subtask {i+1}/{len(subtasks)}: {subtask[:60]}"
                )
            progress_summary = _format_progress(results)
            loop_prompt = (
                f"## Current Subtask ({i+1}/{len(subtasks)})\n{subtask}\n\n"
                f"## Original Task\n{task}\n\n"
                f"## Completed So Far\n{progress_summary or 'None yet.'}"
            )
            memory_hint = f"{subtask}\n\nOriginal task: {task}"
            try:
                result = await pi.run(
                    loop_prompt,
                    cascade=self._cascade,
                    on_progress=on_progress,
                    user_context=_with_memory_task_hint(user_context, memory_hint),
                )
                entry = {"subtask": subtask, "status": "done", "result": result}
            except PiError as e:
                logger.warning(f"[Team:{team.id}] Subtask {i+1} failed: {e}")
                entry = {"subtask": subtask, "status": "failed", "error": str(e)}
            results.append(entry)
            await run_sync(team.memory.append_team_memory, team.id, progress_key, entry)

        successful = [result for result in results if result["status"] == "done"]
        if not successful:
            raise TeamError(
                f"All {len(subtasks)} subtasks failed for team '{team.id}'",
                details={"progress_key": progress_key},
            )

        if on_progress:
            await on_progress("\u270d\ufe0f Synthesizing loop results...")

        synth_prompt = (
            f"## Task\n{task}\n\n"
            f"## Subtask Results\n{_format_full_results(results)}\n\n"
            "Synthesize these results into a comprehensive final response."
        )
        final = await pi.run(
            synth_prompt,
            cascade=False,
            user_context=_with_memory_task_hint(user_context, task),
        )

        try:
            await run_sync(team.memory.delete_team_memory, team.id, progress_key)
        except Exception:
            pass

        return final

    async def _decompose_task(
        self,
        pi: "Pi",
        task: str,
        max_subtasks: int,
        cascade: bool,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        prompt = (
            f"Break this task into sequential subtasks (max {max_subtasks}).\n"
            "Return ONLY a JSON array of strings, each a clear actionable subtask.\n"
            "If the task is simple enough for one step, return a single-item array.\n"
            'Example: ["Research X", "Analyze Y", "Summarize findings"]\n\n'
            f"Task: {task}"
        )
        raw = await pi.run(
            prompt,
            cascade=cascade,
            user_context=_with_memory_task_hint(user_context, task),
        )
        return _parse_json_array(raw, max_subtasks)


def _parse_json_array(raw: str, max_items: int) -> List[str]:
    parsed = extract_json_array(raw, max_items=max_items)
    if parsed is not None:
        return [str(x) for x in parsed]

    lines = [
        line.strip().lstrip("0123456789.-) ")
        for line in raw.strip().split("\n")
        if line.strip()
    ]
    return lines[:max_items] if lines else [raw.strip()]


def _format_progress(results: List[Dict]) -> str:
    lines = []
    for i, result in enumerate(results):
        status = "Done" if result["status"] == "done" else "Failed"
        brief = result.get("result", result.get("error", ""))[:200]
        lines.append(f"{i+1}. [{status}] {result['subtask']}: {brief}")
    return "\n".join(lines)


def _format_full_results(results: List[Dict]) -> str:
    parts = []
    for i, result in enumerate(results):
        if result["status"] == "done":
            parts.append(f"### Subtask {i+1}: {result['subtask']}\n{result['result']}")
        else:
            parts.append(
                f"### Subtask {i+1}: {result['subtask']}\n"
                f"[FAILED: {result['error']}]"
            )
    return "\n\n".join(parts)
