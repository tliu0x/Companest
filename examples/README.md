# Companest Examples

The `minimal-setup` sample is the canonical public demo used by both onboarding docs and configuration tests.

## Run it locally

```bash
pip install -e ".[claude,server]"
cp -r examples/minimal-setup/.companest .companest
cp examples/minimal-setup/.env.example .env
companest validate
companest serve
companest team run general "Summarize this project"
```

## What is included

- `general`: a lightweight assistant team for simple prompts
- `coding`: a coding and review team with workspace-aware tools
- `info-collection`: a feed collector team with a public watchlist example
- `workspaces.json`: a sample workspace registry entry

Use this sample as the starting point for your own `.companest/` directory.
