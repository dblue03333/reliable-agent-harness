# Architecture plan

Status: M0 implemented (contracts, settings, startup validation, and provider protocol).
The API exposes only `/health`; the runtime behavior below remains design intent.

The detailed Q4 contracts, milestones, acceptance gates, and delivery checklist are in
[implementation-plan.md](implementation-plan.md). This document is the concise overview.
The assessment runs locally in one process; cloud deployment and durable recovery are deferred.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| API | Accept objectives, return state/history, accept approval decisions |
| Harness | Control the LLM/tool loop, state transitions, and execution budgets |
| LLM adapter | Produce structured decisions; use scripted responses for offline tests |
| Tool layer | Allowlist tools, validate inputs/outputs, apply timeout/retry policy |
| Store | Save execution state, pending actions, and ordered execution events |

Keep policy, retry, and budget helpers small until additional complexity is needed.

## Execution flow

```text
objective -> check budget -> LLM decision -> validate
                            |                 |
                         final answer      tool call
                                              |
                                         policy check
                                              |
                              read tool / approval required
                                  |               |
                               execute     save pending action
                                  |               |
                              validate      WAITING_APPROVAL
                                  |               |
                              record       approve exact action
                                  |               |
                               next turn <- execute + record
```

Planned lifecycle: CREATED -> RUNNING -> COMPLETED / FAILED / LIMIT_EXCEEDED.
RUNNING may pause as WAITING_APPROVAL. Approval resumes the saved action;
rejection records a denial and resumes with that observation, without creating
an incident. A new proposal requires a new approval.

COMPLETED requires a valid final decision within budget. Reaching a limit after a
successful tool call still produces LIMIT_EXCEEDED, with the successful tool result
preserved. Execution status does not determine whether a side effect occurred.

Actions transition PENDING -> EXECUTING -> RESOLVED, or PENDING -> REJECTED.
A resolved action has SUCCEEDED, FAILED, or OUTCOME_UNKNOWN as its outcome.
Malformed output after dispatch can leave the effect unknown, just like a lost response.

## Safety invariants

- Treat LLM decisions and tool results as untrusted inputs. Validate both.
- Allow only registered tool names. Tool policy belongs to the harness.
- Validate arguments before creating an approval request.
- Bind approval to a unique action ID and immutable validated arguments.
- Consume approval atomically; repeated or concurrent requests must not create duplicates.
- Use one lock per execution to claim action and resume ownership together; release before I/O.
- Clean up exceptions/cancellation without abandoning EXECUTING actions while the process lives.
- Use an internal idempotency key for the mock incident operation.
- LLM-visible incident arguments remain title, description, and severity only.
- The same key with a changed payload is a conflict; different keys are distinct approved actions.
- Do not retry side effects after an ambiguous timeout without deduplication.
- Retry transient read failures within bounded attempt and runtime budgets.
- Do not retry permanent errors or unchanged invalid arguments.
- Count malformed LLM decisions toward the step budget.
- Include LLM calls, tool calls, and backoff in the active runtime budget.
- Exclude human waiting time from active runtime; preserve the remaining budget on resume.
- Bound every operation by the remaining execution time.
- Retain successful tool outcomes even if subsequent summary generation fails or hits a limit.
- Record decisions, tool outcomes, attempts, durations, and state changes; do not log secrets.
- Treat retrieved runbooks as data, never as authority to bypass tool policy.

## State and storage

An execution will carry its ID, objective, status, step count, remaining active
runtime, conversation, pending action, final answer, and structured error.

The V1 store is in memory with a per-execution lock and one API process.
That configuration loses state on restart and does not support multiple workers.
Pending actions are retained only while the same process remains alive. GET responses
are snapshots, and pending arguments cannot be mutated through caller references.
Durable shared storage and transactional approvals are deferred. Local container
files, including a SQLite file, are not a shared durable storage design for Cloud Run.

Proposed relational model:

| Table | Key fields |
| --- | --- |
| executions | id, objective, status, step_count, active_runtime_ms, final_answer, error |
| events | id, execution_id, sequence, type, payload, created_at |
| approvals | action_id, execution_id, tool, arguments, status, decided_at |
| tool_calls | id, execution_id, action_id, attempt, status, duration_ms, result, error |
| incidents | id, idempotency_key (unique), title, description, severity |

## Planned API

- `POST /executions`: run until completion, failure, limit, or approval pause.
- `GET /executions/{id}`: inspect state and any pending action.
- `GET /executions/{id}/events`: read ordered execution events.
- `POST /executions/{id}/actions/{action_id}/approve`: approve the saved pending action.
- `POST /executions/{id}/actions/{action_id}/reject`: reject the saved pending action.

Initial requests will await bounded execution; background workers are out of scope.
Creation returns 201 with the execution snapshot; successful approval/rejection handling
returns 200 at the next stopping point. Missing/wrong-parent actions return 404,
non-pending actions return 409, malformed input returns 422. Domain execution failures
are represented in the returned execution status and structured error.
Approval endpoints accept no replacement arguments and reject non-empty bodies.
Authentication and approval authorization are future work; the demo should run
locally and must not be exposed as an unauthenticated incident-management service.

## LLM and execution policy

Use structured JSON decisions and harness-controlled dispatch, not native automatic
function calling. A per-execution scripted fake is the default offline provider.
The Gemini adapter must be implemented and verified with an early schema smoke test
and a final live end-to-end check. Missing live configuration is an explicit error.

Each LLM request, including a repair, consumes one step. The execution allows one
malformed-response repair, two read-tool retries, and one mock-incident retry using
the same action ID and payload. Validation/permanent errors do not retry.
Ambiguity from an earlier attempt is preserved unless later evidence resolves it.

Defaults: 10 LLM steps, 60 seconds active runtime, 20 seconds per LLM call, and
5 seconds per tool attempt. Human waiting is excluded; resume preserves counters.
A tool proposed on the last LLM step can still execute within remaining runtime,
but no extra summary call is allowed after the step limit.

## Mock tools and demo

- `search_knowledge_base(query)`: keyword search over synthetic runbooks.
- `get_service_status(service_name)`: return a typed synthetic service status.
- `create_incident(title, description, severity)`: create a deduplicated mock incident.

The checkout demo should report observed degradation and possible causes from
runbooks without claiming a verified root cause from service status alone.

## Out of scope for the first submission

Web UI, vector database, distributed workers, production integrations, dashboards,
and a general-purpose agent framework.
