# Reliable Agent Harness

[![CI](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml)

An operations assistant that will investigate service issues through validated tools,
bounded agent execution, and human approval before creating incidents.

**Status: M1 — mock tools and registry implemented.** Three mock handlers, synthetic
fixtures, input/output validation, and process-local incident deduplication now build
on M0 contracts/settings. Ordinary registry calls block incident creation pending
approval. The agent loop, fake/Gemini providers, and human approval workflow are
**not implemented yet**.

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

## Run the M1 tool demo

```sh
uv run python -m agent_harness.tools.demo --data-dir mock_data
```

The demo reads checkout status and runbooks, then shows `approval_required` and
`incident_count: 0` for an ordinary incident request. No LLM or network is used.
Dataset paths are explicit application configuration; see the [data dictionary](mock_data/README.md).
If a local macOS editable install cannot import `agent_harness`,
prefix the command with `PYTHONPATH=src`.

## Development checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

GitHub Actions runs these checks on pushes and pull requests using Python 3.12 and 3.13.
Use short-lived feature branches for subsequent work and open pull requests
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
mock_data/          Synthetic service statuses, runbooks, and data dictionary
postman/            Importable API collection
.github/workflows/  Continuous integration
```

Internal planning and walkthrough documents are kept in the git-ignored
`local_doc/` directory and are not included in a clone of this repository.

## Current limitations

This repository is not a completed assessment submission. There is no LLM
integration, execution storage, approval workflow, or automatic retry/budget control yet.
The mock incident adapter is available to trusted code; its internal claimed-action
bridge is not proof of human approval. M4 will validate ownership and atomically claim
stored approvals. Deduplication is limited to a single tool bundle/process lifetime.
The final report and execution Postman requests will arrive with the remaining features.
The M1 verification ran 122 offline test cases, lint and formatting checks, plus
a non-editable package installation and CLI demo from a separate working directory.
