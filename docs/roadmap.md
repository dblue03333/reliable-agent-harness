# Q4 delivery roadmap

See [implementation-plan.md](implementation-plan.md) for exact contracts, files,
test cases, acceptance gates, configuration, and submission requirements.
Each milestone includes tests; the final verification milestone does not defer
basic correctness or safety checks until the end.

## Repository bootstrap — complete

- [x] Python package, FastAPI health endpoint, and smoke test
- [x] Development dependencies and reproducible lockfile
- [x] GitHub Actions workflow for lint, formatting, and tests
- [x] README, architecture plan, and starter Postman collection
- [x] Detailed Q4 implementation plan and acceptance gates

## M0 — Contracts and settings

- [x] Decision, execution, action, event, and tool schemas
- [x] Typed errors, strict settings, and provider protocol
- [x] Contract tests and missing-live-config behavior

Review: [M0 walkthrough](m0-walkthrough.md). Validation: [evidence](evidence.md).

## M1 — Mock tools and data

- [ ] Synthetic services/runbooks and data dictionary
- [ ] Registry, input/output validation, and three handlers
- [ ] Atomic incident ledger with harness-owned key and payload-conflict checks

## M2 — Fake investigation, state, and basic limits

- [ ] Per-execution fake provider and in-memory store
- [ ] Read loop with history/events, step cap, runtime accounting, and operation deadlines
- [ ] Block incident dispatch until approval slice is implemented
- [ ] Offline happy path, execution isolation, and loop-limit tests

## M3 — Early Gemini schema smoke

- [ ] Real async adapter with structured decisions and no tool execution
- [ ] Live tool/final decision responses validated against contracts
- [ ] Actual model ID and smoke outcome recorded; mark pending if unavailable

## M4 — Approval, concurrency, and cleanup

- [ ] Immutable snapshots, approve/reject, exact action ownership, and resume
- [ ] One execution lock; one resume owner; incident deduplication
- [ ] Duplicate/racing decisions, mutation, and cancellation tests

## M5 — Reliability and boundary cases

- [ ] Transient-only retries, bounded repair, timeout/cancellation classification
- [ ] OUTCOME_UNKNOWN, lost response, and invalid incident output
- [ ] Pause/resume budgets, last-step dispatch, and preserved success after later failure

## M6 — API and Postman

- [ ] Five execution routes with synchronous request completion and documented HTTP behavior
- [ ] Isolated API integration tests and consistent snapshots/events
- [ ] Working Postman collection/environment with captured execution/action IDs

## M7 — Live E2E and verification

- [ ] Real Gemini read flow and incident proposal/approval/final flow
- [ ] Complete offline failure/invariant suite, lint/format, and CI
- [ ] Sanitized evidence of real commands/results, including unresolved limitations

## M8 — Submission

- [ ] Fresh-clone-tested README, consumed env vars, and offline/live instructions
- [ ] Synthetic dataset, data dictionary, report, and complete Postman collection
- [ ] Report actual in-memory design separately from proposed database schema
- [ ] Resolve conflicting dataset-link instructions with recruiter
- [ ] Verify public deliverable links without login

## After submission

Durable shared storage, restart recovery, authentication/authorization, cloud deployment,
cost/token budgets, and expanded telemetry. Q5 is a separate future discussion.
