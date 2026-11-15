# Handoff: P4 (package layout) and P6 (final review)

Temporary. Delete the whole `handoff/` directory in the last commit of this work.

## Where things stand

Three stacked PRs, merge order #15 -> #16 -> #17:

| PR | Branch | Base | Content |
|---|---|---|---|
| #15 | `claude/repo-slimming-4k4ju0` | `main` | Slimming part 1 |
| #16 | `claude/langchain-runtime-4k4ju0` | #15 branch | LangChain runtime |
| #17 (draft) | `claude/multi-agent-orchestration-swap-4k4ju0` | #16 branch | Slimming part 2 |

All remaining work goes on `claude/multi-agent-orchestration-swap-4k4ju0` (PR #17). Do not rewrite history; add commits.

State at commit `b023a01`: `companest/` 14,625 lines in 50 files, `tests/` 5,256 lines, `pytest tests/ -q` gives 468 passed, 3 deselected.

Done on this branch: docstrings trimmed to one line, history docs and `requirements.txt` removed, dead code and unnecessary fallbacks removed in all eight source units, tests pruned.

Owner's standing instructions:

- Short and sharp repo. No narrative prose, no unnecessary fallback branches, no emoji in code.
- Leave gateway logic alone: `companest/client.py`, `companest/master.py`, `scripts/master_gateway.py`, `scripts/channel.py`, `tests/test_master.py` may be moved but not rewritten.
- Report after each step and push after each step.

## P4: group the flat package into subpackages

Goal: `companest/` goes from 33 top-level modules plus three subpackages to 6 modules plus 9 subpackages, and `scripts/` disappears.

Target layout (the move map is `handoff/layout.json`):

```
companest/
  cli.py  config.py  parser.py  exceptions.py  utils.py  events.py
  agents/         pi, team, tools, builtin_tools, git_tools, workspace, model_routing, cascade
  modes/          unchanged
  companies/      company, component, ceo_engine, output
  orchestration/  orchestrator, router, multi_team, background, cost_gate
  jobs/           jobs, scheduler, user_scheduler
  server/         server, event_store
  gateway/        client, master, server (was scripts/master_gateway.py), channel (was scripts/channel.py)
  memory/         existing files plus archiver
  knowledge/      unchanged
```

The owner has seen this draft and not objected. No compatibility shims at old paths.

### Steps

1. Apply the move map. The tool rewrites relative imports with `ast`, absolute dotted names (imports, `patch("companest.x.y")` targets, docs) with one regex pass, and file paths in text files, then runs `git mv` and creates empty `__init__.py` files:

   ```bash
   python handoff/restructure.py . handoff/layout.json --dry-run   # expect: moves: 27, files rewritten: about 54
   python handoff/restructure.py . handoff/layout.json
   ```

   The tool has only been dry-run, never applied. Expect to fix leftovers by hand.

2. Fix what the tool cannot know:
   - `companest/gateway/server.py` (the old `scripts/master_gateway.py`) imports its sibling as a top-level module (`from channel import ...`) and may have `sys.path` or `Path(__file__)` arithmetic. Change only the imports and paths needed to run it as `python -m companest.gateway.server`. It needs `websockets` and `python-telegram-bot`, so nothing reached by `import companest` may import it.
   - `deploy/docker-compose.yml` (`command: python scripts/master_gateway.py --port 19000`) and `infra/companest-ctl.sh` (`ExecStart=$PYTHON_BIN scripts/master_gateway.py --port 19000`) must start `-m companest.gateway.server` instead. The tool rewrites the path text, which produces a wrong command; correct it.
   - `companest/cli.py` has a hint string mentioning `scripts/master_gateway.py`.
   - `grep -rn "Path(__file__)" companest` and check each result still points where it should after the move.
   - Keep new `__init__.py` files empty so `import companest` does not start importing optional dependencies (fastapi, websockets, aiosqlite, apscheduler, boto3, qdrant_client, langchain).
   - `companest/__init__.py` re-exports must keep working: `from companest import X` for every name in `__all__`.
   - Update the path lists in `CLAUDE.md` and `AGENTS.md` (keep the two files aligned) and anything in `README.md`.

3. Split the catch-all `tests/test_design.py` (about 870 lines) into files named after the module under test (`tests/test_router.py`, `tests/test_cost_gate.py`, `tests/test_tools.py`, ...). Move tests only; do not change them. The collected test count must stay the same.

4. Verify, then commit and push:

   ```bash
   python -m compileall companest tests -q
   python -m flake8 companest tests --count --select=E9,F63,F7,F82      # must print 0
   python -m pytest tests/ -q -p no:cacheprovider                        # 468 passed, 3 deselected
   python -c "from companest import CompanestConfig, CompanestOrchestrator, EventBus"
   python -m companest validate examples/minimal-setup/.companest/config.md
   python -m companest validate deploy/config.prod.md
   python -m companest --help
   ```

   Also confirm the console script target in `pyproject.toml` (`companest = "companest.cli:main"`) is still right; `cli.py` does not move.

## P6: final review

Review the whole of PR #17 (`git diff claude/langchain-runtime-4k4ju0...HEAD`), not just P4.

### Known gaps to close

1. Four source units were merged without independent review (the usage limit cut the reviewers off). Review these commits with an adversarial eye, looking for behaviour changes:
   - `a25f072` server and jobs: `server.py`, `event_store.py`, `events.py`, `jobs.py`, `scheduler.py`, `user_scheduler.py`
   - `7ba50d4` team and modes: `team.py`, `modes/*`, `company.py`, `component.py`, `workspace.py`, `git_tools.py`
   - the "Slim orchestration" commit: `orchestrator.py`, `router.py`, `multi_team.py`, `background.py`, `ceo_engine.py`, `output.py`
   - the "Slim cli-config" commit: `cli.py`, `parser.py`, `config.py`, `exceptions.py`, `utils.py`

   For every removed conditional, try/except, default or function, try to build a concrete case where behaviour now differs: a config file or manifest under `examples/` or `deploy/`, an HTTP request, a CLI call, an on-disk file from an older run, an orchestrator not fully initialised. Restore anything you cannot prove safe.

2. Things the authors themselves flagged:
   - `server.py` `get_company` / `fleet_status` now assume `init_teams()` set `team_registry`, `scheduler` and `cost_gate`.
   - `orchestrator.py` lost its `hasattr` guards; a half-initialised orchestrator now raises `AttributeError`. Public methods with no in-repo caller were deleted: `register_team`, `register_enrichment`, `register_enrichment_cycle`, `register_output_sink`, `on_event`, `register_tools`, `from_config_file`, `discover_and_create`.
   - `SmartRouter._emit_routing_audit` lost its catch-all.
   - `jobs.py`: with `resume_job` gone, a job dequeued while already RUNNING raises `JobError` in the worker.
   - Pre-existing bug left in place: in `_init_company` the `memory_seed` loop rebinds `team_id`, so the CEO Pi can be created with the last seed team id.

3. Merge conflicts resolved by hand, not reviewed by anyone else: two in `orchestrator.py`, and the test files `test_design.py`, `test_feeds.py`, `test_cascade.py`, `test_memory_cache.py`, `test_memory_backend_safety.py`, `test_coding.py`, `test_info_collection.py`, `test_model_routing.py`. In the tests, the pruned version was kept, ten tests of deleted code were removed, and `mcp__<server>__<tool>` assertions were rewritten to plain tool names with `sed`. Check no kept test was weakened by that.

4. Security checks to confirm still hold: the auth middleware and `COMPANEST_API_TOKEN` / `COMPANEST_MASTER_TOKEN` handling in `server.py`; `tools_deny` (including entries written in the old `mcp__<server>__<tool>` form); workspace path confinement in `builtin_tools.py`; company access checks (`can_access_team`, sessions tools).

### Decisions waiting on the owner

- Public knowledge write path (`PublicKnowledgePublisher`, `IndexStore.add_entry` / `remove_entry` / `save_index`, `put_doc` / `delete_doc`) has no in-repo caller. Kept. Delete only if the owner says so.
- `Dreamer._write_dream_log` was deleted instead of fixed (it never wrote a file because of a wrong attribute name).
- `MemoryArchiver.restore_snapshot` was deleted as uncalled; S3 backups have no restore helper.
- `test_priority_multiplier[low]` was deleted because it could not fail.

### Not verified so far

- No call to a real model provider or real S3.
- The Docker image has not been built since the Dockerfile changed to install from pyproject extras.
- CI has only run on PR #15.

### Finish

- Delete `handoff/`.
- Mark PR #17 ready for review and update its checklist.
