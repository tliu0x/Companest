# Companest Production Configuration

Use this file as a starting point for a minimal production deployment. Replace the environment variables with your real secrets before exposing the API.

This sample is intentionally conservative:

- API auth is enabled
- debug mode is disabled
- gateway mode is disabled by default
- proxy mode is disabled by default

Enable gateway or proxy features only when you actually need them.

```json
{
  "name": "companest-production",
  "version": "2.0",
  "master": {
    "enabled": false
  },
  "api": {
    "host": "0.0.0.0",
    "port": 8000,
    "auth_token": "${COMPANEST_API_TOKEN}"
  },
  "proxy": {
    "enabled": false
  },
  "debug": false,
  "global_timeout": 300
}
```

## Required Secrets

| Variable | Purpose |
|----------|---------|
| `COMPANEST_API_TOKEN` | REST API authentication |

## Optional Secrets

Use these only if you enable the related features:

| Variable | Purpose |
|----------|---------|
| `COMPANEST_MASTER_TOKEN` | Gateway authentication |
| `LITELLM_MASTER_KEY` | LiteLLM proxy administration |
