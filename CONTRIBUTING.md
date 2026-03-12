# Contributing to Companest

Thanks for helping improve Companest.

## Setup

```bash
git clone https://github.com/<your-username>/Companest.git
cd Companest
pip install -e ".[dev]"
companest validate
```

## Test commands

```bash
pytest tests/ -q
pytest -m live_network -q
python -m compileall companest tests -q
```

`live_network` tests are excluded by default because they call real external services.

## Expectations

- Add or update tests for behavior changes.
- Keep public docs and examples aligned with the code.
- Prefer small, reviewable pull requests.
- Use Conventional Commits when practical.

## Pull Requests

Open pull requests against `main` and include enough context for a reviewer to understand the user-facing impact, risk, and test coverage.
