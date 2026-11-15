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
        "template": "companest-sample"
      }
    }
  },
  "control_state_dir": "/var/lib/companest"
}
```

- `debug: true` allows local runs without `COMPANEST_API_TOKEN`
- Set `debug: false` and configure `COMPANEST_API_TOKEN` before exposing the API
- Proxy is disabled in the public sample so Pis call provider SDKs directly
