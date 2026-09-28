# Reliable Agent Harness

[![CI](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml)

An operations assistant that will investigate service issues through validated tools,
bounded agent execution, and human approval before creating incidents.

**Status: M0 — contracts and settings implemented.** Strict decision/tool/state
schemas, environment configuration, startup validation, and an async provider
interface are available alongside the health API and CI configuration. The agent
loop, mock tool handlers, fake/Gemini providers, and approval endpoints are
**not implemented yet**. Start with the [M0 walkthrough](docs/m0-walkthrough.md).

## Quick start

Prerequisites: Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/dblue03333/reliable-agent-harness.git
cd reliable-agent-harness
uv sync --locked
uv run uvicorn agent_harness.api:app --reload
```

- API docs: http://127.0.0.1:8000/docs
- Health endpoint: http://127.0.0.1:8000/health
- Import `postman/agent-harness.postman_collection.json` to check the running API.

The default configuration selects `fake` and needs no API key. Settings read `.env`
from the working directory; process environment values override it. See `.env.example`.
Selecting `gemini` requires both `GEMINI_API_KEY` and `GEMINI_MODEL` at startup, but
the Gemini adapter itself will be implemented in M3. Budget values are currently
validated configuration; runtime enforcement is a later milestone.

## Development checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

GitHub Actions runs these checks on pushes and pull requests using Python 3.12 and 3.13.
Use short-lived `codex/<topic>` branches for subsequent work and open pull requests
against `main`.

## Planned use case

> Customers are reporting checkout timeouts. Investigate the issue and create
> an incident if necessary.

The agent will inspect service status, search runbooks, separate observations
from hypotheses, and propose an incident. The harness must pause before the
side-effecting tool and execute only the exact action approved by a human.

## Project layout

```text
src/agent_harness/   Python package and API entry point
tests/              Automated tests
docs/               Architecture decisions and delivery roadmap
mock_data/          Location for planned synthetic tool fixtures
postman/            Importable API collection
.github/workflows/  Continuous integration
```

See the detailed [Q4 implementation plan](docs/implementation-plan.md),
[architecture](docs/architecture.md), and [roadmap](docs/roadmap.md) for the intended
harness behavior, safety invariants, test coverage, and submission work.

## Current limitations

This repository is not a completed assessment submission. There is no LLM
integration, execution storage, incident creation, or approval workflow yet.
M0 tests verify contracts and configuration, not runtime approval or retry safety.
The final report, execution Postman requests, mock dataset, and runtime failure
tests will be added with the features they document. See [verification evidence](docs/evidence.md).
