import sys
import json
import argparse
import asyncio
import logging
import os
from pathlib import Path
from enum import Enum

from . import __version__
from .company import CompanyConfig, CompanyRegistry
from .config import CompanestConfig
from .parser import MarkdownConfigParser, generate_config_template

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


class ExitCode(int, Enum):
    SUCCESS = 0
    ERROR = 1
    VALIDATION_ERROR = 2
    CONFIG_NOT_FOUND = 3


class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

    @classmethod
    def disable(cls):
        cls.RED = cls.GREEN = cls.YELLOW = cls.BLUE = ''
        cls.CYAN = cls.BOLD = cls.RESET = ''


def colored(text: str, color: str) -> str:
    return f"{color}{text}{Colors.RESET}"


def print_success(msg: str):
    print(colored(f"  {msg}", Colors.GREEN))


def print_error(msg: str):
    print(colored(f"  {msg}", Colors.RED))


def print_warning(msg: str):
    print(colored(f"  {msg}", Colors.YELLOW))


def print_info(msg: str):
    print(colored(f"  {msg}", Colors.BLUE))


def _api_auth_headers() -> dict[str, str]:
    token = os.getenv("COMPANEST_API_TOKEN")
    if not token:
        logger.warning(
            "COMPANEST_API_TOKEN not set. API calls to an authenticated server will fail. "
            "Set it with: export COMPANEST_API_TOKEN=<your-token>"
        )
        return {}
    return {"Authorization": f"Bearer {token}"}


def _handle_api_error(e: Exception) -> int:
    import httpx as _httpx
    if isinstance(e, _httpx.HTTPStatusError) and e.response.status_code == 401:
        print_error(
            "401 Unauthorized. Set COMPANEST_API_TOKEN to match the server's token:\n"
            "  export COMPANEST_API_TOKEN=<your-token>"
        )
    else:
        print_error(f"Failed to connect to API: {e}")
    return ExitCode.ERROR


def _api_command(failure: str = ""):
    """Wrap body(args, call) as an API command; call(method, path, **kwargs) returns the JSON reply."""
    def decorator(body):
        def command(args) -> int:
            api_url = args.api_url.rstrip("/")
            headers = _api_auth_headers()
            import httpx  # resolved per call: tests swap sys.modules["httpx"]

            def call(method: str, path: str, **kwargs):
                response = getattr(httpx, method)(f"{api_url}{path}", headers=headers, **kwargs)
                response.raise_for_status()
                return response.json()

            try:
                body(args, call)
                return ExitCode.SUCCESS
            except Exception as e:
                if not failure:
                    return _handle_api_error(e)
                print_error(f"{failure}: {e}")
                return ExitCode.ERROR
        return command
    return decorator


ENV_KEYS = [
    {
        "key": "ANTHROPIC_API_KEY",
        "name": "Anthropic API",
        "required": True,
        "description": "Claude API key (for direct API mode)",
        "hint": "Get one at https://console.anthropic.com/",
    },
    {
        "key": "COMPANEST_MASTER_TOKEN",
        "name": "Master Gateway",
        "required": False,
        "description": "Auth token for connecting to master gateway",
        "hint": "Only needed if using master connection mode",
    },
    {
        "key": "TELEGRAM_BOT_TOKEN",
        "name": "Telegram Bot",
        "required": False,
        "description": "Telegram bot token for the master gateway (from @BotFather)",
        "hint": "Needed on the master gateway machine (scripts/master_gateway.py)",
    },
    {
        "key": "OPENAI_API_KEY",
        "name": "OpenAI API",
        "required": False,
        "description": "OpenAI API key (optional, for multi-provider support)",
        "hint": "Get one at https://platform.openai.com/",
    },
    {
        "key": "LITELLM_MASTER_KEY",
        "name": "LiteLLM Admin",
        "required": False,
        "description": "LiteLLM proxy admin key (for key/team management)",
        "hint": "Only needed if using LiteLLM proxy for key isolation",
    },
    {
        "key": "LITELLM_DEFAULT_KEY",
        "name": "LiteLLM Default Key",
        "required": False,
        "description": "Default virtual key for Pi agents (from LiteLLM)",
        "hint": "Generate with: litellm /key/generate",
    },
]


def cmd_init(args) -> int:
    env_path = Path(args.env_file)
    companest_dir = Path(".companest")

    print(colored("\n=== Companest Init ===\n", Colors.BOLD))

    if not companest_dir.exists():
        companest_dir.mkdir(parents=True)
        print_success(f"Created {companest_dir}/")
    else:
        print_info(f"{companest_dir}/ already exists")

    existing = {}
    if env_path.exists():
        print_info(f"Found existing {env_path}")
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.strip().strip("'").strip('"')
                existing[k.strip()] = v

    print(colored("\nAPI Keys:\n", Colors.BOLD))

    missing = []
    present = []

    for entry in ENV_KEYS:
        key = entry["key"]
        value = os.getenv(key) or existing.get(key, "")
        req = colored("required", Colors.RED) if entry["required"] else "optional"

        if value:
            masked = value[:8] + "..." + value[-4:] if len(value) > 16 else "***"
            print(f"  {colored('OK', Colors.GREEN)}  {key} = {masked}  ({entry['name']})")
            present.append((key, value))
        else:
            print(f"  {colored('--', Colors.YELLOW)}  {key}  ({entry['name']}, {req})")
            print(f"      {entry['description']}")
            print(f"      {colored(entry['hint'], Colors.CYAN)}")
            missing.append(entry)

    if missing and sys.stdin.isatty() and not args.check_only:
        print(colored("\nEnter missing keys (press Enter to skip):\n", Colors.BOLD))

        for entry in missing:
            key = entry["key"]
            req_tag = " [required]" if entry["required"] else ""
            try:
                value = input(f"  {key}{req_tag}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if value:
                present.append((key, value))

    if present and not args.check_only:
        lines = ["# Companest environment configuration", f"# Generated by: companest init", ""]
        for key, value in present:
            if " " in value or "'" in value:
                lines.append(f'{key}="{value}"')
            else:
                lines.append(f"{key}={value}")
        lines.append("")

        env_path.write_text("\n".join(lines))
        print(f"\n  {colored('Saved', Colors.GREEN)}: {env_path}")

        gitignore = Path(".gitignore")
        if gitignore.exists():
            content = gitignore.read_text()
            if ".env" not in content:
                print_warning(f"Add .env to .gitignore to avoid leaking keys!")
        else:
            print_warning(f"No .gitignore found  create one with .env in it!")

    print(colored("\nDependencies:\n", Colors.BOLD))
    _check_dependency("websockets", "WebSocket (master gateway)")
    _check_dependency("pydantic", "Config models")
    _check_dependency("fastapi", "API server")
    _check_dependency("uvicorn", "ASGI server")
    _check_dependency("aiosqlite", "Job persistence")
    _check_dependency("httpx", "HTTP client / Claude API")

    required_missing = [e for e in missing if e["required"] and
                        not any(p[0] == e["key"] for p in present)]

    print()
    if required_missing:
        print_warning(
            f"Missing {len(required_missing)} required key(s): "
            + ", ".join(e["key"] for e in required_missing)
        )
        print_info("Run 'companest init' again to set them, or edit .env directly")
        print()
        return ExitCode.ERROR
    else:
        print_success("Ready! Run 'companest serve' to start the control panel.")
        print()
        return ExitCode.SUCCESS


def _check_dependency(module_name: str, description: str):
    try:
        __import__(module_name)
        print(f"  {colored('OK', Colors.GREEN)}  {module_name}  {description}")
    except ImportError:
        print(f"  {colored('--', Colors.RED)}  {module_name}  {description} (pip install {module_name})")


def cmd_validate(args) -> int:
    config_path = Path(args.config)

    print(f"\nValidating: {config_path}\n")

    if not config_path.exists():
        print_error(f"File not found: {config_path}")
        return ExitCode.CONFIG_NOT_FOUND

    try:
        parser = MarkdownConfigParser(strict_mode=args.strict)
        result = parser.parse_file(config_path)
    except Exception as e:
        print_error(f"Parse error: {e}")
        return ExitCode.VALIDATION_ERROR

    if result.warnings:
        print(colored("Warnings:", Colors.YELLOW + Colors.BOLD))
        for warning in result.warnings:
            print_warning(warning)
        print()

    try:
        if str(config_path).endswith(".md"):
            CompanestConfig.from_markdown(str(config_path))
        else:
            CompanestConfig.from_json_file(str(config_path))
    except Exception as e:
        print_error(f"Config validation error: {e}")
        return ExitCode.VALIDATION_ERROR

    print_success("Configuration is valid!")
    return ExitCode.SUCCESS


def cmd_lint(args) -> int:
    config_path = Path(args.config)

    print(f"\nLinting: {config_path}\n")

    if not config_path.exists():
        print_error(f"File not found: {config_path}")
        return ExitCode.CONFIG_NOT_FOUND

    try:
        parser = MarkdownConfigParser()
        result = parser.parse_file(config_path)
    except Exception as e:
        print_error(f"Configuration has errors: {e}")
        return ExitCode.VALIDATION_ERROR

    if result.warnings:
        print(colored("Warnings:", Colors.YELLOW + Colors.BOLD))
        for warning in result.warnings:
            print_warning(warning)
        print()
    else:
        print_success("No warnings - configuration looks good!")

    return ExitCode.SUCCESS


def cmd_generate(args) -> int:
    output_path = Path(args.output)

    print(f"\nGenerating config template: {output_path}")
    print(f"Format: {args.format}\n")

    try:
        generate_config_template(
            output_path,
            format=args.format,
        )
        print_success(f"Generated: {output_path}")
        return ExitCode.SUCCESS
    except Exception as e:
        print_error(f"Failed to generate: {e}")
        return ExitCode.ERROR


def cmd_show(args) -> int:
    config_path = Path(args.config)

    if not config_path.exists():
        print_error(f"File not found: {config_path}")
        return ExitCode.CONFIG_NOT_FOUND

    try:
        parser = MarkdownConfigParser()
        result = parser.parse_file(config_path)

        if args.format == "json":
            print(json.dumps(result.config, indent=2))
        elif args.format == "yaml":
            import yaml
            print(yaml.dump(result.config, default_flow_style=False, sort_keys=False))
        else:
            print(colored("\n=== Companest Configuration ===\n", Colors.BOLD))

            api = result.config.get("api", {})
            if api:
                print(colored("\nAPI:", Colors.CYAN + Colors.BOLD))
                print(f"  host: {api.get('host', '0.0.0.0')}")
                print(f"  port: {api.get('port', 8000)}")

            master = result.config.get("master", {})
            if master.get("enabled"):
                print(colored("\nMaster:", Colors.CYAN + Colors.BOLD))
                print(f"  host: {master.get('host', '')}")
                print(f"  port: {master.get('port', 18789)}")

            print()

        return ExitCode.SUCCESS

    except Exception as e:
        print_error(f"Failed to parse: {e}")
        return ExitCode.ERROR


def cmd_serve(args) -> int:
    config_path = Path(args.config) if args.config else None

    if config_path:
        if not config_path.exists():
            print_error(f"Config not found: {config_path}")
            return ExitCode.CONFIG_NOT_FOUND
        if str(config_path).endswith(".md"):
            config = CompanestConfig.from_markdown(str(config_path))
        else:
            config = CompanestConfig.from_json_file(str(config_path))
    else:
        config = CompanestConfig.discover_config(".")
        if not config:
            print_error(
                "No config found. Create .companest/config.md or specify with --config"
            )
            return ExitCode.CONFIG_NOT_FOUND

    host = args.host or config.api.host
    port = args.port or config.api.port

    print(colored("\n=== Companest Control Panel ===\n", Colors.BOLD))
    print(f"  Config:  {config_path or 'auto-discovered'}")
    print(f"  API:     http://{host}:{port}")
    if config.master.enabled and config.master.host:
        print(f"  Master:  {config.master.ws_url}")
    print()

    if not config.api.auth_token:
        if config.debug:
            print_warning(
                "No API auth token set (COMPANEST_API_TOKEN). "
                "Debug mode allows unauthenticated local access."
            )
        else:
            print_warning(
                "No API auth token set (COMPANEST_API_TOKEN). "
                "Server startup will fail unless debug mode is enabled."
            )
    if not config.master.enabled:
        print_warning(
            "Master connection disabled. "
            "Telegram/chat tasks won't be received."
        )
    if not Path(".companest/teams").exists():
        print_warning(
            "No .companest/teams/ directory found. "
            "Pi Agent Teams will not be available."
        )
    if config.proxy.enabled:
        print_info(f"Proxy: LiteLLM at {config.proxy.base_url}")
        if not config.proxy.default_key:
            print_warning(
                "LiteLLM proxy enabled but no default key set. "
                "Set LITELLM_DEFAULT_KEY env var."
            )
    elif not os.getenv("ANTHROPIC_API_KEY"):
        print_warning(
            "No LiteLLM proxy and no ANTHROPIC_API_KEY. "
            "Pi agents won't be able to call LLM APIs."
        )
    print()

    from .orchestrator import CompanestOrchestrator
    from .jobs import JobManager
    from .server import CompanestAPIServer
    from .master import MasterConnection

    async def _start():
        orchestrator = CompanestOrchestrator(config)

        companest_dir = Path(config.data_dir)
        if (companest_dir / "teams").exists():
            orchestrator.init_teams(
                base_path=str(companest_dir),
                s3_bucket=os.getenv("COMPANEST_S3_BUCKET") or None,
            )
            print_info(
                f"Teams initialized: "
                f"{len(orchestrator.team_registry.list_teams())} registered, "
                f"{len(orchestrator.team_registry.list_meta_teams())} meta"
            )

        job_manager = JobManager(orchestrator, data_dir=Path(".companest"))
        await job_manager.start()

        # user_scheduler and cost_gate only exist once init_teams() has run
        if getattr(orchestrator, "user_scheduler", None) is not None:
            await orchestrator.user_scheduler.start()
            orchestrator.user_scheduler.set_execution_callback(
                orchestrator.background.execute_scheduled_job
            )
            print_info("UserScheduler started")

        config.api.host = host
        config.api.port = port

        api_server = CompanestAPIServer(config, job_manager, orchestrator)

        master_conn = None
        if config.master.enabled and config.master.host:
            master_conn = MasterConnection(config.master, orchestrator)
            orchestrator.set_notification_callback(master_conn.send_notification)

        try:
            if master_conn:
                await asyncio.gather(
                    api_server.start(),
                    master_conn.start(),
                )
            else:
                await api_server.start()
        finally:
            if master_conn:
                await master_conn.stop()
            if getattr(orchestrator, "user_scheduler", None) is not None:
                await orchestrator.user_scheduler.shutdown()
            await job_manager.stop()
            from .knowledge.feeds import close_client as close_feed_client
            await close_feed_client()
            if hasattr(orchestrator, "cost_gate") and orchestrator.cost_gate.litellm_client:
                await orchestrator.cost_gate.litellm_client.close()

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        print("\nShutting down...")

    return ExitCode.SUCCESS


@_api_command()
def cmd_team_list(args, call):
    data = call("get", "/api/teams", timeout=10)

    print(colored("\n=== Agent Teams ===\n", Colors.BOLD))

    registered = data.get("registered", [])
    active = data.get("active", [])
    meta = data.get("meta", [])

    configs = data.get("configs", {})
    for tid in registered:
        c = configs.get(tid, {})
        team_type = "meta" if tid in meta else "on-demand"
        print(
            f"   {colored(tid, Colors.CYAN):20s} "
            f"role={c.get('role', '-'):12s} "
            f"pis={c.get('pi_count', 0)} "
            f"lead={c.get('lead_pi', '-'):10s} "
            f"[{team_type}]"
        )

    print(f"\n  Total: {len(registered)} | Active: {len(active)} | Meta: {len(meta)}\n")


@_api_command("Failed")
def cmd_team_status(args, call):
    data = call("get", f"/api/teams/{args.team_id}", timeout=10)

    print(colored(f"\n=== Team: {data['id']} ===\n", Colors.BOLD))
    print(f"  Role:     {data.get('role', '-')}")
    print(f"  Enabled:  {data.get('enabled', False)}")
    print(f"  Active:   {data.get('active', False)}")
    print(f"  Always-on: {data.get('always_on', False)}")
    print(f"  Lead Pi:  {data.get('lead_pi', '-')}")
    print(f"  Mode:     {data.get('mode', 'default')}")

    pis = data.get("pis", [])
    if pis:
        print(colored("\n  Pis:", Colors.CYAN + Colors.BOLD))
        for pi in pis:
            print(f"    - {pi['id']} ({pi.get('model', '-')})")

    print()


@_api_command("Failed")
def cmd_team_run(args, call):
    payload = {"task": args.task, "skip_cost_check": args.skip_cost}
    print_info(f"Sending task to team '{args.team_id}'...")

    data = call("post", f"/api/teams/{args.team_id}/run", json=payload, timeout=300)

    print_success(f"Team '{data['team_id']}' completed")
    print(colored("\nResult:", Colors.CYAN + Colors.BOLD))
    print(data.get("result", "(empty)"))
    print()


@_api_command("Failed")
def cmd_finance_summary(args, call):
    data = call("get", "/api/finance/summary", timeout=10)

    print(colored("\n=== Finance Summary ===\n", Colors.BOLD))

    budget = data.get("budget", {})
    print(f"  Today:       ${data.get('today', 0):.4f}")
    print(f"  Total:       ${data.get('total', 0):.4f}")
    print(f"  Entries:     {data.get('entries', 0)}")
    print(f"  Daily limit: ${budget.get('daily_limit', 0):.2f}")
    print(f"  Monthly:     ${budget.get('monthly_limit', 0):.2f}")

    by_team = data.get("by_team", {})
    if by_team:
        print(colored("\n  By Team:", Colors.CYAN + Colors.BOLD))
        for tid, cost in sorted(by_team.items(), key=lambda x: x[1], reverse=True):
            print(f"    {tid:20s} ${cost:.4f}")

    print()


@_api_command()
def cmd_fleet_status(args, call):
    data = call("get", "/api/fleet/status", timeout=10)

    print(colored("\n=== Fleet Status ===\n", Colors.BOLD))

    teams = data.get("teams", {})
    print(colored("Teams:", Colors.CYAN + Colors.BOLD))
    print(f"  Registered: {teams.get('registered', 0)}")
    print(f"  Active:     {teams.get('active', 0)}")

    jobs = data.get("jobs", {})
    print(colored("\nJobs:", Colors.CYAN + Colors.BOLD))
    print(f"  Total:     {jobs.get('total', 0)}")
    print(f"  Running:   {jobs.get('running', 0)}")
    print(f"  Completed: {jobs.get('completed', 0)}")
    print(f"  Failed:    {jobs.get('failed', 0)}")
    print(f"  Queue:     {jobs.get('queue_size', 0)}")

    print()


@_api_command("Failed to submit job")
def cmd_job_submit(args, call):
    payload = {
        "task": args.task,
        "submitted_by": "cli",
    }

    data = call("post", "/api/jobs", json=payload, timeout=30)

    print_success(f"Job submitted: {data['job_id']}")
    print_info(f"Status: {data['status']}")


@_api_command("Failed to get job status")
def cmd_job_status(args, call):
    data = call("get", f"/api/jobs/{args.job_id}", timeout=10)

    print(colored(f"\n=== Job {args.job_id[:8]}... ===\n", Colors.BOLD))
    print(f"  Status:  {data['status']}")
    print(f"  Task:    {data['task'][:80]}")
    print(f"  Created: {data['created_at']}")

    if data.get("started_at"):
        print(f"  Started: {data['started_at']}")
    if data.get("completed_at"):
        print(f"  Done:    {data['completed_at']}")
    if data.get("duration_ms"):
        print(f"  Duration: {data['duration_ms']}ms")
    if data.get("error"):
        print_error(f"Error: {data['error']}")
    if data.get("result"):
        print(colored("\nResult:", Colors.CYAN + Colors.BOLD))
        print(data["result"][:500])

    print()


@_api_command("Failed to list jobs")
def cmd_job_list(args, call):
    params = {"limit": args.limit}
    if args.status:
        params["status"] = args.status

    data = call("get", "/api/jobs", params=params, timeout=10)

    jobs = data.get("jobs", [])
    stats = data.get("stats", {})
    total = data.get("total", stats.get("total", len(jobs)))

    print(colored(f"\n=== Jobs ({total} total) ===\n", Colors.BOLD))

    if not jobs:
        print("  No jobs found.")
    else:
        status_colors = {"completed": Colors.GREEN, "failed": Colors.RED, "running": Colors.YELLOW}
        for job in jobs:
            status = job["status"]
            print(
                f"  {colored(status.upper().ljust(10), status_colors.get(status, Colors.BLUE))} "
                f"{job['id'][:8]}... "
                f"{job['task'][:50]}"
            )

    print()


def _get_company_registry(data_dir: str):
    registry = CompanyRegistry(data_dir)
    registry.scan()
    return registry


def _find_company(args):
    registry = _get_company_registry(args.data_dir)
    config = registry.get(args.company_id)
    if not config:
        print_error(f"Company '{args.company_id}' not found")
    return registry, config


def cmd_company_create(args) -> int:
    registry = _get_company_registry(args.data_dir)

    if registry.get(args.company_id):
        print_error(f"Company '{args.company_id}' already exists")
        return ExitCode.ERROR

    try:
        config = CompanyConfig(
            id=args.company_id,
            name=args.name,
            domain=args.domain,
        )
        registry.save(config)

        company_dir = Path(args.data_dir) / "companies" / args.company_id
        for sub in ("teams", "memory", "logs"):
            (company_dir / sub).mkdir(parents=True, exist_ok=True)

        print_success(f"Created company '{args.company_id}' ({args.name})")
        print_info(f"Config: {company_dir / 'company.yaml'}")
        print_info(f"Subdirs: teams/, memory/, logs/")
        return ExitCode.SUCCESS

    except Exception as e:
        print_error(f"Failed to create company: {e}")
        return ExitCode.ERROR


def cmd_company_add_team(args) -> int:
    _, config = _find_company(args)
    if not config:
        return ExitCode.CONFIG_NOT_FOUND

    company_dir = Path(args.data_dir) / "companies" / args.company_id
    team_dir = company_dir / "teams" / args.team_id

    if team_dir.exists():
        print_error(f"Team directory '{args.team_id}' already exists under company '{args.company_id}'")
        return ExitCode.ERROR

    try:
        team_dir.mkdir(parents=True, exist_ok=True)

        # team.md must stay in the markdown format TeamRegistry.scan_company_teams() parses
        role = args.role or "general"
        pi_id = "agent"
        team_md_content = (
            f"# Team: {args.team_id}\n"
            f"- role: {role}\n"
            f"- lead_pi: {pi_id}\n"
            f"- enabled: true\n"
            f"\n"
            f"#### Pi: {pi_id}\n"
            f"- model: deepseek-chat\n"
            f"- tools: researcher\n"
            f"- max_turns: 10\n"
        )
        (team_dir / "team.md").write_text(team_md_content, encoding="utf-8")

        soul_dir = team_dir / "pis" / pi_id
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "soul.md").write_text(
            f"# {args.team_id} Agent\n\nRole: {role}\n",
            encoding="utf-8",
        )

        print_success(f"Added team '{args.team_id}' to company '{args.company_id}'")
        print_info(f"Role: {role}")
        print_info(f"Dir:  {team_dir}")
        return ExitCode.SUCCESS

    except Exception as e:
        print_error(f"Failed to add team: {e}")
        return ExitCode.ERROR


def cmd_company_list(args) -> int:
    registry = _get_company_registry(args.data_dir)

    companies = registry.list_companies()

    print(colored("\n=== Companies ===\n", Colors.BOLD))

    if not companies:
        print("  No companies found.")
        print_info(f"Data dir: {Path(args.data_dir).resolve()}")
        print()
        return ExitCode.SUCCESS

    for cid in companies:
        config = registry.get(cid)
        status_icon = colored("", Colors.GREEN) if config.enabled else colored("", Colors.RED)
        ceo_status = "CEO on" if config.ceo.enabled else "CEO off"
        print(
            f"  {status_icon} {colored(config.id, Colors.CYAN):20s} "
            f"{config.name:20s} "
            f"[{ceo_status}]"
        )

    print(f"\n  Total: {len(companies)}\n")
    return ExitCode.SUCCESS


def cmd_company_status(args) -> int:
    _, config = _find_company(args)
    if not config:
        return ExitCode.CONFIG_NOT_FOUND

    print(colored(f"\n=== Company: {config.id} ===\n", Colors.BOLD))
    print(f"  Name:      {config.name}")
    print(f"  Domain:    {config.domain or '(none)'}")
    print(f"  Enabled:   {config.enabled}")

    print(colored("\n  CEO Agent:", Colors.CYAN + Colors.BOLD))
    print(f"    Enabled:        {config.ceo.enabled}")
    print(f"    Model:          {config.ceo.model}")
    print(f"    Cycle interval: {config.ceo.cycle_interval}s")
    if config.ceo.goals:
        print(f"    Goals:")
        for g in config.ceo.goals:
            print(f"      - {g}")
    if config.ceo.kpis:
        print(f"    KPIs:")
        for k, v in config.ceo.kpis.items():
            print(f"      - {k}: {v}")

    company_dir = Path(args.data_dir) / "companies" / config.id
    teams_dir = company_dir / "teams"
    if teams_dir.exists():
        teams = [d.name for d in teams_dir.iterdir() if d.is_dir()]
        print(colored("\n  Teams:", Colors.CYAN + Colors.BOLD))
        for t in teams:
            print(f"    - {t}")
        if not teams:
            print("    (none)")

    if config.schedules:
        print(colored("\n  Schedules:", Colors.CYAN + Colors.BOLD))
        for s in config.schedules:
            enabled_tag = colored("on", Colors.GREEN) if s.enabled else colored("off", Colors.RED)
            print(f"    - {s.name} [{enabled_tag}] every {s.interval_seconds}s -> {s.team_id}")

    print(colored("\n  Preferences:", Colors.CYAN + Colors.BOLD))
    print(f"    Default mode:    {config.preferences.default_mode}")
    print(f"    Hourly budget:   ${config.preferences.budget_hourly_usd:.2f}")
    print(f"    Monthly budget:  ${config.preferences.budget_monthly_usd:.2f}")

    print()
    return ExitCode.SUCCESS


def cmd_company_start(args) -> int:
    registry, config = _find_company(args)
    if not config:
        return ExitCode.CONFIG_NOT_FOUND

    if config.ceo.enabled:
        print_info(f"CEO cycle for '{args.company_id}' is already enabled")
        return ExitCode.SUCCESS

    config.ceo.enabled = True
    config.enabled = True
    registry.save(config)

    print_success(f"CEO cycle enabled for company '{args.company_id}'")
    return ExitCode.SUCCESS


def cmd_company_stop(args) -> int:
    registry, config = _find_company(args)
    if not config:
        return ExitCode.CONFIG_NOT_FOUND

    if not config.ceo.enabled:
        print_info(f"CEO cycle for '{args.company_id}' is already disabled")
        return ExitCode.SUCCESS

    config.ceo.enabled = False
    registry.save(config)

    print_success(f"CEO cycle disabled for company '{args.company_id}'")
    return ExitCode.SUCCESS


def cmd_company_destroy(args) -> int:
    if not args.confirm:
        print_error("Use --confirm to confirm deletion")
        return ExitCode.ERROR

    registry, config = _find_company(args)
    if not config:
        return ExitCode.CONFIG_NOT_FOUND

    company_name = config.name
    registry.delete(args.company_id)

    print_success(f"Destroyed company '{args.company_id}' ({company_name})")
    return ExitCode.SUCCESS


def main():
    if not sys.stdout.isatty():
        Colors.disable()

    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="companest",
        description="Companest Control Panel CLI"
    )
    parser.add_argument(
        "--version", action="version", version=f"Companest CLI {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")
    groups = {}

    def command(sub, name, func, help):
        p = sub.add_parser(name, help=help)
        p.set_defaults(func=func)
        return p

    def group(name, help):
        groups[name] = subparsers.add_parser(name, help=help)
        return groups[name].add_subparsers(dest=f"{name}_command")

    def api_url(p, help="Companest API URL"):
        p.add_argument("--api-url", default="http://localhost:8000", help=help)

    def data_dir(p):
        p.add_argument("--data-dir", default=".companest", help="Data directory (default: .companest)")

    p = command(subparsers, "init", cmd_init, "Initialize Companest  check API keys, deps, write .env")
    p.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    p.add_argument("--check-only", action="store_true", help="Only check, don't prompt or write")

    p = command(subparsers, "validate", cmd_validate, "Validate a configuration file")
    p.add_argument("config", help="Path to config file")
    p.add_argument("--strict", action="store_true", help="Treat warnings as errors")

    p = command(subparsers, "lint", cmd_lint, "Lint configuration for best practices")
    p.add_argument("config", help="Path to config file")

    p = command(subparsers, "generate", cmd_generate, "Generate a configuration template")
    p.add_argument("-o", "--output", default=".companest/config.md", help="Output path (default: .companest/config.md)")
    p.add_argument("-f", "--format", choices=["json", "yaml"], default="json", help="Config format (default: json)")

    p = command(subparsers, "show", cmd_show, "Display parsed configuration")
    p.add_argument("config", help="Path to config file")
    p.add_argument(
        "-f", "--format", choices=["json", "yaml", "pretty"], default="pretty",
        help="Output format (default: pretty)"
    )

    p = command(subparsers, "serve", cmd_serve, "Start the control panel API server")
    p.add_argument("-c", "--config", default=None, help="Path to config file (auto-discovers if not set)")
    p.add_argument("--host", default=None, help="API host (default: from config)")
    p.add_argument("--port", type=int, default=None, help="API port (default: from config)")

    fleet = group("fleet", "Fleet management")
    api_url(
        command(fleet, "status", cmd_fleet_status, "Show fleet status"),
        "Companest API URL (default: http://localhost:8000)",
    )

    team = group("team", "Pi Agent Team management")
    api_url(command(team, "list", cmd_team_list, "List all teams"))
    p = command(team, "status", cmd_team_status, "Show team details")
    p.add_argument("team_id", help="Team ID")
    api_url(p)
    p = command(team, "run", cmd_team_run, "Run task on a team")
    p.add_argument("team_id", help="Team ID")
    p.add_argument("task", help="Task description")
    p.add_argument("--skip-cost", action="store_true", help="Skip CostGate check")
    api_url(p)

    finance = group("finance", "Finance / spending management")
    api_url(command(finance, "summary", cmd_finance_summary, "Show spending summary"))

    job = group("job", "Job management")
    p = command(job, "submit", cmd_job_submit, "Submit a job")
    p.add_argument("task", help="Task description")
    api_url(p)
    p = command(job, "status", cmd_job_status, "Get job status")
    p.add_argument("job_id", help="Job ID")
    api_url(p)
    p = command(job, "list", cmd_job_list, "List jobs")
    p.add_argument("--status", default=None, help="Filter by status")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    api_url(p)

    company = group("company", "Company lifecycle management")
    p = command(company, "create", cmd_company_create, "Create a new company")
    p.add_argument("company_id", help="Company ID")
    p.add_argument("--name", required=True, help="Company display name")
    p.add_argument("--domain", default="", help="Domain knowledge description")
    data_dir(p)
    p = command(company, "add-team", cmd_company_add_team, "Add a team to a company")
    p.add_argument("company_id", help="Company ID")
    p.add_argument("team_id", help="Team ID")
    p.add_argument("--role", default="general", help="Team role (default: general)")
    data_dir(p)
    data_dir(command(company, "list", cmd_company_list, "List all companies"))
    for name, func, help in (
        ("status", cmd_company_status, "Show company status"),
        ("start", cmd_company_start, "Enable CEO cycle"),
        ("stop", cmd_company_stop, "Disable CEO cycle"),
    ):
        p = command(company, name, func, help)
        p.add_argument("company_id", help="Company ID")
        data_dir(p)
    p = command(company, "destroy", cmd_company_destroy, "Delete company and all data")
    p.add_argument("company_id", help="Company ID")
    p.add_argument("--confirm", action="store_true", help="Confirm deletion")
    data_dir(p)

    args = parser.parse_args()

    if hasattr(args, "func"):
        return args.func(args)
    groups.get(args.command, parser).print_help()
    return ExitCode.SUCCESS


if __name__ == "__main__":
    sys.exit(main())
