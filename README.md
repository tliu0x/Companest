# Companest

Multi-Agent Company Incubator

Companest is a framework for building agent-native companies: specialized AI teams that route work, collaborate across roles, manage cost, and operate through structured company-style workflows.

This repository is the first public alpha release of a system developed through a longer private incubation phase.

## Status

Alpha. Interfaces may still change while packaging, docs, and operational defaults continue to harden.

## Quick Start

### Install

```bash
pip install -e ".[claude,server]"
```

### Run the public sample

```bash
cp -r examples/minimal-setup/.companest .companest
cp examples/minimal-setup/.env.example .env
companest validate
companest serve
companest team run general "What is the capital of France-"
```

### Config discovery order

Companest looks for configuration in this order:

1. `.companest/config.md`
2. `.companest/config.json`
3. `companest.config.md`
4. `companest.config.json`

Environment variable interpolation supports `${VAR}` and `${VAR:-default}`.

## Public sample layout

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
```

If API auth is enabled, export `COMPANEST_API_TOKEN` before using HTTP-backed CLI commands such as `companest team run`, `companest fleet status`, and `companest job submit`.

## Execution Modes

- `default`: single lead Pi handles the task
- `cascade`: move from cheaper to stronger models as needed
- `loop`: decompose, iterate, and synthesize
- `council`: parallel opinions with optional judge scoring
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

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes for Claude models | Anthropic API key |
| `OPENAI_API_KEY` | Optional | OpenAI provider support |
| `COMPANEST_API_TOKEN` | Required in production | REST API bearer token |
| `COMPANEST_MASTER_TOKEN` | Required when using gateway mode | Gateway auth token |
| `LITELLM_MASTER_KEY` | Optional | LiteLLM admin key |
| `LITELLM_DEFAULT_KEY` | Optional | LiteLLM default virtual key |
| `BRAVE_API_KEY` | Optional | Brave Search feed support |
| `X_BEARER_TOKEN` | Optional | X/Twitter feed support |
| `OPENBB_API_URL` | Optional | OpenBB feed server URL |
| `AWS_ACCESS_KEY_ID` | Optional | AWS credentials for S3 archiver |
| `AWS_SECRET_ACCESS_KEY` | Optional | AWS credentials for S3 archiver |
| `AWS_DEFAULT_REGION` | Optional | AWS region for S3 archiver |
| `COMPANEST_S3_BUCKET` | Optional | S3 bucket for memory archives |

## License

Apache License 2.0
