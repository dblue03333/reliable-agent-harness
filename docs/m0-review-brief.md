# Brief độc lập để review M0 — Reliable Agent Harness

## 1. Bối cảnh và trạng thái

Project: Reliable Agent Harness for Incident Investigation & Response.
GitHub: https://github.com/dblue03333/reliable-agent-harness
Workspace: `/Users/kelvinnguyen/Documents/ChatGPT/job`.
Branch local hiện tại: `feat/contract-settings`.

Repo public đã có bootstrap trên main. Các thay đổi M0 và tài liệu thiết kế mới hiện ở local,
chưa commit/push. Reviewer chỉ mở GitHub sẽ không thấy toàn bộ M0; cần đọc source snapshot đính kèm.

Đây là take-home AI Engineer assessment, Q4. Đề cuối cùng yêu cầu:

- Nhận objective qua API hoặc CLI.
- LLM–tool loop với ba mock tools: search_knowledge_base, get_service_status, create_incident.
- Validate input/output; giữ state và history.
- Handle tool failures, timeout, retry, malformed LLM response.
- Step/time limits; human approval trước incident creation.
- Logs/traces, tests, run instructions, report, Postman collection và mock dataset.

Chỉ M0 — Contracts & Settings đã implement. Không đánh đồng M0 với toàn bộ submission.
Q5 không nằm trong phạm vi review này.

## 2. Kiến trúc đích, chưa phải runtime đã có

```text
Objective -> FastAPI -> Harness -> LLM proposes decision
                          |
                   Validate / policy / budget
                          |
              Read tools hoặc wait for approval
                          |
                  Execute -> record -> continue
```

Thiết kế V1: local, một process, in-memory store; harness tự dispatch structured decisions.
Fake provider sẽ là default offline mode; Gemini adapter phải implement và verify ở milestone sau.
Không có UI, vector database, native automatic function calling, background workers hoặc cloud deployment.

Approval sẽ bind exact snapshot/action ID; internal idempotency key do harness tạo.
Những điều này mới là planned runtime behavior, không phải guarantee được M0 kiểm chứng.

## 3. Setup đã thực hiện

- Tạo repo public dưới tài khoản dblue03333; push bootstrap lên main.
- Dùng Python src layout, Hatchling để build package, uv để quản lý môi trường và lockfile.
- Python requirement >=3.12; local verification bằng 3.12.13.
- Runtime dependencies: FastAPI, Pydantic v2, pydantic-settings, Uvicorn.
- Dev dependencies: HTTPX, pytest, pytest-asyncio, Ruff.
- Pytest async mode auto; function-scoped async fixture loop.
- Ruff target py312, line length 100, rules E/F/I/UP/B.
- GitHub Actions chạy install locked deps, lint, format và pytest trên Python 3.12/3.13.
- Bootstrap CI từng pass. M0 chưa push nên chưa có kết quả CI remote cho M0.
- Có .gitignore cho .env, keys, virtualenv, caches và runtime artifacts; .env.example được track.
- Có PR template, README, Postman health request và docs roadmap/architecture.
- Branch M0 ban đầu là codex/m0-contracts-settings, đã rename theo yêu cầu thành feat/contract-settings.

## 4. File structure thực tế

```text
job/
  .env.example
  .gitignore
  .python-version
  pyproject.toml
  uv.lock
  README.md
  .github/
    pull_request_template.md
    workflows/ci.yml
  src/agent_harness/
    __init__.py
    contracts.py
    errors.py
    models.py
    config.py
    api.py
    llm/
      __init__.py
      base.py
    tools/
      __init__.py
      schemas.py
  tests/
    conftest.py
    test_contracts.py
    test_config.py
    test_provider_contract.py
    test_health.py
  mock_data/
    README.md
  postman/
    agent-harness.postman_collection.json
  docs/
    architecture.md
    implementation-plan.md
    roadmap.md
    m0-walkthrough.md
    evidence.md
    m0-review-brief.md
```

Chưa có harness.py, storage.py, budget.py, registry.py, fake.py, gemini.py hoặc tool handlers.
mock_data hiện chỉ có README; chưa có synthetic JSON dataset. Postman hiện chỉ test /health.

## 5. Implementation theo từng file

### contracts.py

ContractModel là base cho domain/tool models:

```text
extra=forbid
strict=True
frozen=True
allow_inf_nan=False
hide_input_in_errors=True
```

Các aliases: Identifier tối đa 100 ký tự, Objective tối đa 4.000, FinalAnswer tối đa 8.000.
Các aliases strip whitespace và reject empty strings.

Strict mode chặn coercion không mong muốn theo kiểu dữ liệu; không có nghĩa mọi input type đều
phải giống Python type tuyệt đối (ví dụ semantics của numeric validation cần xem từng field).
Frozen chặn normal field reassignment, không deep-freeze dicts hoặc chống code Python cố tình bypass.

### tools/schemas.py

Contracts cho đúng ba tools:

| Models | Fields chính |
| --- | --- |
| SearchKnowledgeBaseInput | query không rỗng, tối đa 500 |
| KnowledgeMatch | document_id, title, excerpt tối đa 1.000, relevance trong [0, 1] |
| SearchKnowledgeBaseOutput | tuple matches, tối đa 5; empty tuple hợp lệ |
| GetServiceStatusInput | service_name |
| GetServiceStatusOutput | service_name, healthy/degraded/down, latency_ms >=0, aware checked_at |
| CreateIncidentInput | title 1–200, description 1–4.000, severity low/medium/high/critical |
| CreateIncidentOutput | incident_id, status=created, aware created_at |

Không có idempotency_key/approved trong CreateIncidentInput. Extra fields bị reject.
Incident arguments là scalar-only frozen model, không giữ reference đến input dict ban đầu.
Tuple serialize ra JSON array; output timestamps phải timezone-aware, không nhất thiết nhận đầu vào UTC.

### models.py — decision parsing

ToolCallDecision: type=tool_call, tool, arguments dạng dict[str, JsonValue].
FinalDecision: type=final, answer.
AgentDecision là discriminated union theo type.

parse_decision đọc một JSON document hoàn chỉnh, không extract từ prose/code fence.
json.loads có numeric hooks để reject NaN, Infinity và overflow như 1e999.
Sau đó TypeAdapter validate envelope. Lỗi được chuyển thành DecisionValidationError với thông báo cố định.

Parser chưa validate tool-specific arguments và không execute gì.
Unknown tool name có thể parse thành công; registry M1 sẽ quyết định UNKNOWN_TOOL.
decision_json_schema sinh schema từ models; chưa verify Gemini compatibility.

### models.py — state snapshots

Tên class chính xác: ExecutionState và IncidentAction; không phải ExecutionRecord/IncidentActionRecord.

ExecutionStatus: CREATED, RUNNING, WAITING_APPROVAL, COMPLETED, FAILED, LIMIT_EXCEEDED.
ActionStatus: PENDING, EXECUTING, RESOLVED, REJECTED.
ActionOutcome: SUCCEEDED, FAILED, OUTCOME_UNKNOWN.

IncidentAction lưu IDs, literal tool name, typed arguments, phase/outcome, result/error và timestamps.
ExecutionState lưu objective, status/reason, step/repair/runtime counters, messages, actions,
tool_history, pending_action_id, final_answer/error và timestamps.
IDs mặc định dùng UUID có prefix; timestamps mặc định dùng UTC.

Snapshot validators hiện chặn các trường hợp như:

- Action IDs trùng hoặc action thuộc execution khác.
- WAITING_APPROVAL thiếu đúng một matching PENDING action hoặc đồng thời có EXECUTING action.
- Pending action nằm ngoài WAITING_APPROVAL; executing action nằm ngoài RUNNING.
- Nhiều hơn một EXECUTING action.
- RESOLVED thiếu outcome/resolved_at.
- SUCCEEDED thiếu result, có error hoặc thiếu decision timestamp.
- FAILED/OUTCOME_UNKNOWN thiếu error hoặc lại có trusted success result.
- EXECUTING/REJECTED thiếu decision timestamp; PENDING lại có decision timestamp.
- COMPLETED thiếu final answer/final-answer reason hoặc có execution error.
- FAILED/LIMIT_EXCEEDED thiếu error hay có reason sai nhóm.
- Non-completed execution chứa final answer; active execution chứa terminal metadata.
- Repair count lớn hơn step count; negative/non-finite counters.

ToolCallRecord mô tả một attempt, kèm result/error và outcome consistency.
ExecutionEvent định nghĩa correlation IDs, sequence, type, timing và optional outcome/error.
Chưa có event store, event emitter hoặc JSON logger.

Tách biệt execution status và action outcome cho phép biểu diễn:

```text
Execution FAILED do summary LLM_TIMEOUT
Action SUCCEEDED với incident_id=INC-1001
```

M0 test serialization/state representation cho case này; chưa chạy runtime để tạo incident rồi timeout.
Validators chưa phải transition engine, authorization check hoặc concurrency control.

### errors.py

ErrorCode cung cấp vocabulary cho config, decisions, tools, provider, budgets, actions và cancellation.
ErrorInfo gồm code và message có giới hạn độ dài.
HarnessError mang ErrorInfo; DecisionValidationError và ConfigurationError đang được sử dụng.

Thông báo public ở hai error paths này do application viết sẵn, không copy raw inputs.
ErrorInfo không phải automatic sanitizer; caller có trách nhiệm không truyền secrets vào message.

### config.py

Settings dùng pydantic-settings, load .env từ working directory và process environment.
Thông thường explicit constructor values > environment > .env > defaults.
Settings cố ý parse numeric strings vì env là text; extra=ignore ở settings, khác domain schemas.

Defaults:

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

- Gemini bắt buộc có cả key/model; fake không cần. Không silent fallback.
- Blank optional string được normalize thành None.
- Step/durations dương và hữu hạn; không dùng boolean làm limit.
- Repair/read/incident retry hard caps 1/2/1; có thể đặt 0 để disable.
- Operation timeout không bắt buộc nhỏ hơn global budget: clamp sẽ thuộc runtime sau này.
- Key dùng SecretStr, repr=False, exclude=True.
- load_settings chuyển ValidationError thành safe ConfigurationError.

Đây là parsing/validation của settings. Retry, timeout, cost hoặc step enforcement chưa tồn tại.
LOG_LEVEL cũng chưa được dùng để setup structured logging.

### api.py

create_app(settings=None) tạo FastAPI app. Lifespan startup load/validate settings và giữ trong app.state.
Có thể inject Settings trong tests; không đọc config ở import time.
Chỉ endpoint GET /health trả {"status":"ok"}.
Chọn live mode mà thiếu config làm startup fail trước khi serve requests.
Có đủ config không chứng minh Gemini kết nối được vì adapter chưa implement.

### llm/base.py

LLMRequest: execution_id, objective, step, messages và optional repair_instruction.
LLMProvider Protocol khai báo async generate(request) -> str, trả raw structured JSON.
Provider không được truyền tool handlers/store trong interface; consumer sở hữu validation/dispatch.
Đây là dependency boundary, không phải sandbox chống malicious Python implementation.
Fake/Gemini providers chưa có; StubProvider chỉ tồn tại trong test.

## 6. Tests và verification

tests/conftest.py dùng autouse fixture để xóa các app config env vars và chuyển cwd vào tmp_path,
tránh đọc local .env và credentials thật. Live tests sau này sẽ cần opt-in fixture riêng.

| File | Phạm vi kiểm chứng |
| --- | --- |
| test_contracts.py | Decision parsing, malformed inputs, schema generation, limits, forged fields, snapshot immutability, output validity, state consistency, JSON round-trip, audit fields |
| test_config.py | Default fake, .env/env precedence, Gemini requirements, numeric limits, retry caps và secret representation |
| test_provider_contract.py | Async provider interface; raw output được consumer validate |
| test_health.py | Health endpoint, startup settings và missing-live-config failure |

Kết quả lần kiểm chứng M0 đã ghi nhận:

- 83 test cases pass, gồm parametrized cases.
- Ruff lint, formatting và git diff --check pass.
- Fresh virtualenv cài package từ lockfile ở non-editable mode rồi chạy toàn suite cũng pass.
- Không API key hoặc network calls trong tests.
- M0 chưa được chạy trên remote CI; không có live Gemini verification.

Lệnh thông thường:

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run uvicorn agent_harness.api:app --host 127.0.0.1 --port 8000
```

Trong host macOS của phiên implementation, editable .pth từng bị hidden khiến import package fail.
Workaround local đã dùng:

```sh
PYTHONPATH=src uv run pytest -q
uv run uvicorn agent_harness.api:app --app-dir src --host 127.0.0.1 --port 8000
```

Fresh non-editable install pass mà không cần PYTHONPATH giúp phân biệt vấn đề host và packaging.

## 7. Những gì reviewer không nên hiểu nhầm

- M0 không phải working agent harness hoàn chỉnh.
- strict validation không đồng nghĩa chống prompt injection.
- frozen=True không bảo đảm deep immutability mọi dict hoặc chặn model_construct/model_copy bypass.
- Snapshot validator không kiểm tra transition history và không chứng minh runtime authorization.
- Không có locks nên chưa có concurrency/deadlock behavior để đánh giá thực nghiệm.
- Error helpers không phải hệ thống secret redaction toàn diện.
- OUTCOME_UNKNOWN mới là representation, chưa có runtime classification/recovery.
- Không có dedup ledger, retry loop, time accounting, pause/resume hay budget enforcement.
- Sequence field trong event model chưa bảo đảm events được phát đầy đủ/đúng thứ tự.
- 83 passing cases không chứng minh đầy đủ mọi invariant hoặc real-provider integration.

## 8. Yêu cầu gửi cho AI reviewer

Hãy review source và tests đính kèm như một M0 foundation, không coi các mục runtime chưa làm là bug
nếu chúng thuộc milestone sau. Không mặc định mọi claim trong brief đều đã đúng chỉ vì tests xanh.

Ưu tiên:

1. Model validators có chặn đúng contradictory states theo contracts, hay có holes/overconstraints?
2. Strict validation, serialization, enum/tuple behavior và nested mutability có điểm nào gây lỗi khi tích hợp?
3. Parser boundary có edge cases chưa xử lý, như duplicate keys, resource limits hoặc bypass paths?
4. Config loading/precedence/startup và secret handling có đúng theo code và tests không?
5. Provider protocol có đủ đơn giản và dùng được cho fake/Gemini ở milestone sau không?
6. Test assertions có thật sự kiểm chứng điều chúng tuyên bố; còn thiếu cases nào quan trọng trong M0?
7. Có thiết kế nào nên đơn giản hóa trước M1 thay vì thêm framework abstractions?

Với mỗi finding, nêu file/symbol, trigger/reproduction, actual vs expected behavior, impact,
và smallest reasonable fix. Phân biệt bug M0, future integration risk và optional improvement.
Không yêu cầu build database, distributed locking, Cloud Run hoặc runtime features ngoài scope để pass M0.

Milestone tiếp theo là M1: ba mock handlers, registry, synthetic dataset và actual tool validation boundary.
