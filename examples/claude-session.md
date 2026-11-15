# Claude Code session access

Claude Code can operate a running Companest service through the CLI or HTTP API. The session is a client: Companest owns job execution, company state, memory and schedules. No Telegram gateway or MCP server is required.

## Start the service

Install `.[agents,server,scheduler]`, copy the minimal sample's `.companest` directory, and configure the model-provider credentials in your private environment. The scheduler extra includes SQLite support for persisted jobs and events.

Set a private `COMPANEST_API_TOKEN` in both the server and client environments. Do not paste its value into prompts, tracked files or session output. Then run the server in a separate terminal or service:

```bash
companest serve -c deploy/config.prod.md --host 127.0.0.1 --port 8000
```

This configuration keeps `master.enabled` false. The server must remain running when the Claude Code session disconnects. Use an authenticated HTTPS endpoint when accessing a remote deployment.

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

Retain the returned `job_id` outside the session context and poll that job instead of resubmitting it. A submission whose response was lost may already have been accepted; check the job list before retrying. Job submission currently has no idempotency key. Use bounded polling with a delay, and report pending work when the session's waiting budget expires.

Cancellation marks a job cancelled and prevents its final result from replacing that state. It does not roll back side effects or guarantee that an already-running model or tool call stops immediately. Server restart recovery may retry interrupted jobs, so tasks with external side effects should be designed to tolerate retries.

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
    timeout=30,
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

The control plane remains a single process with async tasks. Blocking S3, archive, workspace-tool and memory/retrieval operations use worker threads; no process worker service is introduced. Shared memory indexes and caches are synchronized within a `MemoryManager` instance. Do not run multiple server processes against the same file-backed state.

Threaded operations cannot be forcibly stopped safely. Cancellation waits for an in-flight blocking operation to finish its cleanup before propagating; a completed write or upload can remain. Model/API timeouts and bounded task polling still apply. This is not a guarantee of exactly-once execution or a consistent snapshot of files changed by external processes.
