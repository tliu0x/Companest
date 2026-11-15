# AGENTS.md

## Scope

- This is the root agent guide for this repository. If a deeper directory adds its own `AGENTS.md`, the deeper file takes precedence for that subtree.
- `AGENTS.md` is the single repository guide for coding agents. Do not maintain duplicate agent-specific guides.

## Project Snapshot

- Companest is a multi-company control plane for agent-native teams, jobs, schedules, memory, and company-scoped execution.
- The codebase mixes CLI orchestration, API serving, background execution, company registration, team routing, memory backends, and public knowledge tooling.
- Optional extras in `pyproject.toml` gate major capabilities such as server mode, scheduler support, gateway mode, and provider integrations. Memory uses FileBackend and keyword retrieval without an external index service.

## Start Here

- Unit responsibilities, module index and state ownership: `companest/README.md`
- Every job and schedule: `companest/jobs/README.md`

- CLI entry and operator workflows: `companest/cli.py`
- API serving: `companest/server/server.py`
- Configuration and discovery: `companest/config.py`, `README.md`, `examples/minimal-setup/.companest/config.md`
- Teams, Pis, routing, and execution modes: `companest/agents/team.py`, `companest/agents/pi.py`, `companest/orchestration/router.py`, `companest/modes/`, `companest/orchestration/orchestrator.py`, `companest/orchestration/multi_team.py`
- Jobs, background work, and scheduling: `companest/jobs/jobs.py`, `companest/orchestration/background.py`, `companest/jobs/scheduler.py`, `companest/jobs/user_scheduler.py`
- Memory: `companest/memory/`
- Feeds, public knowledge, and digests: `companest/knowledge/`
- Company onboarding and public examples: `examples/`, `deploy/config.prod.md`

## Execution Boundary

- `companest/runtime/` contains the Substrate transport, actor workload and in-actor harness.
- All control-plane task entry points use `JobManager`; do not add a local execution fallback or queue.
- TeamRegistry holds declarations, not live instances. Runtime state belongs to Substrate.
- Shared business storage and job capabilities are documented in `deploy/substrate/README.md` and `storage.md`.
- Only default/collaborative/council are execution modes. Cascade is a model strategy; decomposition/delegation are bounded business workflows.

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
- For CLI or API behavior changes, read `companest/cli.py`, `companest/server/server.py`, and the relevant tests before editing.
- For team-mode behavior changes, inspect the matching file in `companest/modes/` and the paired tests in `tests/test_modes.py`, `tests/test_team_mode.py`, and nearby mode-specific tests.
- For memory or knowledge (feeds, digests, public knowledge) changes, run the focused tests such as `tests/test_memory_*`, `tests/test_feeds.py`, `tests/test_digests.py`, and `tests/test_public_knowledge.py`.
- Keep changes scoped. Do not mix provider, scheduler, and API refactors unless the task truly spans them.
- Update the affected unit README when responsibilities, data boundaries, or modules change; update `companest/jobs/README.md` when adding or changing a job.
- Keep execution fields in snake_case end to end: `job_id`, `schedule_id`, `company_id`, `team_id`, `workspace_id`, and `model`. Do not translate them into `id`/`target_team`/`target_team_id`/`target_model` at API or actor boundaries.
- Create each ID once and pass its value unchanged. Reuse the canonical `team_id` across routing, tools, files and retrieval; do not rewrite it into a second tenant identifier. Migrate old persisted schemas once instead of maintaining runtime aliases.

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
- Keep migration/fault-injection simulation scripts outside the repository when requested; use existing behavioral contracts instead of adding duplicate wrapper tests. See `tests/README.md`.
