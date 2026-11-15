# Companest

Multi-Agent Company Incubator

Companest is a control plane for agent-native companies. It runs shared teams and company-private teams in the same runtime, routes work across teams, manages background execution, and exposes an API for multi-company operation.

## Status

Alpha, but usable today as a shared runtime for multiple companies. Interfaces may still change while packaging, docs, and operational defaults continue to harden.

## Public Repository Privacy

This repository is public. Keep personal information out of tracked files, Git history, commit messages, and public PR descriptions or comments. This rule applies to human contributors and coding agents.

- Do not include personal names, private email addresses, phone numbers, addresses, personal account identifiers, local home-directory paths, or links to private AI sessions. Keep repository URLs needed for installation, attribution, and security reporting; do not add unrelated personal profiles or account history.
- Use fictional data and placeholders in documentation, examples, fixtures, and logs. Keep real credentials, personal configuration, and runtime data outside version control.
- Before committing, configure and verify a non-personal Git author and committer identity for this checkout. Do not assume the machine's global Git identity is safe for publication; a noreply email alone does not hide a personal author name.
- Before pushing or publishing a PR, review staged content, outgoing commit metadata and messages, and the PR text for personal information. Never reproduce actual personal data in a cleanup description or test fixture.
- If personal information was already published, remove it from current content and audit every affected branch, tag, and historical commit. A new deletion commit does not remove older copies. Coordinate any history rewrite and follow up on retained PR references or cached views with the hosting provider.

See [SECURITY.md](./SECURITY.md) for private security reporting.

## What Companest Does

- runs shared teams and company-private teams in one runtime
- lets external company repos register by manifest over HTTP
- scopes jobs, routing, schedules, memory, and MCP tools by company
- supports background execution through jobs, scheduler, and teardown lifecycle
- exposes a control-plane API for fleet status, companies, and jobs

## Installation

### Runtime Install

Use this when you want to run the API server and agent teams:

```bash
pip install -e ".[agents,server]"
```

### Development Install

Use this when you want the full test and tooling set:

```bash
pip install -e ".[dev]"
```

## Quick Start

### Run The Public Sample

```bash
cp -r examples/minimal-setup/.companest .companest
cp examples/minimal-setup/.env.example .env
companest validate
companest serve
companest team run general "What is the capital of France?"
```

### Config Discovery Order

Companest looks for configuration in this order:

1. `.companest/config.md`
2. `.companest/config.json`
3. `companest.config.md`
4. `companest.config.json`

Environment variable interpolation supports `${VAR}` and `${VAR:-default}`.

## Public Sample Layout

```text
.companest/
  config.md
  workspaces.json
  teams/
    general/
      team.md
      pis/
        assistant/
          soul.md
    coding/
      team.md
      pis/
        coder/
          soul.md
        reviewer/
          soul.md
    info-collection/
      team.md
      memory/
        watchlist.json
      pis/
        collector/
          soul.md
```

## External Company Workflow

Companest can act as a shared control plane for external company repos that register themselves by manifest.

- Registration guide: [`examples/company-onboarding.md`](./examples/company-onboarding.md)
- Generic template: [`examples/company-template/README.md`](./examples/company-template/README.md)
- Example extension: [`examples/prediction-market/README.md`](./examples/prediction-market/README.md)

## External Company API

The main multi-company workflow is:

1. `POST /api/companies` to register a manifest
2. `GET /api/companies/{id}` to inspect runtime state
3. `POST /api/jobs` with `company_id` to submit scoped work
4. `GET /api/companies/{id}/jobs` to inspect company job history
5. `PATCH /api/companies/{id}` to update a manifest
6. `DELETE /api/companies/{id}` to tear the company down cleanly

See [`examples/company-onboarding.md`](./examples/company-onboarding.md) for the manifest shape and `curl` examples.

## Documentation

- Examples index: [`examples/README.md`](./examples/README.md)
- Company onboarding: [`examples/company-onboarding.md`](./examples/company-onboarding.md)
- Generic company template: [`examples/company-template/README.md`](./examples/company-template/README.md)
- Prediction market example: [`examples/prediction-market/README.md`](./examples/prediction-market/README.md)
- Production config baseline: [`deploy/config.prod.md`](./deploy/config.prod.md)

## CLI

```bash
companest init
companest validate
companest serve
companest serve -c .companest/config.md
companest team list
companest team run <team> "<task>"
companest fleet status
companest finance summary
companest job submit "<task>"
companest job status <id>
companest company list
companest company status <company_id>
companest company create <company_id> --name "Acme" --domain "Research"
```

If API auth is enabled, export `COMPANEST_API_TOKEN` before using HTTP-backed CLI commands such as `companest team run`, `companest fleet status`, and `companest job submit`.

## Execution Modes

- `default`: one lead Pi handles the task
- `cascade`: move from cheaper models to stronger models as needed
- `loop`: decompose, iterate, and synthesize
- `council`: run parallel opinions with optional judging
- `collaborative`: pipeline one Pi into another
- `conditional`: branch based on lead Pi decisions

## Deployment

### Docker

```bash
cd deploy
docker compose up -d
```

### Terraform

```bash
cd infra
terraform init
terraform apply
./infra/companest-ctl.sh deploy
```

You must set `allowed_ssh_cidr` explicitly before deploying.

For a documented production baseline, start from [`deploy/config.prod.md`](./deploy/config.prod.md).

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | Yes (default provider) | DeepSeek API key (default model: deepseek-chat) |
| `MOONSHOT_API_KEY` | Yes (for routing) | Kimi/Moonshot API key (router model: kimi-k2.5) |
| `ANTHROPIC_API_KEY` | Optional | Anthropic API key (for Claude models, not required by default) |
| `OPENAI_API_KEY` | Optional | OpenAI provider support |
| `COMPANEST_API_TOKEN` | Required in production | REST API bearer token |
| `COMPANEST_MASTER_TOKEN` | Required only when gateway mode is enabled | Gateway auth token |
| `LITELLM_MASTER_KEY` | Optional | LiteLLM admin key |
| `LITELLM_DEFAULT_KEY` | Optional | LiteLLM default virtual key |
| `BRAVE_API_KEY` | Optional | Brave Search feed support |
| `X_BEARER_TOKEN` | Optional | X or Twitter feed support |
| `OPENBB_API_URL` | Optional | OpenBB feed server URL |
| `AWS_ACCESS_KEY_ID` | Optional | AWS credentials for S3 archiver |
| `AWS_SECRET_ACCESS_KEY` | Optional | AWS credentials for S3 archiver |
| `AWS_DEFAULT_REGION` | Optional | AWS region for S3 archiver |
| `COMPANEST_S3_BUCKET` | Optional | S3 bucket for memory archives |

## License

Apache License 2.0
