import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from . import ExecutionMode, ProgressCallback, _with_memory_task_hint
from ..exceptions import PiError, TeamError

if TYPE_CHECKING:
    from ..team import AgentTeam

logger = logging.getLogger(__name__)


class CouncilMode(ExecutionMode):
    """Council: all Pis answer independently, lead Pi synthesizes."""

    def __init__(self, cascade: bool = False):
        self._cascade = cascade

    @property
    def name(self) -> str:
        return "council"

    async def execute(
        self,
        team: "AgentTeam",
        task: str,
        on_progress: ProgressCallback = None,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        if len(team.pis) < 2:
            logger.info(f"[Team:{team.id}] Council needs >=2 Pis, falling back to default")
            from .default import DefaultMode
            return await DefaultMode(cascade=self._cascade).execute(
                team, task, on_progress=on_progress, user_context=user_context,
            )

        pi_names = ", ".join(team.pis.keys())
        if on_progress:
            await on_progress(f"\U0001f3db\ufe0f Council: {pi_names}")

        async def _run_pi(pi_id: str) -> tuple:
            try:
                result = await team.pis[pi_id].run(
                    task,
                    cascade=self._cascade,
                    user_context=_with_memory_task_hint(user_context, task),
                )
                return pi_id, result, None
            except PiError as e:
                return pi_id, None, e

        results = await asyncio.gather(
            *[_run_pi(pid) for pid in team.pis],
        )

        perspectives = []
        for pi_id, result, error in results:
            if error is None and result:
                perspectives.append((pi_id, result))
            else:
                logger.warning(f"[Team:{team.id}] Council Pi '{pi_id}' failed: {error}")

        if not perspectives:
            raise TeamError(
                f"All {len(team.pis)} Pis failed in council mode for team '{team.id}'",
            )

        if len(perspectives) == 1:
            return perspectives[0][1]

        judge_scores = None
        if team.rubric:
            if not team.lead_pi_id or team.lead_pi_id not in team.pis:
                raise TeamError(
                    f"Team '{team.id}' has no valid lead_pi for council judge",
                )
            if on_progress:
                await on_progress("\u2696\ufe0f Judging perspectives...")
            judge_scores = await self._judge_perspectives(team, task, perspectives, user_context)
            if judge_scores:
                perspectives = self._filter_perspectives(team, perspectives, judge_scores)

        if on_progress:
            await on_progress("\u270d\ufe0f Synthesizing council perspectives...")

        archetype_map = self._get_pi_archetypes(team)
        perspective_parts = []
        for i, (pi_id, result) in enumerate(perspectives):
            archetype = archetype_map.get(pi_id)
            if archetype:
                label = f"### {archetype.capitalize()} Perspective ({pi_id})"
            else:
                label = f"### Perspective {i+1}"
            perspective_parts.append(f"{label}\n{result}")

        perspective_block = "\n\n".join(perspective_parts)

        judge_section = ""
        if judge_scores:
            judge_section = self._format_judge_section(judge_scores)

        has_archetypes = any(archetype_map.get(pid) for pid, _ in perspectives)
        if has_archetypes:
            synth_instructions = (
                "You received perspectives from analysts with distinct reasoning styles:\n"
                "- Logos: logical/evidence-based analysis\n"
                "- Pathos: empathetic/user-centered analysis\n"
                "- Ethos: experience/credibility-based analysis\n"
                "1. Identify where the three dimensions agree and diverge.\n"
                "2. Synthesize a final answer that balances all three.\n"
                "3. Note which dimension was most relevant for this particular question."
            )
        else:
            synth_instructions = (
                "You received the above independent perspectives from different analysts.\n"
                "1. Identify points of agreement and disagreement.\n"
                "2. Evaluate the reasoning quality of each perspective.\n"
                "3. Synthesize a final, comprehensive answer that incorporates the strongest insights."
            )

        if judge_scores:
            synth_instructions += (
                "\n\nBased on the judge evaluation above, give more weight "
                "to higher-scored perspectives in your synthesis."
            )

        synthesis_prompt = (
            f"## Task\n{task}\n\n"
            f"{judge_section}"
            f"## Independent Perspectives\n{perspective_block}\n\n"
            f"## Instructions\n{synth_instructions}"
        )

        if not team.lead_pi_id or team.lead_pi_id not in team.pis:
            raise TeamError(
                f"Team '{team.id}' has no valid lead_pi for council synthesis",
            )

        return await team.pis[team.lead_pi_id].run(
            synthesis_prompt,
            cascade=False,
            user_context=_with_memory_task_hint(user_context, task),
        )

    def _get_pi_archetypes(self, team: "AgentTeam") -> Dict[str, str]:
        archetypes = {}
        for pi_id in team.pis:
            soul = team.memory.read_pi_soul(team.id, pi_id)
            if soul:
                match = re.search(
                    r"^-\s*archetype\s*:\s*(\w+)", soul,
                    re.MULTILINE | re.IGNORECASE,
                )
                if match:
                    archetypes[pi_id] = match.group(1).lower()
        return archetypes

    async def _judge_perspectives(
        self, team: "AgentTeam", task: str, perspectives: List[Tuple[str, str]],
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        criteria_lines = []
        for entry in team.rubric:
            criteria_lines.append(
                f"- {entry['criterion']} (weight {entry['weight']})"
            )
        criteria_block = "\n".join(criteria_lines)

        persp_block_parts = []
        for pi_id, result in perspectives:
            persp_block_parts.append(f"### {pi_id}\n{result}")
        persp_block = "\n\n".join(persp_block_parts)

        judge_prompt = (
            f"## Task\n{task}\n\n"
            f"## Perspectives to Evaluate\n{persp_block}\n\n"
            f"## Scoring Criteria\nScore each perspective on a 1-10 scale:\n{criteria_block}\n\n"
            "## Output Format\n"
            "Return ONLY valid JSON (no markdown fencing):\n"
            '{"scores": {"<pi_id>": {"<criterion>": <score>, ..., "weighted": <weighted_avg>}, ...}, "notes": "<brief evaluation notes>"}'
        )

        lead_pi = team.pis[team.lead_pi_id]
        try:
            raw = await lead_pi.run(
                judge_prompt,
                cascade=False,
                user_context=_with_memory_task_hint(user_context, task),
            )
            return _parse_judge_response(raw, team.rubric)
        except Exception as e:
            logger.warning(f"[Team:{team.id}] Judge scoring failed: {e}")
            return None

    def _filter_perspectives(
        self,
        team: "AgentTeam",
        perspectives: List[Tuple[str, str]],
        judge_scores: Dict[str, Any],
    ) -> List[Tuple[str, str]]:
        scores = judge_scores.get("scores", {})
        if not scores:
            return perspectives

        scored = []
        for pi_id, result in perspectives:
            pi_score = scores.get(pi_id)
            if pi_score and "weighted" in pi_score:
                scored.append((pi_id, result, pi_score["weighted"]))
            else:
                # Unscored perspectives are kept.
                scored.append((pi_id, result, 10.0))

        if len(scored) <= 2:
            return [(pid, res) for pid, res, _ in scored]

        weights = [s for _, _, s in scored]
        mean_score = sum(weights) / len(weights)
        threshold = mean_score * 0.7

        filtered = [(pid, res) for pid, res, w in scored if w >= threshold]

        # At least 2 perspectives must remain.
        if len(filtered) < 2:
            scored.sort(key=lambda x: x[2], reverse=True)
            filtered = [(pid, res) for pid, res, _ in scored[:2]]

        logger.info(
            f"[Team:{team.id}] Judge filter: {len(perspectives)} -> {len(filtered)} "
            f"(threshold={threshold:.1f})"
        )
        return filtered

    def _format_judge_section(self, judge_scores: Dict[str, Any]) -> str:
        scores = judge_scores.get("scores", {})
        if not scores:
            return ""

        lines = ["## Judge Evaluation"]
        for pi_id, pi_scores in scores.items():
            weighted = pi_scores.get("weighted", 0)
            details = []
            for key, val in pi_scores.items():
                if key != "weighted":
                    details.append(f"{key}={val}")
            detail_str = ", ".join(details)
            lines.append(f"- {pi_id}: {weighted:.1f}/10 ({detail_str})")

        notes = judge_scores.get("notes", "")
        if notes:
            lines.append(f"\nNotes: {notes}")

        return "\n".join(lines) + "\n\n"


def _parse_judge_response(
    raw: str, rubric: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    parsed = None
    try:
        parsed = json.loads(raw.strip())
    except ValueError:
        pass

    if parsed is None:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except ValueError:
                pass

    if not parsed or not isinstance(parsed, dict) or "scores" not in parsed:
        return None

    scores = parsed["scores"]
    if not isinstance(scores, dict):
        return None

    # Weighted scores are recomputed from the rubric; the LLM's own math is not trusted.
    for pi_id, pi_scores in scores.items():
        if not isinstance(pi_scores, dict):
            continue
        weighted = 0.0
        for entry in rubric:
            criterion = entry["criterion"]
            weight = entry["weight"]
            score_val = pi_scores.get(criterion, 0)
            try:
                weighted += float(score_val) * weight
            except (ValueError, TypeError):
                pass
        pi_scores["weighted"] = round(weighted, 2)

    return {"scores": scores, "notes": parsed.get("notes", "")}
