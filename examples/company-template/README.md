# Company Template

Copy this folder into a new external company repo when you want the smallest possible starting point for registration into a Companest control plane.

## Included Files

- `manifest.json`
- `register.py`

## Quick Start

1. Rename `your-company-id` in `manifest.json`.
2. Update company name, domain, routing keywords, schedules, and team prompt.
3. Register it against a running Companest server:

```bash
python register.py http://localhost:8000
```

With auth:

```bash
COMPANEST_API_TOKEN=your-token python register.py http://localhost:8000
```

## What To Edit

- `id`: must be globally unique on the Companest instance
- `shared_teams`: global teams this company may use
- `routing_bindings`: regexes that should fast-route into your private team
- `schedules`: background jobs that should run after registration
- `memory_seed`: files created only when missing
- `teams`: inline private team definitions

## Recommended First Pass

For a first integration, keep it simple:

- one private `analyst-team`
- one `shared_teams` entry: `general`
- one routing binding
- one hourly schedule

Once that works, add more teams, schedules, and MCP servers.

## Recommended Next Step

After the first registration succeeds:

1. Check `GET /api/companies/{id}`.
2. Submit one job with `POST /api/jobs`.
3. Confirm the team routes correctly.
4. Confirm the company schedule appears in fleet status.
