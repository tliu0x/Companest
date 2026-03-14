# Companest Release Notes Draft

## Title

Platform Readiness: Multi-Company Registration, Runtime Lifecycle, Scoped Tooling, and Prediction Market Example

## Summary

This release turns Companest into a practical control plane for external company repos.

The core repo now supports:

- company registration and updates through the HTTP API
- immediate company apply and teardown without waiting for file polling
- company-scoped teams, schedules, routing bindings, memory, jobs, and MCP servers
- stronger tenant isolation across routing, jobs, memory access, and sessions tools
- background operation with safer hot-reload, scheduler cleanup, and job recovery
- a documented onboarding path plus a reusable company template
- a complete example extension for prediction market monitoring

In short: Companest is no longer just a local orchestration framework. It now behaves like a shared control plane that multiple companies can register into, run on, and cleanly remove themselves from.

## What Changed

### 1. Company registration became a real runtime workflow

We completed the path from "company config exists on disk" to "company can register and start running immediately."

Changes in this area:

- Expanded `CompanyConfig` to support:
  - `shared_teams`
  - `routing_bindings`
  - `memory_seed`
  - `mcp_servers`
- Added immediate company lifecycle methods in the orchestrator:
  - `apply_company()`
  - `teardown_company()`
- Updated the API so `POST /api/companies` and `PATCH /api/companies/{id}` apply changes immediately instead of waiting for watcher-based reload.
- Updated `DELETE /api/companies/{id}` to perform full runtime cleanup before removing persisted company state.
- Preserved component-backed companies during rescans so hot-reload does not accidentally drop them or let YAML override registered components.

Impact:

- External company repos can register through the API and start using the system immediately.
- Updates take effect right away.
- Deletion removes runtime resources instead of leaving stale scheduler tasks, bindings, or tools behind.

### 2. Company schedules now actually run

Before this release, company schedule models existed but were not truly wired into the runtime scheduler.

Changes in this area:

- Registered `company.schedules` into the scheduler during company initialization.
- Added scheduler scoping metadata so tasks can be tracked and removed by company.
- Cleaned up company scheduler tasks during delete and hot-reload teardown.
- Exposed company-specific scheduler state through the company detail API.

Impact:

- Companies can define background jobs in their manifest and have them actually run.
- Background cycles are visible and removable as company-scoped runtime resources.

### 3. Private company teams are now physically and logically aligned

There was a path mismatch between how company teams were discovered and how their runtime files were loaded.

Changes in this area:

- Added team path overrides to `MemoryManager` so namespaced teams can resolve to their actual on-disk company team directories.
- Recorded local team IDs during company team scanning.
- Registered and removed team path overrides during company apply and teardown.
- Added explicit company team unregister logic in the team registry.

Impact:

- Private teams loaded from `companies/{id}/teams/...` now resolve their souls and memory correctly.
- Company teardown fully removes private team runtime state.

### 4. Multi-tenant routing and access control were tightened

This release moved company scoping from "best effort" to explicit runtime behavior.

Changes in this area:

- Added router ownership metadata to company routing bindings.
- Added router cleanup by owning company.
- Updated company access rules so team visibility is scoped to:
  - the company's own private teams
  - explicitly allowed shared global teams
- Passed `shared_teams` into routing decisions instead of only storing it in config.
- Added company-aware filtering to available-team selection.

Impact:

- Companies can no longer rely on broad implicit visibility into global teams.
- Company-owned bindings are cleaned up correctly on update and delete.

### 5. Jobs became company-aware end to end

Job execution already understood `company_id` in context, but the operational model was incomplete. This release finished that path.

Changes in this area:

- Added `company_id` to the job model and SQLite schema.
- Added migration support for existing job databases.
- Standardized `company_id` handling in `submit()`:
  - top-level `company_id` is accepted
  - context and top-level values must match if both are provided
  - `company_id` is normalized into execution context automatically
- Added company-aware job filtering and history queries.
- Added `GET /api/companies/{id}/jobs`.
- Expanded company detail and fleet status responses with company-aware job and schedule information.

Impact:

- Jobs can now be indexed, queried, and audited by company.
- A company-scoped API request actually remains company-scoped during execution.

### 6. Company-scoped MCP/tool isolation was added

External MCP server registration previously behaved like a global capability, which was unsafe for multi-company usage.

Changes in this area:

- Added company-scoped MCP registration in `ToolRegistry`.
- Added filtering so a Pi only sees:
  - global MCP servers
  - MCP servers registered for its own company
- Added teardown cleanup for company-scoped MCP registrations.
- Passed `company_id` through `ToolContext` and into Pi execution paths.

Impact:

- Company-private tools no longer leak to unrelated companies.
- Removing a company also removes its scoped MCP servers.

### 7. Sessions tooling now respects tenant boundaries

This was one of the last security-sensitive areas to close.

Changes in this area:

- Tightened `sessions_send`, `sessions_history`, and `sessions_list` access checks.
- Denied access to other companies' private teams even when the caller had no company context.
- Enforced `shared_teams` rules for global team visibility in sessions tooling.
- Passed company and shared-team context into both the main tool registry path and Pi fallback sessions-tool creation paths.

Impact:

- A team can no longer use sessions tooling to inspect or message another company's private inbox.
- Tool-layer behavior is now aligned with orchestrator-level access control.

### 8. Inline manifest validation and company update behavior were hardened

The API could previously save a company before discovering invalid inline team or Pi IDs.

Changes in this area:

- Added pre-save validation for inline team IDs and Pi IDs using the safe ID pattern.
- Applied this validation to both create and update paths.
- Prevented stale inline team directories from surviving when a PATCH payload replaces the team list.
- Kept server-side logging for internal errors while returning generic 500 responses to clients.

Impact:

- Bad payloads fail cleanly without partial persistence.
- PATCH behaves more like an authoritative manifest update for inline teams.

### 9. Runtime stability improved under reload and shutdown

This release also tightened the runtime around long-lived operation.

Changes in this area:

- Safer hot-reload teardown for company-scoped resources.
- Company teardown now removes:
  - scheduler tasks
  - routing bindings
  - enrichments
  - private teams
  - team path overrides
  - company-scoped MCP servers
- Team reload/unregister behavior now respects active task counts.
- Job manager now recovers interrupted jobs across restart.
- In-memory job retention is bounded instead of growing forever.
- Added a WebSocket subscriber limit.
- Added scheduler task timeouts.
- Added a Python 3.12 event-loop compatibility test shim.

Impact:

- The server is safer to run continuously.
- Hot-reload and shutdown are less likely to leave orphaned state behind.

### 10. Prediction market support was added as a reusable extension path

We deliberately kept vertical business logic out of the core runtime while still adding useful shared data adapters.

Core additions:

- Added new feed adapters:
  - `fetch_polymarket`
  - `fetch_kalshi`
  - `fetch_metaculus`
- Registered these sources in the feed registry.
- Added corresponding tool definitions so any team can use them.

Extension/example additions:

- Added `examples/prediction-market/` as a full reference company.
- Included:
  - a manifest
  - inline private teams
  - analyst and collector personas
  - a background market collection schedule
  - routing bindings
  - seeded watchlist memory
  - a registration script

Impact:

- The core platform gains reusable prediction-market data access.
- Teams and personas remain extension-level, which preserves the platform/vertical boundary.

### 11. Onboarding and examples were upgraded

This release includes material for external adopters, not just framework internals.

Added:

- `examples/company-onboarding.md`
- `examples/company-template/`
- `examples/prediction-market/`

Updated:

- root `README.md`
- `examples/README.md`

Impact:

- External company repos now have a documented path to integration.
- New adopters can start from a generic template or a concrete vertical example.

## API Additions and Behavior Changes

### New or expanded company API behavior

- `POST /api/companies`
  - accepts richer company manifests
  - validates inline team and Pi IDs before persistence
  - applies the company immediately
- `PATCH /api/companies/{id}`
  - supports richer manifest updates
  - removes stale inline team directories when teams are replaced
  - tears down and reapplies runtime state immediately
- `GET /api/companies/{id}`
  - returns richer company runtime details including teams, schedules, and recent jobs
- `GET /api/companies/{id}/jobs`
  - returns company-specific job history
- `DELETE /api/companies/{id}`
  - performs full runtime teardown before deleting config
- `POST /api/jobs`
  - accepts top-level `company_id`
  - normalizes `company_id` into execution context

### Behavior changes to call out

- `shared_teams` should now be treated as an explicit allowlist for company access to global teams.
- Inline team payloads are validated earlier and more strictly.
- Sessions tools are now company-scoped and may reject calls that older setups implicitly allowed.
- Company deletion and hot-reload are more aggressive about cleaning runtime resources.

## Security and Isolation Improvements

The major security theme of this release is tenant isolation.

Specifically:

- company-private teams are isolated in routing and execution
- company-private MCP servers are isolated per company
- sessions tooling no longer crosses tenant boundaries
- invalid inline team/Pi IDs are rejected before writing files
- internal exceptions are less likely to leak server details through API responses

## Operational Improvements

- company runtime state is easier to inspect through the API
- jobs can be queried by company
- scheduler tasks carry scope metadata
- interrupted work is handled more safely during restart
- runtime memory growth is reduced
- long-running scheduled tasks have time limits

## Documentation and Example Assets

### Added

- `examples/company-onboarding.md`
- `examples/company-template/README.md`
- `examples/company-template/manifest.json`
- `examples/company-template/register.py`
- `examples/prediction-market/README.md`
- `examples/prediction-market/manifest.json`
- `examples/prediction-market/register.py`

### Updated

- `README.md`
- `examples/README.md`

## Testing and Verification

This release is backed by targeted and full-suite coverage.

Added or expanded tests cover:

- company API registration flow
- company-scoped job submission
- immediate runtime apply and cleanup
- invalid inline team ID rejection without persistence
- component priority over YAML
- company scoping and company ID normalization
- prediction-market feed adapters
- sessions security and shared-team enforcement
- Python 3.12 event-loop compatibility

Latest verification run:

- `py -3.12 -m compileall companest tests -q`
- `python -m pytest -q`
- Result: `599 passed, 3 deselected`

## Suggested PR Description

### Summary

This PR turns Companest into a much more production-ready control plane for external company repos.

It completes the company registration lifecycle, wires company schedules into the runtime, scopes jobs and tools by company, tightens tenant isolation, adds prediction-market feeds to the shared data layer, and ships onboarding assets plus a full prediction-market example extension.

### Highlights

- Added immediate company apply/update/delete through the API
- Added company-scoped schedules, jobs, routing bindings, memory seeds, and MCP servers
- Fixed private team path resolution and full company teardown cleanup
- Tightened tenant isolation across routing, sessions tooling, and company-scoped job execution
- Added Polymarket, Kalshi, and Metaculus feed adapters
- Added onboarding docs, a generic company template, and a prediction-market example
- Expanded tests and verified the full suite passes

### Validation

- `py -3.12 -m compileall companest tests -q`
- `pytest -q`
- `599 passed, 3 deselected`

## Suggested Tag / Release Body

Companest now supports external company registration as a real runtime workflow.

This release adds immediate company apply/update/delete via API, company-scoped jobs and schedules, stronger tenant isolation, scoped MCP tooling, safer teardown and hot-reload behavior, prediction-market feed adapters, and new onboarding assets for external company repos.

It also includes a reusable company template and a full prediction-market example extension.

Validation for this release completed with `599 passed, 3 deselected`.

## Suggested Release Title

`platform-readiness: multi-company control plane + prediction-market example`
