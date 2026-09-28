# M0 walkthrough — Contracts và settings

M0 thiết lập các kiểu dữ liệu và quy tắc cấu hình. Chưa có agent loop, tool handlers,
registry, FakeLLM implementation, Gemini adapter hoặc approval endpoints.
Nhánh làm việc: `feat/contract-settings`.

## 1. Vì sao bắt đầu bằng contracts?

Harness sẽ nhận dữ liệu từ user, LLM và tools. Các nguồn này có thể trả dữ liệu sai.
Nếu mỗi module tự đoán dữ liệu, validation và error handling sẽ không nhất quán.

M0 tạo một đường biên rõ:

```text
User objective / raw LLM JSON / tool result
    -> validate đúng schema
    -> typed object
    -> milestone sau mới quyết định hoặc thực thi
```

Một object đúng schema chưa có nghĩa hành động được authorize. Ví dụ `IncidentAction`
có thể được tạo trong code nhưng M4 vẫn phải kiểm tra approval và ownership trước dispatch.

## 2. Đọc `contracts.py` trước

`ContractModel` đặt các mặc định chung:

- `extra="forbid"`: field không được khai báo gây lỗi; không âm thầm bỏ qua `approved=true`.
- `strict=True`: không biến title dạng số thành string hoặc nhận `"4200"` như numeric latency.
- `allow_inf_nan=False`: typed measurements không chấp nhận NaN/Infinity.
- `frozen=True`: chặn field assignment sau khi validate.
- `hide_input_in_errors=True`: không đưa giá trị input vào chuỗi validation error mặc định.

Frozen không deep-freeze mọi dict, cũng không chống code Python cố tình bypass model.
Approval arguments an toàn hơn vì `CreateIncidentInput` chỉ gồm immutable scalar fields.
Store/GET snapshots và các locks sẽ được implement ở các milestone sau.

## 3. Đọc `tools/schemas.py`

Mỗi tool có input và output riêng. Đây chỉ là contracts, chưa có handler.

Ví dụ `CreateIncidentInput` chỉ có `title`, `description`, `severity`.
Nếu input thêm `idempotency_key` hoặc `approved`, validation phải fail.
Key sẽ do harness truyền bằng internal context ở milestone sau, không phải do model chọn.

`SearchKnowledgeBaseOutput.matches` dùng tuple, cho phép empty result và giới hạn 5 matches.
JSON vẫn serialize tuple thành array. Timestamp outputs bắt buộc có timezone;
defaults của internal records dùng UTC.

## 4. Đọc decision parser trong `models.py`

Hai decision types:

```json
{"type":"tool_call","tool":"get_service_status","arguments":{"service_name":"checkout-api"}}
```

```json
{"type":"final","answer":"Checkout đang degraded."}
```

`parse_decision()` làm hai bước:

1. Parse toàn bộ JSON, reject prose/code fences và số không hữu hạn, kể cả overflow `1e999`.
2. Validate union theo discriminator `type`.

Parser không resolve tool, không validate tool-specific arguments và không dispatch.
Một unknown tool name có thể đi qua envelope parser để registry M1 phân loại thành UNKNOWN_TOOL.
Phân biệt này giúp malformed decision và unknown tool có error semantics rõ ràng.

`decision_json_schema()` sinh JSON Schema từ cùng model definitions. M3 sẽ kiểm chứng
projection tương thích Gemini; M0 không claim đã verify provider schema support.

## 5. Đọc execution/action models

Execution status nói agent đang ở đâu. Action outcome nói side effect đã xảy ra hay chưa.
Hai khái niệm không được gộp lại.

Ví dụ hợp lệ:

```text
Execution: FAILED, reason=ERROR, error=LLM_TIMEOUT
Incident action: RESOLVED, outcome=SUCCEEDED, incident_id=INC-1001
```

Nghĩa là incident thành công nhưng bước tạo final summary có thể đã thất bại.
Test M0 chỉ chứng minh trạng thái này biểu diễn/serialize được; chưa mô phỏng runtime timeout.

Validators chặn một số contradictory snapshots:

- WAITING_APPROVAL mà không có đúng một matching pending action.
- Action thuộc execution khác hoặc action IDs trùng trong execution.
- EXECUTING action nằm trong terminal execution.
- RESOLVED/SUCCEEDED mà thiếu result hoặc còn error.
- Failed/unknown action lại chứa trusted success result.
- COMPLETED mà thiếu final answer hoặc dùng budget termination reason.
- Repair count lớn hơn step count.

Đây là validation cho snapshot, không phải state-transition engine. M2/M4 phải kiểm tra
transition trước/sau, giữ locks và validate snapshot mới.
Không dùng `model_construct()` hoặc `model_copy(update=...)` để cập nhật dữ liệu chưa validate:
các API đó có thể bypass validation. Rebuild bằng `model_validate()` khi cập nhật state.

## 6. Đọc `errors.py`

`ErrorCode` cho máy xử lý; `ErrorInfo.message` là safe application-authored message cho user.
`HarnessError` mang `ErrorInfo`, với hai typed exceptions đang dùng:

- `DecisionValidationError`: parser reject output.
- `ConfigurationError`: startup config không hợp lệ.

Không dump raw LLM response, SDK exception hoặc toàn bộ `ValidationError.errors()` ra public API/log.
Các error codes cho tool/budget đã định nghĩa để giữ vocabulary nhất quán; runtime classification
và retry logic chưa implement trong M0.

## 7. Đọc `config.py` rồi `api.py`

Settings là chỗ duy nhất cố ý cho phép parse numeric strings vì environment variables vốn là text.
Thứ tự thông thường: explicit constructor values > process environment > `.env` > defaults.

Quy tắc:

- Fake mặc định, không cần key/model.
- Chọn Gemini bắt buộc có cả key và model name; không fallback.
- Step count và durations phải dương, hữu hạn; boolean không phải numeric limit.
- Repair/read/incident retry limits có hard caps 1/2/1 và có thể đặt 0.
- Key dùng `SecretStr`, bị loại khỏi repr và model serialization.
- Không bắt tool timeout phải nhỏ hơn runtime; runtime code sẽ clamp bằng `min()` ở M2.

`create_app()` tạo app; lifespan gọi `load_settings()` khi server startup, không phải khi import.
Vì vậy config sai làm startup fail sớm, và tests có thể inject Settings riêng.
`/health` vẫn hoạt động như trước. Settings đã được đọc/validate; enforcement, provider dispatch
và JSON log emission thuộc milestone sau, không phải feature M0 đã có.

## 8. Đọc `llm/base.py`

```python
async def generate(self, request: LLMRequest) -> str: ...
```

Request có execution_id, objective, step, conversation messages và optional repair instruction.
Provider nhận context để đề xuất, không nhận store hoặc tool handlers.
Provider trả raw JSON để harness giữ quyền parse/validate, tính repair budget và execute.

`Protocol` mô tả interface; M0 không tạo fake provider production hoặc gọi Gemini.
Stub trong test chỉ kiểm chứng cách một implementation đáp ứng async interface.

## 9. Review tests theo thứ tự

1. `test_contracts.py`: đọc happy decision, forged arguments, immutable approval arguments,
   invalid outputs, inconsistent states và successful effect retained in terminal execution.
2. `test_config.py`: defaults, `.env` precedence, missing Gemini config, invalid limits, secret handling.
3. `test_provider_contract.py`: provider trả raw output, consumer validate.
4. `test_health.py`: lifecycle startup đọc settings; invalid live config chặn startup.

Fixtures xóa app-specific env vars và chuyển vào temporary directory để không đọc `.env` thật.
Không có test M0 nào gọi network hoặc cần API key. Tests live M3 sẽ cần fixture riêng.

Commands:

```sh
uv sync --locked
uv run pytest -q
uv run pytest tests/test_contracts.py -v
uv run pytest tests/test_config.py -v
uv run ruff check .
uv run ruff format --check .
```

Trên môi trường macOS của phiên này, editable-install `.pth` từng bị gắn cờ hidden,
khiến Python không thêm `src` vào import path. Nếu gặp `ModuleNotFoundError: agent_harness`,
có thể dùng workaround local không thay đổi code:

```sh
PYTHONPATH=src uv run pytest -q
uv run uvicorn agent_harness.api:app --app-dir src --host 127.0.0.1 --port 8000
```

Để phân biệt vấn đề host với package, M0 cũng được cài vào một virtualenv mới từ lockfile
ở chế độ non-editable và chạy toàn bộ tests mà không cần PYTHONPATH.

## 10. Sau review

M0 không chứng minh approval concurrency, retry hoặc budget enforcement đã hoạt động.
M1 tiếp theo sẽ thêm registry và ba mock handlers để biến các contracts thành tool calls thật.
Chưa triển khai M1 trong thay đổi này.
