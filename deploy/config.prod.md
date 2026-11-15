# Companest Production Configuration

Use this file as a starting point for a minimal production deployment. Replace the environment variables with your real secrets before exposing the API.

This sample is intentionally conservative:

- API auth is enabled
- debug mode is disabled
- gateway mode is disabled by default
- proxy mode is disabled by default

Enable gateway or proxy features only when you actually need them.

Execution requires Substrate and persistent mounts; see
[`substrate/README.md`](./substrate/README.md). Replace sample scope/template and
mount paths with your deployment values. There is no local execution fallback.

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
  "global_timeout": 300,
  "data_dir": "/mnt/companest/state",
  "memory_backend": "file",
  "storage_lock_dir": "/mnt/companest/locks",
  "substrate": {
    "executable": "kubectl-ate",
    "kubeconfig": "/etc/companest/substrate-kubeconfig",
    "context": "kind-companest-substrate",
    "storage_id": "example-storage-v1",
    "scopes": {
      "@shared": {
        "atespace": "ate-demo-counter",
        "template": "companest-sample",
        "egress_rules": [
          {
            "http": {
              "hostnames": ["companest-api"],
              "ports": {
                "numbers": [8000]
              }
            }
          },
          {
            "https": {
              "hostnames": ["api.deepseek.com"],
              "effects": {
                "replaceHeaders": [
                  {
                    "header": "Authorization",
                    "prefix": "Bearer ",
                    "credentialUri": "ate-secret://k8s.io/default/companest-models/deepseek/api-key"
                  }
                ]
              }
            }
          },
          {
            "https": {
              "hostnames": ["api.moonshot.cn"],
              "effects": {
                "replaceHeaders": [
                  {
                    "header": "Authorization",
                    "prefix": "Bearer ",
                    "credentialUri": "ate-secret://k8s.io/default/companest-models/moonshot/api-key"
                  }
                ]
              }
            }
          }
        ]
      }
    }
  },
  "control_state_dir": "/var/lib/companest"
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

Substrate actor storage must be provisioned before enabling execution. Set
`storage_lock_dir` to the shared lock mount; use retained shared memory/workspace
directories and separate per-actor journals. See [storage setup](substrate/storage.md).

`substrate.scopes.*.egress_rules` uses the pinned Substrate EgressPolicy rule schema.
Replace the callback hostname/port and credential references with your deployment
values; provision the referenced secrets and company-scoped provider grants separately.
Empty rules deny network access. Templates do not pass egress policy to new actors.
