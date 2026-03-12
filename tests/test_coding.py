"""
Tests for the coding team system.

Tests:
1. Workspace registry (load, validate, context)
2. Git tool functions (mocked subprocess)
3. Tool resolution (coder/code-reviewer presets)
4. Coding team config validation
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companest.workspace import Workspace, WorkspaceRegistry
from companest.tools import (
    GIT_TOOL_NAMES,
    TOOL_PRESETS,
    resolve_tool_names,
)


#  1. Workspace Registry 

class TestWorkspace:
    """Test the Workspace dataclass."""

    def test_validate_path_within_workspace(self, tmp_path):
        ws = Workspace(id="test", path=str(tmp_path))
        assert ws.validate_path(str(tmp_path / "src" / "main.py"))

    def test_validate_path_outside_workspace(self, tmp_path):
        ws = Workspace(id="test", path=str(tmp_path))
        assert not ws.validate_path("/etc/passwd")

    def test_validate_path_traversal(self, tmp_path):
        ws = Workspace(id="test", path=str(tmp_path))
        assert not ws.validate_path(str(tmp_path / ".." / ".." / "etc" / "passwd"))

    def test_exists_true(self, tmp_path):
        ws = Workspace(id="test", path=str(tmp_path))
        assert ws.exists()

    def test_exists_false(self):
        ws = Workspace(id="test", path="/nonexistent/path/xyz")
        assert not ws.exists()


class TestWorkspaceRegistry:
    """Test WorkspaceRegistry loading and operations."""

    def test_load_valid_config(self, tmp_path):
        config = {
            "myproject": {
                "path": str(tmp_path),
                "repo": "git@github.com:user/repo.git",
                "branch": "main",
                "description": "Test project",
            }
        }
        config_file = tmp_path / "workspaces.json"
        config_file.write_text(json.dumps(config))

        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()

        assert "myproject" in registry.list_workspaces()
        ws = registry.get("myproject")
        assert ws is not None
        assert ws.path == str(tmp_path)
        assert ws.repo == "git@github.com:user/repo.git"

    def test_load_no_config_file(self, tmp_path):
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()
        assert registry.list_workspaces() == []

    def test_load_invalid_json(self, tmp_path):
        (tmp_path / "workspaces.json").write_text("not json")
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()
        assert registry.list_workspaces() == []

    def test_load_missing_path(self, tmp_path):
        config = {"bad": {"repo": "git@example.com"}}
        (tmp_path / "workspaces.json").write_text(json.dumps(config))
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()
        assert registry.list_workspaces() == []

    def test_get_nonexistent(self, tmp_path):
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()
        assert registry.get("nope") is None

    def test_build_context(self, tmp_path):
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

        ctx = registry.build_context("companest")
        assert "Active Workspace: companest" in ctx
        assert str(tmp_path) in ctx
        assert "git@github.com:example-org/example-repo.git" in ctx

    def test_build_context_nonexistent(self, tmp_path):
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()
        assert registry.build_context("nope") == ""

    def test_list_available(self, tmp_path):
        config = {
            "proj": {
                "path": str(tmp_path),
                "description": "Test",
            }
        }
        (tmp_path / "workspaces.json").write_text(json.dumps(config))
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()

        available = registry.list_available()
        assert len(available) == 1
        assert available[0]["id"] == "proj"
        assert available[0]["exists"] is True


#  2. Git Tools (mocked) 

class TestGitTools:
    """Test git tool functions with mocked subprocess."""

    def _mock_process(self, stdout="", stderr="", returncode=0):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(
            stdout.encode(), stderr.encode(),
        ))
        proc.returncode = returncode
        return proc

    @pytest.mark.asyncio
    async def test_git_status(self, tmp_path):
        from companest.git_tools import git_status
        # Create a fake .git dir
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = self._mock_process(
                stdout="## main\n M file.py\n -  new.py\n"
            )
            result = await git_status(str(tmp_path))
            assert "main" in result
            assert "file.py" in result

    @pytest.mark.asyncio
    async def test_git_status_not_repo(self, tmp_path):
        from companest.git_tools import git_status
        result = await git_status(str(tmp_path))
        assert "Error" in result
        assert "not a git repository" in result

    @pytest.mark.asyncio
    async def test_git_status_nonexistent_path(self):
        from companest.git_tools import git_status
        result = await git_status("/nonexistent/path/xyz")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_git_log(self, tmp_path):
        from companest.git_tools import git_log
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = self._mock_process(
                stdout="* abc1234 feat: add login\n* def5678 fix: typo\n"
            )
            result = await git_log(str(tmp_path), count=5)
            assert "abc1234" in result

    @pytest.mark.asyncio
    async def test_git_diff(self, tmp_path):
        from companest.git_tools import git_diff
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = self._mock_process(
                stdout="+added line\n-removed line\n"
            )
            result = await git_diff(str(tmp_path))
            assert "added line" in result

    @pytest.mark.asyncio
    async def test_git_branch_list(self, tmp_path):
        from companest.git_tools import git_branch
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = self._mock_process(
                stdout="* main\n  feat/login\n"
            )
            result = await git_branch(str(tmp_path))
            assert "main" in result

    @pytest.mark.asyncio
    async def test_git_commit_no_message(self, tmp_path):
        from companest.git_tools import git_commit
        (tmp_path / ".git").mkdir()

        result = await git_commit(str(tmp_path), message="")
        assert "Error" in result
        assert "commit message is required" in result

    @pytest.mark.asyncio
    async def test_git_commit_success(self, tmp_path):
        from companest.git_tools import git_commit
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            # First call: git add
            # Second call: git status --porcelain
            # Third call: git commit
            mock_exec.side_effect = [
                self._mock_process(stdout=""),  # git add
                self._mock_process(stdout="M file.py\n"),  # status
                self._mock_process(stdout="[main abc1234] fix: thing\n"),  # commit
            ]
            result = await git_commit(str(tmp_path), message="fix: thing", files="file.py")
            assert "abc1234" in result

    @pytest.mark.asyncio
    async def test_git_commit_nothing_to_commit(self, tmp_path):
        from companest.git_tools import git_commit
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [
                self._mock_process(stdout=""),  # git add
                self._mock_process(stdout=""),  # status (empty = clean)
            ]
            result = await git_commit(str(tmp_path), message="test")
            assert "Nothing to commit" in result

    @pytest.mark.asyncio
    async def test_git_push(self, tmp_path):
        from companest.git_tools import git_push
        (tmp_path / ".git").mkdir()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = self._mock_process(
                stdout="Everything up-to-date\n"
            )
            result = await git_push(str(tmp_path))
            assert "up-to-date" in result


#  3. Tool Resolution 

class TestCoderToolResolution:
    """Test coder and code-reviewer preset resolution."""

    def test_coder_preset_exists(self):
        assert "coder" in TOOL_PRESETS
        tools = TOOL_PRESETS["coder"]
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" in tools
        assert "Bash" in tools
        assert "Glob" in tools
        assert "Grep" in tools
        assert "git_status" in tools
        assert "git_diff" in tools
        assert "git_commit" in tools
        assert "git_branch" in tools
        assert "git_log" in tools
        assert "memory_read" in tools

    def test_code_reviewer_preset_exists(self):
        assert "code-reviewer" in TOOL_PRESETS
        tools = TOOL_PRESETS["code-reviewer"]
        assert "Read" in tools
        assert "git_status" in tools
        assert "git_diff" in tools
        assert "git_log" in tools
        # Reviewer should NOT have write/commit tools
        assert "Write" not in tools
        assert "Edit" not in tools
        assert "git_commit" not in tools
        assert "git_push" not in tools

    def test_resolve_coder_preset(self):
        resolved = resolve_tool_names(["coder"])
        # Built-in SDK tools
        assert "Read" in resolved
        assert "Write" in resolved
        assert "Edit" in resolved
        assert "Bash" in resolved
        assert "Glob" in resolved
        assert "Grep" in resolved
        # Git tools get mcp__git__ prefix
        assert "mcp__git__git_status" in resolved
        assert "mcp__git__git_diff" in resolved
        assert "mcp__git__git_commit" in resolved
        assert "mcp__git__git_branch" in resolved
        assert "mcp__git__git_log" in resolved
        # Memory tools
        assert "mcp__mem__memory_read" in resolved

    def test_resolve_individual_git_tools(self):
        resolved = resolve_tool_names(["git_status", "git_diff"])
        assert resolved == ["mcp__git__git_status", "mcp__git__git_diff"]

    def test_git_tool_names_constant(self):
        assert GIT_TOOL_NAMES == {
            "git_status", "git_diff", "git_log",
            "git_branch", "git_commit", "git_push",
        }


#  4. Team Config 

class TestCodingTeamConfig:
    """Test the coding team config files."""

    def test_team_md_valid(self):
        path = Path("examples/minimal-setup/.companest/teams/coding/team.md")
        assert path.exists(), "team.md should exist"
        content = path.read_text()
        assert "coding" in content
        assert "coder" in content
        assert "reviewer" in content
        assert "tools_deny: none" in content

    def test_coder_soul_valid(self):
        path = Path("examples/minimal-setup/.companest/teams/coding/pis/coder/soul.md")
        assert path.exists(), "coder soul.md should exist"
        content = path.read_text()
        assert "git_branch" in content
        assert "git_commit" in content
        assert "feature branch" in content

    def test_reviewer_soul_valid(self):
        path = Path("examples/minimal-setup/.companest/teams/coding/pis/reviewer/soul.md")
        assert path.exists(), "reviewer soul.md should exist"
        content = path.read_text()
        assert "APPROVE" in content
        assert "REQUEST CHANGES" in content
        assert "security" in content.lower()


#  5. Workspaces Config 

class TestWorkspacesConfig:
    """Test the workspaces.json file."""

    def test_workspaces_json_valid(self):
        path = Path("examples/minimal-setup/.companest/workspaces.json")
        assert path.exists(), "workspaces.json should exist"
        data = json.loads(path.read_text())
        assert isinstance(data, dict)
        assert "companest" in data
        assert "path" in data["companest"]


#  6. Integration 

class TestWorkspaceOrchestratorIntegration:
    """Test workspace injection into orchestrator run_team."""

    def test_workspace_context_injected_into_extra(self, tmp_path):
        """Verify workspace_path gets set in Pi._extra_tool_context."""
        from companest.workspace import WorkspaceRegistry

        config = {"test": {"path": str(tmp_path), "description": "Test"}}
        (tmp_path / "workspaces.json").write_text(json.dumps(config))
        registry = WorkspaceRegistry(str(tmp_path))
        registry.load()

        ws = registry.get("test")
        assert ws is not None
        assert ws.path == str(tmp_path)

        context = registry.build_context("test")
        assert "Active Workspace: test" in context
        assert str(tmp_path) in context
