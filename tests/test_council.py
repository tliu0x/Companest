"""Council Mode Unit Tests"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from companest.team import AgentTeam, TeamConfig, _parse_rubric
from companest.modes.council import _parse_judge_response, CouncilMode
from companest.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import PiError, TeamError

RUBRIC = [
    {"criterion": "reasoning", "weight": 0.4},
    {"criterion": "depth", "weight": 0.3},
    {"criterion": "clarity", "weight": 0.3},
]


def _make_team(base, pi_ids, rubric=None, souls=None) -> AgentTeam:
    (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
    for pid in pi_ids:
        d = base / "teams" / "test" / "pis" / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "soul.md").write_text((souls or {}).get(pid, f"Pi {pid}"))
    (base / "soul.md").write_text("soul")
    (base / "user.md").write_text("user")
    config = TeamConfig(
        id="test", role="general", lead_pi=pi_ids[0],
        pis=[PiConfig(id=pid) for pid in pi_ids], rubric=rubric,
    )
    return AgentTeam(config, MemoryManager(str(base)))


def _execute(team, task, **kwargs):
    return asyncio.get_event_loop().run_until_complete(CouncilMode().execute(team, task, **kwargs))


def test_single_pi_falls_back(tmp_path):
    team = _make_team(tmp_path, ["alpha"])
    team.pis["alpha"].run = AsyncMock(return_value="solo answer")

    assert _execute(team, "What is 2+2-") == "solo answer"
    team.pis["alpha"].run.assert_called_once()


def test_two_pis_synthesize_without_rubric_or_archetypes(tmp_path):
    team = _make_team(tmp_path, ["alpha", "beta"])
    assert team.rubric is None
    team.pis["alpha"].run = AsyncMock(side_effect=["Alpha perspective", "Synthesized answer"])
    team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

    result = _execute(team, "Evaluate launch readiness", user_context={"company_id": "acme"})
    assert result == "Synthesized answer"

    # Lead: perspective + synthesis (no judge step); beta: perspective only.
    assert team.pis["alpha"].run.call_count == 2
    assert team.pis["beta"].run.call_count == 1

    synthesis_call = team.pis["alpha"].run.call_args_list[1]
    prompt = synthesis_call[0][0]
    assert "Perspective 1" in prompt
    assert "Perspective 2" in prompt
    assert "Alpha perspective" in prompt
    assert "Beta perspective" in prompt
    assert "different analysts" in prompt
    assert "three dimensions" not in prompt
    assert "Judge Evaluation" not in prompt

    beta_call = team.pis["beta"].run.call_args
    assert beta_call.kwargs["user_context"]["memory_task_hint"] == "Evaluate launch readiness"
    assert synthesis_call.kwargs["user_context"]["memory_task_hint"] == "Evaluate launch readiness"


def test_all_fail_raises(tmp_path):
    team = _make_team(tmp_path, ["alpha", "beta"])
    team.pis["alpha"].run = AsyncMock(side_effect=PiError("alpha failed"))
    team.pis["beta"].run = AsyncMock(side_effect=PiError("beta failed"))

    with pytest.raises(TeamError, match="All .* Pis failed"):
        _execute(team, "Impossible task")


def test_one_fails_one_succeeds_returns_survivor_without_synthesis(tmp_path):
    team = _make_team(tmp_path, ["alpha", "beta"])
    team.pis["alpha"].run = AsyncMock(side_effect=PiError("alpha failed"))
    team.pis["beta"].run = AsyncMock(return_value="Beta solo answer")

    assert _execute(team, "Mixed task") == "Beta solo answer"
    assert team.pis["alpha"].run.call_count == 1
    assert team.pis["beta"].run.call_count == 1


def test_archetype_labels_in_synthesis(tmp_path):
    ids = ["ethos", "logos", "pathos"]
    team = _make_team(
        tmp_path, ids,
        souls={pid: f"# Pi: {pid}\n- archetype: {pid}\nSoul text." for pid in ids},
    )
    team.pis["logos"].run = AsyncMock(return_value="Logos analysis")
    team.pis["pathos"].run = AsyncMock(return_value="Pathos reflection")
    team.pis["ethos"].run = AsyncMock(side_effect=["Ethos judgment", "Final synthesis"])

    assert _execute(team, "What is consciousness-") == "Final synthesis"

    prompt = team.pis["ethos"].run.call_args_list[1][0][0]
    assert "Logos Perspective" in prompt
    assert "Pathos Perspective" in prompt
    assert "Ethos Perspective" in prompt
    assert "three dimensions" in prompt


@pytest.mark.parametrize("raw, expected", [
    ("reasoning=4, depth=3, clarity=3", [("reasoning", 0.4), ("depth", 0.3), ("clarity", 0.3)]),
    ("  reasoning = 0.4 ,  depth = 0.6  ", [("reasoning", 0.4), ("depth", 0.6)]),
    # A non-numeric weight drops that entry only.
    ("reasoning=abc, depth=0.5", [("depth", 1.0)]),
    ("", None),
    ("no equals here", None),
])
def test_parse_rubric(raw, expected):
    result = _parse_rubric(raw)
    if expected is None:
        assert result is None
    else:
        assert [(e["criterion"], e["weight"]) for e in result] == expected


@pytest.mark.parametrize("rubric_line, expected", [
    ("- rubric: reasoning=0.4, depth=0.3, clarity=0.3\n", ["reasoning", "depth", "clarity"]),
    ("", None),
])
def test_team_config_rubric_from_markdown(tmp_path, rubric_line, expected):
    (tmp_path / "team.md").write_text(
        "# Team: test\n- role: general\n- lead_pi: alpha\n- enabled: true\n" + rubric_line
    )
    rubric = TeamConfig.from_markdown(tmp_path / "team.md").rubric
    if expected is None:
        assert rubric is None
    else:
        assert [e["criterion"] for e in rubric] == expected
        assert abs(sum(e["weight"] for e in rubric) - 1.0) < 0.01


class TestJudgeResponseParsing:
    def test_valid_json_recalculates_weighted(self):
        """Weighted score is recalculated from rubric, not trusted from LLM."""
        raw = json.dumps({
            "scores": {
                "logos": {"reasoning": 8, "depth": 7, "clarity": 9, "weighted": 999},
                "pathos": {"reasoning": 6, "depth": 5, "clarity": 7},
            },
            "notes": "Logos stronger overall",
        })
        result = _parse_judge_response(raw, RUBRIC)
        # 8*0.4 + 7*0.3 + 9*0.3 = 8.0; 6*0.4 + 5*0.3 + 7*0.3 = 6.0
        assert result["scores"]["logos"]["weighted"] == 8.0
        assert result["scores"]["pathos"]["weighted"] == 6.0
        assert result["notes"] == "Logos stronger overall"

    def test_json_in_markdown_fence(self):
        raw = '```json\n{"scores": {"a": {"reasoning": 5, "depth": 5, "clarity": 5}}, "notes": ""}\n```'
        assert _parse_judge_response(raw, RUBRIC)["scores"]["a"]["weighted"] == 5.0

    @pytest.mark.parametrize("raw", ["not json at all", '{"notes": "no scores"}'])
    def test_unusable_response_returns_none(self, raw):
        assert _parse_judge_response(raw, RUBRIC) is None


class TestFilterPerspectives:
    @staticmethod
    def _filter(tmp_path, weighted):
        """Filter one perspective per Pi; `weighted` maps pi_id -> judge score."""
        pi_ids = list(weighted) or ["a", "b", "c"]
        team = _make_team(tmp_path, pi_ids, rubric=RUBRIC)
        perspectives = [(pid, f"{pid} answer") for pid in pi_ids]
        judge_scores = {"scores": {pid: {"weighted": w} for pid, w in weighted.items()}, "notes": ""}
        return [pid for pid, _ in CouncilMode()._filter_perspectives(team, perspectives, judge_scores)]

    def test_filter_drops_low_scores(self, tmp_path):
        # Mean = 6.33, threshold = 4.43 -> c dropped.
        assert self._filter(tmp_path, {"a": 9.0, "b": 8.0, "c": 2.0}) == ["a", "b"]

    def test_filter_keeps_minimum_two(self, tmp_path):
        # Mean = 4.0, threshold = 2.8 -> only a passes, but at least two must remain.
        kept = self._filter(tmp_path, {"a": 10.0, "b": 1.0, "c": 1.0})
        assert len(kept) == 2
        assert kept[0] == "a"

    def test_filter_with_two_perspectives_keeps_both(self, tmp_path):
        assert self._filter(tmp_path, {"a": 9.0, "b": 2.0}) == ["a", "b"]

    def test_filter_with_empty_scores(self, tmp_path):
        assert self._filter(tmp_path, {}) == ["a", "b", "c"]


class TestCouncilWithRubric:
    def test_council_with_judge_scoring(self, tmp_path):
        """Full flow: perspectives -judge -filter -synthesis."""
        team = _make_team(tmp_path, ["alpha", "beta", "gamma"], rubric=RUBRIC)
        judge_response = json.dumps({
            "scores": {
                "alpha": {"reasoning": 8, "depth": 7, "clarity": 9},
                "beta": {"reasoning": 7, "depth": 6, "clarity": 8},
                "gamma": {"reasoning": 3, "depth": 2, "clarity": 3},
            },
            "notes": "Gamma is weak",
        })
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",
            judge_response,
            "Final synthesis with judge context",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")
        team.pis["gamma"].run = AsyncMock(return_value="Gamma perspective")

        assert _execute(team, "Evaluate X") == "Final synthesis with judge context"
        assert team.pis["alpha"].run.call_count == 3

        synthesis_prompt = team.pis["alpha"].run.call_args_list[2][0][0]
        assert "Judge Evaluation" in synthesis_prompt
        assert "higher-scored perspectives" in synthesis_prompt

    def test_council_judge_failure_graceful(self, tmp_path):
        """Judge output unparseable -skip scoring, proceed with synthesis as normal."""
        team = _make_team(tmp_path, ["alpha", "beta"], rubric=RUBRIC)
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",
            "not valid json at all",
            "Synthesized without judge",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

        assert _execute(team, "Evaluate X") == "Synthesized without judge"
        assert "Judge Evaluation" not in team.pis["alpha"].run.call_args_list[2][0][0]
