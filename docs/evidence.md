# Verification evidence

## M0 — Contracts and settings

Scope: strict decision/tool/state schemas, safe errors, environment settings,
startup validation, and an async provider protocol. No execution loop or live adapter yet.

- Environment: local macOS, Python 3.12.13.
- Offline tests: 83 passed (including parametrized invalid-input cases).
- Ruff lint and formatting checks passed.
- Fresh isolated virtualenv: locked non-editable package installation, then full tests.
- No API key, real model call, or network call in tests.
- Existing GitHub CI configuration retained; this milestone has not been pushed or run on GitHub yet.

Commands used for source validation:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check
```

Fresh-install verification used `uv sync --locked --offline --no-editable` with a temporary
`UV_PROJECT_ENVIRONMENT`, followed by that environment's `python -m pytest -q`.
This verifies installed-package imports independently of the local editable `.pth` issue
described in [the walkthrough](m0-walkthrough.md).

What the tests establish: schemas reject invalid data, state representations preserve
successful effects separately from execution failures, configuration precedence/limits
work, secrets are excluded from public settings output, and invalid live config prevents
application startup.

They do not establish runtime safety, retries, budget enforcement, approval atomicity,
durable recovery, or real Gemini integration. Those remain later milestone gates.
