# Reliable Agent Harness

[![CI](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dblue03333/reliable-agent-harness/actions/workflows/ci.yml)

An operations assistant built around validated tools, bounded agent execution,
and human approval before creating incidents.

**Implemented:** CLI and HTTP API, validated tool I/O, exact-action approval,
bounded retries/repair, and execution history. State and incident deduplication are
process-local. Fake mode is scripted. Live Gemini read, approval and rejection flows
passed on 2026-09-30 with `gemini-3.5-flash-lite` and SDK `1.75.0`.

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

Inspect bounded response repair with a deliberately malformed first response:

```sh
uv run python -m agent_harness.demo --scenario repair --objective "Investigate checkout" --data-dir mock_data
```

This offline script completes in four LLM steps with `repair_attempt_count=1`.
Set `MAX_LLM_REPAIR_ATTEMPTS=0` to stop at the invalid response, or
`MAX_AGENT_STEPS=1` to see repair blocked by the step limit. Tool faults and lost
incident responses are injected in `tests/test_reliability.py`, not through public routes.

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
- Malformed decision JSON/schema or provider envelopes get at most one repair
  request per execution, including across approval pauses. Repair consumes a step
  and runtime; only static harness-authored feedback is sent. Unknown tools and
  invalid tool arguments fail immediately. LLM transport errors/timeouts do not retry.
- Read tools retry only `tool_transient` and `tool_timeout`, up to two retries
  (three attempts), with backoff of 0.25s and 0.5s. Permanent/input/output errors
  and unexpected exceptions are not retried. Backoff consumes active runtime and
  is cancellable; no new attempt starts after the budget expires.
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
  conservatively `outcome_unknown`. Incident transient errors/timeouts can retry
  once after 0.25s, with the **same action ID and approved payload**, only when the
  trusted adapter declares `supports_incident_replay`. The bundled mock does;
  replacement adapters default to no replay. Malformed output never triggers a retry.
- Each attempt has a distinct call ID and numbered history/event record. The action
  remains `executing` across backoff. A validated replay receipt resolves it as
  `succeeded`; earlier ambiguous attempts remain in history. A later pre-dispatch
  failure cannot erase earlier ambiguity. Remaining unknown outcomes stop execution;
  check the external system before taking further action.
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
| `GEMINI_API_KEY`, `GEMINI_MODEL` | empty | Required for Gemini API/smoke/E2E; no model default |
| `FAKE_SCENARIO` | `approval` | API-only script: `approval` (reads → proposal → final) or `investigation` (reads → final) |
| `MOCK_DATA_DIR` | `mock_data` | API dataset directory relative to working directory, or absolute path |
| `MAX_AGENT_STEPS` | `10` | LLM request cap |
| `MAX_ACTIVE_RUNTIME_SECONDS` | `60` | Total active run budget |
| `LLM_TIMEOUT_SECONDS` | `20` | Per LLM operation timeout |
| `DEFAULT_TOOL_TIMEOUT_SECONDS` | `5` | Per tool operation timeout |
| `MAX_LLM_REPAIR_ATTEMPTS` | `1` | Execution-wide repair cap; 0 disables, hard cap 1 |
| `MAX_READ_TOOL_RETRIES` | `2` | Retries after the first attempt; 0 disables, hard cap 2 |
| `MAX_INCIDENT_RETRIES` | `1` | Retries for replay-safe incident adapters; 0 disables, hard cap 1 |
| `LOG_LEVEL` | `INFO` | CLI/API event logging (host logging configuration may override) |

Run the local API from the repository root using one worker:

```sh
LLM_PROVIDER=fake FAKE_SCENARIO=approval uv run uvicorn agent_harness.api:app --host 127.0.0.1 --port 8000
```

`/docs` provides interactive OpenAPI documentation; `/health` confirms startup.
Startup validates settings and loads the mock dataset. A single app lifespan owns one
harness, store, ledger and provider; shutdown closes the owned Gemini client.
Do not use multiple workers: each would have separate state. Restart or `--reload`
loses executions and pending approvals. No authentication is implemented; use loopback locally.

| Method / route | Behavior |
| --- | --- |
| `POST /executions` | Body `{"objective":"Investigate checkout timeouts"}`; 201 snapshot at the first stopping point |
| `GET /executions/{execution_id}` | 200 detached snapshot, including `pending_action`, actions and tool history |
| `GET /executions/{execution_id}/events` | 200 ordered event array |
| `POST /executions/{execution_id}/actions/{action_id}/approve` | 200 after executing the saved action and resuming; **no request body** |
| `POST /executions/{execution_id}/actions/{action_id}/reject` | 200 after recording denial and resuming; **no request body** |

POST waits until completion, failure, limit or approval pause; there are no background jobs
or 202 responses. Missing/foreign IDs return 404, stale/competing decisions 409, unsupported methods 405, and invalid
HTTP input 422. Router-generated errors use the same envelope; 405 preserves `Allow`. Approve/reject reject even `{}`, `null` or whitespace bodies. HTTP errors
use `{"error":{"code":"...","message":"..."}}` with sanitized messages. Unhandled API
errors return 500. Errors caught during harness execution return a **201/200 execution
snapshot** with `failed`/`limit_exceeded`; always inspect status and action outcomes.
A request-task cancellation triggers harness cleanup, but a client disconnect does not
guarantee task cancellation. If a response is lost, inspect the known execution/action IDs
before acting again. Creation requests do not have a client-supplied idempotency key.

Snapshots contain objectives and validated observations. Operational events omit raw payloads;
neither snapshots nor endpoints are designed for public unauthenticated hosting.
The API fake provider is explicitly scripted and does not infer a plan from arbitrary objectives.
The default script reads status and KB, then pauses for approval; investigation mode completes
without a proposal. Select the script at startup, not through a per-request fault/scenario flag.

Import both files into Postman:

- [Collection](postman/agent-harness.postman_collection.json)
- [Local environment](postman/local.postman_environment.json)

Choose the local environment. Requests capture separate execution/action IDs for approval and
rejection. For manual walkthrough, inspect the saved proposal before sending the approval request.
**Collection Runner intentionally approves one synthetic incident and rejects another**;
it is for the default fake approval script and limits, not a live-provider benchmark.
It verifies 201/200 snapshots, zero incident attempts before approval, ordered events,
receipt preservation, rejected replacement payloads, duplicate decisions and missing IDs.

Run the same collection from a second terminal (optional Node.js/npm tooling):

```sh
npx --yes newman@6.2.1 run postman/agent-harness.postman_collection.json -e postman/local.postman_environment.json
```

To use another local port, append `--env-var base_url=http://127.0.0.1:8766`.
The collection can be rerun: each run creates fresh executions and overwrites captured IDs.

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
RUN_LIVE_TESTS=1 uv run pytest tests/live/test_gemini.py -m live -q
```

Use either command for a smoke run; running both repeats the API calls. The smoke
requests one service-status tool proposal and one final decision, validates both with
the existing contracts, and **never executes a tool**. Successful output includes the
actual configured model, SDK version, schema fingerprint, timestamp and request count.
Failure returns a safe error and nonzero exit; missing configuration never falls back
to the fake provider. The provider wraps the contract-derived decision in a required `decision` field.
The adapter unwraps it; the harness validates the inner decision and tool arguments.

`GeminiLLMProvider` implements the same interface used by `AgentHarness`; the fake CLI
remains explicitly offline. If `.env` selects Gemini, run the fake demo with
`LLM_PROVIDER=fake uv run python -m agent_harness.demo --objective "Inspect checkout"`.
The HTTP API selects the Gemini adapter at startup when configured.
The E2E runner below checks read/approval flows through this same app and harness.

The SDK receives metadata/schema and messages, never tool handlers. Tool observations
stay in user content marked as untrusted data; they are not promoted to system instructions.
Structured output guides generation; the harness still validates decisions and tool inputs.
See [Google's structured-output documentation](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
and the [official SDK documentation](https://googleapis.github.io/python-genai/).

## Live harness verification (M7, opt-in)

Configure Gemini as above, then run each scenario from the repository root:

```sh
uv run python -m agent_harness.llm.e2e --scenario read --data-dir mock_data
uv run python -m agent_harness.llm.e2e --scenario approve --data-dir mock_data
uv run python -m agent_harness.llm.e2e --scenario reject --data-dir mock_data
```

The approval command displays the exact saved proposal and asks you to type `approve`.
Declining or EOF fails verification with zero incident effects. Each command owns an
isolated in-memory store and mock incident ledger, discarded on exit. The rejection
scenario explicitly rejects its proposal. No production incident system is contacted.
For unattended verification, `--scenario approve --approve-mock-incident` explicitly
authorizes the single synthetic incident in that run; the flag is restricted to this
mock-only runner and is not an API option.

Alternatively, run all three scenarios as opted-in tests (automatically approves one
synthetic incident; this repeats real API usage if you already ran the commands):

```sh
RUN_LIVE_TESTS=1 uv run pytest tests/live/test_live_e2e.py -m live -q
```

The runner uses the real Gemini adapter/network and real HTTP routes, execution loop,
validation, tools and store. HTTP requests enter the app **in-process via ASGITransport**;
this does not verify a deployed server, proxy, authentication or network disconnects.
Offline tests exercise the same runner with scripted decisions and the real SDK over
mocked HTTP; CI does not contact Gemini.

A pass requires both reads to succeed, their validated observations to reach the model,
completion within budgets, consistent saved state and ordered events. Incident scenarios
also require zero effects before approval, the exact saved action, no new proposal and
409 on a duplicate decision. Approval must produce one mock receipt whose incident ID
appears in the final answer; rejection must produce no incident attempts or effects.
These checks establish execution invariants, not factual correctness of every generated
sentence or broad prompt-injection resistance. The final narrative still needs review.

JSON stdout contains model/SDK, schema fingerprint, timestamp, steps, attempts, action
outcomes and event types. It omits credentials, arguments, raw responses and conversation
content. The interactive proposal is printed to stderr for review. Exit `0` means all
checks passed; exit `1` means failed/incomplete verification. Receipt evidence remains
in a failed report if the incident succeeded but the later summary failed. Missing
configuration fails without falling back to fake. Store local reports under ignored
`local_doc/`. On 2026-09-30, read completed in 3 LLM requests (zero incidents),
approve in 4 (one mock incident), and reject in 4 (zero incident attempts/effects).
All three passed with default budgets, zero retries and zero repairs. These are individual
verification runs, not a statistical reliability benchmark.

## Development checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Default tests skip live tests even if credentials exist. Opting in with missing
configuration fails rather than silently skipping. Offline tests replace HTTP transport
and use a dummy key; the real SDK request/response code is exercised without a network.

Tests cover contracts/settings, all three mock tools, validation, incident
idempotency, read investigations, execution isolation, duplicate-run rejection,
safe event logs, malformed output, tool/provider failures, blocked incidents,
step/runtime limits, deadlines and cancellation. Approval tests verify exact snapshots,
competing decisions, cancellation before/after dispatch, lost responses, repeated proposals,
resume budgets and preserved incident receipts. Gemini tests also cover schema/role
mapping, provider errors, truncated/blocked/non-text responses, no hidden retries and cleanup. They require no API key or network.
Reliability tests cover exhausted/recovered retries, same-key replay after lost responses,
bounded repair (including the real SDK with mocked HTTP), cancellation/deadlines during
backoff, last-step recovery and preserved ambiguity after later preflight failure.
`llm_requested.is_repair` and `retry_scheduled` events expose repair and retry decisions
without logging raw responses; retry events identify the failed call and next attempt/delay.
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
  llm/e2e.py                         Opt-in live API/harness verification and safe evidence
  tools/                             Registry, handlers and tool-only demo
  demo.py                            Scripted CLI and explicit approval prompt
  api.py                             Lifespan, execution/approval routes and safe HTTP errors
tests/                              Automated tests
mock_data/                          Synthetic statuses/runbooks and data dictionary
postman/                            Execution/approval collection and local environment
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

Retry safety relies on the adapter honoring its declared idempotency contract. The mock
ledger has no durability across restarts, and there is no reconciliation service for unknown
outcomes. Backoff is a fixed bounded schedule without jitter; deployment-scale retry policy
and provider transport retries are outside this version.

Pending: submission report and final delivery packaging (M8).
Live read/approval/rejection verification passed on 2026-09-30 as detailed above.
The Gemini schema smoke passed on 2026-09-29 with `gemini-3.5-flash-lite` and SDK
`1.75.0`; it verified two provider decisions with zero tool dispatches.
