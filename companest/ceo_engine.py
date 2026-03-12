"""
Companest CEO Engine

Structured operating cycle for company CEO agents.
Replaces the one-line cycle_prompt with a 4-step operational loop:
1. Status check (read memory)
2. Analysis & decision
3. Execute (dispatch to teams)
4. Record (write back to memory)
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


CEO_CYCLE_PROMPT_TEMPLATE = """\
## Operating Cycle #{cycle_number}

### Step 1: Status Check
1. Use memory_read("status-board.json") to read the current status board
2. Use memory_read("todo-list.json") to read the to-do list
3. Use memory_read("cycle-results.json") to review last cycle's results

### Step 2: Analysis & Decision
Based on company goals and current status, decide on this cycle's actions:
- Unfinished high-priority tasks- -> Follow up on execution
- Need new data collection- -> Dispatch to data team
- Results pending analysis- -> Dispatch to analysis team
- Everything normal- -> Record "no anomalies"

### Step 3: Execute
Use run_team(team_id, task) or run_auto(task) to dispatch tasks.
Available teams:
{available_teams_section}

### Step 4: Record
1. memory_write("status-board.json", ...) to update the status board
2. memory_write("cycle-log.json", ...) to append this cycle's log
3. If important findings, write to memory_write("findings.json", ...)

### Output Format
Output this cycle's summary as JSON:
{{"cycle": {cycle_number}, "actions_taken": [...], "findings": [...], "next_priority": "..."}}
"""


def build_cycle_prompt(
    available_teams: List[str],
    cycle_number: int = 1,
) -> str:
    """Build a structured CEO cycle prompt."""
    if available_teams:
        teams_section = "\n".join(f"- {t}" for t in available_teams)
    else:
        teams_section = "- (use run_auto to let the router decide)"

    return CEO_CYCLE_PROMPT_TEMPLATE.format(
        cycle_number=cycle_number,
        available_teams_section=teams_section,
    )


def generate_ceo_soul(
    company_name: str,
    domain: str,
    goals: Optional[List[str]] = None,
    kpis: Optional[Dict[str, str]] = None,
    available_teams: Optional[List[str]] = None,
) -> str:
    """Generate a CEO soul.md with goals, KPIs, and available teams."""
    parts = [
        f"# CEO Agent  {company_name}",
        "",
        f"You are the autonomous operating CEO of {company_name}. "
        "You do not wait for user instructions  you proactively check status, make decisions, and dispatch tasks.",
    ]

    if domain:
        parts.append(f"\n## Domain Knowledge\n{domain}")

    if goals:
        parts.append("\n## Company Goals")
        parts.extend(f"- {g}" for g in goals)

    if kpis:
        parts.append("\n## Key Performance Indicators (KPIs)")
        for name, desc in kpis.items():
            parts.append(f"- **{name}**: {desc}")

    if available_teams:
        parts.append("\n## Available Teams")
        parts.extend(f"- {t}" for t in available_teams)

    parts.append(
        "\n## Operating Principles\n"
        "1. Each cycle: read memory first, understand current state before acting\n"
        "2. Prioritize actions based on goal priority\n"
        "3. Dispatch tasks to teams  do not perform execution-level work yourself\n"
        "4. All decisions and results must be written back to memory\n"
        "5. Cost-conscious: prefer cascade mode"
    )

    return "\n".join(parts)
