# Claude Code session access

Claude Code can operate a running Companest service through the CLI or HTTP API. The session is a client: Companest owns job execution, company state, memory and schedules. No Telegram gateway or MCP server is required.

## Start the service

Install `.[agents,server,scheduler]`, copy the minimal sample's `.companest` directory, and configure the model-provider credentials in your private environment. The scheduler extra includes SQLite support for persisted jobs and events.

Set a private `COMPANEST_API_TOKEN` in both the server and client environments. Do not paste its value into prompts, tracked files or session output. Then run the server in a separate terminal or service:

```bash
companest serve -c deploy/config.prod.md --host 127.0.0.1 --port 8000
```

This configuration keeps `master.enabled` false. Jobs continue in Substrate when the client or API process disconnects. The API must be available for budget/schedule callbacks; loss of that service fails closed before further authorized work. Use an authenticated HTTPS endpoint when accessing a remote deployment.

## Use the CLI from a session

```bash
export COMPANEST_API_URL=http://127.0.0.1:8000
companest team list --json
companest job submit "Summarize the current research briefing" --team-id general --submitted-by claude-code --json
companest job status <job-id> --json
companest job list --status running --json
companest job cancel <job-id> --json
```

For a registered company, add `--company-id <company-id>`. To use a registered workspace, add `--workspace-id <workspace-id>`. Omit `--team-id` to auto-route. These IDs select existing server-side resources; they do not register a company or grant permissions. `--submitted-by` is an audit label, not an authentication identity.

`--json` emits one complete API response on stdout, including the full job result. CLI/API failures return a nonzero exit code and JSON on stderr. A successful status query can still report a failed or cancelled job: always inspect `status` and `error`.

Retain the returned `job_id` outside the session context and poll that job instead of resubmitting it. A submission whose response was lost may already have been accepted; check the job list before retrying. Use a stable `--idempotency-key` (HTTP `idempotency_key`) when retrying the same task/context. Reusing a key with different content is rejected. Use bounded polling with a delay, and report pending work when the session's waiting budget expires.

Cancellation destroys the actor through Substrate and is acknowledged only after
that operation succeeds. It does not roll back external effects. If deletion is
unavailable, the API returns 503 and saves the cancellation intent; later job
queries reconcile it. Server restart reconnects to the saved actor reference
and never replays unknown work. Inspect `execution.error` as well as job status:
transport uncertainty does not imply that a task failed.

A session instruction can be:

> Use the installed `companest` CLI with `--json`. Discover available teams, submit the requested task once, retain its job ID, and query status until completed, failed, or cancelled. Read the full result from the JSON response. Use the configured API URL and token without displaying credentials. Ask before taking actions outside the user's requested scope.

## Use the HTTP API directly

The existing endpoints provide the same workflow:

- `GET /api/teams`: discover teams.
- `POST /api/jobs`: submit `task`, optional `company_id`, `context.team_id`, `context.workspace_id`, and `submitted_by`.
- `GET /api/jobs/{job_id}`: retrieve status, full result and error.
- `GET /api/jobs?company_id=<company-id>`: list company jobs.
- `POST /api/jobs/{job_id}/cancel`: request cancellation.

This example reads credentials from the environment and submits one job:

```python
import os
import httpx

with httpx.Client(
    base_url=os.environ.get("COMPANEST_API_URL", "http://127.0.0.1:8000"),
    headers={"Authorization": f"Bearer {os.environ['COMPANEST_API_TOKEN']}"},
    timeout=90,
) as client:
    response = client.post("/api/jobs", json={
        "task": "Summarize the current research briefing",
        "context": {"team_id": "general"},
        "submitted_by": "claude-code",
    })
    response.raise_for_status()
    print(response.json())
```

The API token is an administrative service credential, not a per-company identity. Company-scoped task routing does not turn it into a tenant-limited credential.

## I/O and concurrency

The control plane remains one process per durable job database. Substrate owns
execution instances, placement, suspension and recovery. Shared memory uses a
process-shared filesystem lock; workspace writers are serialized by a lock on
the shared lock mount. Mutable state is never copied into each job image.

A memory snapshot can resume an in-flight actor process. A cold startup with a
running journal marks an unknown outcome and does not replay the task. This is
not an exactly-once guarantee for model/tool effects. See the deployment guide
for the storage and ingress policy requirements.

Company creation, updates, deletion, binding writes and configuration reloads also offload disk I/O. Lifecycle changes are serialized, and configuration readers see the previous complete registry until a disk operation succeeds. Once a lifecycle change starts, client cancellation waits for it to finish before releasing the writer lock; cancellation is not a rollback.

For embedded async services, use `await orchestrator.init_teams_async(...)` before accepting requests. The synchronous `init_teams(...)` remains available for synchronous/offline callers. Synchronous component `on_init` and `on_teardown` hooks run in worker threads. Use the context's public tool-registry, event-bus and routing methods to register runtime behavior; those calls are dispatched to the owning event loop. Memory namespace methods are synchronized for worker access. Hooks declared `async def` are awaited on the event loop and must offload their own blocking work. Hooks must not directly mutate private runtime attributes or wait for another company lifecycle operation to complete.
