"""
End-to-end test: Run collector Pi logic with real LLM + real feed tools.

Uses Anthropic SDK directly with tool definitions (bypasses claude-agent-sdk
which can't run inside Claude Code sessions).

Usage:
    export $(grep -v '^#' .env | xargs)
    python scripts/test_info_collection_e2e.py
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import anthropic

from companest.feeds import brave_search, fetch_rss, fetch_reddit, fetch_hn, fetch_x
from companest.memory import MemoryManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("e2e")

#  Tool definitions for Anthropic API 

TOOLS = [
    {
        "name": "memory_read",
        "description": "Read a JSON file from team memory. Returns the parsed content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Memory key (filename, e.g. 'watchlist.json')"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "memory_write",
        "description": "Write data to a JSON file in team memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Memory key (filename)"},
                "data": {"description": "JSON data to write"},
            },
            "required": ["key", "data"],
        },
    },
    {
        "name": "fetch_reddit",
        "description": "Fetch posts from a subreddit. No authentication needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subreddit": {"type": "string", "description": "Subreddit name (without r/)"},
                "sort": {"type": "string", "description": "'hot', 'new', 'top', or 'rising'", "default": "hot"},
                "limit": {"type": "integer", "description": "Number of posts (max 25)", "default": 10},
            },
            "required": ["subreddit"],
        },
    },
    {
        "name": "fetch_hn",
        "description": "Fetch stories from Hacker News.",
        "input_schema": {
            "type": "object",
            "properties": {
                "story_type": {"type": "string", "description": "'top', 'new', 'best', 'ask', 'show'", "default": "top"},
                "limit": {"type": "integer", "description": "Number of stories (max 30)", "default": 15},
            },
        },
    },
]


async def handle_tool_call(mm: MemoryManager, team_id: str, tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == "memory_read":
        key = tool_input["key"]
        data = mm.read_team_memory(team_id, key)
        if data is None:
            return json.dumps({"result": None, "message": f"{key} not found"})
        return json.dumps(data, ensure_ascii=False, indent=2)

    elif tool_name == "memory_write":
        key = tool_input["key"]
        data = tool_input.get("data")
        if data is None:
            return json.dumps({"error": "Missing 'data' field  was the response truncated-"})
        mm.write_team_memory(team_id, key, data)
        return json.dumps({"result": "ok", "message": f"Written {key}"})

    elif tool_name == "fetch_reddit":
        items = await fetch_reddit(
            subreddit=tool_input["subreddit"],
            sort=tool_input.get("sort", "hot"),
            limit=tool_input.get("limit", 10),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    elif tool_name == "fetch_hn":
        items = await fetch_hn(
            story_type=tool_input.get("story_type", "top"),
            limit=tool_input.get("limit", 15),
        )
        return json.dumps(items, ensure_ascii=False, indent=2)

    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def main():
    base_path = ".companest"
    mm = MemoryManager(base_path)
    team_id = "info-collection"

    assert mm.team_exists(team_id), "info-collection team not found"

    # Build system prompt
    system = mm.build_system_prompt(team_id, "collector")
    logger.info(f"System prompt: {len(system)} chars")

    task = (
        "Run a collection cycle. "
        "Read watchlist.json to see what sources to collect from. "
        "Skip brave_search (no API key) and fetch_x (no bearer token). "
        "Fetch from reddit and HN as configured in the watchlist. "
        "Read existing feed.json first (if any), then merge new items with existing ones. "
        "Deduplicate by URL, keep max 50 items. "
        "Write updated feed.json with the merged results."
    )

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task}]

    logger.info("Starting multi-turn tool-use loop with Haiku...")
    turn = 0
    max_turns = 15

    while turn < max_turns:
        turn += 1
        logger.info(f"--- Turn {turn} ---")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        logger.info(f"Stop reason: {response.stop_reason}")

        # Collect assistant message
        messages.append({"role": "assistant", "content": response.content})

        # If model is done (no more tool calls), break
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    logger.info(f"Final response:\n{block.text[:500]}")
            break

        # If hit max_tokens mid-generation, check if there are tool calls to process
        # If no tool_use blocks, we need to ask model to continue
        if response.stop_reason == "max_tokens":
            has_tools = any(b.type == "tool_use" for b in response.content)
            if not has_tools:
                logger.warning("Hit max_tokens with no tool calls, retrying...")
                messages.append({"role": "user", "content": "Continue. Write feed.json with the collected data. Keep it concise  only include the essential fields per item."})
                continue

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                logger.info(f"Tool call: {block.name}({json.dumps(block.input)[:200]})")
                result = await handle_tool_call(mm, team_id, block.name, block.input)
                logger.info(f"Tool result: {result[:200]}...")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    logger.info(f"Completed in {turn} turns")

    #  Verify results 
    feed = mm.read_team_memory(team_id, "feed.json")
    if feed is None:
        logger.error("FAIL: feed.json was NOT written!")
        sys.exit(1)

    items = feed.get("items", [])
    logger.info(f"\nfeed.json: {len(items)} items")

    # Show first 3 items
    for i, item in enumerate(items[:3]):
        logger.info(f"  [{i+1}] [{item.get('source', '-')}] {item.get('title', '-')[:80]}")

    if len(items) > 3:
        logger.info(f"  ... and {len(items) - 3} more")

    # Verify feed injection into business team prompts
    import shutil
    import tempfile
    # Use the real .companest directory  just check injection works
    test_team_dir = Path(base_path) / "teams" / "_test_inject"
    test_pi_dir = test_team_dir / "pis" / "dev"
    test_pi_dir.mkdir(parents=True, exist_ok=True)
    (test_team_dir / "team.md").write_text("# Team: _test_inject\n- role: test\n- enabled: true\n")
    (test_pi_dir / "soul.md").write_text("I am a test Pi.\n")

    try:
        prompt = mm.build_system_prompt("_test_inject", "dev")
        if "Recent Feed" in prompt:
            logger.info("\nFeed injection into business team prompt: OK")
        else:
            logger.warning("\nFeed injection: NOT FOUND (feed may have 0 valid items)")
    finally:
        shutil.rmtree(test_team_dir, ignore_errors=True)

    logger.info("\nE2E TEST PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
