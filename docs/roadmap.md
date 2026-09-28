# Delivery roadmap

## 0. Repository bootstrap

- [x] Python package, FastAPI health endpoint, and smoke test
- [x] Development dependencies and reproducible lockfile
- [x] GitHub Actions workflow for lint, formatting, and tests
- [x] README, architecture plan, and starter Postman collection

## 1. First complete investigation flow

- [ ] Define strict decision, state, and tool schemas
- [ ] Add synthetic service statuses and runbooks
- [ ] Implement read tools and typed tool registry
- [ ] Add scripted fake and real LLM adapters
- [ ] Implement bounded loop, state/history, and objective API
- [ ] Test successful investigation and input/output validation

## 2. Approval and side effects

- [ ] Implement mock incident tool with internal idempotency key
- [ ] Persist the exact pending action and pause execution
- [ ] Add approve/reject endpoints and resume behavior
- [ ] Test zero incidents before approval and after rejection
- [ ] Test duplicate/concurrent approval and changed-action rejection

## 3. Failure handling and observability

- [ ] Add transient-only retries, timeouts, and structured errors
- [ ] Bound malformed-response repair attempts
- [ ] Enforce step and active runtime limits across resume
- [ ] Record structured events for every attempt and transition
- [ ] Test transient recovery, permanent failure, invalid output, timeouts,
      malformed LLM responses, and execution limits

## 4. Submission

- [ ] Expand Postman collection to cover all implemented endpoints
- [ ] Document real environment variables and offline/live run instructions
- [ ] Finish short report with storage design, limitations, and improvements
- [ ] Add reproducible happy-path, failure, and approval demos
- [ ] Confirm dataset submission location with the recruiter: the supplied
      instructions conflict on personal Drive/OneDrive links
- [ ] Verify repository and deliverable links without login

## After submission

Persistent restart recovery, authentication and authorization, approval expiry,
token/cost budgets, OpenTelemetry, and broader evaluation scenarios.
