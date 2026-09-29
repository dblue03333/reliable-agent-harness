# Reliable Agent Harness

[![CI](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml)

An operations assistant built around validated tools, bounded agent execution,
and human approval before creating incidents.

**Status: M2 — offline read loop implemented.** A scripted fake provider drives
service lookup and runbook search through the harness. Executions have isolated
in-memory state, conversation/attempt history, ordered JSON events, step/runtime
limits and operation timeouts. Incident proposals terminate with `approval_required`
and zero side effects until the approval workflow is implemented in M4.
This is an incremental implementation, not a completed assessment submission.

## Quick start

Prerequisites: Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/dblue03333/reliable-agent-harness.git
cd reliable-agent-harness
uv sync --locked
uv run python -m agent_harness.demo --objective "Investigate checkout timeouts" --data-dir mock_data
```

The default configuration selects `fake` and needs no API key or network.
The CLI stores the objective, but **its responses follow a fixed script**; it does
not diagnose arbitrary objectives. JSON execution/history/events go to stdout;
JSON event logs go to stderr. Logs omit objectives, arguments and response bodies.
The stdout history contains the objective and observations; handle it accordingly.
Exit codes: `0` completed, `1` failed/limited execution, `2` invalid CLI/config/dataset.

Run the two controlled stopping scenarios (exit `1` is expected):

```sh
MAX_AGENT_STEPS=3 uv run python -m agent_harness.demo --scenario step-limit --objective "Investigate checkout" --data-dir mock_data
uv run python -m agent_harness.demo --scenario incident-blocked --objective "Create an incident" --data-dir mock_data
```

Dataset paths are relative to the current directory unless absolute. When running
from another directory, pass the absolute path to `mock_data`; see the
[data dictionary](mock_data/README.md). If a local macOS editable install cannot
import `agent_harness`, prefix commands with `PYTHONPATH=src` from the repository root.
The earlier tool-only demo remains available via `python -m agent_harness.tools.demo`.

## Runtime behavior

```text
objective -> CREATED -> claim RUNNING
  -> check runtime and remaining steps -> increment step -> call LLM
  -> validate JSON decision
     final -> COMPLETED
     read tool -> validate input -> execute within deadline -> validate output
               -> record result -> feed observation back to LLM -> repeat
     incident -> FAILED / approval_required (M4 will add pause/approve/resume)
  error -> FAILED; exhausted budget -> LIMIT_EXCEEDED
```

- Every LLM request consumes a step; tool attempts do not. A read on the last
  available step may finish, but there is no free LLM call to summarize it.
- Active runtime uses a monotonic clock across the entire run. Each operation is
  bounded by the smaller of its timeout and remaining runtime. Global expiry is
  `runtime_limit`; an earlier operation timeout is `llm_timeout` or `tool_timeout`.
- Validated tool successes remain in history if later work fails or exhausts limits.
- Malformed responses and tool failures stop immediately in M2. Automatic repair
  and retries are reserved for M5, even though their settings already exist.
- Cancellation records terminal state and any interrupted read attempt, then
  propagates `CancelledError` to the caller. Async adapters must cooperate with
  cancellation; asyncio cannot forcibly interrupt blocking code.
- A synchronous claim prevents two tasks on the same event loop from advancing
  one execution. Terminal executions cannot be rerun. Snapshots are deep copies;
  the store is for trusted harness code, not a security boundary.

## Configuration and API

Settings read `.env` in the working directory; process environment values override it.
See [.env.example](.env.example).

| Variable | Default | Current use |
| --- | --- | --- |
| `LLM_PROVIDER` | `fake` | Offline CLI requires fake; no silent fallback from Gemini |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | empty | Validated when Gemini selected; adapter deferred to M3 |
| `MAX_AGENT_STEPS` | `10` | LLM request cap |
| `MAX_ACTIVE_RUNTIME_SECONDS` | `60` | Total active run budget |
| `LLM_TIMEOUT_SECONDS` | `20` | Per LLM operation timeout |
| `DEFAULT_TOOL_TIMEOUT_SECONDS` | `5` | Per tool operation timeout |
| `MAX_LLM_REPAIR_ATTEMPTS` | `1` | Reserved for M5; no repair in M2 |
| `MAX_READ_TOOL_RETRIES` | `2` | Reserved for M5; no retry in M2 |
| `MAX_INCIDENT_RETRIES` | `1` | Reserved for M5; incident dispatch blocked in M2 |
| `LOG_LEVEL` | `INFO` | CLI event logging |

The API still exposes **health only**; execution endpoints are planned for M6:

```sh
uv run uvicorn agent_harness.api:app --reload
```

- API docs: http://127.0.0.1:8000/docs
- Health endpoint: http://127.0.0.1:8000/health
- Import `postman/agent-harness.postman_collection.json` to check health.

## Development checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests cover contracts/settings, all three mock tools, validation, incident
idempotency, read investigations, execution isolation, duplicate-run rejection,
safe event logs, malformed output, tool/provider failures, blocked incidents,
step/runtime limits, deadlines and cancellation. They require no API key or network.
GitHub Actions is configured for Python 3.12 and 3.13 on pushes and pull requests.

## Project layout

```text
src/agent_harness/
  models.py, contracts.py, errors.py   Validated data and safe errors
  config.py                          Settings
  harness.py                         Execution loop and policy
  storage.py                         In-memory snapshots and ordered events
  budget.py                          Active runtime and operation deadlines
  llm/base.py, llm/fake.py            Provider interface and offline script
  tools/                             Registry, handlers and tool-only demo
  demo.py                            Objective-driven CLI entry point
  api.py                             Health endpoint
tests/                              Automated tests
mock_data/                          Synthetic statuses/runbooks and data dictionary
postman/                            Importable health API collection
.github/workflows/                  CI
```

Internal plans, evidence and walkthroughs live in the git-ignored `local_doc/`
directory and are not included in a clone of this repository.

## Current limitations and next steps

State/events and incident deduplication live in one process; restart loses them.
The store supports one event loop and has no multi-worker/thread guarantees,
authentication, persistence, retention limits or restart recovery. Event logging
is standard Python logging, not a durable audit log or OpenTelemetry exporter.
The mock incident adapter's internal claimed-action bridge is for trusted code;
it is not proof of human approval. The harness never invokes it in M2.

Next: real Gemini schema smoke (M3), approval/resume with action ownership (M4),
retries/repair and ambiguity handling (M5), execution API/Postman (M6), live
verification and submission documentation (M7/M8). No live LLM integration or
complete approval workflow is claimed by the current offline tests.
