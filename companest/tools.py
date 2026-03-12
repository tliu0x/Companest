"""
Companest Tools

Memory tools for Pi agents, exposed as MCP servers for claude-agent-sdk
and as function tools for openai-agents.

Built-in SDK tools (Read, Write, Edit, Bash, WebSearch, etc.) are referenced
by name in allowed_tools -no definitions needed here.

This module defines:
- ToolDefinition: define a tool once (name, description, params, handler)
- Adapter functions: definitions_to_mcp(), definitions_to_openai()
- Definition builders: create_*_tool_defs() for each tool group
- Backward-compat wrappers: create_*_mcp_server(), create_*_openai_tools()
- Tool preset mappings for different Pi roles
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .memory import MemoryManager, MemoryBackend, FileBackend, MemorySearchService

logger = logging.getLogger(__name__)

# -- Tool presets (referenced by name in team.md configs) --------

TOOL_PRESETS = {
    "pi-core": ["Read", "Write", "Edit", "Glob", "Grep"],
    "pi-safe": ["Read", "Glob", "Grep", "memory_read"],
    "researcher": ["WebSearch", "WebFetch", "memory_read", "memory_write"],
    "analyst": ["WebSearch", "memory_read", "memory_write", "memory_list", "memory_index", "memory_search"],
    "reporter": ["memory_read"],
    "reviewer": ["Read", "Glob", "Grep", "memory_read"],
    "archiver": ["memory_read", "memory_write", "memory_list", "memory_index", "memory_search"],
    "accountant": ["memory_read", "memory_write"],
    "scheduler": ["schedule_task", "list_schedules", "cancel_schedule"],
    "collector": [
        "brave_search", "fetch_rss", "fetch_reddit", "fetch_hn", "fetch_x",
        "fetch_openbb",
        "memory_read", "memory_write", "memory_list",
    ],
    "messenger": [
        "sessions_send", "sessions_list", "sessions_history",
        "memory_read", "memory_write",
    ],
    "coder": [
        "Read", "Write", "Edit", "Glob", "Grep", "Bash",
        "git_status", "git_diff", "git_log", "git_branch", "git_commit",
        "memory_read", "memory_write", "memory_list",
    ],
    "code-reviewer": [
        "Read", "Glob", "Grep",
        "git_status", "git_diff", "git_log",
        "memory_read",
    ],
    "ceo": [
        "run_team", "run_auto",
        "memory_read", "memory_write", "memory_list",
    ],
}

# Default deny list -tools blocked unless Pi explicitly opts out with "none"
DEFAULT_TOOLS_DENY = {"Bash"}

# Claude Agent SDK built-in tool names
CLAUDE_BUILTIN_TOOLS = {
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebSearch", "WebFetch",
}

# Custom tool names (need MCP server or function definitions)
CUSTOM_TOOL_NAMES = {"memory_read", "memory_write", "memory_list", "memory_index", "memory_search"}

# Scheduler tool names (need separate MCP server with user context)
SCHEDULER_TOOL_NAMES = {"schedule_task", "list_schedules", "cancel_schedule"}

# Feed tool names (data collection, need feed MCP server)
FEED_TOOL_NAMES = {"brave_search", "fetch_rss", "fetch_reddit", "fetch_hn", "fetch_x", "fetch_openbb"}

# Sessions tool names (agent-to-agent messaging)
SESSIONS_TOOL_NAMES = {"sessions_send", "sessions_list", "sessions_history"}

# Orchestrator tool names (CEO agent calls run_team/run_auto)
ORCHESTRATOR_TOOL_NAMES = {"run_team", "run_auto"}

# Git tool names (coding agents, need git MCP server with workspace context)
GIT_TOOL_NAMES = {"git_status", "git_diff", "git_log", "git_branch", "git_commit", "git_push"}


# -- Skills ----------------------------------------------------


@dataclass
class SkillDefinition:
    """A global skill loaded from .companest/skills/{name}/skill.md."""
    name: str           # directory name
    tools: List[str]    # tool names this skill provides
    description: str    # human-readable
    instructions: str   # injected into Pi system prompt when active


def load_skills(base_path: str) -> Dict[str, SkillDefinition]:
    """
    Scan .companest/skills/ directory, parse each skill.md into a SkillDefinition.

    Expected format of skill.md:
        # Skill: web-research
        - tools: WebSearch, WebFetch, memory_write
        - description: Comprehensive web research with source tracking

        ## Instructions
        When performing web research:
        1. Search multiple sources for breadth
        ...
    """
    skills_dir = Path(base_path) / "skills"
    if not skills_dir.exists():
        return {}

    skills: Dict[str, SkillDefinition] = {}
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_md = skill_dir / "skill.md"
        if not skill_dir.is_dir() or not skill_md.exists():
            continue

        try:
            text = skill_md.read_text(encoding="utf-8")
            name = skill_dir.name

            # Parse tools
            tools_match = re.search(r"^-\s*tools\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            tools = [t.strip() for t in tools_match.group(1).split(",")] if tools_match else []

            # Parse description
            desc_match = re.search(r"^-\s*description\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            description = desc_match.group(1).strip() if desc_match else ""

            # Parse instructions (everything after ## Instructions)
            instr_match = re.search(r"##\s+Instructions\s*\n(.*)", text, re.DOTALL | re.IGNORECASE)
            instructions = instr_match.group(1).strip() if instr_match else ""

            skills[name] = SkillDefinition(
                name=name, tools=tools, description=description, instructions=instructions,
            )
            logger.info(f"Loaded skill '{name}': {len(tools)} tools")
        except Exception as e:
            logger.error(f"Failed to load skill {skill_dir.name}: {e}")

    return skills


# -- ToolDefinition + Adapters ---------------------------------


@dataclass
class ToolDefinition:
    """
    Define a tool once. Adapters convert to MCP or OpenAI format.

    Parameters dict maps param_name -> spec:
        {"key": {"type": "string", "description": "...", "optional": True}}
    Handler signature: async (args_dict) -> str
    """
    name: str
    description: str
    parameters: Dict[str, dict]
    handler: Callable  # async (dict) -> str


def definitions_to_mcp(defs: List[ToolDefinition], server_name: str):
    """
    Convert ToolDefinitions to an in-process MCP server.
    Returns None if claude_agent_sdk is not installed.
    """
    try:
        from claude_agent_sdk import tool, create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not installed, skipping MCP server")
        return None

    mcp_tools = []
    for d in defs:
        # Strip "optional" from param specs before passing to SDK
        params = {
            k: {kk: vv for kk, vv in v.items() if kk != "optional"}
            for k, v in d.parameters.items()
        }

        @tool(d.name, d.description, params)
        async def _handler(args, _h=d.handler):
            text = await _h(args)
            return {"content": [{"type": "text", "text": text}]}

        mcp_tools.append(_handler)

    return create_sdk_mcp_server(
        name=server_name,
        version="1.0.0",
        tools=mcp_tools,
    )


def _to_openai_json_schema(params: Dict[str, dict]) -> dict:
    """Convert flat param dict to full JSON Schema for OpenAI function tools."""
    properties = {}
    required = []
    for name, spec in params.items():
        prop = {k: v for k, v in spec.items() if k not in ("optional",)}
        properties[name] = prop
        if not spec.get("optional", False):
            required.append(name)

    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def definitions_to_openai(defs: List[ToolDefinition]) -> list:
    """
    Convert ToolDefinitions to OpenAI Agents SDK FunctionTool objects.
    Returns empty list if openai-agents is not installed.
    """
    try:
        from agents import FunctionTool
    except ImportError:
        logger.debug("openai-agents not installed, skipping OpenAI tools")
        return []

    tools = []
    for d in defs:
        schema = _to_openai_json_schema(d.parameters)

        async def _invoke(ctx, json_string, _h=d.handler):
            args = json.loads(json_string)
            return await _h(args)

        tools.append(FunctionTool(
            name=d.name,
            description=d.description,
            params_json_schema=schema,
            on_invoke_tool=_invoke,
        ))

    return tools


# -- Tool Definition Builders ----------------------------------


def create_memory_tool_defs(
    memory: MemoryManager, team_id: str, pi_id: str,
    memory_backend: "Optional[MemoryBackend]" = None,
) -> List[ToolDefinition]:
    """Create ToolDefinitions for memory operations."""

    # Use the provided backend if available, otherwise default to FileBackend.
    # All memory operations go through the backend so that switching backends
    # (e.g. file -> viking) affects the entire memory surface, not just search.
    _backend = memory_backend if memory_backend is not None else FileBackend(memory)
    _search_service = MemorySearchService(_backend)

    async def memory_read(args):
        key = args["key"]
        data = _backend.read(team_id, key)
        return json.dumps(data, ensure_ascii=False, indent=2) if data is not None else "null"

    async def memory_write(args):
        key = args["key"]
        try:
            data = json.loads(args["value"])
        except json.JSONDecodeError:
            data = args["value"]
        _backend.write(team_id, key, data)
        return f"Written to {team_id}/memory/{key}"

    async def memory_list(args):
        keys = _backend.list_keys(team_id)
        return json.dumps(keys)

    async def memory_index(args):
        index = _backend.get_index(team_id)
        return json.dumps(index, indent=2, ensure_ascii=False)

    async def memory_search(args):
        query = args["query"]
        mode = args.get("mode", "auto")
        limit = int(args.get("limit", 20))
        include_archive = args.get("include_archive", True)
        explain = args.get("explain", False)
        # Normalize string booleans from tool calls
        if isinstance(include_archive, str):
            include_archive = include_archive.lower() not in ("false", "0", "no")
        if isinstance(explain, str):
            explain = explain.lower() in ("true", "1", "yes")
        results = _search_service.search(
            team_id,
            query,
            mode=mode,
            limit=limit,
            include_archive=include_archive,
            explain=explain,
        )
        return json.dumps(results, ensure_ascii=False, indent=2)

    return [
        ToolDefinition(
            name="memory_read",
            description="Read a key from team shared memory. Returns JSON content or null.",
            parameters={"key": {"type": "string", "description": "Memory file name (e.g. watchlist.json)"}},
            handler=memory_read,
        ),
        ToolDefinition(
            name="memory_write",
            description="Write a key-value to team shared memory. Value should be valid JSON.",
            parameters={
                "key": {"type": "string", "description": "Memory file name (e.g. watchlist.json)"},
                "value": {"type": "string", "description": "JSON string to write"},
            },
            handler=memory_write,
        ),
        ToolDefinition(
            name="memory_list",
            description="List all keys in team shared memory.",
            parameters={},
            handler=memory_list,
        ),
        ToolDefinition(
            name="memory_index",
            description="List all memory entries with metadata (importance, tier, access_count, summary).",
            parameters={},
            handler=memory_index,
        ),
        ToolDefinition(
            name="memory_search",
            description=(
                "Search team memory and archive by keyword. Returns matching entries "
                "ranked by relevance. Semantic and hybrid requests currently "
                "fall back to exact search unless a semantic backend is available."
            ),
            parameters={
                "query": {
                    "type": "string",
                    "description": "Search term (matches against key names, summary, and tags)",
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Search mode: auto (default), exact, semantic, hybrid. "
                        "Semantic and hybrid currently downgrade to exact "
                        "unless the configured backend really supports them."
                    ),
                    "default": "auto",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 20)",
                    "default": 20,
                },
                "include_archive": {
                    "type": "boolean",
                    "description": "Include archived entries in search (default true)",
                    "default": True,
                },
                "explain": {
                    "type": "boolean",
                    "description": "Include scoring details (_score, _mode) in results (default false)",
                    "default": False,
                },
            },
            handler=memory_search,
        ),
    ]


def create_scheduler_tool_defs(
    user_scheduler, user_id: str, chat_id: str, channel: str,
) -> List[ToolDefinition]:
    """Create ToolDefinitions for scheduling operations."""

    async def schedule_task(args):
        try:
            trigger_args = json.loads(args["trigger_args"])
        except (json.JSONDecodeError, TypeError):
            return "Error: trigger_args must be valid JSON"
        try:
            job = await user_scheduler.add_job(
                user_id=user_id,
                chat_id=chat_id,
                channel=channel,
                task=args["task"],
                description=args.get("description", ""),
                trigger_type=args["trigger_type"],
                trigger_args=trigger_args,
                team_id=args.get("team_id"),
            )
            return (
                f"Scheduled! Job ID: {job.id}\n"
                f"Type: {job.trigger_type}\n"
                f"Task: {job.task[:100]}\n"
                f"Trigger: {json.dumps(job.trigger_args)}"
            )
        except Exception as e:
            return f"Scheduling failed: {e}"

    async def list_schedules(args):
        jobs = await user_scheduler.list_jobs(user_id=user_id)
        if not jobs:
            return "No active scheduled tasks."
        lines = []
        for j in jobs:
            lines.append(
                f"- [{j.id}] {j.description or j.task[:60]} "
                f"({j.trigger_type}: {json.dumps(j.trigger_args)}) "
                f"fires={j.fire_count}"
            )
        return "\n".join(lines)

    async def cancel_schedule(args):
        ok = await user_scheduler.cancel_job(args["schedule_id"], user_id=user_id)
        if ok:
            return f"Cancelled schedule {args['schedule_id']}"
        return f"Schedule not found: {args['schedule_id']}"

    return [
        ToolDefinition(
            name="schedule_task",
            description=(
                "Schedule a task to run later or on a recurring basis. "
                "Use trigger_type 'cron' for recurring (e.g. daily at 9am), "
                "'interval' for periodic (e.g. every 30 minutes), "
                "or 'date' for one-shot (e.g. in 3 hours)."
            ),
            parameters={
                "task": {"type": "string", "description": "The task/prompt to execute when triggered"},
                "description": {"type": "string", "description": "Human-readable description of the schedule"},
                "trigger_type": {"type": "string", "description": "Trigger type: 'cron', 'interval', or 'date'"},
                "trigger_args": {
                    "type": "string",
                    "description": (
                        "JSON string of trigger arguments. "
                        'cron: {"hour": 9, "minute": 0} or {"day_of_week": "mon-fri", "hour": 9}. '
                        'interval: {"minutes": 30} or {"hours": 2}. '
                        'date: {"run_date": "2026-02-16T09:00:00"}'
                    ),
                },
                "team_id": {
                    "type": "string",
                    "description": "Optional: specific team to route the task to (omit for auto-routing)",
                    "optional": True,
                },
            },
            handler=schedule_task,
        ),
        ToolDefinition(
            name="list_schedules",
            description="List all active scheduled tasks for the current user.",
            parameters={},
            handler=list_schedules,
        ),
        ToolDefinition(
            name="cancel_schedule",
            description="Cancel a scheduled task by its ID (supports partial ID match).",
            parameters={
                "schedule_id": {"type": "string", "description": "Schedule ID (or partial ID) to cancel"},
            },
            handler=cancel_schedule,
        ),
    ]


def create_feed_tool_defs() -> List[ToolDefinition]:
    """Create ToolDefinitions for data feed operations."""
    from . import feeds

    async def brave_search(args):
        items = await feeds.brave_search(
            query=args["query"],
            count=int(args.get("count", 10)),
            freshness=args.get("freshness", ""),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def fetch_rss(args):
        items = await feeds.fetch_rss(
            url=args["url"],
            limit=int(args.get("limit", 15)),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def fetch_reddit(args):
        items = await feeds.fetch_reddit(
            subreddit=args["subreddit"],
            sort=args.get("sort", "hot"),
            limit=int(args.get("limit", 10)),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def fetch_hn(args):
        items = await feeds.fetch_hn(
            story_type=args.get("story_type", "top"),
            limit=int(args.get("limit", 15)),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def fetch_x(args):
        items = await feeds.fetch_x(
            username=args["username"],
            limit=int(args.get("limit", 10)),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def fetch_openbb(args):
        items = await feeds.fetch_openbb(
            symbols=args.get("symbols", ""),
            data_type=args.get("data_type", "quote"),
            provider=args.get("provider", "yfinance"),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    return [
        ToolDefinition(
            name="brave_search",
            description="Search the web via Brave Search. Returns news, articles, discussions.",
            parameters={
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "number", "description": "Number of results (default 10, max 20)", "optional": True},
                "freshness": {
                    "type": "string",
                    "description": "Time filter: '' (any), 'pd' (past day), 'pw' (past week), 'pm' (past month)",
                    "optional": True,
                },
            },
            handler=brave_search,
        ),
        ToolDefinition(
            name="fetch_rss",
            description="Fetch and parse an RSS or Atom feed URL. Returns recent entries.",
            parameters={
                "url": {"type": "string", "description": "Feed URL (RSS 2.0 or Atom)"},
                "limit": {"type": "number", "description": "Max items (default 15)", "optional": True},
            },
            handler=fetch_rss,
        ),
        ToolDefinition(
            name="fetch_reddit",
            description="Fetch posts from a subreddit. No authentication needed.",
            parameters={
                "subreddit": {"type": "string", "description": "Subreddit name (without r/ prefix)"},
                "sort": {"type": "string", "description": "'hot', 'new', 'top', or 'rising' (default: hot)", "optional": True},
                "limit": {"type": "number", "description": "Number of posts (default 10, max 25)", "optional": True},
            },
            handler=fetch_reddit,
        ),
        ToolDefinition(
            name="fetch_hn",
            description="Fetch stories from Hacker News.",
            parameters={
                "story_type": {"type": "string", "description": "'top', 'new', 'best', 'ask', or 'show' (default: top)", "optional": True},
                "limit": {"type": "number", "description": "Number of stories (default 15, max 30)", "optional": True},
            },
            handler=fetch_hn,
        ),
        ToolDefinition(
            name="fetch_x",
            description="Fetch tweets from a specific X/Twitter user. Requires X_BEARER_TOKEN env var.",
            parameters={
                "username": {"type": "string", "description": "X handle (without @)"},
                "limit": {"type": "number", "description": "Number of tweets (default 10)", "optional": True},
            },
            handler=fetch_x,
        ),
        ToolDefinition(
            name="fetch_openbb",
            description=(
                "Fetch financial data from OpenBB API server. "
                "Supports equity quotes, historical prices, financial news, economy indicators, and crypto."
            ),
            parameters={
                "symbols": {
                    "type": "string",
                    "description": "Comma-separated ticker symbols (e.g. 'AAPL,MSFT,NVDA'). Not needed for news.",
                },
                "data_type": {
                    "type": "string",
                    "description": (
                        "Type of data: 'quote' (latest price), 'historical' (OHLCV), "
                        "'news' (financial news), 'economy' (macro indicators), 'crypto' (crypto prices). "
                        "Default: 'quote'"
                    ),
                    "optional": True,
                },
                "provider": {
                    "type": "string",
                    "description": "Data provider backend (default 'yfinance'). Others: 'fmp', 'polygon', 'intrinio'",
                    "optional": True,
                },
            },
            handler=fetch_openbb,
        ),
    ]


def create_sessions_tool_defs(
    memory: MemoryManager, team_id: str, pi_id: str, team_registry=None,
) -> List[ToolDefinition]:
    """Create ToolDefinitions for agent-to-agent messaging."""
    import datetime

    async def sessions_send(args):
        target = args["target_team"]
        msg = args["message"]
        entry = {
            "from_team": team_id,
            "from_pi": pi_id,
            "message": msg,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        memory.append_team_memory(target, "inbox.json", entry)
        return f"Message sent to {target}/inbox.json"

    async def sessions_list(args):
        if team_registry and hasattr(team_registry, "list_teams"):
            teams = [t for t in team_registry.list_teams()
                     if t not in (team_registry.list_meta_teams() if hasattr(team_registry, "list_meta_teams") else [])]
        else:
            teams = memory.list_teams()
        return json.dumps(teams)

    async def sessions_history(args):
        target = args.get("team_id", team_id)
        limit = int(args.get("limit", 10))
        inbox = memory.read_team_memory(target, "inbox.json")
        if inbox is None:
            inbox = []
        messages = inbox[-limit:] if isinstance(inbox, list) else []
        return json.dumps(messages, ensure_ascii=False, indent=2)

    return [
        ToolDefinition(
            name="sessions_send",
            description="Send a message to another team's inbox. Used for cross-team collaboration.",
            parameters={
                "target_team": {"type": "string", "description": "Team ID to send the message to"},
                "message": {"type": "string", "description": "The message content"},
            },
            handler=sessions_send,
        ),
        ToolDefinition(
            name="sessions_list",
            description="List all teams available for messaging.",
            parameters={},
            handler=sessions_list,
        ),
        ToolDefinition(
            name="sessions_history",
            description="Read recent messages from a team's inbox.",
            parameters={
                "team_id": {"type": "string", "description": "Team ID to read inbox from (defaults to own team)", "optional": True},
                "limit": {"type": "number", "description": "Max messages to return (default 10)", "optional": True},
            },
            handler=sessions_history,
        ),
    ]


def create_git_tool_defs(ctx: "ToolContext") -> List[ToolDefinition]:
    """Create ToolDefinitions for git operations.

    Requires ctx.extra to contain:
    - workspace_path: str -absolute path to the git repo root
    """
    from . import git_tools

    workspace_path = ctx.extra.get("workspace_path", "")

    async def _git_status(args):
        return await git_tools.git_status(workspace_path)

    async def _git_diff(args):
        return await git_tools.git_diff(
            workspace_path,
            file_path=args.get("file_path", ""),
            staged=args.get("staged", "false").lower() in ("true", "1", "yes"),
        )

    async def _git_log(args):
        return await git_tools.git_log(
            workspace_path,
            count=int(args.get("count", 10)),
        )

    async def _git_branch(args):
        return await git_tools.git_branch(
            workspace_path,
            name=args.get("name", ""),
            checkout=args.get("checkout", "false").lower() in ("true", "1", "yes"),
        )

    async def _git_commit(args):
        return await git_tools.git_commit(
            workspace_path,
            message=args.get("message", ""),
            files=args.get("files", ""),
        )

    async def _git_push(args):
        return await git_tools.git_push(
            workspace_path,
            remote=args.get("remote", "origin"),
            branch=args.get("branch", ""),
        )

    return [
        ToolDefinition(
            name="git_status",
            description="Show git working tree status: current branch, staged, modified, and untracked files.",
            parameters={},
            handler=_git_status,
        ),
        ToolDefinition(
            name="git_diff",
            description="Show file changes in the working tree or staging area.",
            parameters={
                "file_path": {
                    "type": "string",
                    "description": "Specific file to diff (relative to repo root). Empty = all files.",
                    "optional": True,
                },
                "staged": {
                    "type": "string",
                    "description": "'true' to show staged changes (--cached). Default: 'false'.",
                    "optional": True,
                },
            },
            handler=_git_diff,
        ),
        ToolDefinition(
            name="git_log",
            description="Show recent commit history with graph.",
            parameters={
                "count": {
                    "type": "number",
                    "description": "Number of commits to show (default 10, max 50).",
                    "optional": True,
                },
            },
            handler=_git_log,
        ),
        ToolDefinition(
            name="git_branch",
            description="List, create, or switch git branches.",
            parameters={
                "name": {
                    "type": "string",
                    "description": "Branch name. Empty = list all branches.",
                    "optional": True,
                },
                "checkout": {
                    "type": "string",
                    "description": "'true' to switch to (or create) the branch. Default: 'false'.",
                    "optional": True,
                },
            },
            handler=_git_branch,
        ),
        ToolDefinition(
            name="git_commit",
            description="Stage files and create a git commit.",
            parameters={
                "message": {
                    "type": "string",
                    "description": "Commit message (required).",
                },
                "files": {
                    "type": "string",
                    "description": "Comma-separated file paths to stage. Empty = stage all modified files.",
                    "optional": True,
                },
            },
            handler=_git_commit,
        ),
        ToolDefinition(
            name="git_push",
            description="Push commits to remote repository. Use with caution.",
            parameters={
                "remote": {
                    "type": "string",
                    "description": "Remote name (default: 'origin').",
                    "optional": True,
                },
                "branch": {
                    "type": "string",
                    "description": "Branch to push. Empty = current branch.",
                    "optional": True,
                },
            },
            handler=_git_push,
        ),
    ]


def create_orchestrator_tool_defs(ctx: "ToolContext") -> List[ToolDefinition]:
    """Create ToolDefinitions for CEO Agent orchestrator tools (run_team, run_auto).

    Requires ctx.extra to contain:
    - run_team_fn: async (task, team_id, mode, user_context) -> str
    - run_auto_fn: async (task, mode, user_context) -> (str, RoutingDecision)
    """
    run_team_fn = ctx.extra.get("run_team_fn")
    run_auto_fn = ctx.extra.get("run_auto_fn")
    company_id = ctx.extra.get("company_id")

    async def run_team(args):
        if not run_team_fn:
            return "Error: run_team not available in this context"
        team_id = args["team_id"]
        task = args["task"]
        mode = args.get("mode", "cascade")
        uc = {"company_id": company_id} if company_id else {}
        try:
            result = await run_team_fn(
                task=task, team_id=team_id, mode=mode, user_context=uc,
            )
            return result
        except Exception as e:
            return f"Error running team {team_id}: {e}"

    async def run_auto(args):
        if not run_auto_fn:
            return "Error: run_auto not available in this context"
        task = args["task"]
        mode = args.get("mode")
        uc = {"company_id": company_id} if company_id else {}
        try:
            result, decision = await run_auto_fn(
                task=task, mode=mode, user_context=uc,
            )
            return (
                f"Routed to: {[t.team_id for t in decision.teams]} "
                f"(confidence={decision.confidence:.0%})\n\n{result}"
            )
        except Exception as e:
            return f"Error in auto-routing: {e}"

    return [
        ToolDefinition(
            name="run_team",
            description=(
                "Run a task via a specific team. Use when you know which team to call. "
                "Returns the team's result as text."
            ),
            parameters={
                "team_id": {"type": "string", "description": "Target team ID (e.g. 'stock', 'engineering')"},
                "task": {"type": "string", "description": "The task/prompt to send to the team"},
                "mode": {
                    "type": "string",
                    "description": "Execution mode: cascade (default), default, loop, council",
                    "optional": True,
                },
            },
            handler=run_team,
        ),
        ToolDefinition(
            name="run_auto",
            description=(
                "Auto-route a task to the best team(s) using SmartRouter. "
                "Use when you're not sure which team to call."
            ),
            parameters={
                "task": {"type": "string", "description": "The task/prompt to route and execute"},
                "mode": {
                    "type": "string",
                    "description": "Execution mode override (optional, router decides if omitted)",
                    "optional": True,
                },
            },
            handler=run_auto,
        ),
    ]


# -- Backward-compat wrappers (used by pi.py fallback paths) --


def create_memory_mcp_server(memory, team_id, pi_id):
    return definitions_to_mcp(create_memory_tool_defs(memory, team_id, pi_id), "mem")

def create_memory_openai_tools(memory, team_id, pi_id):
    return definitions_to_openai(create_memory_tool_defs(memory, team_id, pi_id))

def create_scheduler_mcp_server(user_scheduler, user_id, chat_id, channel):
    return definitions_to_mcp(create_scheduler_tool_defs(user_scheduler, user_id, chat_id, channel), "sched")

def create_scheduler_openai_tools(user_scheduler, user_id, chat_id, channel):
    return definitions_to_openai(create_scheduler_tool_defs(user_scheduler, user_id, chat_id, channel))

def create_feed_mcp_server():
    return definitions_to_mcp(create_feed_tool_defs(), "feed")

def create_feed_openai_tools():
    return definitions_to_openai(create_feed_tool_defs())

def create_sessions_mcp_server(memory, team_id, pi_id, team_registry=None):
    return definitions_to_mcp(create_sessions_tool_defs(memory, team_id, pi_id, team_registry), "sessions")

def create_sessions_openai_tools(memory, team_id, pi_id, team_registry=None):
    return definitions_to_openai(create_sessions_tool_defs(memory, team_id, pi_id, team_registry))


# -- Tool Registry --------------------------------------------


@dataclass
class ToolContext:
    """Execution context passed to tool factories."""
    memory: MemoryManager
    team_id: str
    pi_id: str
    tools_config: List[str]
    user_context: Optional[Dict[str, Any]] = None
    user_scheduler: Any = None
    team_registry: Any = None
    tools_deny: Set[str] = field(default_factory=set)
    extra: Dict[str, Any] = field(default_factory=dict)
    memory_backend: Optional[MemoryBackend] = None


@dataclass
class ToolProvider:
    """A registered tool source."""
    name: str
    tool_names: Set[str]
    mcp_factory: Optional[Callable[[ToolContext], Any]] = None
    openai_factory: Optional[Callable[[ToolContext], list]] = None
    requires: Optional[Callable[[ToolContext], bool]] = None


class ToolRegistry:
    """
    Pluggable tool registry.

    Manages built-in and custom tool providers. External projects
    register providers to inject custom tools into Pi agents.
    """

    def __init__(self):
        self._providers: Dict[str, ToolProvider] = {}
        self._custom_presets: Dict[str, List[str]] = {}
        self._skills: Dict[str, SkillDefinition] = {}
        self._external_mcp_configs: Dict[str, dict] = {}  # name -> sdk config
        self.memory_backend: Optional[MemoryBackend] = None  # set by orchestrator
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register the four built-in tool providers."""
        self.register(ToolProvider(
            name="memory",
            tool_names=CUSTOM_TOOL_NAMES,
            mcp_factory=self._memory_mcp_factory,
            openai_factory=self._memory_openai_factory,
        ))
        self.register(ToolProvider(
            name="scheduler",
            tool_names=SCHEDULER_TOOL_NAMES,
            mcp_factory=self._scheduler_mcp_factory,
            openai_factory=self._scheduler_openai_factory,
            requires=self._scheduler_requires,
        ))
        self.register(ToolProvider(
            name="feed",
            tool_names=FEED_TOOL_NAMES,
            mcp_factory=self._feed_mcp_factory,
            openai_factory=self._feed_openai_factory,
            requires=self._feed_requires,
        ))
        self.register(ToolProvider(
            name="sessions",
            tool_names=SESSIONS_TOOL_NAMES,
            mcp_factory=self._sessions_mcp_factory,
            openai_factory=self._sessions_openai_factory,
            requires=self._sessions_requires,
        ))
        self.register(ToolProvider(
            name="orchestrator",
            tool_names=ORCHESTRATOR_TOOL_NAMES,
            mcp_factory=self._orchestrator_mcp_factory,
            openai_factory=self._orchestrator_openai_factory,
            requires=self._orchestrator_requires,
        ))
        self.register(ToolProvider(
            name="git",
            tool_names=GIT_TOOL_NAMES,
            mcp_factory=self._git_mcp_factory,
            openai_factory=self._git_openai_factory,
            requires=self._git_requires,
        ))

    def register(self, provider: ToolProvider) -> None:
        """Add or replace a tool provider."""
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        """Remove a tool provider."""
        self._providers.pop(name, None)

    def register_preset(self, name: str, tool_names: List[str]) -> None:
        """Add a custom tool preset."""
        self._custom_presets[name] = tool_names

    def register_skill(self, skill: SkillDefinition) -> None:
        """Register a skill and add it as a custom preset."""
        self._skills[skill.name] = skill
        if skill.tools:
            self._custom_presets[skill.name] = skill.tools

    def register_external_mcp(self, name: str, sdk_config: dict) -> None:
        """Register an external MCP server config (shared across all teams)."""
        self._external_mcp_configs[name] = sdk_config

    def get_external_mcp_servers(self) -> Dict[str, dict]:
        """Return all registered external MCP server configs."""
        return dict(self._external_mcp_configs)

    def get_skill_instructions(self, tools_config: List[str]) -> str:
        """Return combined instructions for all active skills.

        In default-all mode (empty tools_config), all skills are active.
        Otherwise, only skills whose name or tools appear in tools_config.
        """
        parts = []
        for skill in self._skills.values():
            if not skill.instructions:
                continue
            if not tools_config:
                parts.append(f"# Skill: {skill.name}\n{skill.instructions}")
            elif skill.name in tools_config or any(t in tools_config for t in skill.tools):
                parts.append(f"# Skill: {skill.name}\n{skill.instructions}")
        return "\n\n".join(parts)

    def resolve_all_tools(self, tools_deny: Optional[Set[str]] = None) -> List[str]:
        """Resolve ALL known tools minus deny set (for default-all mode)."""
        all_tools: List[str] = []

        # Built-in SDK tools
        all_tools.extend(sorted(CLAUDE_BUILTIN_TOOLS))

        # Custom tool names (with MCP prefix)
        for name in sorted(CUSTOM_TOOL_NAMES):
            all_tools.append(f"mcp__mem__{name}")
        for name in sorted(SCHEDULER_TOOL_NAMES):
            all_tools.append(f"mcp__sched__{name}")
        for name in sorted(FEED_TOOL_NAMES):
            all_tools.append(f"mcp__feed__{name}")
        for name in sorted(SESSIONS_TOOL_NAMES):
            all_tools.append(f"mcp__sessions__{name}")

        # Tools from custom providers (skip orchestrator/git -require explicit opt-in)
        for provider in self._providers.values():
            if provider.name in ("memory", "scheduler", "feed", "sessions", "orchestrator", "git"):
                continue
            for tool_name in provider.tool_names:
                all_tools.append(f"mcp__{provider.name}__{tool_name}")

        # Dedupe
        all_tools = list(dict.fromkeys(all_tools))

        # Apply deny filter
        if tools_deny:
            all_tools = [t for t in all_tools if t not in tools_deny]

        return all_tools

    def get_mcp_servers(self, ctx: ToolContext) -> Dict[str, Any]:
        """Get MCP servers from all active providers."""
        servers = {}
        for provider in self._providers.values():
            if not provider.mcp_factory:
                continue
            if not self._is_active(provider, ctx):
                continue
            server = provider.mcp_factory(ctx)
            if server:
                servers[provider.name] = server
        return servers

    def get_openai_tools(self, ctx: ToolContext) -> list:
        """Get OpenAI function tools from all active providers."""
        tools = []
        for provider in self._providers.values():
            if not provider.openai_factory:
                continue
            if not self._is_active(provider, ctx):
                continue
            provider_tools = provider.openai_factory(ctx)
            if provider_tools:
                tools.extend(provider_tools)
        return tools

    def resolve_tool_names(
        self, tools_config: List[str], tools_deny: Optional[Set[str]] = None,
    ) -> List[str]:
        """Resolve tool names, including custom provider prefixes."""
        # Default-all mode: empty tools_config -all tools
        if not tools_config:
            return self.resolve_all_tools(tools_deny=tools_deny)

        # Resolve custom presets (skills) alongside built-in presets
        expanded = []
        for name in tools_config:
            if name in self._custom_presets:
                expanded.extend(self._custom_presets[name])
            else:
                expanded.append(name)

        resolved = resolve_tool_names(expanded)

        # Add mcp__{provider}__{tool} prefixes for custom (non-builtin) providers
        for provider in self._providers.values():
            if provider.name in ("memory", "scheduler", "feed", "sessions"):
                continue
            for tool_name in provider.tool_names:
                if tool_name in expanded or any(
                    p in expanded for p in self._custom_presets
                    if tool_name in self._custom_presets.get(p, [])
                ):
                    resolved.append(f"mcp__{provider.name}__{tool_name}")

        resolved = list(dict.fromkeys(resolved))

        # Apply deny filter
        if tools_deny:
            resolved = [t for t in resolved if t not in tools_deny]

        return resolved

    def list_providers(self) -> List[str]:
        """List all registered provider names."""
        return list(self._providers.keys())

    def _is_active(self, provider: ToolProvider, ctx: ToolContext) -> bool:
        """Check if a provider should be activated for this context."""
        # Default-all mode: empty tools_config -all providers active
        if not ctx.tools_config:
            if provider.requires is not None:
                # In default-all mode, only check prerequisites (not tools_config membership)
                return provider.requires(ctx, default_all=True)
            return True
        if provider.requires is not None:
            return provider.requires(ctx)
        # Default: active if any of its tool names appear in tools_config
        resolved_names = set()
        for name in ctx.tools_config:
            if name in TOOL_PRESETS:
                resolved_names.update(TOOL_PRESETS[name])
            elif name in self._custom_presets:
                resolved_names.update(self._custom_presets[name])
            else:
                resolved_names.add(name)
        return bool(provider.tool_names & resolved_names)

    # -- Built-in provider factories (thin wrappers) -----------

    @staticmethod
    def _memory_mcp_factory(ctx: ToolContext) -> Any:
        defs = create_memory_tool_defs(ctx.memory, ctx.team_id, ctx.pi_id, memory_backend=ctx.memory_backend)
        return definitions_to_mcp(defs, "mem")

    @staticmethod
    def _memory_openai_factory(ctx: ToolContext) -> list:
        defs = create_memory_tool_defs(ctx.memory, ctx.team_id, ctx.pi_id, memory_backend=ctx.memory_backend)
        return definitions_to_openai(defs)

    @staticmethod
    def _scheduler_mcp_factory(ctx: ToolContext) -> Any:
        uc = ctx.user_context
        if not uc or not ctx.user_scheduler:
            return None
        defs = create_scheduler_tool_defs(
            ctx.user_scheduler,
            user_id=uc.get("user_id", ""),
            chat_id=uc.get("chat_id", ""),
            channel=uc.get("channel", "telegram"),
        )
        return definitions_to_mcp(defs, "sched")

    @staticmethod
    def _scheduler_openai_factory(ctx: ToolContext) -> list:
        uc = ctx.user_context
        if not uc or not ctx.user_scheduler:
            return []
        defs = create_scheduler_tool_defs(
            ctx.user_scheduler,
            user_id=uc.get("user_id", ""),
            chat_id=uc.get("chat_id", ""),
            channel=uc.get("channel", "telegram"),
        )
        return definitions_to_openai(defs)

    @staticmethod
    def _scheduler_requires(ctx: ToolContext, default_all: bool = False) -> bool:
        if not ctx.user_context or not ctx.user_scheduler:
            return False
        if default_all:
            return True
        return any(
            t in SCHEDULER_TOOL_NAMES or t == "scheduler"
            for t in ctx.tools_config
        )

    @staticmethod
    def _feed_requires(ctx: ToolContext, default_all: bool = False) -> bool:
        if default_all:
            return True
        return any(
            t in FEED_TOOL_NAMES or t == "collector"
            for t in ctx.tools_config
        )

    @staticmethod
    def _feed_mcp_factory(ctx: ToolContext) -> Any:
        return definitions_to_mcp(create_feed_tool_defs(), "feed")

    @staticmethod
    def _feed_openai_factory(ctx: ToolContext) -> list:
        return definitions_to_openai(create_feed_tool_defs())

    @staticmethod
    def _sessions_requires(ctx: ToolContext, default_all: bool = False) -> bool:
        if default_all:
            return True
        return any(
            t in SESSIONS_TOOL_NAMES or t == "messenger"
            for t in ctx.tools_config
        )

    @staticmethod
    def _sessions_mcp_factory(ctx: ToolContext) -> Any:
        defs = create_sessions_tool_defs(
            ctx.memory, ctx.team_id, ctx.pi_id,
            team_registry=ctx.team_registry,
        )
        return definitions_to_mcp(defs, "sessions")

    @staticmethod
    def _sessions_openai_factory(ctx: ToolContext) -> list:
        defs = create_sessions_tool_defs(
            ctx.memory, ctx.team_id, ctx.pi_id,
            team_registry=ctx.team_registry,
        )
        return definitions_to_openai(defs)

    @staticmethod
    def _orchestrator_requires(ctx: ToolContext, default_all: bool = False) -> bool:
        """Orchestrator tools are active only when run_team_fn is in extra."""
        if not ctx.extra.get("run_team_fn"):
            return False
        if default_all:
            return True
        return any(
            t in ORCHESTRATOR_TOOL_NAMES or t == "ceo"
            for t in ctx.tools_config
        )

    @staticmethod
    def _orchestrator_mcp_factory(ctx: ToolContext) -> Any:
        defs = create_orchestrator_tool_defs(ctx)
        return definitions_to_mcp(defs, "orchestrator")

    @staticmethod
    def _orchestrator_openai_factory(ctx: ToolContext) -> list:
        defs = create_orchestrator_tool_defs(ctx)
        return definitions_to_openai(defs)

    @staticmethod
    def _git_requires(ctx: ToolContext, default_all: bool = False) -> bool:
        """Git tools are active only when workspace_path is in extra."""
        if not ctx.extra.get("workspace_path"):
            return False
        if default_all:
            return True
        return any(
            t in GIT_TOOL_NAMES or t in ("coder", "code-reviewer")
            for t in ctx.tools_config
        )

    @staticmethod
    def _git_mcp_factory(ctx: ToolContext) -> Any:
        defs = create_git_tool_defs(ctx)
        return definitions_to_mcp(defs, "git")

    @staticmethod
    def _git_openai_factory(ctx: ToolContext) -> list:
        defs = create_git_tool_defs(ctx)
        return definitions_to_openai(defs)


# -- Tool name resolution -------------------------------------


def resolve_tool_names(tools_config: List[str], _seen: Optional[set] = None) -> List[str]:
    """
    Resolve tool config strings to actual tool names.
    Handles both individual names and preset references.

    Example:
        resolve_tool_names(["web_search", "memory_read"])
        -["WebSearch", "mcp__mem__memory_read"]
    """
    if _seen is None:
        _seen = set()
    resolved = []
    for name in tools_config:
        # Check presets first (with cycle detection)
        if name in TOOL_PRESETS:
            if name in _seen:
                continue
            _seen.add(name)
            resolved.extend(resolve_tool_names(TOOL_PRESETS[name], _seen))
            continue

        # Normalize common aliases
        normalized = _normalize_tool_name(name)
        if normalized in CLAUDE_BUILTIN_TOOLS:
            resolved.append(normalized)
        elif normalized in CUSTOM_TOOL_NAMES:
            resolved.append(f"mcp__mem__{normalized}")
        elif normalized in SCHEDULER_TOOL_NAMES:
            resolved.append(f"mcp__sched__{normalized}")
        elif normalized in FEED_TOOL_NAMES:
            resolved.append(f"mcp__feed__{normalized}")
        elif normalized in SESSIONS_TOOL_NAMES:
            resolved.append(f"mcp__sessions__{normalized}")
        elif normalized in ORCHESTRATOR_TOOL_NAMES:
            resolved.append(f"mcp__orchestrator__{normalized}")
        elif normalized in GIT_TOOL_NAMES:
            resolved.append(f"mcp__git__{normalized}")
        else:
            # Pass through as-is (user might know what they're doing)
            resolved.append(name)

    return list(dict.fromkeys(resolved))  # dedupe preserving order


def _normalize_tool_name(name: str) -> str:
    """Normalize tool name aliases to canonical form."""
    aliases = {
        "web_search": "WebSearch",
        "websearch": "WebSearch",
        "web_fetch": "WebFetch",
        "webfetch": "WebFetch",
        "read": "Read",
        "write": "Write",
        "edit": "Edit",
        "bash": "Bash",
        "exec": "Bash",
        "glob": "Glob",
        "grep": "Grep",
        "memory_read": "memory_read",
        "memory_write": "memory_write",
        "memory_list": "memory_list",
        "memory_index": "memory_index",
        "memory_search": "memory_search",
        "schedule_task": "schedule_task",
        "list_schedules": "list_schedules",
        "cancel_schedule": "cancel_schedule",
        "brave_search": "brave_search",
        "fetch_rss": "fetch_rss",
        "fetch_reddit": "fetch_reddit",
        "fetch_hn": "fetch_hn",
        "fetch_x": "fetch_x",
        "fetch_openbb": "fetch_openbb",
        "openbb": "fetch_openbb",
        "sessions_send": "sessions_send",
        "sessions_list": "sessions_list",
        "sessions_history": "sessions_history",
        "run_team": "run_team",
        "run_auto": "run_auto",
        "git_status": "git_status",
        "git_diff": "git_diff",
        "git_log": "git_log",
        "git_branch": "git_branch",
        "git_commit": "git_commit",
        "git_push": "git_push",
    }
    return aliases.get(name.lower(), name)
