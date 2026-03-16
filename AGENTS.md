# AGENTS.md

## Scope

- This is the root agent guide for this repository. If a deeper directory adds its own `AGENTS.md`, the deeper file takes precedence for that subtree.
- There is currently no root `CLAUDE.md` here. If one is added later, keep it aligned with this file so Codex and Claude see the same core project knowledge.

## Project Snapshot

- Companest is a multi-company control plane for agent-native teams, jobs, schedules, memory, and company-scoped execution.
- The codebase mixes CLI orchestration, API serving, background execution, company registration, team routing, memory backends, and public knowledge tooling.
- Optional extras in `pyproject.toml` gate major capabilities such as server mode, scheduler support, gateway mode, Qdrant, UI, and provider integrations.

## Start Here

- CLI entry and operator workflows: `companest/cli.py`
- App and API serving: `companest/app.py`, `companest/server.py`, `companest/admin.py`
- Configuration and discovery: `companest/config.py`, `README.md`, `examples/minimal-setup/.companest/config.md`
- Teams, Pis, routing, and execution modes: `companest/team.py`, `companest/pi.py`, `companest/router.py`, `companest/modes/`, `companest/orchestrator.py`, `companest/multi_team.py`
- Jobs, background work, and scheduling: `companest/jobs.py`, `companest/background.py`, `companest/scheduler.py`, `companest/user_scheduler.py`, `companest/watcher.py`
- Memory, digests, and public knowledge: `companest/memory/`, `companest/digests/`, `companest/public_knowledge/`
- Company onboarding and public examples: `examples/`, `deploy/config.prod.md`, `docs/`

## Source Of Truth

- Config discovery order is documented in `README.md` and implemented in the configuration layer. Preserve backward compatibility unless the change is deliberate and documented.
- Treat `companest/config.py` as canonical for config structure and environment resolution.
- If a feature depends on an optional extra, update `pyproject.toml`, docs, and any validation steps together.

## Common Commands

```bash
pip install -e ".[dev]"
companest validate
companest serve
pytest tests/ -q
pytest -m live_network -q
python -m compileall companest tests -q
```

## Working Rules

- Keep examples runnable. If you change config shape, manifests, or onboarding flow, update `README.md`, `examples/`, and `deploy/config.prod.md` in the same change.
- For CLI or API behavior changes, read `companest/cli.py`, `companest/app.py`, `companest/server.py`, and the relevant tests before editing.
- For team-mode behavior changes, inspect the matching file in `companest/modes/` and the paired tests in `tests/test_modes.py`, `tests/test_team_mode.py`, and nearby mode-specific tests.
- For memory, digest, or public-knowledge changes, run the focused tests such as `tests/test_memory_*`, `tests/test_digests.py`, and `tests/test_public_knowledge.py`.
- Keep changes scoped. Do not mix provider, scheduler, and API refactors unless the task truly spans them.

## Code Conventions

- Follow existing module style; this repo contains both synchronous CLI code and async/network-facing code.
- Prefer `pathlib.Path` and centralized config parsing over scattered path or env handling.
- Keep public interfaces stable where possible because examples and external company manifests depend on them.
- When adding new config or runtime behavior, favor explicit validation and clear operator-facing errors.

## Security And Privacy

- Never hardcode API keys, bearer tokens, SSH CIDRs, LiteLLM keys, or provider credentials.
- Production auth defaults matter. Do not weaken `COMPANEST_API_TOKEN`, `COMPANEST_MASTER_TOKEN`, or shell/tool restrictions without updating docs and tests.
- Review `SECURITY.md` before changing deployment or gateway behavior.
- Use placeholders like `<API_TOKEN>`, `<SECRET>`, and `<CIDR>` in docs and examples.

## Validation Expectations

- Run targeted tests for the area you touched, then `pytest tests/ -q` for broader confidence.
- Run `pytest -m live_network -q` only when intentionally validating real external integrations.
- Run `python -m compileall companest tests -q` after wide Python changes to catch import and syntax issues.
