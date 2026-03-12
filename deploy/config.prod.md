# Companest Production Configuration

Use this file as a starting point for a production deployment. Replace the environment variables with your real secrets before exposing the API publicly.

```json
{
  "name": "companest-production",
  "version": "2.0",
  "master": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 19000,
    "auth_token": "${COMPANEST_MASTER_TOKEN}"
  },
  "api": {
    "host": "0.0.0.0",
    "port": 8000,
    "auth_token": "${COMPANEST_API_TOKEN}"
  },
  "proxy": {
    "enabled": true,
    "base_url": "http://localhost:4000"
  },
  "debug": false,
  "global_timeout": 300
}
```

## Required secrets

| Variable | Purpose |
|----------|---------|
| `COMPANEST_API_TOKEN` | REST API authentication |
| `COMPANEST_MASTER_TOKEN` | Gateway authentication |
| `LITELLM_MASTER_KEY` | LiteLLM proxy administration |
