#!/usr/bin/env python3
"""
Companest Smoke Test

Three levels:
  Level 1: Offline  config, teams, routing, memory, cost gate (no API calls)
  Level 2: Single Pi call  actually calls Claude API (costs ~$0.01)
  Level 3: Full serve  starts API server, sends request via HTTP

Usage:
  python scripts/smoke_test.py           # Level 1 only (free)
  python scripts/smoke_test.py --live    # Level 1 + 2 (calls Claude API)
  python scripts/smoke_test.py --serve   # Level 1 + 2 + 3 (full stack)
"""

import sys
import os
import asyncio
import argparse

# Load .env
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip("'").strip('"')
            os.environ.setdefault(k.strip(), v)

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def green(s): return f"\033[92m{s}\033[0m"
def red(s): return f"\033[91m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def bold(s): return f"\033[1m{s}\033[0m"


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {green('PASS')}  {name}")
        passed += 1
    else:
        print(f"  {red('FAIL')}  {name}  {detail}")
        failed += 1
    return condition


def level1_offline():
    """Offline checks  no API calls, no network."""
    print(bold("\n=== Level 1: Offline Checks ===\n"))

    # 1. Config loading
    from companest.config import CompanestConfig
    config = CompanestConfig.from_markdown(".companest/config.md")
    check("Config loads from .companest/config.md", config is not None)

    # 2. Memory manager
    from companest.memory import MemoryManager
    memory = MemoryManager(".companest")
    soul = memory.read_master_soul()
    check("Memory reads soul.md", soul is not None and len(soul) > 0)

    user = memory.read_master_user()
    check("Memory reads user.md", user is not None)

    teams = memory.list_teams()
    check("Memory lists teams", len(teams) >= 5, f"got {teams}")

    # 3. Team registry
    from companest.team import TeamRegistry
    registry = TeamRegistry(base_path=".companest/teams", memory=memory)
    registry.scan_configs()
    team_list = registry.list_teams()
    check("TeamRegistry scans teams", len(team_list) >= 5, f"got {team_list}")

    # Get a team on demand
    stock_team = registry.get_or_create("stock")
    check("Stock team created on demand", stock_team is not None)
    check("Stock team has pis", len(stock_team.pis) > 0, f"got {list(stock_team.pis.keys())}")
    check("Stock team lead_pi set", stock_team.lead_pi_id is not None, f"lead={stock_team.lead_pi_id}")

    # 4. Router
    from companest.router import TeamRouter
    router = TeamRouter(available_teams=team_list)

    team_id = router.route("Analyze TSLA recent stock trends")
    check("Router: stock query  stock", team_id == "stock", f"got {team_id}")

    team_id = router.route("help me debug this Python bug")
    check("Router: debug query  engineering", team_id == "engineering", f"got {team_id}")

    team_id = router.route("what is the meaning of consciousness")
    check("Router: philosophy query  philosophy", team_id == "philosophy", f"got {team_id}")

    team_id, conf = router.route_with_confidence("@bizdev negotiate this deal")
    check("Router: explicit @bizdev tag", team_id == "bizdev" and conf == 1.0,
          f"got {team_id} conf={conf}")

    team_id, conf = router.route_with_confidence("TSLA stock candlestick MACD")
    check("Router: high confidence stock", conf > 0.5, f"conf={conf}")

    team_id, conf = router.route_with_confidence("hello")
    check("Router: ambiguous  low confidence", conf < 0.3, f"conf={conf}")

    # 5. Cost gate
    from companest.cost_gate import CostGate
    gate = CostGate(memory)
    estimate = gate.estimate_cost("Analyze TSLA", "claude-sonnet-4-5-20250929", "stock")
    check("CostGate estimates cost", estimate.estimated_cost_usd > 0,
          f"${estimate.estimated_cost_usd:.4f}")
    check("CostGate estimate reasonable", estimate.estimated_cost_usd < 0.10,
          f"${estimate.estimated_cost_usd:.4f}")

    # 6. Scheduler
    from companest.scheduler import Scheduler
    scheduler = Scheduler()

    async def dummy(): pass
    scheduler.add("test", dummy, interval=60)
    status = scheduler.get_status()
    check("Scheduler adds task", "test" in status["tasks"])

    # 7. Orchestrator init
    from companest.orchestrator import CompanestOrchestrator
    orch = CompanestOrchestrator(config)
    orch.init_teams(base_path=".companest")
    check("Orchestrator init_teams succeeds", hasattr(orch, "team_registry"))
    check("Orchestrator sees teams",
          len(orch.team_registry.list_teams()) >= 5,
          f"got {orch.team_registry.list_teams()}")

    # 8. Memory path traversal blocked
    from companest.exceptions import CompanestError
    try:
        memory.read_team_memory("../../etc", "passwd")
        check("Path traversal blocked", False, "should have raised")
    except CompanestError:
        check("Path traversal blocked", True)

    # 9. Archiver safe tar members
    from companest.archiver import MemoryArchiver
    check("MemoryArchiver has _safe_tar_members",
          hasattr(MemoryArchiver, "_safe_tar_members"))


async def level2_live():
    """Live API call  actually runs a Pi agent via Claude SDK."""
    print(bold("\n=== Level 2: Live Pi Call (costs ~$0.01) ===\n"))

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"  {yellow('SKIP')}  No ANTHROPIC_API_KEY set")
        return

    check("ANTHROPIC_API_KEY present", bool(api_key))

    from companest.memory import MemoryManager
    from companest.pi import Pi, PiConfig

    memory = MemoryManager(".companest")

    # Create a minimal Pi with haiku (cheapest)
    config = PiConfig(
        id="smoke-test",
        model="claude-haiku-4-5-20251001",
        tools=["memory_read"],
        max_turns=3,
    )
    pi = Pi(config, memory, team_id="stock")
    check("Pi created", pi is not None, f"model={pi.model}")

    try:
        result = await pi.run("Say 'smoke test passed' and nothing else.", timeout=30.0)
        check("Pi.run() returns result", bool(result), f"len={len(result)}")
        check("Pi response contains expected text",
              "smoke" in result.lower() or "test" in result.lower() or "passed" in result.lower(),
              f"got: {result[:100]}")
    except Exception as e:
        check("Pi.run() succeeds", False, str(e))

    # Test team-level execution
    from companest.config import CompanestConfig
    from companest.orchestrator import CompanestOrchestrator

    config = CompanestConfig.from_markdown(".companest/config.md")
    orch = CompanestOrchestrator(config)
    orch.init_teams(base_path=".companest")

    try:
        result = await orch.run_team(
            "Say 'team test passed' and nothing else.",
            "stock",
            skip_cost_check=True,
        )
        check("orchestrator.run_team() works", bool(result), f"len={len(result)}")
    except Exception as e:
        check("orchestrator.run_team() works", False, str(e))


async def level3_serve():
    """Start the API server and make HTTP requests."""
    print(bold("\n=== Level 3: API Server ===\n"))

    try:
        import httpx
        import uvicorn
    except ImportError:
        print(f"  {yellow('SKIP')}  httpx or uvicorn not installed")
        return

    from companest.config import CompanestConfig
    from companest.orchestrator import CompanestOrchestrator
    from companest.jobs import JobManager
    from companest.server import CompanestAPIServer
    from pathlib import Path

    config = CompanestConfig.from_markdown(".companest/config.md")
    config.api.port = 18765  # Use non-standard port for testing
    config.api.auth_token = "smoke-test-token"

    orch = CompanestOrchestrator(config)
    orch.init_teams(base_path=".companest")
    job_manager = JobManager(orch, data_dir=Path(".companest"))
    await job_manager.start()

    server = CompanestAPIServer(config, job_manager, orch)
    app = server.create_app()

    # Start server in background
    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=18765, log_level="error")
    uvi_server = uvicorn.Server(uvi_config)
    task = asyncio.create_task(uvi_server.serve())

    await asyncio.sleep(1)  # Wait for server to start

    headers = {"Authorization": "Bearer smoke-test-token"}
    base = "http://127.0.0.1:18765"

    async with httpx.AsyncClient() as client:
        # Health (no auth needed)
        r = await client.get(f"{base}/health")
        check("GET /health returns 200", r.status_code == 200)

        # Teams (auth required)
        r = await client.get(f"{base}/api/teams", headers=headers)
        check("GET /api/teams returns 200", r.status_code == 200)
        data = r.json()
        check("Teams list has entries", len(data.get("registered", [])) >= 5,
              f"got {data.get('registered', [])}")

        # Auth blocked without token
        r = await client.get(f"{base}/api/teams")
        check("GET /api/teams without auth  401", r.status_code == 401)

        # Finance summary
        r = await client.get(f"{base}/api/finance/summary", headers=headers)
        check("GET /api/finance/summary returns 200", r.status_code == 200)

        # Scheduler status
        r = await client.get(f"{base}/api/scheduler/status", headers=headers)
        check("GET /api/scheduler/status returns 200", r.status_code == 200)

    # Cleanup
    uvi_server.should_exit = True
    await task
    await job_manager.stop()


async def main():
    parser = argparse.ArgumentParser(description="Companest Smoke Test")
    parser.add_argument("--live", action="store_true", help="Level 2: Call Claude API (~$0.01)")
    parser.add_argument("--serve", action="store_true", help="Level 3: Start API server")
    args = parser.parse_args()

    level1_offline()

    if args.live or args.serve:
        await level2_live()

    if args.serve:
        await level3_serve()

    print(bold(f"\n{'='*40}"))
    total = passed + failed
    if failed == 0:
        print(green(f"  All {total} checks passed!"))
    else:
        print(f"  {green(f'{passed} passed')}, {red(f'{failed} failed')} / {total} total")
    print()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
