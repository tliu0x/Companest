# Alpha 0.1

Release date: 2026-03-13

This release captures the work completed since [`alpha_0.0`](../../README.md).

## Summary

Companest has moved from a public alpha bootstrap into a usable multi-company control plane.

Since `alpha_0.0`, the project gained:

- company registration through the HTTP API
- immediate company apply, update, and teardown at runtime
- company-scoped jobs, schedules, routing, memory, and MCP tools
- stronger tenant isolation for multi-company usage
- prediction market feed adapters and a full example extension
- onboarding documentation and reusable company templates
- broader test coverage and a passing full test suite

## What Changed Since Alpha 0.0

### 1. Multi-company control plane support

Companest can now manage multiple companies in one runtime instead of only acting as a local orchestration framework.

Added:

- richer `CompanyConfig` fields such as `shared_teams`, `routing_bindings`, `memory_seed`, and `mcp_servers`
- immediate runtime application through company lifecycle methods
- company CRUD and inspection through the API
- company-scoped runtime cleanup for updates and deletes

Result:

- external company repos can register by manifest and start running immediately

### 2. Company-scoped scheduling and background execution

Company schedules are now fully connected to the runtime scheduler.

Added:

- company schedule registration during company initialization
- scheduler task scope metadata
- cleanup of company-owned tasks during teardown and hot reload

Result:

- companies can define background jobs in their manifests and have them run predictably

### 3. Private team runtime fixes

We fixed the mismatch between scanned company team directories and runtime team file resolution.

Added:

- team path overrides in memory management
- local team ID tracking during company team scanning
- explicit company team unregister behavior

Result:

- private teams under `companies/{id}/teams/` now load and clean up correctly

### 4. Routing, access control, and tenant isolation

We tightened company isolation across routing and team visibility.

Added:

- company-owned routing bindings
- cleanup of bindings by owning company
- company-aware shared-team allowlists
- stricter access checks for company-private teams

Result:

- companies no longer inherit broad implicit access to global or private teams

### 5. Company-aware jobs and company job history

Job execution now keeps company scope from submission through persistence and runtime execution.

Added:

- `company_id` in the job model and SQLite schema
- normalization of `company_id` into execution context
- company-scoped job history queries
- richer company detail responses that include recent job activity

Result:

- jobs can be audited and queried by company, and execution stays aligned with company scope

### 6. Company-scoped MCP and tool isolation

We added scoped MCP registration so company-private tools do not leak across tenants.

Added:

- company-scoped MCP registration and lookup
- company-aware tool context propagation
- cleanup of scoped MCP registrations during teardown

Result:

- a company only sees its own scoped MCP servers plus globally shared ones

### 7. Sessions tool security hardening

We closed a remaining gap in cross-team messaging.

Added:

- access checks in `sessions_send`
- access checks in `sessions_history`
- filtered visibility in `sessions_list`
- company and shared-team context in fallback tool creation paths

Result:

- sessions tooling now respects company boundaries and shared-team policy

### 8. Safer manifest handling and API validation

We tightened inline manifest validation before persistence.

Added:

- validation of inline team IDs and Pi IDs before writing files
- better update behavior for inline teams during `PATCH`
- cleaner internal error handling for API responses

Result:

- invalid manifests fail earlier and do not partially persist company state

### 9. Runtime stability improvements

We hardened the server for longer-lived operation.

Added:

- safer hot-reload teardown behavior
- protection around team reload and unregister while tasks are active
- recovery for interrupted jobs across restart
- bounded in-memory job retention
- WebSocket subscriber limits
- scheduler task timeouts
- Python 3.12 test compatibility support

Result:

- the runtime is safer under reload, shutdown, and sustained background usage

### 10. Prediction market support

We added shared prediction market feed adapters without baking vertical company logic into the core runtime.

Added to core:

- `fetch_polymarket`
- `fetch_kalshi`
- `fetch_metaculus`
- corresponding tool definitions

Added as examples:

- `examples/prediction-market/manifest.json`
- `examples/prediction-market/register.py`
- `examples/prediction-market/README.md`

Result:

- the platform can ingest prediction market data, and external repos have a full extension example to follow

### 11. Documentation and onboarding

We expanded the documentation from a local demo into a real onboarding path.

Added or updated:

- root `README.md`
- `examples/README.md`
- `examples/company-onboarding.md`
- `examples/company-template/README.md`
- `examples/company-template/manifest.json`
- `examples/company-template/register.py`
- `deploy/config.prod.md`

Result:

- new adopters now have a clear path from local demo to external company registration

### 12. Testing and validation

We added coverage for the new platform behavior and security boundaries.

Added or expanded:

- company API flow tests
- component priority tests
- company design and scoping tests
- new prediction market feed tests
- sessions security tests
- test compatibility helpers for Python 3.12

Validation at release time:

- `py -3.12 -m compileall companest tests -q`
- `python -m pytest -q`
- result: `599 passed, 3 deselected`

## Upgrade Notes

- `shared_teams` should now be treated as an explicit allowlist for access to global teams
- `POST /api/jobs` should include top-level `company_id` for company-scoped work
- `PATCH /api/companies/{id}` behaves more like an authoritative update when inline teams are included
- sessions tools now enforce company boundaries more strictly than before

## Tag

Recommended tag: `alpha_0.1`
