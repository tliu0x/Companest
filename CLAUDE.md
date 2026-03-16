# CLAUDE.md -- Agent Guidelines for Companest

## Project Overview

Companest is a multi-agent company incubator and control plane. It runs shared teams and company-private teams in one runtime, routes work across teams, manages background execution (jobs, scheduler), and exposes a REST API for multi-company operation.

## Tech Stack

- **Python 3.10+**
- **Pydantic** >= 2.0 -- configuration and data validation
- **httpx** -- async HTTP client
- **FastAPI** + **uvicorn** -- REST API server (optional `[server]` extra)
- **anthropic** + **claude-agent-sdk** -- Claude backend (optional `[claude]` extra)
- **openai-agents** -- OpenAI backend (optional `[openai]` extra)
- **APScheduler** + **SQLAlchemy** + **aiosqlite** -- scheduler (optional `[scheduler]` extra)
- **qdrant-client** + **fastembed** -- vector memory (optional `[qdrant]` extra)
- **boto3** -- S3 archiver (optional `[s3]` extra)
- **websockets** -- gateway mode (optional `[gateway]` extra)
- **PyYAML** -- config parsing
- **defusedxml** -- safe XML handling

## Architecture

- **Orchestrator** (`orchestrator.py`): Central runtime that loads config, boots teams, manages company lifecycle, and coordinates execution.
- **Teams** (`team.py`): Groups of Pis (agents) with execution modes (default, cascade, loop, council, collaborative, conditional).
- **Pis** (`pi.py`): Individual agents with soul files, model bindings, and tool access.
- **Company isolation** (`company.py`): Multi-tenant scoping for jobs, routing, schedules, memory, and tools.
- **Model routing** (`model_routing.py`, `router.py`): Smart routing across providers (DeepSeek, Moonshot, Anthropic, OpenAI, LiteLLM).
- **Jobs** (`jobs.py`): Background job execution with lifecycle management.
- **Scheduler** (`scheduler.py`, `user_scheduler.py`): Cron-style task scheduling per company.
- **Server** (`server.py`): FastAPI REST API for fleet management, company CRUD, job submission.
- **Tools** (`tools.py`): Tool registry with company-scoped MCP tool support.
- **Config** (`config.py`, `parser.py`): Markdown and JSON config loading with env var interpolation.

## Key Files

| File | Role |
|------|------|
| `companest/orchestrator.py` | Central runtime, boots teams and companies |
| `companest/team.py` | Team config, registry, execution modes |
| `companest/pi.py` | Individual agent (Pi) config and execution |
| `companest/company.py` | Multi-tenant company isolation |
| `companest/server.py` | FastAPI REST API |
| `companest/router.py` | Smart model routing |
| `companest/model_routing.py` | Provider bindings and model selection |
| `companest/jobs.py` | Background job manager |
| `companest/scheduler.py` | APScheduler integration |
| `companest/tools.py` | Tool registry and MCP tool support |
| `companest/config.py` | Config loading and validation |
| `companest/parser.py` | Markdown/JSON config parser |
| `companest/cli.py` | CLI entry point |
| `companest/feeds.py` | Market data feeds (Polymarket, Kalshi, Metaculus) |
| `companest/cost_gate.py` | Cost tracking and budget enforcement |
| `companest/archiver.py` | S3/local memory archiving |
| `companest/workspace.py` | Workspace management |

## Code Style

- Follow existing patterns in the codebase.
- Use type hints on all function signatures.
- Use `async`/`await` for all I/O operations.
- Pydantic models for configuration and data shapes.
- Keep modules focused -- one responsibility per file.
- Use `defusedxml` instead of stdlib XML parsers.

## Testing

```bash
# Run all tests (excludes live_network by default)
pytest tests/ -q

# Run live network tests (calls real external services)
pytest -m live_network -q

# Syntax check
python -m compileall companest tests -q
```

- Framework: `pytest` + `pytest-asyncio`
- Tests in `tests/`, marker `live_network` for tests that hit real APIs.
- Mock external services in non-live tests.
- See `pyproject.toml` `[tool.pytest.ini_options]` for default markers.

## Commit Rules

- **Commit messages must be in English. No Chinese characters in commit messages.**
- Explanatory comments in code can be in any language.
- Use conventional commit style: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
- Keep commits focused -- one logical change per commit.
- See `CONTRIBUTING.md` for PR expectations.

## Security Rules

- **Never commit personal information**: no real API keys, tokens, credentials, or email addresses.
- Use placeholders in examples and documentation.
- Secrets belong in `.env` (gitignored) or environment variables, never in source code or config files checked into git.
- Company-scoped data must respect isolation boundaries -- a company's jobs, memory, routing, and tools must not leak to other companies.
- Use `defusedxml` for any XML parsing to prevent XXE attacks.

## Config System

- Config discovery order: `.companest/config.md` -> `.companest/config.json` -> `companest.config.md` -> `companest.config.json`
- Environment variable interpolation: `${VAR}` and `${VAR:-default}`
- Team definitions live in `.companest/teams/<team_id>/team.md`
- Pi (agent) definitions live in `.companest/teams/<team_id>/pis/<pi_id>/soul.md`
- Company-private teams live under `.companest/companies/<company_id>/teams/`

## Multi-Company Model

- Companies register via manifest (`POST /api/companies`)
- All resources (jobs, schedules, memory, tools) are scoped by `company_id`
- Global teams are shared; company-private teams are isolated
- The `sessions_*` tools (send/history/list) are for team-to-team messaging within the platform
