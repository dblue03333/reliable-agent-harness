# Q4 — Kế hoạch implementation đã chốt

Trạng thái: M0 đã implement; các milestone M1–M8 vẫn là kế hoạch.
Repo có API `/health`, contracts/settings, startup validation, tests, uv lockfile và CI configuration.
Chưa có agent loop. Xem [M0 walkthrough](m0-walkthrough.md) và [evidence](evidence.md).
Tài liệu này chỉ bao gồm Q4. Q5 và cloud deployment không thuộc phạm vi hiện tại.

## 1. Kết quả cần bàn giao

Một operations assistant chạy local, một process, có khả năng:

1. Nhận objective qua FastAPI.
2. Gọi LLM để lấy decision có cấu trúc, validate và điều phối ba mock tools.
3. Giữ execution state, action history và events trong memory.
4. Pause trước incident creation, nhận approve/reject rồi resume.
5. Kiểm soát concurrency, retries, timeouts, malformed decisions và execution budgets.
6. Trả kết quả trung thực khi side effect thành công, thất bại hoặc chưa xác định.
7. Chạy offline bằng fake provider; có Gemini adapter thực và bằng chứng live integration.
8. Có tests, README, Postman, synthetic dataset và report ngắn.

Không xây UI, vector database, native function calling, background workers, database persistence,
reconciliation service, OAuth/RBAC hoặc Cloud Run trong assessment.

## 2. Các quyết định cố định

| Chủ đề | Quyết định |
| --- | --- |
| Runtime | Python 3.12 là local baseline; duy trì CI 3.12/3.13 |
| API | FastAPI async handlers; POST await tới điểm dừng, không background job |
| Validation | Pydantic v2; forbid extra fields, giới hạn độ dài, enum và strict validation phù hợp |
| LLM protocol | Structured JSON decision; harness tự dispatch tools |
| Providers | Fake mặc định; Gemini bắt buộc implement; live mode thiếu cấu hình phải fail rõ |
| Storage | In-memory, một process và một event loop; không restart recovery |
| Concurrency | Một `asyncio.Lock` cho mỗi execution; tool dedup có bảo vệ riêng |
| Approval | Snapshot độc lập, immutable; approve/reject không nhận replacement arguments |
| Idempotency | Harness tạo action ID, dùng làm internal key của incident adapter |
| Retry | Read tools tối đa 2 retries; mock incident tối đa 1 retry cùng key/payload |
| Budget | Monotonic active runtime; mỗi LLM request, kể cả repair, tiêu hao một step |
| Logs | JSON events có sequence và IDs; không ghi secrets/raw private reasoning |
| Deployment | Local; cloud chỉ xét sau submission và sau thiết kế durable storage |

Các giá trị retry trên là số lần thử lại, không phải tổng attempts: read tối đa 3 attempts,
incident tối đa 2 attempts. Không xây generic retry-policy framework trong V1.

## 3. Cấu trúc repo đích

```text
src/agent_harness/
  api.py                 app factory, routes, dependency wiring
  config.py              typed settings và startup validation
  contracts.py           shared strict model và constrained scalar types
  models.py              execution/action/decision/event contracts
  errors.py              typed internal failures và public error codes
  harness.py             loop, approval/resume, policy và terminal cleanup
  storage.py             in-memory records, locks, snapshots, ordered events
  budget.py              clock, active segments, step accounting
  llm/
    base.py              provider contract
    fake.py              per-execution scripted provider
    gemini.py            async Gemini adapter, raw structured response
  tools/
    schemas.py           ba bộ input/output models
    registry.py          allowlist, validation, bounded tool execution
    knowledge_base.py    keyword search
    service_status.py    synthetic status lookup
    incident.py          mock creation và idempotency ledger
mock_data/
  knowledge_base.json
  service_status.json
  README.md              data dictionary và nguồn synthetic
tests/
  conftest.py            isolated fixtures, fake clock/sleeper, fault adapters
  test_contracts.py
  test_tools.py
  test_harness.py
  test_approval.py
  test_failures.py
  test_limits.py
  test_api.py
  live/test_gemini.py     opt-in; không nằm trong default test run
scripts/
  demo.py                local fake scenarios, in-process failure injection
postman/
  agent-harness.postman_collection.json
  local.postman_environment.json
docs/
  architecture.md
  implementation-plan.md
  roadmap.md
  report.md
  evidence.md            actual commands, outcomes và hạn chế kiểm chứng
```

Giữ layout `src/agent_harness` và `mock_data` hiện có. Chỉ tạo file khi milestone cần nó.
Không tạo hàng loạt empty abstractions. Fault injection là test/demo dependency, không phải public API.

## 4. Contracts và state semantics

### Execution

Các field chính:

- `execution_id`, `objective`, `status`, `termination_reason`.
- `step_count`, `repair_attempt_count`, `active_runtime_seconds`.
- `messages`, `actions`, `tool_history`, `pending_action_id`.
- `final_answer`, `error`, `created_at`, `updated_at`.

Events được store giữ thành ordered log; history chỉ có một nguồn cập nhật, không duy trì nhiều
bản copy mutable độc lập. API trả snapshot, không trả reference nội bộ.

| Status | Ý nghĩa |
| --- | --- |
| CREATED | Execution vừa được tạo |
| RUNNING | Một request đang sở hữu quyền advance execution |
| WAITING_APPROVAL | Có một pending action và không có active loop |
| COMPLETED | Đã nhận final decision hợp lệ trong budget |
| FAILED | Dừng vì provider/tool/validation/cancellation hoặc unknown side effect |
| LIMIT_EXCEEDED | Không thể tiếp tục vì step hoặc active runtime budget |

`COMPLETED` không dùng để che việc hết step. Tool đã thành công vẫn giữ nguyên kết quả nếu
execution sau đó là FAILED hoặc LIMIT_EXCEEDED. Terminal executions không resume trong V1.

### Action và kết quả side effect

Action có `action_id`, `execution_id`, `tool`, immutable validated arguments, status, outcome,
timestamps, result và error. Dùng các phase:

```text
PENDING -> EXECUTING -> RESOLVED
       \-> REJECTED
```

`outcome` chỉ có ở RESOLVED: SUCCEEDED, FAILED hoặc OUTCOME_UNKNOWN.
Rejected action không phải tool failure; handler chưa được gọi.
Nếu execution dừng trước dispatch, đóng pending action với FAILED và reason `NOT_DISPATCHED`.

- Known failure trước dispatch: chắc chắn chưa có effect.
- Validated success response: SUCCEEDED.
- Timeout/cancellation/malformed output sau dispatch: OUTCOME_UNKNOWN nếu chưa có bằng chứng chắc chắn.
- Nếu retry xác minh được success bằng cùng key: chuyển sang SUCCEEDED.
- Nếu attempt trước ambiguous, attempt sau lỗi trước dispatch không xóa ambiguity cũ.
- Không có reconciliation service. Unknown còn lại làm execution FAILED với reason rõ và yêu cầu kiểm tra.

### Agent decisions

Chỉ hai loại: `ToolCallDecision(type, tool, arguments)` và `FinalDecision(type, answer)`.
Validate envelope trước, rồi resolve tool từ registry và validate arguments bằng đúng schema của tool.
Không parse tool calls từ prose, không extract JSON bằng regex để đoán ý model.

Provider trả raw structured response; harness giữ trách nhiệm parse/validate. Gemini adapter có thể
chuyển provider-specific response sang raw JSON nhưng không được thực thi tool.
Schema gửi Gemini phải được tạo từ contracts đang dùng, không duy trì bản contract viết tay thứ hai.
Smoke test sớm sẽ xác minh schema projection mà provider chấp nhận.

Malformed decision cho tối đa một repair request trên toàn execution, nếu còn budget.
Unknown tool và invalid tool arguments kết thúc bằng typed error, không tới handler và không retry
nguyên request. V1 ưu tiên predictable fail-fast; adaptive recovery để sau.

### Tool contracts

| Tool | Input do LLM tạo | Validated output |
| --- | --- | --- |
| search_knowledge_base | query không rỗng, tối đa 500 ký tự | matches gồm document_id, title, excerpt, relevance |
| get_service_status | service_name không rỗng, tối đa 100 ký tự | service_name, healthy/degraded/down, latency_ms không âm, checked_at |
| create_incident | title 1–200, description 1–4000, severity low/medium/high/critical | incident_id, status=created, created_at |

Objective tối đa 4.000 ký tự, final answer tối đa 8.000; search trả tối đa 5 excerpts, mỗi excerpt
tối đa 1.000 ký tự. Chặn whitespace-only inputs. Các cap này là assessment defaults, không phải chuẩn production.
Service không tồn tại là typed permanent error; KB không có kết quả trả `matches=[]` hợp lệ.

Incident adapter nhận thêm execution context do harness tạo, không expose key trong LLM arguments.
Lưu ledger `action_id -> (immutable payload, incident result)` trước simulated response loss.
Cùng key/cùng payload trả cùng result; cùng key/khác payload báo conflict. Atomic check/create dùng
critical section của mock adapter. Hai action ID khác nhau không được semantic deduplicate tự động.

## 5. Loop và execution ownership

Luồng mỗi segment chạy:

1. Execution chuyển RUNNING, mở active-runtime segment.
2. Kiểm tra remaining runtime và remaining LLM steps.
3. Tăng step trước khi gọi LLM; áp LLM timeout trong global deadline.
4. Parse/validate response; malformed response tiêu hao step và bounded repair allowance.
5. Final decision hợp lệ: lưu answer và COMPLETED.
6. Tool decision: validate tool và arguments, kiểm tra policy và runtime.
7. Read tool: attempt/retry theo policy, validate output, ghi observation rồi quay lại loop.
8. Incident: lưu immutable pending action, đóng active segment, WAITING_APPROVAL và trả response.

Approval handler kiểm tra execution/action ownership, pending state và runtime trong execution lock.
Request thắng claim chuyển action EXECUTING và execution RUNNING; release lock trước LLM/tool I/O.
Mọi POST cạnh tranh phải kiểm tra state dưới cùng lock. GET trả snapshot nhất quán dưới lock ngắn.

Claim và dispatch nằm trong cùng phạm vi try/finally bảo vệ cleanup. Cleanup lưu active time,
outcome, error và events; preserve mọi validated success đã có. CancelledError được re-raise sau cleanup.
Không giữ execution lock suốt network call; không tự tạo background task.

Reject thắng claim: action REJECTED, không dispatch; thêm observation rejection rồi resume loop trong
budget còn lại. Proposal mới từ LLM cần action ID và approval mới. Không hứa model sẽ không đề xuất lại;
steps vẫn bị tiêu hao và giới hạn tổng số proposals.

## 6. Budget và error policy

Defaults:

```text
MAX_AGENT_STEPS=10
MAX_ACTIVE_RUNTIME_SECONDS=60
LLM_TIMEOUT_SECONDS=20
DEFAULT_TOOL_TIMEOUT_SECONDS=5
MAX_LLM_REPAIR_ATTEMPTS=1
MAX_READ_TOOL_RETRIES=2
MAX_INCIDENT_RETRIES=1
```

Hai retry limits cuối có hard cap 2 và 1 trong assessment settings. Provider SDK retries phải bị
tắt nếu hỗ trợ hoặc được kiểm soát rõ trong adapter; outer deadline luôn bao toàn LLM operation.
Không thêm provider transport retry loop riêng trong V1; lỗi transport được ghi và dừng có kiểm soát.

- Mỗi LLM request kể cả repair dùng một step; tool attempts không thêm step.
- LLM, tools, active processing và backoff đều tiêu active runtime.
- Thời gian chờ approval không tiêu active runtime; các counters không reset khi resume.
- Mỗi operation dùng timeout `min(operation_timeout, remaining_active_runtime)`.
- Backoff cũng bị global deadline giới hạn; không dispatch attempt mới khi hết budget.
- Dùng monotonic clock; UTC chỉ để audit. Tests inject clock/sleeper.
- Timeout bảo vệ cooperative async code; không claim hard real-time isolation.

Tool được đề xuất ở LLM step cuối vẫn được execute, kể cả sau approval, nếu còn runtime.
Sau tool execution, nếu cần gọi LLM tiếp nhưng hết steps: LIMIT_EXCEEDED, giữ result, final_answer=null.
Không synthesize một câu trả lời rồi giả vờ nó được LLM tạo hoặc objective đã hoàn thành.

| Failure | Policy |
| --- | --- |
| Transient read error/timeout | Tối đa 2 retries; backoff 0.25s, 0.5s trong runtime |
| Read permanent/input/output error | Không retry; execution FAILED |
| Mock incident transient/ambiguous timeout | Tối đa 1 retry cùng action ID và payload; backoff 0.25s |
| Incident malformed output | Không tự retry; OUTCOME_UNKNOWN nếu effect chưa xác minh |
| Incident permanent/conflict error | Không retry; phân loại outcome theo bằng chứng dispatch |
| Malformed LLM envelope | Một repair trên execution nếu còn step/runtime |
| LLM transport/timeout | FAILED; global deadline hết thì LIMIT_EXCEEDED |
| Cancel trước dispatch | FAILED, không side effect |
| Cancel sau dispatch chưa biết kết quả | Action OUTCOME_UNKNOWN; execution FAILED |

Global runtime exhaustion dùng LIMIT_EXCEEDED ngay cả khi action outcome còn unknown; action history
và error details vẫn phải nêu rõ khả năng effect đã xảy ra. Không suy outcome chỉ từ execution status.

## 7. API contract

POST chạy trong request đến điểm dừng; không có `202 Accepted` hoặc polling job abstraction.

| Route | Response bình thường |
| --- | --- |
| GET /health | 200 |
| POST /executions | 201 với execution snapshot sau segment đầu |
| GET /executions/{execution_id} | 200 snapshot |
| GET /executions/{execution_id}/events | 200 ordered events |
| POST /executions/{execution_id}/actions/{action_id}/approve | 200 sau approved action và resumed segment |
| POST /executions/{execution_id}/actions/{action_id}/reject | 200 sau rejection và resumed segment |

Approve/reject không nhận replacement payload; reject non-empty request body thay vì âm thầm bỏ qua.
Unknown execution/action hoặc action không thuộc execution: 404. Stale/non-pending action: 409.
Malformed API input: 422. Runtime failures sau khi execution được nhận trả snapshot với status/reason,
không biến mọi domain failure thành HTTP 500. Lỗi ứng dụng ngoài dự kiến vẫn log và xử lý riêng.

Response tối thiểu có execution_id, status, termination_reason, step_count, active runtime,
pending_action nếu có, final_answer, tool_history và structured error. Luôn đọc được incident success
dù summary generation thất bại sau đó. Không trả secrets, provider raw errors hoặc mutable state.

## 8. Observability và test design

Event fields: execution_id, sequence, UTC timestamp, event_type, step, action_id/tool_call_id nếu có,
tool, attempt, duration_ms, outcome/error code. IDs phải cho phép nối proposal, approval và attempts.

Ghi events cho created, state transition, llm requested/returned/invalid, action proposed/claimed/rejected,
tool attempt started/finished, retry scheduled, budget exhausted và execution terminated.
Mỗi material event được lưu vào ordered history và emit structured log. Không log API keys hoặc chain-of-thought.
Sanitize exception messages; validated mock tool arguments/results có thể lưu có giới hạn.

Test fixtures tạo store/provider/tool ledger riêng cho từng test. Script cursor của FakeLLM phải riêng
cho execution, không dùng global cursor chia sẻ giữa requests. Fake demo được ghi rõ là scripted scenario,
không giả vờ hiểu arbitrary objective như model thật.

| Nhóm | Tests phải chứng minh |
| --- | --- |
| Contracts | Unknown tool, invalid/extra fields, invalid output, malformed decision, whitespace/length caps |
| Read flow | Objective -> status/KB -> observation -> final; empty search và missing service rõ ràng |
| Approval | Không approval thì zero calls; reject zero calls; approve execute exact snapshot |
| Concurrency | Hai approves; approve/reject race; wrong execution/action; một lần resume |
| Snapshot | Mutation nguồn hoặc GET response không thay đổi approved payload |
| Idempotency | Same key/payload cùng incident; changed payload conflict; response loss không duplicate |
| Ambiguity | Malformed incident output; retry không giải quyết được ambiguity; no blind new-key retry |
| Reliability | Transient recovery; permanent no-retry; retries exhausted; LLM/tool timeout; repair bounded |
| Cancellation | Trước dispatch, trong side effect, sau recorded success; không bỏ quên EXECUTING |
| Budgets | Infinite decisions; repair tiêu step; backoff tiêu runtime; pause miễn runtime; resume không reset |
| Boundary | Last-step incident vẫn chạy; budget hết trước dispatch không gọi tool; result tồn tại sau summary failure |
| Isolation | Hai executions không trộn fake sequence, history, pending actions hoặc approvals |
| Evidence | Events đủ IDs/order/outcomes; API hiển thị partial success trung thực |
| Injection scope | Scripted malicious KB/LLM proposal vẫn dừng ở approval gate; không claim chống mọi injection |
| Integration | API happy/approve/reject/conflict; opt-in real Gemini read flow và approval flow |

Race tests dùng asyncio Events/barriers để buộc overlap, không dựa vào sleep ngẫu nhiên.
Fake clock kiểm tra accounting; vẫn có ít nhất một test deadline với cooperative async operation thật.
Không dùng số lượng tests hoặc coverage percentage làm thay thế cho invariants.

## 9. Milestones và acceptance gates

### M0 — Contracts và settings

Tạo models, errors, settings và minimal provider protocol. Chốt schemas từ bảng trên.
Implement strict validation, statuses, outcomes, termination reasons và immutable incident arguments.
Không sửa layout package, CI hoặc dependency manager chỉ để đổi phong cách.

Gate: contract/settings tests pass; missing Gemini config chỉ fail khi chọn Gemini; fake cần zero secrets.

### M1 — Tools và synthetic data

Tạo khoảng 5 services, 8 runbooks synthetic; keyword matching deterministic có tie-break ổn định.
Implement registry, input/output validation, ba handlers và incident ledger ngay từ đầu.
Faults được inject bằng wrapper/test doubles, không nhúng switches nguy hiểm vào endpoint public.

Gate: tools chạy trực tiếp; invalid input zero handler calls; same key dedup; changed payload conflict;
data dictionary và fixtures không chứa thông tin thật.

### M2 — Fake read flow, state/history và budgets cơ bản

Implement store, per-execution provider scripts, harness loop và event emission cùng nhau.
Đưa step cap, active-runtime tracking và operation timeouts vào ngay khi có loop.
Chưa có approval hoàn chỉnh thì mọi incident proposal phải bị block, không được có đường tạm bypass gate.

Gate: status -> KB -> final chạy offline; repeated decisions dừng; execution và history nhất quán;
hai concurrent investigations không dùng chung fake cursor.

### M3 — Gemini schema smoke sớm

Thêm Google GenAI SDK có lockfile, implement async adapter và schema projection từ contracts.
Provider chỉ trả structured data. Test live một ToolCallDecision và một FinalDecision.
Đây là integration spike nhỏ trước phần approval/reliability đầy đủ, không chờ cuối dự án.

Gate: actual live responses validate; model ID và kết quả được ghi trong evidence.
Nếu chưa có key/quota: ghi rõ gate chưa verified, tiếp tục phần offline; không tuyên bố live đã pass.
API key chỉ lưu local ignored .env hoặc môi trường, không dán vào repo/report/log/chat.

### M4 — Approval/resume, atomic ownership và cleanup

Thêm pending snapshot, approve/reject, per-execution locking, single resume owner.
Gắn incident ledger với action ID; implement cancellation/exception cleanup trong cùng slice.
Không đợi hardening milestone mới bảo vệ approved side effect.

Gate: zero pre-approval calls; exact approved payload; sequential/concurrent duplicates an toàn;
approve/reject race; no abandoned EXECUTING sau exception/cancel khi process còn sống.

### M5 — Reliability và boundary cases

Hoàn thiện typed failure classification, bounded retries/backoff, malformed repair,
OUTCOME_UNKNOWN và bảo toàn results khi step/runtime/summary thất bại.
Hoàn thiện pause/resume accounting, last-step dispatch và exhausted-budget pre-dispatch.

Gate: failure/limit/cancellation suite pass với assertions cả state, call counts, effects và events.

### M6 — API và Postman

Giữ `/health`; dùng app factory/dependency injection để test isolated state.
Wire 5 execution endpoints theo HTTP contract. Chạy một process, không background tasks.
Postman tự capture execution_id/action_id, assert outcomes; approve và reject dùng executions riêng.

Gate: API integration tests pass; chạy collection trên server thật local; docs tại `/docs` khớp behavior.
Failure scenarios chạy qua script/fixtures; không thêm public fault-injection routes chỉ vì Postman.

### M7 — Live E2E và verification

Run Gemini read investigation và incident proposal -> WAITING_APPROVAL -> approve -> mock incident -> final.
Live tests assert invariants và schema, không assert nguyên văn câu trả lời hoặc thứ tự tool không cần thiết.
Lưu model ID, ngày chạy, tool/events summary và test commands, không secrets.

Gate: offline full suite + lint/format + existing CI pass; live flow được kiểm chứng thật.
Nếu provider vẫn blocked, báo limitation rõ; live verification còn pending trong DoD.

### M8 — Submission package

Hoàn thiện README, .env.example, Postman environment, data dictionary, report và evidence.
Fresh checkout/install từ lockfile; chạy fake API/tests/collection theo đúng hướng dẫn.
Đối chiếu report với features thực tế; không claim durable persistence, global exactly-once hoặc broad injection immunity.
Dataset để trong repo; giải quyết riêng mâu thuẫn hướng dẫn Drive/OneDrive với recruiter, không tự quyết thay.

Gate: người khác có thể chạy offline không key và hiểu cách bật live; GitHub/deliverable links public;
report có storage/database design, stack, results, limitations và future improvements.

## 10. Config và commands dự kiến

```dotenv
LLM_PROVIDER=fake
GEMINI_API_KEY=
GEMINI_MODEL=
MAX_AGENT_STEPS=10
MAX_ACTIVE_RUNTIME_SECONDS=60
LLM_TIMEOUT_SECONDS=20
DEFAULT_TOOL_TIMEOUT_SECONDS=5
MAX_LLM_REPAIR_ATTEMPTS=1
MAX_READ_TOOL_RETRIES=2
MAX_INCIDENT_RETRIES=1
LOG_LEVEL=INFO
```

Gemini model phải là model ID thực sự đã smoke-test ở thời điểm implementation; không đoán tên model.
Settings đọc `.env` rõ ràng nếu hỗ trợ; .env.example chỉ chứa vars thực sự được code tiêu thụ.

Commands hiện đã có:

```sh
uv sync --locked
uv run uvicorn agent_harness.api:app --host 127.0.0.1 --port 8000 --workers 1
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Commands sẽ bổ sung ở milestone tương ứng:

```sh
uv run python scripts/demo.py --scenario approval
uv run python scripts/demo.py --scenario transient-read
uv run python scripts/demo.py --scenario step-limit
RUN_LIVE_TESTS=1 uv run pytest tests/live -m live
```

Live tests mặc định skip; fake suite không tạo network requests. Không dùng `--reload` khi verify approval
flow vì file changes có thể restart process và làm mất state.

## 11. Workflow và bằng chứng hoàn thành

Mỗi slice: code + behavior tests + cập nhật trạng thái docs trong cùng thay đổi.
Dùng branch `codex/<topic>` và các commit nhỏ có nghĩa; không commit secrets hoặc runtime ledgers.
Không mark roadmap checkbox hoàn thành chỉ vì interface/stub tồn tại.

Ba demo cốt lõi: investigation + approval, transient read recovery, step-limit termination.
Rejection và lost-response case phải có test và evidence; có thể thêm demo nếu dễ tái hiện.
Report tối đa khoảng 6–8 sections: problem, architecture/state, contracts, approval, reliability/budget,
test results, storage/stack/data, limitations/improvements.

Database design trong report mô tả actual dictionaries/locks và proposed schema riêng:
executions, ordered events, actions/approvals, tool attempts, incidents với unique idempotency key.
Không trình bày future SQL tables như database đã implement.

## 12. Safety contract và Definition of Done

Safety contract:

1. LLM không gọi tool handler trực tiếp.
2. Unknown tool/invalid arguments không tới handler.
3. Invalid outputs không trở thành trusted observations.
4. Incident cần approval gắn đúng execution, action và snapshot.
5. Chỉ một request claim/resume action; repeat/conflicting requests không tạo thêm effect.
6. Internal idempotency metadata không do LLM/client quyết định.
7. Unknown effect không được báo thành definitely-not-created.
8. Retry không đổi action ID/payload và không vượt budgets.
9. Human waiting không tiêu active runtime; resume không reset counters.
10. Tool success còn nguyên khi phần tiếp theo fail/cancel/exhaust.
11. KB content không có quyền thay đổi deterministic policy.
12. Material transitions/attempts có ordered audit events.

Definition of Done:

- [ ] Ba mock tools, real LLM adapter và fake provider hoạt động.
- [ ] Approval/rejection, concurrency, snapshots và idempotency có behavioral tests.
- [ ] Validation, malformed repair, retries, timeout, cancellation và limits có tests.
- [ ] Live schema smoke và live E2E được chạy, có bằng chứng; không chỉ có skipped tests.
- [ ] API/history phản ánh partial success và unknown outcomes trung thực.
- [ ] README fresh-clone verified; offline tests/demo không cần API key.
- [ ] Postman collection/environment chạy được, chứa zero secrets.
- [ ] Dataset synthetic + dictionary, report, config và limitations đầy đủ.
- [ ] CI pass; docs chỉ claim những behavior đã implement và verify.
- [ ] Public submission links đã kiểm tra; dataset destination phù hợp hướng dẫn đã làm rõ.

Không thêm scope trước khi các mục trên hoàn thành. V2 mới xem durable storage, auth, cloud,
cost budgets và OpenTelemetry. Điểm bắt đầu implementation là M0, rồi M1/M2 và schema smoke M3.
