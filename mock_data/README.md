# Synthetic operations dataset

All records were written for this project. They contain no real customer, employer,
credential, production, or aircraft data. This is a fixed demo snapshot, not live telemetry.

## Files and data dictionary

`service_status.json` contains a `services` array with five records:

| Field | Type / meaning |
| --- | --- |
| service_name | Unique, case-sensitive identifier, up to 100 characters |
| status | healthy, degraded, or down |
| latency_ms | Finite, nonnegative number; synthetic latency in milliseconds |

The services are checkout-api (degraded), payment-api (healthy), auth-api (healthy),
cache-service (degraded), and notification-worker (down). The down worker's zero
latency is a placeholder, not a successful zero-latency response. Consumers must
interpret the status field. The handler adds `checked_at` as the lookup time in UTC;
it is not the timestamp of a real monitoring probe.

`knowledge_base.json` contains a `runbooks` array with eight records:

| Field | Type / meaning |
| --- | --- |
| document_id | Unique stable identifier, up to 100 characters |
| title | Nonempty string, up to 200 characters |
| content | Nonempty runbook body, up to 4,000 characters |
| services | Nonempty array of names present in service_status.json |

Topics cover checkout latency, payment, authentication, database pools, cache,
notification queues, API timeouts, and incident escalation. Runbooks distinguish
hypotheses from observations; none supplies proof of a production root cause.

## Loading and search behavior

`load_mock_dataset(directory)` validates both files at setup. Invalid records,
duplicate IDs/names, missing files, or unknown service references fail with a safe
`MockDataError`. Paths come from application configuration, never from LLM arguments.
Data is loaded once; editing JSON does not change an already-created tool bundle.

Search tokenizes English letters/digits case-insensitively and uses unique tokens.
Relevance = matching query tokens / total query tokens, in [0, 1]. Search includes
title, content, and service names. Results sort by score descending then document ID,
return at most five matches, and truncate each excerpt to 1,000 characters.
No overlap or a query without searchable tokens yields an empty result.
There is no stemming, embedding search, semantic ranking, or confidence calibration.
Relevance is lexical overlap, not probability that a runbook explains the problem.

## Incidents and failures

Created incidents live in the `IncidentTool` in-memory ledger; no incidents.json is
written. Each tool bundle starts its own ledger and IDs from INC-1001. This is
in-process deduplication, not global uniqueness or restart recovery.

Tests inject transient failures, lost responses and malformed outputs via wrappers.
There are no failure switches in user-facing tool inputs or API endpoints.

Use the repository files directly for review. Installed-package callers supply an
explicit dataset directory; these root-level JSON fixtures are not embedded in the wheel.
