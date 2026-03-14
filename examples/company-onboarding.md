# Company Onboarding

This guide shows how an external company repo can register itself with a running Companest control plane and start using shared infrastructure.

## What Companest Owns

Companest provides the shared runtime:

- REST API
- job queue
- scheduler
- memory isolation
- team routing
- cost controls
- company lifecycle management

## What Your Company Repo Owns

Your company repo provides:

- manifest metadata
- private team definitions
- routing bindings
- schedules
- optional company-scoped MCP servers
- initial memory seed

## Before You Start

Your Companest host should already be running and should have:

- a valid `.companest/` runtime directory
- any shared teams your company wants to use, such as `general`
- `COMPANEST_API_TOKEN` set when running in production

Start the server locally:

```bash
companest serve
```

## Registration Flow

The normal lifecycle is:

1. Build a manifest in your company repo.
2. `POST /api/companies` to register it.
3. Confirm the company via `GET /api/companies/{id}`.
4. Submit work via `POST /api/jobs` with `company_id`.
5. Inspect job history via `GET /api/companies/{id}/jobs`.
6. Update with `PATCH /api/companies/{id}` when the manifest changes.
7. Remove with `DELETE /api/companies/{id}` when done.

## Manifest Shape

Companest accepts a single JSON manifest that includes inline team definitions.

If you want a copy-paste starting point, use:

- [`company-template/manifest.json`](./company-template/manifest.json)
- [`company-template/register.py`](./company-template/register.py)
- [`company-template/README.md`](./company-template/README.md)

Minimal example:

```json
{
  "id": "acme-research",
  "name": "Acme Research",
  "domain": "Market intelligence and research automation",
  "enabled": true,
  "shared_teams": ["general"],
  "routing_bindings": [
    {
      "pattern": "research|market map|competitor",
      "team_id": "acme-research/analyst-team",
      "mode": "cascade"
    }
  ],
  "schedules": [
    {
      "name": "daily-briefing",
      "team_id": "acme-research/analyst-team",
      "prompt": "Refresh the daily company briefing from current memory and sources.",
      "interval_seconds": 3600,
      "mode": "cascade",
      "enabled": true
    }
  ],
  "memory_seed": {
    "shared": {
      "briefing-config.json": {
        "topics": ["competitors", "pricing", "launches"]
      }
    },
    "teams": {}
  },
  "mcp_servers": [],
  "teams": [
    {
      "id": "analyst-team",
      "team_md": "# Team: analyst-team\n- role: research\n- lead_pi: analyst\n- enabled: true\n- mode: cascade\n\n#### Pi: analyst\n- model: claude-sonnet-4-5-20250929\n- tools: memory_read, memory_write, brave_search\n- max_turns: 12",
      "pis": [
        {
          "id": "analyst",
          "soul_md": "You are Acme's research analyst. Produce concise, actionable findings."
        }
      ]
    }
  ]
}
```

## Manifest Field Notes

- `id`: company identifier. This becomes the namespace prefix for private teams.
- `shared_teams`: explicit allowlist of global teams this company may access.
- `routing_bindings`: fast regex routes owned by this company.
- `schedules`: company-scoped background jobs. These are registered immediately.
- `memory_seed`: written only when the target file does not already exist.
- `mcp_servers`: registered only for this company.
- `teams`: inline private team definitions written into `companies/{id}/teams/`.

## Team And Pi ID Rules

Companest validates inline team IDs and Pi IDs before writing them to disk.

Safe examples:

- `analyst-team`
- `collector`
- `research_v2`

Unsafe examples:

- `../../etc`
- `team/name`
- `team name`

## Recommended Repo Layout

Your external repo can stay simple:

```text
my-company/
  companest/
    manifest.json
    register.py
    README.md
```

You do not need to copy files onto the Companest host manually if you use inline `teams`.

## Access Control Rules

- Private teams are visible only to their owning company.
- Global teams are available only if they are listed in `shared_teams`.
- `POST /api/jobs` should include the top-level `company_id`.
- Companest normalizes `company_id` into the execution context so routing, memory, budgets, and tool access remain company-scoped.

## Register A Company

Example using `curl`:

```bash
curl -X POST http://localhost:8000/api/companies \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $COMPANEST_API_TOKEN" \
  --data @companest/manifest.json
```

If auth is disabled in debug mode, omit the `Authorization` header.

## Inspect Runtime State

After registration:

```bash
curl http://localhost:8000/api/companies/acme-research
curl http://localhost:8000/api/fleet/status
```

You should see:

- namespaced private teams such as `acme-research/analyst-team`
- company scheduler tasks
- recent jobs for that company

## Submit Work

Submit a job against the company:

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $COMPANEST_API_TOKEN" \
  -d '{
    "task": "Prepare a competitor summary for this week",
    "company_id": "acme-research",
    "context": {
      "priority": "normal"
    }
  }'
```

## Update A Company

Use `PATCH /api/companies/{id}` with the updated manifest fields.

Important behavior:

- if `teams` is included, Companest treats it as the new source of truth for inline teams
- stale team directories not present in the new payload are removed
- the company is torn down and re-applied immediately

## Delete A Company

Delete with:

```bash
curl -X DELETE http://localhost:8000/api/companies/acme-research \
  -H "Authorization: Bearer $COMPANEST_API_TOKEN"
```

Companest will clean up:

- company scheduler tasks
- company router bindings
- company enrichments
- private teams
- team path overrides
- company-scoped MCP servers

## Shared Team Guidance

For new company manifests, prefer setting `shared_teams` explicitly.

Recommended:

```json
{
  "shared_teams": ["general"]
}
```

Avoid relying on implicit access. Explicit allowlists are clearer and safer.

## Starting Points

Use the prediction market example as a reference implementation:

- [`prediction-market/manifest.json`](./prediction-market/manifest.json)
- [`prediction-market/README.md`](./prediction-market/README.md)
- [`prediction-market/register.py`](./prediction-market/register.py)

For a generic starting point, use:

- [`company-template/manifest.json`](./company-template/manifest.json)
- [`company-template/README.md`](./company-template/README.md)

## Readiness Checklist

- Companest server starts cleanly.
- Shared teams required by your manifest already exist.
- `POST /api/companies` returns success.
- `GET /api/companies/{id}` shows your private teams and schedules.
- `POST /api/jobs` with `company_id` succeeds.
- Background schedules appear in `/api/fleet/status`.
- `DELETE /api/companies/{id}` removes all company-scoped runtime resources.
