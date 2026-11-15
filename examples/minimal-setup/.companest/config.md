# Minimal Companest Configuration

A minimal local development configuration for the public sample.

```json
{
  "name": "companest-minimal",
  "version": "2.0",
  "api": {
    "host": "127.0.0.1",
    "port": 8000,
    "auth_token": "${COMPANEST_API_TOKEN}"
  },
  "master": {
    "enabled": false
  },
  "proxy": {
    "enabled": false
  },
  "debug": true,
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

- `debug: true` allows local runs without `COMPANEST_API_TOKEN`
- Set `debug: false` and configure `COMPANEST_API_TOKEN` before exposing the API
- Proxy is disabled in the public sample so Pis call provider SDKs directly

`substrate.scopes.*.egress_rules` uses the pinned Substrate EgressPolicy rule schema.
Replace the callback hostname/port and credential references with your deployment
values; provision the referenced secrets and company-scoped provider grants separately.
Empty rules deny network access. Templates do not pass egress policy to new actors.
