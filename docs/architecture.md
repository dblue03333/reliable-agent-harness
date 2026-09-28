# Architecture plan

Status: design intent. Only the API health endpoint is implemented today.

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

## Safety invariants

- Treat LLM decisions and tool results as untrusted inputs. Validate both.
- Allow only registered tool names. Tool policy belongs to the harness.
- Validate arguments before creating an approval request.
- Bind approval to a unique action ID and immutable validated arguments.
- Consume approval atomically; repeated or concurrent requests must not create duplicates.
- Use an internal idempotency key for the mock incident operation.
- Do not retry side effects after an ambiguous timeout without deduplication.
- Retry transient read failures within bounded attempt and runtime budgets.
- Do not retry permanent errors or unchanged invalid arguments.
- Count malformed LLM decisions toward the step budget.
- Include LLM calls, tool calls, and backoff in the active runtime budget.
- Exclude human waiting time from active runtime; preserve the remaining budget on resume.
- Bound every operation by the remaining execution time.
- Record decisions, tool outcomes, attempts, durations, and state changes; do not log secrets.
- Treat retrieved runbooks as data, never as authority to bypass tool policy.

## State and storage

An execution will carry its ID, objective, status, step count, remaining active
runtime, conversation, pending action, final answer, and structured error.

The initial store can be in memory with a per-execution lock and one API process.
That configuration loses state on restart and does not support multiple workers.
SQLite is the planned persistence upgrade, using transactions for approvals.

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
- `GET /executions/{id}/steps`: read ordered execution events.
- `POST /executions/{id}/approve`: approve a specific pending action ID.
- `POST /executions/{id}/reject`: reject a specific pending action ID.

Initial requests will await bounded execution; background workers are out of scope.
Authentication and approval authorization are future work; the demo should run
locally and must not be exposed as an unauthenticated incident-management service.

## Mock tools and demo

- `search_knowledge_base(query)`: keyword search over synthetic runbooks.
- `get_service_status(service_name)`: return a typed synthetic service status.
- `create_incident(title, description, severity)`: create a deduplicated mock incident.

The checkout demo should report observed degradation and possible causes from
runbooks without claiming a verified root cause from service status alone.

## Out of scope for the first submission

Web UI, vector database, distributed workers, production integrations, dashboards,
and a general-purpose agent framework.
