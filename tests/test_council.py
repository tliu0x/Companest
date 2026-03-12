"""
Council Mode Unit Tests

Tests for AgentTeam.run_council():
1. Single Pi -fallback to default run()
2. Two Pis -independent answers -synthesis
3. All Pis fail -TeamError
4. One Pi fails, one succeeds -direct return (no synthesis needed)
5. Archetype labels in council synthesis
6. No archetype -anonymous perspective labels (backward compat)
7. Rubric parsing (string -list[dict])
8. Judge prompt construction
9. Filter logic (mock Pi returns JSON scores)
10. No rubric -backward compatible (no judge step)
"""

import asyncio
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from companest.team import AgentTeam, TeamConfig, _parse_rubric
from companest.modes.council import _parse_judge_response, CouncilMode
from companest.pi import PiConfig
from companest.memory import MemoryManager
from companest.exceptions import PiError, TeamError


def _make_team(tmpdir: str, pi_ids: list[str]) -> AgentTeam:
    """Build an AgentTeam with the given Pi IDs (mocked -no soul.md needed)."""
    base = Path(tmpdir)
    (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
    for pid in pi_ids:
        (base / "teams" / "test" / "pis" / pid).mkdir(parents=True, exist_ok=True)
        (base / "teams" / "test" / "pis" / pid / "soul.md").write_text(f"Pi {pid}")
    (base / "soul.md").write_text("soul")
    (base / "user.md").write_text("user")

    mm = MemoryManager(str(base))
    config = TeamConfig(
        id="test",
        role="general",
        lead_pi=pi_ids[0],
        pis=[PiConfig(id=pid) for pid in pi_ids],
    )
    return AgentTeam(config, mm)


class TestCouncilFallback:
    """Single Pi -falls back to default run()."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_single_pi_falls_back(self):
        team = _make_team(self.tmpdir, ["alpha"])
        team.pis["alpha"].run = AsyncMock(return_value="solo answer")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("What is 2+2-")
        )
        assert result == "solo answer"
        # Should call run() once (fallback), not gather
        team.pis["alpha"].run.assert_called_once()


class TestCouncilSynthesis:
    """Two+ Pis -independent answers -lead Pi synthesizes."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_two_pis_synthesize(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])

        # Stage 1: Both Pis answer independently
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",  # Stage 1 call
            "Synthesized answer",  # Stage 2+3 synthesis call
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("Evaluate X")
        )
        assert result == "Synthesized answer"

        # Alpha called twice: once for perspective, once for synthesis
        assert team.pis["alpha"].run.call_count == 2
        # Beta called once: perspective only
        assert team.pis["beta"].run.call_count == 1

        # Synthesis prompt should contain both perspectives
        synthesis_call = team.pis["alpha"].run.call_args_list[1]
        synthesis_prompt = synthesis_call[0][0]
        assert "Perspective 1" in synthesis_prompt
        assert "Perspective 2" in synthesis_prompt
        assert "Alpha perspective" in synthesis_prompt
        assert "Beta perspective" in synthesis_prompt


class TestCouncilAllFail:
    """All Pis fail -TeamError."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_all_fail_raises(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        team.pis["alpha"].run = AsyncMock(side_effect=PiError("alpha failed"))
        team.pis["beta"].run = AsyncMock(side_effect=PiError("beta failed"))

        with pytest.raises(TeamError, match="All .* Pis failed"):
            asyncio.get_event_loop().run_until_complete(
                team.run_council("Impossible task")
            )


class TestCouncilPartialFailure:
    """One Pi fails, one succeeds -direct return (no synthesis)."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_one_fails_one_succeeds(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        team.pis["alpha"].run = AsyncMock(side_effect=PiError("alpha failed"))
        team.pis["beta"].run = AsyncMock(return_value="Beta solo answer")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("Mixed task")
        )
        # Only 1 succeeded -direct return, no synthesis
        assert result == "Beta solo answer"
        # Alpha called once (failed), Beta called once (succeeded)
        assert team.pis["alpha"].run.call_count == 1
        assert team.pis["beta"].run.call_count == 1


def _make_team_with_archetypes(tmpdir: str) -> AgentTeam:
    """Build a team with logos/pathos/ethos Pis (archetype tags in soul.md)."""
    base = Path(tmpdir)
    (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
    for pid, archetype in [("logos", "logos"), ("pathos", "pathos"), ("ethos", "ethos")]:
        d = base / "teams" / "test" / "pis" / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "soul.md").write_text(f"# Pi: {pid}\n- archetype: {archetype}\nSoul text.")
    (base / "soul.md").write_text("soul")
    (base / "user.md").write_text("user")

    mm = MemoryManager(str(base))
    config = TeamConfig(
        id="test",
        role="general",
        lead_pi="ethos",
        pis=[PiConfig(id=pid) for pid in ["logos", "pathos", "ethos"]],
    )
    return AgentTeam(config, mm)


class TestPiArchetypes:
    """Tests for _get_pi_archetypes() helper."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_extracts_archetypes_from_soul(self):
        team = _make_team_with_archetypes(self.tmpdir)
        archetypes = CouncilMode()._get_pi_archetypes(team)
        assert archetypes == {"logos": "logos", "pathos": "pathos", "ethos": "ethos"}

    def test_no_archetype_returns_empty(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        archetypes = CouncilMode()._get_pi_archetypes(team)
        assert archetypes == {}


class TestCouncilArchetypeLabels:
    """Council with archetype tags uses dimension labels in synthesis prompt."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_archetype_labels_in_synthesis(self):
        team = _make_team_with_archetypes(self.tmpdir)
        team.pis["logos"].run = AsyncMock(side_effect=[
            "Logos analysis",
        ])
        team.pis["pathos"].run = AsyncMock(return_value="Pathos reflection")
        team.pis["ethos"].run = AsyncMock(side_effect=[
            "Ethos judgment",
            "Final synthesis",  # Lead Pi synthesis call
        ])

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("What is consciousness-")
        )
        assert result == "Final synthesis"

        # Check synthesis prompt contains archetype labels
        synthesis_call = team.pis["ethos"].run.call_args_list[1]
        prompt = synthesis_call[0][0]
        assert "Logos Perspective" in prompt
        assert "Pathos Perspective" in prompt
        assert "Ethos Perspective" in prompt
        # Should contain the archetype-aware instructions
        assert "three dimensions" in prompt

    def test_anonymous_labels_without_archetypes(self):
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha answer",
            "Synthesized",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta answer")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("Simple question")
        )
        assert result == "Synthesized"

        # Check synthesis prompt uses anonymous labels
        synthesis_call = team.pis["alpha"].run.call_args_list[1]
        prompt = synthesis_call[0][0]
        assert "Perspective 1" in prompt
        assert "Perspective 2" in prompt
        # Should NOT contain archetype instructions
        assert "three dimensions" not in prompt
        assert "different analysts" in prompt


# -- Rubric Parsing Tests ----------------------------------------

class TestRubricParsing:
    """Test _parse_rubric() helper."""

    def test_basic_parsing(self):
        result = _parse_rubric("reasoning=0.4, depth=0.3, clarity=0.3")
        assert len(result) == 3
        assert result[0]["criterion"] == "reasoning"
        assert result[1]["criterion"] == "depth"
        assert result[2]["criterion"] == "clarity"

    def test_weights_normalized(self):
        result = _parse_rubric("reasoning=4, depth=3, clarity=3")
        total = sum(e["weight"] for e in result)
        assert abs(total - 1.0) < 0.01

    def test_already_normalized(self):
        result = _parse_rubric("a=0.5, b=0.5")
        assert abs(result[0]["weight"] - 0.5) < 0.01
        assert abs(result[1]["weight"] - 0.5) < 0.01

    def test_empty_string(self):
        assert _parse_rubric("") is None

    def test_invalid_format(self):
        assert _parse_rubric("no equals here") is None

    def test_invalid_weight(self):
        # "abc" is not a float -that entry skipped
        result = _parse_rubric("reasoning=abc, depth=0.5")
        assert len(result) == 1
        assert result[0]["criterion"] == "depth"

    def test_single_criterion(self):
        result = _parse_rubric("quality=1.0")
        assert len(result) == 1
        assert result[0]["weight"] == 1.0

    def test_extra_whitespace(self):
        result = _parse_rubric("  reasoning = 0.4 ,  depth = 0.6  ")
        assert len(result) == 2
        assert result[0]["criterion"] == "reasoning"
        assert result[1]["criterion"] == "depth"


class TestJudgeResponseParsing:
    """Test _parse_judge_response() helper."""

    def setup_method(self):
        self.rubric = [
            {"criterion": "reasoning", "weight": 0.4},
            {"criterion": "depth", "weight": 0.3},
            {"criterion": "clarity", "weight": 0.3},
        ]

    def test_valid_json(self):
        raw = json.dumps({
            "scores": {
                "logos": {"reasoning": 8, "depth": 7, "clarity": 9},
                "pathos": {"reasoning": 6, "depth": 5, "clarity": 7},
            },
            "notes": "Logos stronger overall",
        })
        result = _parse_judge_response(raw, self.rubric)
        assert result is not None
        assert "logos" in result["scores"]
        # Recalculated: 8*0.4 + 7*0.3 + 9*0.3 = 3.2 + 2.1 + 2.7 = 8.0
        assert result["scores"]["logos"]["weighted"] == 8.0
        # Recalculated: 6*0.4 + 5*0.3 + 7*0.3 = 2.4 + 1.5 + 2.1 = 6.0
        assert result["scores"]["pathos"]["weighted"] == 6.0
        assert result["notes"] == "Logos stronger overall"

    def test_json_in_markdown_fence(self):
        raw = '```json\n{"scores": {"a": {"reasoning": 5, "depth": 5, "clarity": 5}}, "notes": ""}\n```'
        result = _parse_judge_response(raw, self.rubric)
        assert result is not None
        assert result["scores"]["a"]["weighted"] == 5.0

    def test_invalid_json(self):
        result = _parse_judge_response("not json at all", self.rubric)
        assert result is None

    def test_missing_scores_key(self):
        raw = json.dumps({"notes": "no scores"})
        result = _parse_judge_response(raw, self.rubric)
        assert result is None

    def test_recalculates_weighted(self):
        """Weighted score is recalculated from rubric, not trusted from LLM."""
        raw = json.dumps({
            "scores": {
                "a": {"reasoning": 10, "depth": 10, "clarity": 10, "weighted": 999},
            },
            "notes": "",
        })
        result = _parse_judge_response(raw, self.rubric)
        # Should be 10*0.4 + 10*0.3 + 10*0.3 = 10.0, not 999
        assert result["scores"]["a"]["weighted"] == 10.0


# -- Filter Logic Tests ------------------------------------------

class TestFilterPerspectives:
    """Test AgentTeam._filter_perspectives()."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _make_team_with_rubric(self, pi_ids):
        base = Path(self.tmpdir)
        (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
        for pid in pi_ids:
            (base / "teams" / "test" / "pis" / pid).mkdir(parents=True, exist_ok=True)
            (base / "teams" / "test" / "pis" / pid / "soul.md").write_text(f"Pi {pid}")
        (base / "soul.md").write_text("soul")
        (base / "user.md").write_text("user")

        mm = MemoryManager(str(base))
        rubric = [
            {"criterion": "reasoning", "weight": 0.5},
            {"criterion": "clarity", "weight": 0.5},
        ]
        config = TeamConfig(
            id="test", role="general", lead_pi=pi_ids[0],
            pis=[PiConfig(id=pid) for pid in pi_ids],
            rubric=rubric,
        )
        return AgentTeam(config, mm)

    def test_filter_drops_low_scores(self):
        team = self._make_team_with_rubric(["a", "b", "c"])
        perspectives = [("a", "A answer"), ("b", "B answer"), ("c", "C answer")]
        judge_scores = {
            "scores": {
                "a": {"reasoning": 9, "clarity": 9, "weighted": 9.0},
                "b": {"reasoning": 8, "clarity": 8, "weighted": 8.0},
                "c": {"reasoning": 2, "clarity": 2, "weighted": 2.0},
            },
            "notes": "",
        }
        filtered = CouncilMode()._filter_perspectives(team, perspectives, judge_scores)
        pi_ids = [pid for pid, _ in filtered]
        # Mean = (9+8+2)/3 = 6.33, threshold = 6.33*0.7 = 4.43
        # c (2.0) < 4.43 -> dropped
        assert "a" in pi_ids
        assert "b" in pi_ids
        assert "c" not in pi_ids

    def test_filter_keeps_minimum_two(self):
        team = self._make_team_with_rubric(["a", "b", "c"])
        perspectives = [("a", "A"), ("b", "B"), ("c", "C")]
        judge_scores = {
            "scores": {
                "a": {"reasoning": 10, "clarity": 10, "weighted": 10.0},
                "b": {"reasoning": 1, "clarity": 1, "weighted": 1.0},
                "c": {"reasoning": 1, "clarity": 1, "weighted": 1.0},
            },
            "notes": "",
        }
        filtered = CouncilMode()._filter_perspectives(team, perspectives, judge_scores)
        # Mean = 4.0, threshold = 2.8; b and c both < 2.8
        # But we keep at least 2 -> top 2 by score = a, b (or c, same score)
        assert len(filtered) >= 2

    def test_filter_with_two_perspectives_keeps_both(self):
        team = self._make_team_with_rubric(["a", "b"])
        perspectives = [("a", "A"), ("b", "B")]
        judge_scores = {
            "scores": {
                "a": {"reasoning": 9, "clarity": 9, "weighted": 9.0},
                "b": {"reasoning": 2, "clarity": 2, "weighted": 2.0},
            },
            "notes": "",
        }
        filtered = CouncilMode()._filter_perspectives(team, perspectives, judge_scores)
        # Only 2 perspectives -> no filtering (<=2 guard)
        assert len(filtered) == 2

    def test_filter_with_empty_scores(self):
        team = self._make_team_with_rubric(["a", "b", "c"])
        perspectives = [("a", "A"), ("b", "B"), ("c", "C")]
        judge_scores = {"scores": {}, "notes": ""}
        filtered = CouncilMode()._filter_perspectives(team, perspectives, judge_scores)
        assert len(filtered) == 3  # No scores -> no filtering


# -- Council with Rubric Integration Tests -----------------------

class TestCouncilWithRubric:
    """Test full council flow with judge scoring."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _make_team_with_rubric(self, pi_ids):
        base = Path(self.tmpdir)
        (base / "teams" / "test" / "memory").mkdir(parents=True, exist_ok=True)
        for pid in pi_ids:
            (base / "teams" / "test" / "pis" / pid).mkdir(parents=True, exist_ok=True)
            (base / "teams" / "test" / "pis" / pid / "soul.md").write_text(f"Pi {pid}")
        (base / "soul.md").write_text("soul")
        (base / "user.md").write_text("user")

        mm = MemoryManager(str(base))
        rubric = [
            {"criterion": "reasoning", "weight": 0.4},
            {"criterion": "depth", "weight": 0.3},
            {"criterion": "clarity", "weight": 0.3},
        ]
        config = TeamConfig(
            id="test", role="general", lead_pi=pi_ids[0],
            pis=[PiConfig(id=pid) for pid in pi_ids],
            rubric=rubric,
        )
        return AgentTeam(config, mm)

    def test_council_with_judge_scoring(self):
        """Full flow: perspectives -judge -filter -synthesis."""
        team = self._make_team_with_rubric(["alpha", "beta", "gamma"])

        judge_response = json.dumps({
            "scores": {
                "alpha": {"reasoning": 8, "depth": 7, "clarity": 9},
                "beta": {"reasoning": 7, "depth": 6, "clarity": 8},
                "gamma": {"reasoning": 3, "depth": 2, "clarity": 3},
            },
            "notes": "Gamma is weak",
        })

        # alpha is lead_pi: perspective call, judge call, synthesis call
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",
            judge_response,
            "Final synthesis with judge context",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")
        team.pis["gamma"].run = AsyncMock(return_value="Gamma perspective")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("Evaluate X")
        )
        assert result == "Final synthesis with judge context"

        # Lead Pi called 3 times: perspective + judge + synthesis
        assert team.pis["alpha"].run.call_count == 3

        # Synthesis prompt should contain judge evaluation section
        synthesis_call = team.pis["alpha"].run.call_args_list[2]
        synthesis_prompt = synthesis_call[0][0]
        assert "Judge Evaluation" in synthesis_prompt
        assert "higher-scored perspectives" in synthesis_prompt

    def test_council_no_rubric_skips_judge(self):
        """No rubric -no judge step (backward compatible)."""
        team = _make_team(self.tmpdir, ["alpha", "beta"])
        # No rubric on this team
        assert team.rubric is None

        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",
            "Synthesized answer",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("Evaluate X")
        )
        assert result == "Synthesized answer"

        # Lead Pi called only 2 times: perspective + synthesis (no judge)
        assert team.pis["alpha"].run.call_count == 2

        # Synthesis prompt should NOT contain judge section
        synthesis_call = team.pis["alpha"].run.call_args_list[1]
        synthesis_prompt = synthesis_call[0][0]
        assert "Judge Evaluation" not in synthesis_prompt

    def test_council_judge_failure_graceful(self):
        """Judge fails -skip scoring, proceed with synthesis as normal."""
        team = self._make_team_with_rubric(["alpha", "beta"])

        # alpha is lead_pi: perspective call, judge call (fails), synthesis call
        team.pis["alpha"].run = AsyncMock(side_effect=[
            "Alpha perspective",
            "not valid json at all",  # Judge fails to parse
            "Synthesized without judge",
        ])
        team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

        result = asyncio.get_event_loop().run_until_complete(
            team.run_council("Evaluate X")
        )
        assert result == "Synthesized without judge"

        # Synthesis prompt should NOT have judge section (parsing failed)
        synthesis_call = team.pis["alpha"].run.call_args_list[2]
        synthesis_prompt = synthesis_call[0][0]
        assert "Judge Evaluation" not in synthesis_prompt


# -- TeamConfig.from_markdown Rubric Tests -----------------------

class TestTeamConfigRubric:
    """Test rubric parsing in TeamConfig.from_markdown()."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.base = Path(self.tmpdir)
        self.team_dir = self.base / "teams" / "test"
        self.team_dir.mkdir(parents=True)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_rubric_parsed_from_markdown(self):
        (self.team_dir / "team.md").write_text(
            "# Team: test\n"
            "- role: general\n"
            "- lead_pi: alpha\n"
            "- enabled: true\n"
            "- rubric: reasoning=0.4, depth=0.3, clarity=0.3\n"
        )
        config = TeamConfig.from_markdown(self.team_dir / "team.md")
        assert config.rubric is not None
        assert len(config.rubric) == 3
        assert config.rubric[0]["criterion"] == "reasoning"
        total = sum(e["weight"] for e in config.rubric)
        assert abs(total - 1.0) < 0.01

    def test_no_rubric_field(self):
        (self.team_dir / "team.md").write_text(
            "# Team: test\n"
            "- role: general\n"
            "- lead_pi: alpha\n"
            "- enabled: true\n"
        )
        config = TeamConfig.from_markdown(self.team_dir / "team.md")
        assert config.rubric is None



def test_council_passes_memory_task_hint(tmp_path):
    team = _make_team(str(tmp_path), ["alpha", "beta"])
    team.pis["alpha"].run = AsyncMock(side_effect=[
        "Alpha perspective",
        "Synthesized answer",
    ])
    team.pis["beta"].run = AsyncMock(return_value="Beta perspective")

    asyncio.get_event_loop().run_until_complete(
        team.run_council("Evaluate launch readiness", user_context={"company_id": "acme"})
    )

    beta_call = team.pis["beta"].run.call_args
    assert beta_call.kwargs["user_context"]["memory_task_hint"] == "Evaluate launch readiness"

    synthesis_call = team.pis["alpha"].run.call_args_list[1]
    assert synthesis_call.kwargs["user_context"]["memory_task_hint"] == "Evaluate launch readiness"
