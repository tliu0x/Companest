"""
Companest Workspace Registry

Manages project workspaces  each workspace points to a local git repo
that coding agents can operate on. Provides path isolation, file operations,
and context injection for Pi agents.

Config file: .companest/workspaces.json
"""

import fnmatch
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Workspace:
    """A registered project workspace."""
    id: str
    path: str           # absolute path to repo root
    repo: str = ""      # git remote URL (informational)
    branch: str = "main"  # default branch
    description: str = ""

    def exists(self) -> bool:
        return Path(self.path).is_dir()

    def is_git_repo(self) -> bool:
        return (Path(self.path) / ".git").exists()

    def validate_path(self, target: str) -> bool:
        """Check that target path is within this workspace (path traversal guard)."""
        try:
            workspace_root = Path(self.path).resolve()
            target_resolved = Path(target).resolve()
            return target_resolved == workspace_root or str(target_resolved).startswith(str(workspace_root) + os.sep)
        except (OSError, ValueError):
            return False

    def resolve_path(self, relative_path: str) -> Optional[Path]:
        """Resolve a relative path within this workspace. Returns None if invalid."""
        absolute = Path(self.path) / relative_path
        if not self.validate_path(str(absolute)):
            return None
        return absolute

    def read_file(self, relative_path: str, encoding: str = "utf-8") -> str:
        """Read a file within the workspace.

        Args:
            relative_path: Path relative to workspace root.
            encoding: File encoding (default utf-8).

        Returns:
            File contents as string.

        Raises:
            ValueError: If path is outside workspace.
            FileNotFoundError: If file doesn't exist.
        """
        resolved = self.resolve_path(relative_path)
        if resolved is None:
            raise ValueError(f"Path '{relative_path}' is outside workspace '{self.id}'")
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {relative_path}")
        return resolved.read_text(encoding=encoding)

    def write_file(self, relative_path: str, content: str, encoding: str = "utf-8") -> str:
        """Write content to a file within the workspace.

        Creates parent directories if needed.

        Args:
            relative_path: Path relative to workspace root.
            content: File content to write.
            encoding: File encoding (default utf-8).

        Returns:
            Absolute path of the written file.

        Raises:
            ValueError: If path is outside workspace.
        """
        resolved = self.resolve_path(relative_path)
        if resolved is None:
            raise ValueError(f"Path '{relative_path}' is outside workspace '{self.id}'")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding=encoding)
        return str(resolved)

    def list_files(self, glob_pattern: str = "**/*", max_results: int = 500) -> List[str]:
        """List files in the workspace matching a glob pattern.

        Args:
            glob_pattern: Glob pattern relative to workspace root.
            max_results: Maximum number of results.

        Returns:
            List of relative paths matching the pattern.
        """
        root = Path(self.path)
        if not root.is_dir():
            return []
        results = []
        for p in root.glob(glob_pattern):
            if p.is_file() and not any(part.startswith(".git") for part in p.parts):
                rel = str(p.relative_to(root)).replace("\\", "/")
                results.append(rel)
                if len(results) >= max_results:
                    break
        return sorted(results)

    def search_content(self, pattern: str, glob_filter: str = "**/*",
                       max_results: int = 50) -> List[Dict[str, Any]]:
        """Search file contents for a pattern (simple substring match).

        Args:
            pattern: Text pattern to search for.
            glob_filter: Glob pattern to filter files.
            max_results: Maximum number of matching lines to return.

        Returns:
            List of {"file": relative_path, "line": line_number, "text": line_content}.
        """
        root = Path(self.path)
        if not root.is_dir():
            return []
        results = []
        for file_path in root.glob(glob_filter):
            if not file_path.is_file():
                continue
            if any(part.startswith(".git") for part in file_path.parts):
                continue
            try:
                for line_num, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern in line:
                        rel = str(file_path.relative_to(root)).replace("\\", "/")
                        results.append({"file": rel, "line": line_num, "text": line.strip()[:200]})
                        if len(results) >= max_results:
                            return results
            except (OSError, UnicodeDecodeError):
                continue
        return results


class WorkspaceRegistry:
    """
    Load and manage project workspaces from .companest/workspaces.json.

    Usage:
        registry = WorkspaceRegistry("/path/to/.companest")
        registry.load()
        ws = registry.get("companest")
        ws.path  # "/home/ubuntu/Companest"
    """

    def __init__(self, base_path: str):
        self._base_path = Path(base_path)
        self._config_path = self._base_path / "workspaces.json"
        self._workspaces: Dict[str, Workspace] = {}

    def load(self) -> None:
        """Load workspaces from config file."""
        if not self._config_path.exists():
            logger.info("No workspaces.json found, workspace features disabled")
            return

        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load workspaces.json: {e}")
            return

        if not isinstance(data, dict):
            logger.error("workspaces.json must be a JSON object")
            return

        for ws_id, ws_config in data.items():
            if not isinstance(ws_config, dict):
                logger.warning(f"Skipping invalid workspace '{ws_id}'")
                continue
            if "path" not in ws_config:
                logger.warning(f"Workspace '{ws_id}' missing 'path', skipping")
                continue

            ws = Workspace(
                id=ws_id,
                path=ws_config["path"],
                repo=ws_config.get("repo", ""),
                branch=ws_config.get("branch", "main"),
                description=ws_config.get("description", ""),
            )

            if not ws.exists():
                logger.warning(f"Workspace '{ws_id}' path does not exist: {ws.path}")

            self._workspaces[ws_id] = ws
            logger.info(f"Loaded workspace '{ws_id}': {ws.path}")

    def get(self, workspace_id: str) -> Optional[Workspace]:
        """Get a workspace by ID."""
        return self._workspaces.get(workspace_id)

    def list_workspaces(self) -> List[str]:
        """List all workspace IDs."""
        return list(self._workspaces.keys())

    def list_available(self) -> List[Dict]:
        """List workspaces with metadata (for tool output)."""
        result = []
        for ws in self._workspaces.values():
            result.append({
                "id": ws.id,
                "path": ws.path,
                "repo": ws.repo,
                "branch": ws.branch,
                "description": ws.description,
                "exists": ws.exists(),
                "is_git": ws.is_git_repo() if ws.exists() else False,
            })
        return result

    def build_context(self, workspace_id: str) -> str:
        """Build workspace context string for system prompt injection."""
        ws = self.get(workspace_id)
        if not ws:
            return ""
        lines = [
            f"## Active Workspace: {ws.id}",
            f"- Path: {ws.path}",
        ]
        if ws.repo:
            lines.append(f"- Repo: {ws.repo}")
        if ws.branch:
            lines.append(f"- Default branch: {ws.branch}")
        if ws.description:
            lines.append(f"- Description: {ws.description}")
        lines.append("")
        lines.append(
            "All file operations (Read, Write, Edit, Glob, Grep) and git tools "
            "operate within this workspace. Use absolute paths based on the workspace path above."
        )
        return "\n".join(lines)

    async def git_status(self, workspace_id: str) -> str:
        """Run git status in a workspace."""
        ws = self.get(workspace_id)
        if not ws:
            return f"Error: workspace '{workspace_id}' not found"
        try:
            from .git_tools import git_status
            return await git_status(ws.path)
        except ImportError:
            return "Error: git_tools module not available"

    async def git_diff(self, workspace_id: str, staged: bool = False) -> str:
        """Run git diff in a workspace."""
        ws = self.get(workspace_id)
        if not ws:
            return f"Error: workspace '{workspace_id}' not found"
        try:
            from .git_tools import git_diff
            return await git_diff(ws.path, staged=staged)
        except ImportError:
            return "Error: git_tools module not available"

    async def git_commit(self, workspace_id: str, message: str, files: str = "") -> str:
        """Run git commit in a workspace."""
        ws = self.get(workspace_id)
        if not ws:
            return f"Error: workspace '{workspace_id}' not found"
        try:
            from .git_tools import git_commit
            return await git_commit(ws.path, message=message, files=files)
        except ImportError:
            return "Error: git_tools module not available"

    async def git_log(self, workspace_id: str, count: int = 10) -> str:
        """Run git log in a workspace."""
        ws = self.get(workspace_id)
        if not ws:
            return f"Error: workspace '{workspace_id}' not found"
        try:
            from .git_tools import git_log
            return await git_log(ws.path, count=count)
        except ImportError:
            return "Error: git_tools module not available"
