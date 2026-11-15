"""Built-in agent tools: workspace file access, shell, and web."""

import asyncio
import ipaddress
import json
import re
import socket
import threading
from functools import wraps
from typing import List
from urllib.parse import urlparse

from ..utils import offload
from .workspace import Workspace

MAX_OUTPUT_CHARS = 30_000
BASH_TIMEOUT_SECONDS = 120
FETCH_TIMEOUT_SECONDS = 20


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(text) - MAX_OUTPUT_CHARS} more characters]"


def _is_public_host(host: str) -> bool:
    """Reject loopback, private, link-local and other non-public addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


def create_builtin_tool_defs(ctx) -> List:
    from .tools import ToolDefinition

    defs = _web_tool_defs(ToolDefinition)
    workspace_path = ctx.extra.get("workspace_path")
    if workspace_path:
        defs.extend(_workspace_tool_defs(ToolDefinition, Workspace(id="run", path=str(workspace_path))))
    return defs


_workspace_lock = threading.RLock()


def _file_tool(func):
    @offload
    @wraps(func)
    def wrapped(args):
        with _workspace_lock:
            return func(args)
    return wrapped


def _workspace_tool_defs(ToolDefinition, ws: Workspace) -> List:
    @_file_tool
    def read(args):
        try:
            return _clip(ws.read_file(args["path"]))
        except (ValueError, OSError) as e:
            return f"Error: {e}"

    @_file_tool
    def write(args):
        try:
            ws.write_file(args["path"], args["content"])
        except (ValueError, OSError) as e:
            return f"Error: {e}"
        return f"Wrote {len(args['content'])} characters to {args['path']}"

    @_file_tool
    def edit(args):
        try:
            text = ws.read_file(args["path"])
        except (ValueError, OSError) as e:
            return f"Error: {e}"
        count = text.count(args["old_string"])
        if count != 1:
            return f"Error: old_string must match exactly once, found {count} matches"
        ws.write_file(args["path"], text.replace(args["old_string"], args["new_string"]))
        return f"Edited {args['path']}"

    @_file_tool
    def glob(args):
        return json.dumps(ws.list_files(args.get("pattern") or "**/*"))

    @_file_tool
    def grep(args):
        try:
            regex = re.compile(args["pattern"])
        except re.error as e:
            return f"Error: invalid regular expression: {e}"
        matches = []
        for rel in ws.list_files(args.get("glob") or "**/*", max_results=5000):
            try:
                lines = ws.read_file(rel).splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                if regex.search(line):
                    matches.append(f"{rel}:{number}: {line.strip()}")
                    if len(matches) >= 200:
                        return _clip("\n".join(matches))
        return _clip("\n".join(matches)) or "No matches"

    async def bash(args):
        proc = await asyncio.create_subprocess_shell(
            args["command"],
            cwd=ws.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=BASH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: command timed out after {BASH_TIMEOUT_SECONDS}s"
        return _clip(f"exit code {proc.returncode}\n{out.decode('utf-8', errors='replace')}")

    path = {"type": "string", "description": "Path relative to the workspace root"}
    return [
        ToolDefinition("Read", "Read a text file from the workspace.", {"path": path}, read),
        ToolDefinition(
            "Write", "Create or overwrite a file in the workspace.",
            {"path": path, "content": {"type": "string", "description": "Full file content"}}, write,
        ),
        ToolDefinition(
            "Edit", "Replace one exact occurrence of old_string with new_string in a workspace file.",
            {
                "path": path,
                "old_string": {"type": "string", "description": "Text to replace; must occur exactly once"},
                "new_string": {"type": "string", "description": "Replacement text"},
            }, edit,
        ),
        ToolDefinition(
            "Glob", "List workspace files matching a glob pattern.",
            {"pattern": {"type": "string", "description": "Glob pattern, e.g. 'src/**/*.py'", "optional": True}}, glob,
        ),
        ToolDefinition(
            "Grep", "Search workspace files with a regular expression.",
            {
                "pattern": {"type": "string", "description": "Regular expression"},
                "glob": {"type": "string", "description": "Limit the search to files matching this glob", "optional": True},
            }, grep,
        ),
        ToolDefinition(
            "Bash", "Run a shell command in the workspace root.",
            {"command": {"type": "string", "description": "Shell command"}}, bash,
        ),
    ]


def _web_tool_defs(ToolDefinition) -> List:
    from ..knowledge import feeds

    async def web_search(args):
        items = await feeds.brave_search(query=args["query"], count=int(args.get("count", 10)))
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def web_fetch(args):
        url = args["url"]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "Error: only http and https URLs are supported"
        if not await asyncio.to_thread(_is_public_host, parsed.hostname):
            return "Error: refusing to fetch a non-public address"
        import httpx
        try:
            # Redirects are not followed, so a public URL cannot bounce the request to an internal address.
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
                resp = await client.get(url, headers={"User-Agent": "Companest/1.0"})
        except httpx.HTTPError as e:
            return f"Error: {e}"
        if resp.is_redirect:
            return f"Redirected to {resp.headers.get('location', '')}; fetch that URL if it is the page you need"
        text = resp.text
        if "html" in resp.headers.get("content-type", ""):
            text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
        return _clip(f"HTTP {resp.status_code}\n{text}")

    return [
        ToolDefinition(
            "WebSearch", "Search the web. Requires BRAVE_API_KEY.",
            {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "number", "description": "Number of results (default 10, max 20)", "optional": True},
            }, web_search,
        ),
        ToolDefinition(
            "WebFetch", "Fetch a public web page and return its text.",
            {"url": {"type": "string", "description": "http or https URL"}}, web_fetch,
        ),
    ]
