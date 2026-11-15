"""Tests for the coding team system."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from companest.git_tools import git_branch, git_commit, git_diff, git_log, git_push, git_status
from companest.workspace import Workspace, WorkspaceRegistry
from companest.tools import TOOL_PRESETS, resolve_tool_names


def test_validate_path_confines_to_workspace(tmp_path):
    ws = Workspace(id="test", path=str(tmp_path))
    assert ws.validate_path(str(tmp_path / "src" / "main.py"))
    assert not ws.validate_path("/etc/passwd")
    assert not ws.validate_path(str(tmp_path / ".." / ".." / "etc" / "passwd"))


class TestWorkspaceRegistry:
    def test_load_and_build_context(self, tmp_path):
        config = {
            "companest": {
                "path": str(tmp_path),
                "repo": "git@github.com:example-org/example-repo.git",
                "branch": "main",
                "description": "Test",
            }
        }
        (tmp_path / "workspaces.json").write_text(json.dumps(config))
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()

        assert registry.list_workspaces() == ["companest"]
        ws = registry.get("companest")
        assert ws.path == str(tmp_path)
        assert ws.repo == "git@github.com:example-org/example-repo.git"

        ctx = registry.build_context("companest")
        assert "Active Workspace: companest" in ctx
        assert str(tmp_path) in ctx
        assert "git@github.com:example-org/example-repo.git" in ctx
        assert registry.build_context("nope") == ""

    @pytest.mark.parametrize("content", [
        None,
        "not json",
        json.dumps({"bad": {"repo": "git@example.com"}}),
    ], ids=["no_file", "invalid_json", "missing_path"])
    def test_load_unusable_config_yields_no_workspaces(self, tmp_path, content):
        if content is not None:
            (tmp_path / "workspaces.json").write_text(content)
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()
        assert registry.list_workspaces() == []


def _proc(stdout=""):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    proc.returncode = 0
    return proc


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


@pytest.mark.asyncio
class TestGitTools:
    """Git tool functions with mocked subprocess."""

    @pytest.mark.parametrize("tool, kwargs, stdout, expected", [
        (git_status, {}, "## main\n M file.py\n -  new.py\n", ["main", "file.py"]),
        (git_log, {"count": 5}, "* abc1234 feat: add login\n* def5678 fix: typo\n", ["abc1234"]),
        (git_diff, {}, "+added line\n-removed line\n", ["added line"]),
        (git_branch, {}, "* main\n  feat/login\n", ["main"]),
        (git_push, {}, "Everything up-to-date\n", ["up-to-date"]),
    ], ids=["status", "log", "diff", "branch_list", "push"])
    async def test_returns_git_output(self, repo, tool, kwargs, stdout, expected):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc(stdout)
            result = await tool(repo, **kwargs)
        for text in expected:
            assert text in result

    async def test_git_status_not_repo(self, tmp_path):
        result = await git_status(str(tmp_path))
        assert "Error" in result
        assert "not a git repository" in result

    async def test_git_status_nonexistent_path(self):
        assert "Error" in await git_status("/nonexistent/path/xyz")

    async def test_git_commit_no_message(self, repo):
        result = await git_commit(repo, message="")
        assert "Error" in result
        assert "commit message is required" in result

    async def test_git_commit_success(self, repo):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            # git add, git status --porcelain, git commit
            mock_exec.side_effect = [
                _proc(), _proc("M file.py\n"), _proc("[main abc1234] fix: thing\n"),
            ]
            result = await git_commit(repo, message="fix: thing", files="file.py")
        assert "abc1234" in result

    async def test_git_commit_nothing_to_commit(self, repo):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            # git add, then a clean status
            mock_exec.side_effect = [_proc(), _proc()]
            result = await git_commit(repo, message="test")
        assert "Nothing to commit" in result


class TestCoderToolResolution:
    def test_code_reviewer_preset_is_read_only(self):
        tools = TOOL_PRESETS["code-reviewer"]
        for name in ("Read", "git_status", "git_diff", "git_log"):
            assert name in tools
        for name in ("Write", "Edit", "git_commit", "git_push"):
            assert name not in tools

    def test_resolve_coder_preset(self):
        resolved = resolve_tool_names(["coder"])
        for name in ("Read", "Write", "Edit", "Bash", "Glob", "Grep"):
            assert name in resolved
        for name in ("git_status", "git_diff", "git_commit", "git_branch", "git_log"):
            assert f"mcp__git__{name}" in resolved
        assert "mcp__mem__memory_read" in resolved
