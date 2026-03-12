"""
Companest Git Tools

Async git operations for coding agents. Each function takes a workspace_path
and executes git commands within that directory.

All functions validate that the workspace path exists and is a git repo.
Path traversal is prevented by the Workspace.validate_path() check.

These are exposed as MCP tools via the ToolRegistry.
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Max output length to avoid flooding LLM context
_MAX_OUTPUT = 8000


async def _run_git(
    args: List[str], cwd: str, timeout: float = 30.0,
) -> str:
    """Run a git command and return stdout, or error string on failure."""
    git_dir = Path(cwd)
    if not git_dir.is_dir():
        return f"Error: workspace path does not exist: {cwd}"
    if not (git_dir / ".git").exists():
        return f"Error: not a git repository: {cwd}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        return f"Error: git command timed out after {timeout}s"
    except FileNotFoundError:
        return "Error: git is not installed"
    except Exception as e:
        return f"Error: {e}"

    output = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        return f"Error (exit {proc.returncode}):\n{err.strip()}"

    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + f"\n... (truncated, {len(output)} chars total)"

    return output if output.strip() else "(no output)"


async def git_status(workspace_path: str) -> str:
    """Show working tree status: branch, staged, modified, untracked files."""
    return await _run_git(
        ["status", "--short", "--branch"], cwd=workspace_path,
    )


async def git_diff(
    workspace_path: str, file_path: str = "", staged: bool = False,
) -> str:
    """
    Show file changes.

    Args:
        workspace_path: Root path of the git repo.
        file_path: Optional specific file to diff (relative to repo root).
        staged: If True, show staged changes (--cached).
    """
    args = ["diff"]
    if staged:
        args.append("--cached")
    args.append("--stat")
    # First show the summary
    summary = await _run_git(args, cwd=workspace_path)

    # Then show the actual diff (possibly for a specific file)
    detail_args = ["diff"]
    if staged:
        detail_args.append("--cached")
    if file_path:
        detail_args.append("--")
        detail_args.append(file_path)
    detail = await _run_git(detail_args, cwd=workspace_path)

    return f"=== Summary ===\n{summary}\n=== Diff ===\n{detail}"


async def git_log(workspace_path: str, count: int = 10) -> str:
    """Show recent commit log."""
    n = min(max(count, 1), 50)
    return await _run_git(
        ["log", f"-{n}", "--oneline", "--decorate", "--graph"],
        cwd=workspace_path,
    )


async def git_branch(
    workspace_path: str,
    name: str = "",
    checkout: bool = False,
) -> str:
    """
    List, create, or switch branches.

    Args:
        workspace_path: Root path of the git repo.
        name: Branch name. Empty = list all branches.
        checkout: If True and name is given, switch to (or create) that branch.
    """
    if not name:
        return await _run_git(["branch", "-a", "--no-color"], cwd=workspace_path)

    if checkout:
        # Try checkout existing, fallback to create new
        result = await _run_git(["checkout", name], cwd=workspace_path)
        if "error" in result.lower() and "did not match" in result.lower():
            result = await _run_git(
                ["checkout", "-b", name], cwd=workspace_path,
            )
        return result

    return await _run_git(["branch", name], cwd=workspace_path)


async def git_commit(
    workspace_path: str,
    message: str,
    files: str = "",
) -> str:
    """
    Stage files and create a commit.

    Args:
        workspace_path: Root path of the git repo.
        message: Commit message.
        files: Comma-separated file paths to stage. Empty = stage all modified.
    """
    if not message:
        return "Error: commit message is required"

    # Stage files
    if files:
        file_list = [f.strip() for f in files.split(",") if f.strip()]
        stage_result = await _run_git(["add"] + file_list, cwd=workspace_path)
    else:
        stage_result = await _run_git(["add", "-u"], cwd=workspace_path)

    if stage_result.startswith("Error"):
        return f"Staging failed: {stage_result}"

    # Check there's something to commit
    status = await _run_git(["status", "--porcelain"], cwd=workspace_path)
    if not status.strip() or status == "(no output)" or status.startswith("Error"):
        return "Nothing to commit (working tree clean)"

    # Commit
    return await _run_git(["commit", "-m", message], cwd=workspace_path)


async def git_push(
    workspace_path: str,
    remote: str = "origin",
    branch: str = "",
    set_upstream: bool = True,
) -> str:
    """
    Push commits to remote.

    Args:
        workspace_path: Root path of the git repo.
        remote: Remote name (default: origin).
        branch: Branch to push. Empty = current branch.
        set_upstream: If True, set upstream tracking (-u).
    """
    args = ["push"]
    if set_upstream:
        args.append("-u")
    args.append(remote)
    if branch:
        args.append(branch)

    return await _run_git(args, cwd=workspace_path)
