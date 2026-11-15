# Companest Examples

Execution requires the pinned Substrate runtime and persistent actor mounts; see
[`deploy/substrate/README.md`](../deploy/substrate/README.md).

The `examples/` directory contains three practical starting points.

## Available Examples

### `minimal-setup`

Use this when you want a set of public team declarations to validate and install on persistent storage.

Included:

- `general`: a lightweight assistant team
- `coding`: a coding and review team with workspace-aware tools
- `info-collection`: a feed collector team with a sample watchlist
- `workspaces.json`: a sample workspace registry entry

### `company-template`

Use this when you want to create a new external company repo that registers into a running Companest control plane.

Included:

- a starter `manifest.json`
- a `register.py` helper
- a short customization guide

### `prediction-market`

Use this when you want a complete example of a vertical company extension.

Included:

- private teams
- company schedules
- routing bindings
- memory seeds
- feed-tool usage
- API-based registration

## Run The Local Demo

Follow the [Substrate deployment guide](../deploy/substrate/README.md) first.
Install the public declarations into your mounted data directory, set the
control config's mount paths and scope-to-template map, then run:

```bash
pip install -e ".[server,scheduler]"
companest validate <CONFIG_PATH>
companest serve -c <CONFIG_PATH>
companest team run general "Summarize this project"
```

The public workspace registry is a mount example. Set its path and matching
storage identity file; company workspaces also require `company_id`.

## Registering External Companies

For a Claude Code session using the running service, see [CLI/API session access](./claude-session.md).

If you want a separate repo to register itself into a running Companest control plane, start here:

- [`company-onboarding.md`](./company-onboarding.md)
- [`company-template/README.md`](./company-template/README.md)
- [`prediction-market/README.md`](./prediction-market/README.md)
