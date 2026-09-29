# Reliable Agent Harness

[![CI](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml)

An operations assistant built around validated tools, bounded agent execution,
and human approval before creating incidents.

**Status: M4 — approval/resume implemented.**
The harness accepts objectives, validates LLM/tool decisions, tracks isolated state/history,
and enforces step/runtime limits. Incident proposals now pause for explicit human approval
of the exact stored action. Approve executes it once; reject resumes with a denial observation.
Duplicate or competing decisions cannot claim the same action twice.

Gemini schema smoke passed on 2026-09-29 using `gemini-3.5-flash-lite` and SDK `1.75.0`.
M4 is verified offline; full live investigation/approval E2E is still a later gate.
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
Exit codes: `0` completed, `1` failed/limited execution, `2` invalid CLI/config/dataset,
`3` waiting for approval.

Run controlled stopping scenarios (step-limit exits `1`; incident-pending exits `3`):

```sh
MAX_AGENT_STEPS=3 uv run python -m agent_harness.demo --scenario step-limit --objective "Investigate checkout" --data-dir mock_data
uv run python -m agent_harness.demo --scenario incident-pending --objective "Create an incident" --data-dir mock_data
```

Run the interactive approval demo in one process:

```sh
uv run python -m agent_harness.demo --scenario approval --objective "Propose a checkout incident" --data-dir mock_data
```

The CLI displays the exact title, description, severity and action ID, then waits for
`approve` or `reject`. Approve creates one mock incident; reject creates none. Any other
input or EOF leaves the action pending. The scripted final answer directs you to the
actual recorded outcome instead of assuming approval. `incident-blocked` remains an
alias for `incident-pending` for earlier demo commands.

State is in memory: when the CLI exits, its pending execution is lost. To approve/reject,
use the interactive scenario in that same process, or keep one `AgentHarness` instance
alive and call `approve(execution_id, action_id)` / `reject(execution_id, action_id)`.
These are trusted application entry points, not authentication or HTTP endpoints.

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
     incident -> save exact action -> WAITING_APPROVAL
        approve(action_id) -> claim action -> execute -> record result -> resume loop
        reject(action_id)  -> record denial (no dispatch) -> resume loop
  error -> FAILED; exhausted budget -> LIMIT_EXCEEDED
```

- Every LLM request consumes a step; tool attempts do not. A read on the last
  available step may finish, but there is no free LLM call to summarize it.
- Active runtime uses a monotonic clock across the entire run. Each operation is
  bounded by the smaller of its timeout and remaining runtime. Global expiry is
  `runtime_limit`; an earlier operation timeout is `llm_timeout` or `tool_timeout`.
- Validated tool successes remain in history if later work fails or exhausts limits.
- Malformed responses and tool failures stop immediately in M4. Automatic repair
  and retries are reserved for M5, even though their settings already exist.
- Cancellation records terminal state and any interrupted read attempt, then
  propagates `CancelledError` to the caller. Async adapters must cooperate with
  cancellation; asyncio cannot forcibly interrupt blocking code.
- A per-execution lock protects initial claim and approval/rejection decisions. It
  is released before tool/LLM I/O. Synchronous state updates and detached snapshots
  are consistent on one event loop; this does not support multiple workers/threads.
- Approve/reject accept only execution/action IDs, never replacement arguments.
  Foreign/missing actions return `not_found`; stale/duplicate decisions return
  `action_conflict`. A new proposal always needs a new approval, even after rejection.
- Human waiting time is excluded from active runtime. Resume carries forward time
  and step counters. An incident proposed on the last step may execute after approval
  if runtime remains; a later summary can still end with `step_limit`.
- Before dispatch, interruption resolves the action as failed / `not_dispatched`.
  After dispatch, timeout/cancellation/invalid output or other unverified error is
  conservatively `outcome_unknown`. No automatic retries occur in M4. An unknown
  outcome does not prove that no incident was created; check the external system.
- Successful incident receipts remain recorded even if subsequent LLM work fails,
  is cancelled, or reaches a budget limit. Terminal executions cannot be resumed.
- This single-user local harness trusts the human-facing caller of approve/reject;
  authentication and authorization of remote users are not implemented.

## Configuration and API

Settings read `.env` in the working directory; process environment values override it.
See [.env.example](.env.example).

| Variable | Default | Current use |
| --- | --- | --- |
| `LLM_PROVIDER` | `fake` | Offline CLI requires fake; no silent fallback from Gemini |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | empty | Required for the M3 Gemini adapter/smoke; no model default |
| `MAX_AGENT_STEPS` | `10` | LLM request cap |
| `MAX_ACTIVE_RUNTIME_SECONDS` | `60` | Total active run budget |
| `LLM_TIMEOUT_SECONDS` | `20` | Per LLM operation timeout |
| `DEFAULT_TOOL_TIMEOUT_SECONDS` | `5` | Per tool operation timeout |
| `MAX_LLM_REPAIR_ATTEMPTS` | `1` | Reserved for M5; no repair in M4 |
| `MAX_READ_TOOL_RETRIES` | `2` | Reserved for M5; no retry in M4 |
| `MAX_INCIDENT_RETRIES` | `1` | Reserved for M5; M4 makes one approved attempt |
| `LOG_LEVEL` | `INFO` | CLI event logging |

The API still exposes **health only**; execution endpoints are planned for M6:

```sh
uv run uvicorn agent_harness.api:app --reload
```

- API docs: http://127.0.0.1:8000/docs
- Health endpoint: http://127.0.0.1:8000/health
- Import `postman/agent-harness.postman_collection.json` to check health.

## Gemini schema smoke (M3, opt-in)

Create a local `.env` (git-ignored) or set the equivalent environment variables:

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=<your-local-key>
GEMINI_MODEL=gemini-3.5-flash-lite
```

Never commit a real key. The example model passed the live smoke on 2026-09-29; runtime settings still require
an explicit model ID. Availability and access can change, so rerun smoke when switching models.
Run from the repository root; these commands make real API calls and may incur usage:

```sh
uv run python -m agent_harness.llm.smoke --data-dir mock_data
# Alternative: the explicitly opted-in live test
RUN_LIVE_TESTS=1 uv run pytest tests/live -m live -q
```

Use either command for a smoke run; running both repeats the API calls. The smoke
requests one service-status tool proposal and one final decision, validates both with
the existing contracts, and **never executes a tool**. Successful output includes the
actual configured model, SDK version, schema fingerprint, timestamp and request count.
Failure returns a safe error and nonzero exit; missing configuration never falls back
to the fake provider. The verified schema uses a required `decision` envelope around
the contract-derived union. The adapter unwraps that exact field, then the harness validates
the inner decision. A root-level union produced `{}` during live testing and was replaced;
local decision/tool validation was not relaxed.

`GeminiLLMProvider` implements the same interface used by `AgentHarness`; the fake CLI
remains explicitly offline. If `.env` selects Gemini, run the fake demo with
`LLM_PROVIDER=fake uv run python -m agent_harness.demo --objective "Inspect checkout"`.
The HTTP API still has no execution routes. Full live read/approval flows remain M7.

The SDK receives metadata/schema and messages, never tool handlers. Tool observations
stay in user content marked as untrusted data; they are not promoted to system instructions.
Structured output guides generation; the harness still validates decisions and tool inputs.
See [Google's structured-output documentation](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
and the [official SDK documentation](https://googleapis.github.io/python-genai/).

## Development checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Default tests skip the live test even if credentials exist. Opting in with missing
configuration fails rather than silently skipping. Offline tests replace HTTP transport
and use a dummy key; the real SDK request/response code is exercised without a network.

Tests cover contracts/settings, all three mock tools, validation, incident
idempotency, read investigations, execution isolation, duplicate-run rejection,
safe event logs, malformed output, tool/provider failures, blocked incidents,
step/runtime limits, deadlines and cancellation. Approval tests verify exact snapshots,
competing decisions, cancellation before/after dispatch, lost responses, repeated proposals,
resume budgets and preserved incident receipts. Gemini tests also cover schema/role
mapping, provider errors, truncated/blocked/non-text responses, no hidden retries and cleanup. They require no API key or network.
GitHub Actions is configured for Python 3.12 and 3.13 on pushes and pull requests.

## Project layout

```text
src/agent_harness/
  models.py, contracts.py, errors.py   Validated data and safe errors
  config.py                          Settings
  harness.py                         Execution loop, approval/resume and cleanup
  storage.py                         In-memory snapshots, claim locks and events
  budget.py                          Active runtime and operation deadlines
  llm/base.py, llm/fake.py            Provider interface and offline script
  llm/gemini.py, llm/schema.py        Async adapter and schema projection
  llm/smoke.py                       Explicit live schema check (no tool execution)
  tools/                             Registry, handlers and tool-only demo
  demo.py                            Scripted CLI and explicit approval prompt
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
Log export is best effort: sink exceptions do not interrupt state transitions or cleanup;
events remain in memory and `store.log_export_failures` counts export failures.
The mock incident adapter's internal claimed-action bridge is for trusted code.
The harness invokes it only after the stored pending action is claimed by an explicit
approve call. In-process callers are trusted; this is not a sandbox for hostile Python code.

Next: bounded retries/repair and richer failure classification (M5), execution
API/Postman (M6), live end-to-end verification and submission documentation (M7/M8).
The M3 smoke verifies two real provider decisions with zero tool dispatches; it does
not establish a full live investigation or approval flow. Those remain later gates.
