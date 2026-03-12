"""
Collaborative execution mode -multi-Pi pipeline.

Output of Pi A feeds into Pi B, etc. Useful for multi-stage
processing (e.g. research -> analyze -> summarize).

Each Pi in the pipeline receives structured context including the
original task, its position in the pipeline, and the previous stage output.
"""

import logging
from typing import Any, Dict, List, Optional

from . import ExecutionMode, ProgressCallback
from ..exceptions import PiError, TeamError

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..team import AgentTeam

logger = logging.getLogger(__name__)


def _with_memory_task_hint(
    user_context: Optional[Dict[str, Any]],
    task_hint: str,
) -> Dict[str, Any]:
    """Attach a retrieval hint for internal pipeline prompts."""
    ctx = dict(user_context) if user_context else {}
    if task_hint:
        ctx["memory_task_hint"] = task_hint
    return ctx


class CollaborativeMode(ExecutionMode):
    """Multi-Pi pipeline: output of Pi A -> input of Pi B.

    Args:
        pipeline: Ordered list of Pi IDs. Defaults to all Pis in team order.
        cascade: Whether to use cascade model selection for each stage.
        stop_on_failure: If True (default), abort pipeline on first failure.
            If False, skip the failed stage and pass previous output forward.
    """

    def __init__(
        self,
        pipeline: Optional[List[str]] = None,
        cascade: bool = False,
        stop_on_failure: bool = True,
    ):
        self._pipeline = pipeline
        self._cascade = cascade
        self._stop_on_failure = stop_on_failure

    @property
    def name(self) -> str:
        return "collaborative"

    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: Optional[ProgressCallback] = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        pipeline = self._pipeline or list(team.pis.keys())
        if not pipeline:
            raise TeamError(f"Team '{team.id}' has no Pis for collaborative pipeline")

        # Validate all Pi IDs exist
        for pi_id in pipeline:
            if pi_id not in team.pis:
                raise TeamError(
                    f"Pi '{pi_id}' not found in team '{team.id}'",
                    details={"pipeline": pipeline, "available": list(team.pis.keys())},
                )

        total_stages = len(pipeline)
        stage_results: List[Dict[str, Any]] = []
        current_output = ""

        for stage_idx, pi_id in enumerate(pipeline):
            stage_num = stage_idx + 1
            pi = team.pis[pi_id]

            if on_progress:
                await on_progress(
                    f"\u2699\ufe0f Pipeline stage {stage_num}/{total_stages}: {pi_id}"
                )

            # Build structured prompt for this stage
            prompt = _build_stage_prompt(
                task=task,
                stage_num=stage_num,
                total_stages=total_stages,
                pi_id=pi_id,
                previous_output=current_output,
                stage_results=stage_results,
            )

            try:
                result = await pi.run(
                    prompt,
                    cascade=self._cascade,
                    on_progress=on_progress,
                    user_context=_with_memory_task_hint(user_context, task),
                )
                current_output = result
                stage_results.append({
                    "stage": stage_num,
                    "pi_id": pi_id,
                    "status": "done",
                    "output": result,
                })
            except (PiError, Exception) as e:
                logger.warning(
                    f"[Team:{team.id}] Pipeline stage {stage_num} ({pi_id}) failed: {e}"
                )
                stage_results.append({
                    "stage": stage_num,
                    "pi_id": pi_id,
                    "status": "failed",
                    "error": str(e),
                })

                if self._stop_on_failure:
                    raise TeamError(
                        f"Pipeline failed at stage {stage_num} ({pi_id}): {e}",
                        details={
                            "stage": stage_num,
                            "pi_id": pi_id,
                            "completed_stages": stage_idx,
                        },
                    )
                # skip mode: continue with previous output
                if on_progress:
                    await on_progress(
                        f"\u26a0\ufe0f Stage {stage_num} ({pi_id}) failed, skipping"
                    )

        # Record pipeline execution in team memory
        try:
            import json
            team.memory.append_team_memory(
                team.id, "pipeline-log.json",
                {"stages": len(pipeline), "results": [
                    {"pi": r["pi_id"], "status": r["status"]}
                    for r in stage_results
                ]},
            )
        except Exception:
            pass  # memory write is best-effort

        if not current_output:
            raise TeamError(
                f"Pipeline produced no output for team '{team.id}'",
                details={"pipeline": pipeline},
            )

        return current_output


def _build_stage_prompt(
    task: str,
    stage_num: int,
    total_stages: int,
    pi_id: str,
    previous_output: str,
    stage_results: List[Dict[str, Any]],
) -> str:
    """Build a structured prompt for a pipeline stage."""
    parts = [
        f"## Original Task\n{task}",
        f"\n## Pipeline Context\nYou are **stage {stage_num} of {total_stages}** in a processing pipeline (role: {pi_id}).",
    ]

    if previous_output:
        parts.append(f"\n## Previous Stage Output\n{previous_output}")
    else:
        parts.append(
            "\n## Note\nYou are the first stage. Process the original task above."
        )

    if stage_num == total_stages and total_stages > 1:
        parts.append(
            "\n## Instruction\nYou are the **final stage**. "
            "Produce the definitive, polished response based on all previous work."
        )

    return "\n".join(parts)


