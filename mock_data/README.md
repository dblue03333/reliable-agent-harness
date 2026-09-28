# Mock dataset

Synthetic runbooks and service statuses will be added with the mock tools.
No real customer, employer, credential, or production data belongs here.

Planned fixtures:

- `knowledge_base.json`: runbook IDs, titles, searchable text, and service names.
- `service_status.json`: known services and typed health observations.

Created incidents are runtime state, not committed fixtures. Failure and latency
scenarios should be injected in tests so that the default demo is deterministic.
