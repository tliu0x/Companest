# Security Policy

## Supported Versions

Companest is currently in alpha. Security fixes are applied to the latest public `main` branch and the newest tagged release.

## Reporting a Vulnerability

Please use a GitHub private security advisory for this repository:

- [Open a private advisory](https://github.com/taoge64/Companest/security/advisories/new)

Include a clear description, reproduction steps, impact, and any suggested mitigation.

Do not open public issues for security vulnerabilities.

## Current Security Expectations

- `COMPANEST_API_TOKEN` is required for production API deployments.
- `COMPANEST_MASTER_TOKEN` is required when the gateway connection is enabled.
- Terraform requires an explicit SSH CIDR; there is no permissive default.
- Operators should review `tools_deny` carefully before enabling shell access.

## Operational Best Practices

- Rotate `COMPANEST_API_TOKEN`, `COMPANEST_MASTER_TOKEN`, and `LITELLM_MASTER_KEY` regularly.
- Store secrets in environment variables or a secrets manager.
- Restrict API and gateway network exposure.
- Review public examples before using them as production defaults.
