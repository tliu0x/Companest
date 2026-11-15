import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Workspace:
    id: str
    path: str           # absolute path to repo root
    repo: str = ""      # git remote URL (informational)
    branch: str = "main"
    description: str = ""
    company_id: Optional[str] = None
    storage_id: str = ""

    def validate_storage(self, company_id: Optional[str]) -> None:
        from ..runtime.storage import check_identity
        if self.company_id != company_id:
            raise ValueError(f"Workspace '{self.id}' belongs to another company scope")
        check_identity(Path(self.path), self.storage_id)

    def exists(self) -> bool:
        return Path(self.path).is_dir()

    def validate_path(self, target: str) -> bool:
        """Check that target path is within this workspace (path traversal guard)."""
        try:
            workspace_root = Path(self.path).resolve()
            target_resolved = Path(target).resolve()
            return target_resolved == workspace_root or str(target_resolved).startswith(str(workspace_root) + os.sep)
        except (OSError, ValueError):
            return False

    def resolve_path(self, relative_path: str) -> Optional[Path]:
        absolute = Path(self.path) / relative_path
        if not self.validate_path(str(absolute)):
            return None
        return absolute

    def read_file(self, relative_path: str, encoding: str = "utf-8") -> str:
        resolved = self.resolve_path(relative_path)
        if resolved is None:
            raise ValueError(f"Path '{relative_path}' is outside workspace '{self.id}'")
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {relative_path}")
        return resolved.read_text(encoding=encoding)

    def write_file(self, relative_path: str, content: str, encoding: str = "utf-8") -> str:
        resolved = self.resolve_path(relative_path)
        if resolved is None:
            raise ValueError(f"Path '{relative_path}' is outside workspace '{self.id}'")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding=encoding)
        return str(resolved)

    def list_files(self, glob_pattern: str = "**/*", max_results: int = 500) -> List[str]:
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


class WorkspaceRegistry:
    def __init__(self, base_path: str):
        self._config_path = Path(base_path) / "workspaces.json"
        self._workspaces: Dict[str, Workspace] = {}

    def load(self) -> None:
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
                company_id=ws_config.get("company_id"),
                storage_id=ws_config.get("storage_id", ""),
            )

            if not ws.exists():
                logger.warning(f"Workspace '{ws_id}' path does not exist: {ws.path}")

            self._workspaces[ws_id] = ws
            logger.info(f"Loaded workspace '{ws_id}': {ws.path}")

    def get(self, workspace_id: str) -> Optional[Workspace]:
        return self._workspaces.get(workspace_id)

    def list_workspaces(self) -> List[str]:
        return list(self._workspaces.keys())

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
