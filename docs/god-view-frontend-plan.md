# Companest Console Frontend Plan (Infra-first Revision v2)

## Goal

Replace the NiceGUI admin with a modern React-based operator console that is deep-linkable, separately deployable, and extensible toward topology visualization.

## Technical Motivation

The current NiceGUI admin (`companest/admin.py`, ~480 LOC) works but has concrete limitations:

- Pages are not deep-linkable — operators cannot share URLs to a specific company or job.
- NiceGUI runs in-process with the orchestrator via `ui.run_with()` — the console cannot be deployed or scaled independently.
- NiceGUI renders server-side — adding client-side interactivity (filtering, sorting, real-time updates) requires fighting the framework.
- No Jobs page, no Events page, no raw JSON inspection — operators must use CLI or read logs for these.
- Adding new pages requires Python, not a component-driven UI framework — harder to iterate on layout and UX.

## What Changed (v2)

This revision incorporates findings from a grounded codebase review:

- All 7 backend contract items are now assigned to specific phases.
- Phase 0 now lists every scaffolding task explicitly (no hidden work).
- Phase 2 rewritten: all write endpoints already exist — Phase 2 is frontend-only.
- Events timeline moved from Phase 1 to Phase 4 (no REST history endpoint exists).
- Phase gates rewritten to be programmatically verifiable.
- Phase 5 (God View) collapsed from 5 parallel tracks to a short vision statement.
- Phase 6 (deployment) split: minimum deployment moved to Phase 0, NiceGUI migration stays late.
- Added: Locked Technical Decisions, Alternatives Considered, Current Baseline Gaps, Unresolved Questions, Pre-Mortem, PR Delivery Model.

## Non-Goals

- RBAC or multi-user auth — single bearer token is sufficient for v1.
- Mobile-first design — this is an operator desktop tool.
- Topology as the home page — topology comes after the console proves useful.
- Offline or PWA support — the console requires a live backend.
- Internationalization (i18n) — English only for v1.
- Replacing the CLI — the console complements the CLI, does not replace it.
- Historical analytics warehouse — real-time and recent state only.
- Coupling to current internal company names or types.
- Coupling to `network_mode: host` as the only deployment model.

## Locked Technical Decisions

### LTD-1: Frontend stack is Vite + React + TypeScript + Tailwind + ShadCN

**Context:** Need a frontend stack that embeds into a Python monorepo, builds to static files, and supports a component-driven operator console.

**Decision:** Vite + React + TypeScript + TanStack Router + TanStack Query + Tailwind CSS + ShadCN components.

**Consequences:** Introduces Node.js tooling into a Python-only repo. Requires a frontend CI job. Build output is static files served by FastAPI.

### LTD-2: SPA served by FastAPI StaticFiles mount at `/console`

**Context:** Need to serve the built SPA without adding nginx or a separate container. Docker Compose uses `network_mode: host` and the Terraform security group only opens port 22.

**Decision:** Mount the Vite build output via FastAPI `StaticFiles` at `/console` with HTML5 history fallback. NiceGUI stays at `/admin`. API stays at `/api`. Dev uses Vite proxy to `localhost:8000`.

**Consequences:** No nginx needed. Same-origin by default (no CORS in production). Dev proxy config required in `vite.config.ts`. Later, a reverse proxy can replace this if needed.

### LTD-3: TypeScript types generated from FastAPI OpenAPI schema

**Context:** FastAPI auto-generates an OpenAPI schema at `/openapi.json`. Keeping TypeScript types in sync with Python models manually will drift.

**Decision:** Use `openapi-typescript` to generate TypeScript types from the FastAPI schema. Validate critical runtime responses with Zod (job status enums, financial amounts).

**Consequences:** Requires exporting and committing the OpenAPI schema, or generating types as a build step.

### LTD-4: Auth token stored in sessionStorage

**Context:** The backend uses a single static bearer token (`COMPANEST_API_TOKEN`). No session endpoint exists.

**Decision:** Operator enters the token once on a login page. Token stored in `sessionStorage` (clears on tab close). Injected into all API calls via a fetch wrapper. On any 401 response, redirect to login.

**Consequences:** Token does not persist across browser sessions. Acceptable for an operator tool. No XSS-safe httpOnly cookie path without a backend login endpoint.

## Alternatives Considered

### Alternative 1: Extend NiceGUI instead of building a React frontend

NiceGUI already covers 6 operator pages. Adding Jobs, Events, and raw JSON views to NiceGUI would deliver most of Phase 1's value without a second tech stack.

**Rejected because:** NiceGUI pages are not deep-linkable (server-rendered with session state). NiceGUI runs in-process — cannot deploy the console independently. Adding rich client-side behavior (filtering, sorting, WebSocket-driven updates) requires fighting NiceGUI's server-side model. The long-term goal (topology, extensible component system) requires a client-side framework.

### Alternative 2: Use htmx + Jinja2 templates instead of React

Lighter than React, stays in the Python ecosystem, supports progressive enhancement.

**Rejected because:** htmx is excellent for server-rendered CRUD but weak for real-time dashboards, complex client state (WebSocket + REST cache coordination), and the eventual topology graph. The skill gap between htmx and React is also smaller than it appears — ShadCN provides most of the component work.

## Current Baseline Gaps

These are verified against the actual codebase:

| Gap | Severity | Actual Code Reference | Fix Complexity |
|-----|----------|----------------------|----------------|
| `AuthMiddleware` blocks OPTIONS preflight | **Phase 0 blocker** | `server.py:113-126` — no OPTIONS exemption | 1-line fix |
| `AuthMiddleware` may block WebSocket upgrades | **Phase 0 blocker** | `server.py:113-126` — no `/ws/` exemption | 1-line fix |
| `GET /api/jobs` lacks `company_id` query param | **Phase 0 blocker** | `server.py:194-217` — param not exposed; `jobs.py:293` already supports it internally | 1-line fix |
| `GET /api/jobs` `total` returns page size, not total matching count | **Phase 1 blocker** | `server.py:213` — `len(jobs)` after slicing | Small fix |
| Error responses inconsistent | **Phase 1 fix** | Auth returns `{"error": "..."}`, HTTPException returns `{"detail": "..."}`, some return 200 + `{"note": "..."}` | Standardize to `{"detail": "..."}` |
| `GET /api/teams` returns nested dict, not array | **Document** | `server.py:340-344` — returns `{"configs": {...}, "active": [...]}` | Frontend transforms |
| No `GET /api/events` for historical events | **Phase 4** | `events.py` — EventBus is in-memory only, no persistence | New endpoint + persistence |
| `GET /api/fleet/status` has N+1 per-company queries | **Phase 3** | `server.py:247` — loops companies calling `list_jobs` each | Cache or aggregate query |
| No `/api/meta` capability discovery | **Phase 3** | Does not exist | New endpoint |
| `GET /api/companies` lacks summary fields | **Phase 3** | `server.py:520-537` — returns id/name/domain/enabled only | Add aggregated fields |
| No WebSocket resync semantics | **Phase 4** | No sequence IDs, no replay, dropped events silent | New protocol |
| YAML company config write not atomic | **Phase 2** | `company.py:324` — `write_text()` directly | write-to-temp + rename |
| WebSocket token in URL query param | **Phase 4** | `server.py:265` — token visible in logs/history | Ticket-based auth |

## Existing Write Endpoints (already implemented)

Phase 2 frontend work does NOT require building these — they already exist:

| Action | Endpoint | server.py line |
|--------|----------|---------------|
| Cancel job | `POST /api/jobs/{id}/cancel` | 221 |
| Trigger scheduler task | `POST /api/scheduler/{task_name}/trigger` | 424 |
| Cancel user schedule | `DELETE /api/schedules/{schedule_id}` | 448 |
| Reset circuit breaker | `POST /api/finance/circuit-breaker/reset` | 394 |
| Resolve cost approval | `POST /api/finance/approve/{approval_id}` | 404 |
| Create company | `POST /api/companies` | 539 |
| Update company | `PATCH /api/companies/{company_id}` | 612 |
| Delete company | `DELETE /api/companies/{company_id}` | 678 |
| Enable/disable company | `PATCH /api/companies/{company_id}` with `{"enabled": bool}` | 612 |
| Add company binding | `POST /api/companies/{company_id}/bind` | 706 |
| Set global bindings | `PUT /api/bindings` | 732 |
| Run task on team | `POST /api/teams/{team_id}/run` | 365 |

## API Response Shape Notes

An implementing agent must know these non-obvious response shapes:

- `GET /api/teams` returns `{"configs": {team_id: {...}}, "active": [...]}` — a nested dict, **not** an array. Frontend must transform `Object.values(response.configs)` into a list.
- `GET /api/schedules` returns **user-created schedules** (APScheduler). `GET /api/scheduler/status` returns **system scheduler tasks**. These are two different concepts. The Schedules nav page should show both.
- Many endpoints return HTTP 200 with `{"note": "orchestrator not initialized"}` when the orchestrator is not ready. Frontend must check for the `note` field and show an appropriate state.
- `GET /api/finance/report` accepts `hours` query param (float, default 24).
- `POST /api/companies` accepts inline team definitions, bindings, preferences, ceo config, schedules, env, shared_teams, routing_bindings, memory_seed, mcp_servers.

## Product Principles

- API-first and CLI-aligned. The frontend mirrors the same concepts and naming as the CLI and config layer.
- Readability before visualization. Tables, summaries, forms, and state panels come before graphs.
- Safe by default. No hidden destructive actions, no secret injection into static bundles, no dependence on animation for meaning.
- Works without WebSocket. REST is the source of truth. WebSocket improves freshness, not correctness.
- Progressive disclosure. Overview pages stay simple; detail pages expose raw config and runtime metadata when needed.
- No company-type hardcoding. The UI works for arbitrary company domains and future plugins.

## Information Architecture

### Global navigation

1. Overview
2. Companies
3. Jobs
4. Teams
5. Schedules
6. Finance
7. Bindings
8. Topology (optional, later — feature-flagged)

Events page is deferred to Phase 4 (requires backend event persistence).
Diagnostics is removed until concrete scope is defined.

### Company workspace (Phase 3)

Each company has a dedicated workspace with tabs for: summary, config, teams, recent jobs, schedules, finance/budget, bindings, raw manifest JSON.

## Design Constraints

- Every entity page must show ID, scope, enabled state, and last-updated context.
- Every async panel must show loading, empty, partial-data, and error states (use ShadCN skeleton + alert defaults, not a custom design system).
- Every important object should have a raw JSON view for debugging.
- Every write action needs confirmation dialog, visible success/failure toast, and automatic query invalidation.
- Every page must be deep-linkable.
- The UI mirrors backend concepts directly: company, team, job, schedule, binding, cost gate.

## NiceGUI Coexistence

During migration, both UIs run on the same FastAPI server:

- NiceGUI stays at `/admin` (exempted from auth middleware at `server.py:116`).
- New console at `/console` (served via `StaticFiles` mount).
- API at `/api/*`, WebSocket at `/ws/*`.
- No route conflicts. NiceGUI is removed module-by-module after the console reaches parity for each page.

---

## Phased Delivery Plan

## Phase 0: Foundation and Backend Fixes

Intent: Get the frontend skeleton running against the real backend with all blockers removed.

### Backend fixes (prerequisites)

- Fix `AuthMiddleware` to skip OPTIONS requests (CORS preflight)
- Fix `AuthMiddleware` to skip `/ws/` paths (WebSocket upgrades)
- Add `company_id: Optional[str] = None` to `GET /api/jobs` endpoint
- Fix `total` in `GET /api/jobs` to return total matching count, not page size
- Standardize error responses to `{"detail": "..."}` format in auth middleware

### Frontend scaffolding

- Initialize Vite + React + TypeScript project in `console/` directory
- Configure `tsconfig.json` with strict mode and path aliases
- Install and configure Tailwind CSS + ShadCN component library
- Install and configure TanStack Router with file-based routes
- Install and configure TanStack Query with default stale/retry settings
- Create API client module: fetch wrapper, auth header injection from sessionStorage, error normalization (handle `detail`, `error`, and `note` response shapes), base URL config
- Create `vite.config.ts` with dev proxy: `/api/*` and `/ws/*` → `localhost:8000`
- Create layout shell: sidebar navigation, header, auth gate (redirect to login if no token)
- Create shared primitives: loading skeleton, empty state, error alert, data table (ShadCN Table)
- Create token login page

### Deployment minimum

- Add `StaticFiles` mount in `server.py` at `/console` serving `console/dist/` with SPA fallback
- Add frontend CI job to `.github/workflows/ci.yml`: `npm ci && npm run build && npm run lint && npm run typecheck`

### Phase 0 non-goals

- No data-fetching pages (those are Phase 1).
- No write actions.
- No WebSocket integration.
- No production Dockerfile changes (dev proxy is sufficient).

Gate:

- `npm run dev` starts without errors
- `curl http://localhost:5173/console` returns HTTP 200 with the app shell HTML
- Login page accepts a token and stores it in sessionStorage
- Layout shell renders with sidebar navigation (all nav items link to stub pages)
- Backend pytest still passes after the middleware fixes
- Frontend CI job passes: build + lint + typecheck

## Phase 1: Operator Core Console (Read-heavy)

Intent: Ship read-only pages that let an operator see fleet state, companies, jobs, teams, schedules, and finance.

### Pages

- **Overview page** — calls `GET /api/fleet/status`, shows: total companies, active teams, job counts by status, per-company summary cards. Links to company and job detail pages.
- **Companies list page** — calls `GET /api/companies`, shows: table with id, name, domain, enabled, bindings_count, ceo_enabled. Row click navigates to company detail stub.
- **Jobs list page** — calls `GET /api/jobs` (with status and company_id filters), shows: table with id, task (truncated), status badge, company_id, timestamps. Pagination using total count. Row click navigates to job detail.
- **Job detail page** — calls `GET /api/jobs/{id}`, shows: full task text, status, context, subtasks, result, error, timestamps, raw JSON drawer.
- **Teams page** — calls `GET /api/teams`, transforms `configs` dict into array, shows: table with id, role, lead_pi, mode, enabled, always_on. Note: response shape is `{"configs": {...}, "active": [...]}`, not an array.
- **Schedules page** — calls both `GET /api/schedules` (user schedules) and `GET /api/scheduler/status` (system tasks), shows: two tables or tabbed view distinguishing user-created schedules from system scheduler tasks.
- **Finance page** — calls `GET /api/finance/summary` and `GET /api/finance/report`, shows: spending summary cards, circuit breaker status, per-team spending breakdown. Shows pending approvals if any exist.

### Cross-cutting

- All pages use TanStack Query with 30-second stale time and auto-refetch on window focus.
- Loading, empty, and error states on every data panel using shared primitives from Phase 0.
- Deep-linkable URLs for every page and every entity detail (e.g., `/console/jobs/abc123`).
- Raw JSON drawer available on job detail and company detail (expandable panel showing the full API response).

### Phase 1 non-goals

- No write actions (Phase 2).
- No Events timeline (Phase 4 — requires backend event persistence).
- No company workspace tabs (Phase 3).
- No WebSocket live updates (Phase 4).
- No topology (Phase 5).

Gate:

- All 7 pages render real data from the backend API
- `npm run build` produces a production bundle served correctly at `/console`
- Each entity on the Overview links to its detail page (navigable in ≤2 clicks)
- The app is fully functional with WebSocket disabled
- Playwright smoke test: login → overview loads → navigate to jobs → job detail renders

## Phase 2: Write Actions

Intent: Build frontend forms and confirmation flows for all existing write endpoints. No backend changes needed — all endpoints already exist.

### Actions to implement (frontend only)

- Cancel job — button on job detail page, confirmation dialog, calls `POST /api/jobs/{id}/cancel`
- Trigger scheduler task — button on schedules page, calls `POST /api/scheduler/{task_name}/trigger`
- Cancel user schedule — button on schedules page, confirmation dialog, calls `DELETE /api/schedules/{schedule_id}`
- Reset circuit breaker — button on finance page, confirmation dialog, calls `POST /api/finance/circuit-breaker/reset`
- Resolve cost approval — approve/reject buttons on finance page, calls `POST /api/finance/approve/{approval_id}`
- Create company — form page, calls `POST /api/companies`
- Update company — edit form, calls `PATCH /api/companies/{company_id}`
- Delete company — confirmation dialog with company name input, calls `DELETE /api/companies/{company_id}`
- Enable/disable company — toggle on companies list and detail, calls `PATCH /api/companies/{company_id}` with `{"enabled": bool}`
- Add company binding — form on company detail, calls `POST /api/companies/{company_id}/bind`
- Set global bindings — form on bindings page, calls `PUT /api/bindings`
- Run task on team — form on teams page, calls `POST /api/teams/{team_id}/run`

### Backend fix in this phase

- Make `CompanyRegistry.save()` atomic: write to temp file, then `os.replace()` (`company.py:324`).

### Cross-cutting

- Every write action shows: confirmation dialog (for destructive actions) → loading spinner → success toast + query invalidation → or error toast with detail message.
- Optimistic updates where safe (enable/disable toggle). Pessimistic updates for creates/deletes.

### Phase 2 non-goals

- No company workspace (Phase 3).
- No inline config validation (Phase 3).
- No batch operations.

Gate:

- All 12 write actions work end-to-end against the real backend
- Every destructive action (delete, cancel) shows a confirmation dialog
- Every write action shows visible success or failure feedback
- After any mutation, the affected list/detail page refreshes automatically (query invalidation)
- NiceGUI admin write actions have parity or better in the new console

## Phase 3: Company Workspace and Backend Enrichment

Intent: Give each company a dedicated detail workspace. Add backend endpoints that improve the console at scale.

### Frontend

- Company workspace at `/console/companies/{id}` with tabs: Summary, Config (read-only), Teams, Recent Jobs, Schedules, Finance, Bindings, Raw JSON.
- Each tab calls the appropriate existing endpoint (company detail, company jobs, etc.).
- Inline validation feedback for company update form (from Phase 2).
- Apply-preview: show diff before submitting company update.

### Backend improvements

- Add summary fields to `GET /api/companies`: active_team_count, recent_job_count, last_activity_timestamp.
- Add `GET /api/meta` capability discovery endpoint (finance, scheduler, websocket, public_knowledge flags).
- Optimize `GET /api/fleet/status` to avoid N+1 per-company job queries (cache or aggregate SQL query).

### Phase 3 non-goals

- No onboarding wizard for external manifests (future).
- No topology.

Gate:

- Company workspace renders all tabs with real data
- Companies list page shows enriched summary fields
- `GET /api/meta` returns correct capability flags
- Overview page loads in <2 seconds with 20+ companies (N+1 resolved)

## Phase 4: Realtime and Events

Intent: Add WebSocket live updates, event persistence, and the Events timeline page.

### Backend

- Add event persistence: store events in aiosqlite with timestamp, type, and payload.
- Add `GET /api/events` endpoint: query by type, time range, company_id. Paginated.
- Add WebSocket sequence IDs so the client can detect missed events.
- Migrate WebSocket auth from URL query param to first-message handshake (security improvement).

### Frontend

- WebSocket hook with auto-reconnect and exponential backoff.
- On reconnect: compare last-seen sequence ID, trigger full REST refresh if events were missed.
- Events timeline page: table with type, timestamp, payload summary, company link. Filterable by type and time range.
- Activity badges on Overview and company pages (new events since last view).
- Stale-state indicator: "Last updated X seconds ago" with manual refresh button.
- Browser-background recovery: refresh all queries on tab re-focus after >60 seconds.

### Phase 4 non-goals

- No topology.
- No advanced event analytics.

Gate:

- Events page renders historical events from REST endpoint
- WebSocket connection auto-recovers after backend restart
- Missed events trigger automatic REST refresh (verified by Playwright test)
- The app remains fully functional with WebSocket disabled (Events page uses REST only)

## Phase 5: God View Topology (Later-stage)

Intent: Add an optional topology graph module after the core console is proven useful.

This phase is intentionally kept as a vision statement. Detailed planning will happen after Phases 1-4 ship.

Scope:

- Static topology: company nodes with status badges, click-through into company workspace.
- Live topology (stretch): event-driven activity overlays, interaction edges.
- Feature-flagged and removable without harming the core console.

Entry criteria before starting:

- Phases 1-4 shipped and stable.
- Company workspace exists with deep links.
- WebSocket reconnect and stale-state handling in place.
- Overview and company summary contracts stable.

Gate:

- The graph complements list/detail pages, does not replace them.
- The feature can be removed without breaking the console.
- Performance acceptable at the expected company count.

---

## Unresolved Questions

### Must resolve before implementation starts

- None remaining — all Phase 0 decisions are locked above.

### Can resolve during implementation

- Exact TanStack Router file structure (flat vs nested route files).
- Whether to commit the OpenAPI schema to the repo or generate types on each build.
- ShadCN theme customization (use defaults initially, customize later).

### Can defer until after shipping

- Whether to add a reverse proxy (nginx/Caddy) in front of FastAPI for production.
- Whether to support multiple Companest instances from a single console.
- Dark mode.

## Prior Art

- **Kubernetes Dashboard**: Read-heavy resource browser with detail views. Similar information architecture (list → detail → actions). Validates that tables + status badges + raw YAML view is sufficient for operators.
- **Grafana**: Dashboard-first with deep-linking. Shows that REST-first with optional live updates is the right layering.
- **Consul UI**: Infrastructure console with service topology as a later addition. Validates building the operator console first, topology second.

## Pre-Mortem

Imagine this plan failed completely. The three most likely reasons:

1. **The console duplicates NiceGUI without being better.** If Phase 1 ships but operators still use NiceGUI because it has write actions and the console doesn't, the console becomes shelfware. **Mitigation:** Phase 2 immediately follows Phase 1. The plan explicitly lists NiceGUI parity as a Phase 2 gate.

2. **The Phase 0 scaffolding becomes a tarpit.** Configuring Vite + React + TypeScript + Tailwind + ShadCN + TanStack Router + TanStack Query from scratch in a Python repo could absorb weeks of yak-shaving. **Mitigation:** Phase 0 lists every sub-item explicitly. An AI agent can execute them in parallel. The gate is concrete: `npm run dev` works, login page works, CI passes.

3. **Backend N+1 and response shape issues make the console feel broken.** The fleet_status N+1 and the inconsistent error shapes could make the console slow and unreliable. **Mitigation:** The three highest-severity backend fixes are Phase 0 prerequisites. N+1 is explicitly a Phase 3 item with a concrete gate (load time with 20+ companies).

## Scope Priority

### Must-have before topology

- Overview, Companies, Jobs, Teams, Schedules, Finance, Bindings (Phase 1)
- Auth (Phase 0)
- All write actions (Phase 2)

### Should-have before deprecating NiceGUI

- Company workspace (Phase 3)
- Events timeline (Phase 4)

### Later-stage

- God View topology (Phase 5)
- Dark mode
- Historical analytics
- Multi-user auth and RBAC

---

## PR Delivery Model

### Phase 0 PRs

**PR 0a: Backend fixes**
- Goal: Remove all blockers for frontend development
- In scope: OPTIONS fix, `/ws/` skip, `company_id` on jobs endpoint, `total` count fix, error format standardization
- Out of scope: New endpoints, new features
- Merge gate: `python -m pytest -q` passes, manual test of OPTIONS with curl
- Parallelizable: Yes — can be developed while PR 0b is in progress

**PR 0b: Frontend scaffold**
- Goal: Vite project boots with layout shell and login page
- In scope: Vite + TS + Tailwind + ShadCN setup, TanStack Router/Query config, API client module, dev proxy, layout shell, login page, loading/error/empty primitives
- Out of scope: Any data-fetching pages, write actions, WebSocket
- Merge gate: `npm run dev` works, `npm run build` succeeds, login flow works against real backend
- Parallelizable: Yes — can be developed while PR 0a is in progress

**PR 0c: Deployment and CI**
- Goal: SPA is served at `/console` and frontend CI runs
- In scope: StaticFiles mount in server.py, frontend CI job in ci.yml
- Out of scope: Dockerfile changes, nginx, production TLS
- Merge gate: `curl localhost:8000/console` returns the app shell, CI pipeline passes
- Parallelizable: Depends on PR 0b (needs build output)

### Phase 1 PRs

**PR 1a: Overview + Companies + Jobs pages**
- Goal: Core operator pages with the highest-value data
- In scope: Overview page, companies list, jobs list + detail, raw JSON drawer
- Out of scope: Teams, schedules, finance
- Merge gate: All 4 pages render real data, deep links work, Playwright smoke test passes
- Parallelizable: No (first PR to add data-fetching patterns that later PRs follow)

**PR 1b: Teams + Schedules + Finance pages**
- Goal: Complete the read-only page set
- In scope: Teams page (with response transform), schedules page (both user + system), finance page (summary + report + circuit breaker status + pending approvals)
- Out of scope: Write actions, Events
- Merge gate: All 3 pages render real data
- Parallelizable: Yes — can start after PR 1a establishes data-fetching patterns

### Phase 2 PRs

**PR 2a: Job and scheduler write actions**
- Goal: Cancel job, trigger scheduler, cancel schedule, reset circuit breaker, resolve approval
- In scope: Confirmation dialogs, success/error toasts, query invalidation
- Out of scope: Company CRUD
- Merge gate: All 5 actions work end-to-end
- Parallelizable: Yes — can be developed in parallel with PR 2b

**PR 2b: Company CRUD and bindings**
- Goal: Create, update, delete, enable/disable company. Binding management.
- In scope: Company forms, delete confirmation, enable/disable toggle, binding forms, atomic YAML writes
- Out of scope: Company workspace tabs, inline validation
- Merge gate: All company and binding write actions work end-to-end
- Parallelizable: Yes — can be developed in parallel with PR 2a

### Phase 3 PRs

**PR 3a: Company workspace**
- Goal: Tabbed company detail view
- In scope: All workspace tabs, diff preview for updates
- Merge gate: Company workspace renders all tabs with real data

**PR 3b: Backend enrichment**
- Goal: Summary fields, capability discovery, N+1 fix
- In scope: `/api/meta`, enriched `/api/companies`, optimized `/api/fleet/status`
- Merge gate: Overview loads in <2s with 20+ companies, `/api/meta` returns correct flags
- Parallelizable: Yes — can be developed in parallel with PR 3a

### Phase 4 PRs

**PR 4a: Event persistence and REST endpoint**
- Goal: Backend support for event history
- In scope: Event table in aiosqlite, `GET /api/events`, sequence IDs on WebSocket
- Merge gate: `GET /api/events` returns recent events, events persist across restarts

**PR 4b: Events page and WebSocket integration**
- Goal: Events timeline + live updates across all pages
- In scope: Events page, WebSocket hook, reconnect, stale indicator, activity badges
- Merge gate: Events page works with REST only; WebSocket adds live updates; auto-recovery after disconnect
- Parallelizable: Depends on PR 4a

### Phase 5: Single PR

- Scoped when Phases 1-4 are stable. Not planned in detail now.

## Immediate Next Step

Implement PR 0a (backend fixes) and PR 0b (frontend scaffold) in parallel.
