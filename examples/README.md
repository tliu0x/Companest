# Companest Examples

The `examples/` directory contains three practical starting points.

## Available Examples

### `minimal-setup`

Use this when you want a local `.companest/` runtime that you can validate and run immediately.

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

```bash
pip install -e ".[claude,server]"
cp -r examples/minimal-setup/.companest .companest
cp examples/minimal-setup/.env.example .env
companest validate
companest serve
companest team run general "Summarize this project"
```

## Registering External Companies

If you want a separate repo to register itself into a running Companest control plane, start here:

- [`company-onboarding.md`](./company-onboarding.md)
- [`company-template/README.md`](./company-template/README.md)
- [`prediction-market/README.md`](./prediction-market/README.md)
