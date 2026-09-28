# Reliable Agent Harness

[![CI](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml)

An operations assistant that will investigate service issues through validated tools,
bounded agent execution, and human approval before creating incidents.

**Status: project bootstrap.** The API health endpoint, development environment,
smoke test, CI, and implementation plan are available. The agent loop, mock tools,
and approval endpoints are planned and are **not implemented yet**.

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

The bootstrap does not require an API key or environment variables.

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

See [architecture](docs/architecture.md) and [roadmap](docs/roadmap.md) for the
intended harness behavior, safety invariants, test coverage, and submission work.

## Current limitations

This repository is not a completed assessment submission. There is no LLM
integration, execution storage, incident creation, or approval workflow yet.
The existing test verifies API bootstrapping only. The final report, execution
Postman requests, mock dataset, and failure tests will be added with the features
they document.
