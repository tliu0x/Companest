import inspect
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .memory import MemoryManager, MemoryBackend, FileBackend, MemorySearchService

logger = logging.getLogger(__name__)


TOOL_PRESETS = {
    "pi-core": ["Read", "Write", "Edit", "Glob", "Grep"],
    "pi-safe": ["Read", "Glob", "Grep", "memory_read"],
    "researcher": ["WebSearch", "WebFetch", "memory_read", "memory_write", "search_public_knowledge", "read_public_knowledge"],
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
        "search_public_knowledge", "read_public_knowledge",
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

# Blocked unless a Pi opts out with tools_deny: ["none"]
DEFAULT_TOOLS_DENY = {"Bash"}

CLAUDE_BUILTIN_TOOLS = {
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebSearch", "WebFetch",
}
CUSTOM_TOOL_NAMES = {"memory_read", "memory_write", "memory_list", "memory_index", "memory_search"}
PUBLIC_KNOWLEDGE_TOOL_NAMES = {"search_public_knowledge", "read_public_knowledge"}
SCHEDULER_TOOL_NAMES = {"schedule_task", "list_schedules", "cancel_schedule"}
FEED_TOOL_NAMES = {
    "brave_search", "fetch_rss", "fetch_reddit", "fetch_hn", "fetch_x", "fetch_openbb",
    "fetch_polymarket", "fetch_kalshi", "fetch_metaculus",
}
SESSIONS_TOOL_NAMES = {"sessions_send", "sessions_list", "sessions_history"}
ORCHESTRATOR_TOOL_NAMES = {"run_team", "run_auto"}
GIT_TOOL_NAMES = {"git_status", "git_diff", "git_log", "git_branch", "git_commit", "git_push"}


@dataclass
class SkillDefinition:
    """A global skill loaded from .companest/skills/{name}/skill.md."""
    name: str
    tools: List[str]
    description: str
    instructions: str   # injected into Pi system prompt when active


def load_skills(base_path: str) -> Dict[str, SkillDefinition]:
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
            tools_match = re.search(r"^-\s*tools\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            tools = [t.strip() for t in tools_match.group(1).split(",")] if tools_match else []

            desc_match = re.search(r"^-\s*description\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
            description = desc_match.group(1).strip() if desc_match else ""

            instr_match = re.search(r"##\s+Instructions\s*\n(.*)", text, re.DOTALL | re.IGNORECASE)
            instructions = instr_match.group(1).strip() if instr_match else ""

            skills[name] = SkillDefinition(
                name=name, tools=tools, description=description, instructions=instructions,
            )
            logger.info(f"Loaded skill '{name}': {len(tools)} tools")
        except Exception as e:
            logger.error(f"Failed to load skill {skill_dir.name}: {e}")

    return skills


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, dict]
    handler: Callable  # async (dict) -> str


def _to_json_schema(params: Dict[str, dict]) -> dict:
    properties = {}
    required = []
    for name, spec in params.items():
        properties[name] = {k: v for k, v in spec.items() if k != "optional"}
        if not spec.get("optional", False):
            required.append(name)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def definitions_to_langchain(defs: List[ToolDefinition]) -> list:
    from langchain_core.tools import StructuredTool

    tools = []
    for d in defs:
        async def _invoke(_h=d.handler, **kwargs):
            return await _h(kwargs)

        tools.append(StructuredTool.from_function(
            coroutine=_invoke,
            name=d.name,
            description=d.description,
            args_schema=_to_json_schema(d.parameters),
        ))
    return tools


def create_memory_tool_defs(
    memory: MemoryManager, team_id: str, pi_id: str,
    memory_backend: "Optional[MemoryBackend]" = None,
) -> List[ToolDefinition]:
    _backend = memory_backend if memory_backend is not None else FileBackend(memory)
    _search_service = MemorySearchService(_backend)

    async def memory_read(args):
        data = _backend.read(team_id, args["key"])
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
        return json.dumps(_backend.list_keys(team_id))

    async def memory_index(args):
        return json.dumps(_backend.get_index(team_id), indent=2, ensure_ascii=False)

    async def memory_search(args):
        include_archive = args.get("include_archive", True)
        explain = args.get("explain", False)
        # Models sometimes send booleans as strings
        if isinstance(include_archive, str):
            include_archive = include_archive.lower() not in ("false", "0", "no")
        if isinstance(explain, str):
            explain = explain.lower() in ("true", "1", "yes")
        results = _search_service.search(
            team_id,
            args["query"],
            mode=args.get("mode", "auto"),
            limit=int(args.get("limit", 20)),
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
        return "\n".join(
            f"- [{j.id}] {j.description or j.task[:60]} "
            f"({j.trigger_type}: {json.dumps(j.trigger_args)}) "
            f"fires={j.fire_count}"
            for j in jobs
        )

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
    from .knowledge import feeds

    def feed_handler(fetch_name: str):
        # The fetcher is resolved at call time; only arguments it declares are passed,
        # coerced to the type of its default.
        async def handler(args):
            fetch = getattr(feeds, fetch_name)
            kwargs = {}
            for name, param in inspect.signature(fetch).parameters.items():
                if name not in args:
                    continue
                value = args[name]
                if isinstance(param.default, int) and not isinstance(param.default, bool):
                    value = int(value)
                kwargs[name] = value
            return json.dumps(await fetch(**kwargs), ensure_ascii=False, indent=2)
        return handler

    brave_search, fetch_rss, fetch_reddit, fetch_hn, fetch_x, fetch_openbb, fetch_polymarket, fetch_kalshi, fetch_metaculus = (
        feed_handler(name) for name in (
            "brave_search", "fetch_rss", "fetch_reddit", "fetch_hn", "fetch_x",
            "fetch_openbb", "fetch_polymarket", "fetch_kalshi", "fetch_metaculus",
        )
    )

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
        ToolDefinition(
            name="fetch_polymarket",
            description="Fetch prediction markets from Polymarket. Returns market questions, probabilities, and volumes.",
            parameters={
                "query": {"type": "string", "description": "Search query to filter markets", "optional": True},
                "limit": {"type": "number", "description": "Number of markets (default 10, max 50)", "optional": True},
                "active_only": {"type": "boolean", "description": "Only return active markets (default true)", "optional": True},
            },
            handler=fetch_polymarket,
        ),
        ToolDefinition(
            name="fetch_kalshi",
            description="Fetch prediction markets from Kalshi. Returns market titles, prices, and volumes.",
            parameters={
                "query": {"type": "string", "description": "Search query to filter markets", "optional": True},
                "limit": {"type": "number", "description": "Number of markets (default 10, max 50)", "optional": True},
                "status": {"type": "string", "description": "Market status: 'open', 'closed', 'settled' (default: open)", "optional": True},
            },
            handler=fetch_kalshi,
        ),
        ToolDefinition(
            name="fetch_metaculus",
            description="Fetch prediction questions from Metaculus. Returns questions, community predictions, and activity.",
            parameters={
                "query": {"type": "string", "description": "Search query to filter questions", "optional": True},
                "limit": {"type": "number", "description": "Number of questions (default 10, max 50)", "optional": True},
                "order_by": {"type": "string", "description": "Sort: '-activity', '-publish_time', '-resolve_time' (default: -activity)", "optional": True},
            },
            handler=fetch_metaculus,
        ),
    ]


def create_sessions_tool_defs(
    memory: MemoryManager, team_id: str, pi_id: str, *,
    company_id: Optional[str] = None,
    shared_teams: Optional[List[str]] = None,
) -> List[ToolDefinition]:
    import datetime

    def _can_access_target(target: str) -> bool:
        if "/" not in target:
            # Global team: open unless the company has a shared-team whitelist
            return company_id is None or shared_teams is None or target in shared_teams
        return company_id is not None and company_id == target.split("/", 1)[0]

    async def sessions_send(args):
        target = args["target_team"]
        if not _can_access_target(target):
            return f"Access denied: cannot send to {target} (cross-company)"
        entry = {
            "from_team": team_id,
            "from_pi": pi_id,
            "message": args["message"],
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        memory.append_team_memory(target, "inbox.json", entry)
        return f"Message sent to {target}/inbox.json"

    async def sessions_list(args):
        return json.dumps([t for t in memory.list_teams() if _can_access_target(t)])

    async def sessions_history(args):
        target = args.get("team_id", team_id)
        if not _can_access_target(target):
            return json.dumps({"error": f"Access denied: cannot read {target} (cross-company)"})
        limit = int(args.get("limit", 10))
        inbox = memory.read_team_memory(target, "inbox.json")
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
    run_team_fn = ctx.extra.get("run_team_fn")
    run_auto_fn = ctx.extra.get("run_auto_fn")
    company_id = ctx.extra.get("company_id")
    uc = {"company_id": company_id} if company_id else {}

    async def run_team(args):
        team_id = args["team_id"]
        try:
            return await run_team_fn(
                task=args["task"], team_id=team_id, mode=args.get("mode", "cascade"), user_context=dict(uc),
            )
        except Exception as e:
            return f"Error running team {team_id}: {e}"

    async def run_auto(args):
        try:
            result, decision = await run_auto_fn(
                task=args["task"], mode=args.get("mode"), user_context=dict(uc),
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


@dataclass
class ToolContext:
    memory: MemoryManager
    team_id: str
    pi_id: str
    tools_config: List[str]
    user_context: Optional[Dict[str, Any]] = None
    user_scheduler: Any = None
    tools_deny: Set[str] = field(default_factory=set)
    extra: Dict[str, Any] = field(default_factory=dict)
    memory_backend: Optional[MemoryBackend] = None
    company_id: Optional[str] = None


@dataclass
class ToolProvider:
    """A registered tool source: `factory(ctx)` returns its ToolDefinitions."""
    name: str
    tool_names: Set[str]
    factory: Optional[Callable[[ToolContext], List[ToolDefinition]]] = None
    requires: Optional[Callable[[ToolContext], bool]] = None


def _requires(names: Set[str], presets: tuple = (), prerequisite: Optional[Callable[[ToolContext], bool]] = None):
    def check(ctx: ToolContext, default_all: bool = False) -> bool:
        if prerequisite is not None and not prerequisite(ctx):
            return False
        return default_all or any(t in names or t in presets for t in ctx.tools_config)
    return check


def _scheduler_defs(ctx: ToolContext) -> List[ToolDefinition]:
    uc = ctx.user_context
    return create_scheduler_tool_defs(
        ctx.user_scheduler,
        user_id=uc.get("user_id", ""),
        chat_id=uc.get("chat_id", ""),
        channel=uc.get("channel", "telegram"),
    )


def _sessions_defs(ctx: ToolContext) -> List[ToolDefinition]:
    return create_sessions_tool_defs(
        ctx.memory, ctx.team_id, ctx.pi_id,
        company_id=ctx.company_id,
        shared_teams=ctx.extra.get("company_shared_teams"),
    )


def _public_knowledge_defs(ctx: ToolContext) -> List[ToolDefinition]:
    from .knowledge.public import create_public_knowledge_tool_defs
    return create_public_knowledge_tool_defs()


def _builtin_defs(ctx: ToolContext) -> List[ToolDefinition]:
    from .builtin_tools import create_builtin_tool_defs
    return create_builtin_tool_defs(ctx)


def _public_knowledge_enabled(ctx: ToolContext) -> bool:
    return os.environ.get("ENABLE_PUBLIC_KNOWLEDGE_V1", "").lower() in ("1", "true")


class ToolRegistry:
    def __init__(self):
        self._providers: Dict[str, ToolProvider] = {}
        self._custom_presets: Dict[str, List[str]] = {}
        self._skills: Dict[str, SkillDefinition] = {}
        self._external_mcp_configs: Dict[str, dict] = {}  # company-scoped names are "{company_id}/{name}"
        self.memory_backend: Optional[MemoryBackend] = None  # set by orchestrator
        for provider in (
            ToolProvider("builtin", CLAUDE_BUILTIN_TOOLS, _builtin_defs, requires=lambda ctx, default_all=False: True),
            ToolProvider(
                "memory", CUSTOM_TOOL_NAMES,
                lambda ctx: create_memory_tool_defs(ctx.memory, ctx.team_id, ctx.pi_id, memory_backend=ctx.memory_backend),
            ),
            ToolProvider(
                "scheduler", SCHEDULER_TOOL_NAMES, _scheduler_defs,
                _requires(SCHEDULER_TOOL_NAMES, ("scheduler",), lambda ctx: bool(ctx.user_context and ctx.user_scheduler)),
            ),
            ToolProvider(
                "feed", FEED_TOOL_NAMES, lambda ctx: create_feed_tool_defs(),
                _requires(FEED_TOOL_NAMES, ("collector",)),
            ),
            ToolProvider(
                "sessions", SESSIONS_TOOL_NAMES, _sessions_defs,
                _requires(SESSIONS_TOOL_NAMES, ("messenger",)),
            ),
            ToolProvider(
                "orchestrator", ORCHESTRATOR_TOOL_NAMES, create_orchestrator_tool_defs,
                _requires(ORCHESTRATOR_TOOL_NAMES, ("ceo",), lambda ctx: bool(ctx.extra.get("run_team_fn"))),
            ),
            ToolProvider(
                "git", GIT_TOOL_NAMES, create_git_tool_defs,
                _requires(GIT_TOOL_NAMES, ("coder", "code-reviewer"), lambda ctx: bool(ctx.extra.get("workspace_path"))),
            ),
            ToolProvider(
                "public_knowledge", PUBLIC_KNOWLEDGE_TOOL_NAMES, _public_knowledge_defs,
                _requires(PUBLIC_KNOWLEDGE_TOOL_NAMES, ("researcher", "collector"), _public_knowledge_enabled),
            ),
        ):
            self.register(provider)

    def register(self, provider: ToolProvider) -> None:
        self._providers[provider.name] = provider

    def register_skill(self, skill: SkillDefinition) -> None:
        self._skills[skill.name] = skill
        if skill.tools:
            self._custom_presets[skill.name] = skill.tools

    def register_external_mcp(self, name: str, sdk_config: dict) -> None:
        self._external_mcp_configs[name] = sdk_config

    def register_company_mcp(self, company_id: str, name: str, sdk_config: dict) -> None:
        self._external_mcp_configs[f"{company_id}/{name}"] = sdk_config

    def unregister_company_mcp(self, company_id: str) -> None:
        for name in [n for n in self._external_mcp_configs if n.startswith(f"{company_id}/")]:
            del self._external_mcp_configs[name]

    def get_skill_instructions(self, tools_config: List[str]) -> str:
        return "\n\n".join(
            f"# Skill: {skill.name}\n{skill.instructions}"
            for skill in self._skills.values()
            if skill.instructions and (
                not tools_config or skill.name in tools_config or any(t in tools_config for t in skill.tools)
            )
        )

    def resolve_tool_names(
        self, tools_config: List[str], tools_deny: Optional[Set[str]] = None,
    ) -> List[str]:
        """Allowed tool names; an empty tools_config means every tool except orchestrator and git."""
        if tools_config:
            expanded = [t for name in tools_config for t in self._custom_presets.get(name, [name])]
            # A skill nested in a skill only contributes its exact-named non-core tools.
            exact = set(expanded).union(*(self._custom_presets.get(name, ()) for name in expanded))
            names = resolve_tool_names(expanded) + [
                t for p in self._providers.values() if p.name not in ("memory", "scheduler", "feed", "sessions")
                for t in p.tool_names if t in exact
            ]
        else:
            names = [
                n for p in self._providers.values() if p.name not in ("orchestrator", "git")
                for n in sorted(p.tool_names)
            ]
        # Deny entries may use the legacy mcp__<server>__<tool> form.
        deny = set(tools_deny or ()) | {d.rsplit("__", 1)[-1] for d in tools_deny or ()}
        return [n for n in dict.fromkeys(names) if n not in deny]

    def _is_active(self, provider: ToolProvider, ctx: ToolContext) -> bool:
        if not ctx.tools_config:
            return provider.requires is None or provider.requires(ctx, default_all=True)
        if provider.requires is not None:
            return provider.requires(ctx)
        # Exact names, one preset level deep: resolving further would grant memory tools to more Pis.
        named = {
            t for name in ctx.tools_config
            for t in TOOL_PRESETS.get(name) or self._custom_presets.get(name) or [name]
        }
        return bool(provider.tool_names & named)

    def get_tool_defs(self, ctx: ToolContext) -> List[ToolDefinition]:
        allowed = set(self.resolve_tool_names(ctx.tools_config, ctx.tools_deny))
        defs: Dict[str, ToolDefinition] = {}
        for provider in self._providers.values():
            if provider.factory and self._is_active(provider, ctx):
                for d in provider.factory(ctx):
                    if d.name in allowed:
                        defs.setdefault(d.name, d)
        return list(defs.values())

    def get_tools(self, ctx: ToolContext) -> list:
        return definitions_to_langchain(self.get_tool_defs(ctx))

    async def get_external_mcp_tools(self, company_id: Optional[str] = None, tools_deny: Set[str] = frozenset()) -> list:
        """Load tools from the global MCP servers and those scoped to this company."""
        configs = {
            name: cfg for name, cfg in self._external_mcp_configs.items()
            if "/" not in name or (company_id and name.startswith(f"{company_id}/"))
        }
        if not configs:
            return []
        from langchain_mcp_adapters.client import MultiServerMCPClient

        tools = []
        for name, cfg in configs.items():
            try:
                client = MultiServerMCPClient({name: _to_mcp_connection(cfg)})
                tools.extend(await client.get_tools())
            except Exception as e:
                logger.warning("External MCP server %r unavailable: %s", name, e)
        return [t for t in tools if t.name not in tools_deny]


def _to_mcp_connection(cfg: dict) -> dict:
    transport = cfg.get("type", "stdio")
    if transport == "stdio":
        conn = {"transport": "stdio", "command": cfg["command"], "args": list(cfg.get("args", []))}
        if cfg.get("env"):
            conn["env"] = cfg["env"]
        return conn
    conn = {"transport": "streamable_http" if transport == "http" else transport, "url": cfg["url"]}
    if cfg.get("headers"):
        conn["headers"] = cfg["headers"]
    return conn


_TOOL_ALIASES = {
    "web_search": "WebSearch", "websearch": "WebSearch",
    "web_fetch": "WebFetch", "webfetch": "WebFetch",
    "read": "Read", "write": "Write", "edit": "Edit", "glob": "Glob", "grep": "Grep",
    "bash": "Bash", "exec": "Bash",
    "openbb": "fetch_openbb",
}
_LOWERCASE_TOOL_NAMES = (
    CUSTOM_TOOL_NAMES | SCHEDULER_TOOL_NAMES | FEED_TOOL_NAMES | SESSIONS_TOOL_NAMES
    | ORCHESTRATOR_TOOL_NAMES | GIT_TOOL_NAMES
)


def _normalize_tool_name(name: str) -> str:
    lowered = name.lower()
    if lowered in _TOOL_ALIASES:
        return _TOOL_ALIASES[lowered]
    # Unknown names keep their case; a legacy mcp__<server>__ prefix is dropped.
    return lowered if lowered in _LOWERCASE_TOOL_NAMES else name.rsplit("__", 1)[-1]


def resolve_tool_names(tools_config: List[str], _seen: Optional[set] = None) -> List[str]:
    """Expand presets and aliases into canonical tool names (deduped, order preserved)."""
    if _seen is None:
        _seen = set()
    resolved = []
    for name in tools_config:
        if name not in TOOL_PRESETS:
            resolved.append(_normalize_tool_name(name))
        elif name not in _seen:  # presets may reference each other
            _seen.add(name)
            resolved.extend(resolve_tool_names(TOOL_PRESETS[name], _seen))
    return list(dict.fromkeys(resolved))
