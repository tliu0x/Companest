# Change History

## 2026-04-08

### Removed

- Removed the unsupported `proxy.subscription_mode` configuration and the related Claude subscription-only routing path.

### Changed

- Restored Claude model execution to supported provider paths only: standard Anthropic API credentials or proxy-based routing.
- Updated production configuration guidance to remove subscription-mode deployment instructions.
- Replaced the abandoned launch-plan document with this change history so branch history records shipped changes instead of outdated rollout plans.

### Fixed

- Added fail-fast validation so legacy configs that still set `proxy.subscription_mode` now raise a clear configuration error.
- Updated regression coverage for removed config handling.
