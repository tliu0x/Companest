# Change History

## 2026-04-08

### Added

- Added persistent operator event history backed by SQLite, including `/api/events`, sequence IDs, and WebSocket reconnect recovery.
- Added console pages for company detail, events, and topology, plus a realtime provider for live badges, unread counts, and refresh state.

### Removed

- Removed the unsupported `proxy.subscription_mode` configuration and the related Claude subscription-only routing path.

### Changed

- Restored Claude model execution to supported provider paths only: standard Anthropic API credentials or proxy-based routing.
- Updated production configuration guidance to remove subscription-mode deployment instructions.
- Replaced the abandoned launch-plan document with this change history so branch history records shipped changes instead of outdated rollout plans.
- Expanded company, finance, scheduler, and team API responses so the console can render stable empty states and richer company summaries.
- Standardized the project runtime on Python 3.12 in packaging, CI, and the deployment image.

### Fixed

- Added fail-fast validation so legacy configs that still set `proxy.subscription_mode` now raise a clear configuration error.
- Preserved company-scoped event history for job cancellations and related lifecycle events.
- Updated regression coverage for removed config handling, company API behavior, and event history persistence.
