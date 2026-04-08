# Change History

## 2026-04-08

### Added

- Added persistent operator event history backed by SQLite, including `/api/events`, sequence IDs, and WebSocket reconnect recovery.
- Added a dedicated operator-console surface for company detail, events, and topology views.
- Added realtime console plumbing for live badges, unread counts, reconnect state, and manual refresh recovery.

### Removed

- Removed the unsupported `proxy.subscription_mode` configuration and the related Claude subscription-only routing path.

### Changed

- Restored Claude model execution to supported provider paths only: standard Anthropic API credentials or proxy-based routing.
- Updated production configuration guidance to remove subscription-mode deployment instructions.
- Replaced the abandoned launch-plan document with this change history so branch history records shipped changes instead of outdated rollout plans.
- Expanded company, finance, scheduler, team, and event API responses so the console can render richer detail views and stable empty states.
- Standardized the project runtime on Python 3.12 in packaging, CI, and the deployment image.

### Frontend Scope

- Extended the console information architecture with new routes for events, topology, and per-company drill-down workflows.
- Improved company operations UX with richer summary cards, finance and schedule visibility, binding management, and inline update/delete flows.
- Added realtime status affordances so operators can tell whether the UI is live, reconnecting, or stale without opening browser devtools.
- Introduced deploy-friendly client configuration for API and WebSocket base URLs, plus a feature flag for topology rollout.

### Fixed

- Added fail-fast validation so legacy configs that still set `proxy.subscription_mode` now raise a clear configuration error.
- Preserved company-scoped event history for job cancellations and related lifecycle events.
- Updated regression coverage for removed config handling, company API behavior, and event history persistence.
