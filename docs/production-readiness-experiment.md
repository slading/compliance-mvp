# Production-readiness experiment

## Purpose and scope

This experiment evaluates the existing `POST /api/v1/validate` pipeline under
controlled load without contacting Groq or any other external service. It covers
`ComplianceRequest` validation, `StaticPolicyService`,
`CompositeDecisionService`, safe merge, and `ComplianceResponse` serialization.

The assisted path must use a deterministic in-process fake implementing the
existing `LLMAnalyzer.analyze(purpose)` protocol. The fake returns an
`LLMProviderResult` for successful calls and raises the same internal
`GroqClientError` subclasses produced by `GroqComplianceClient` for failure
calls. It must never instantiate `AsyncGroq` or perform network I/O.

Only synthetic transaction data is permitted. Purpose text, API keys, request
bodies, and complete transaction records must not be written to ordinary logs or
experiment artifacts.

## Service-level objectives

Latency is measured end to end around the in-process ASGI request, from before
`AsyncClient.post()` until the complete response is available. Samples are
grouped by route outcome before percentile calculation.

| Path or invariant | Acceptance target |
| --- | --- |
| Static `SKIP` | p95 latency <= 100 ms |
| Successful fake `ASSIST` | p95 latency <= 500 ms |
| Injected provider failures | 0 unhandled HTTP 500 responses |
| Safe fallback | 100% of injected provider failures return `decision="FLAGGED"` and `requires_human_review=true` |
| Static routing isolation | 0 calls to `LLMAnalyzer.analyze()` for `llm_routing="SKIP"` |

An experiment passes only when every target is satisfied. HTTP 422 responses
caused by deliberately invalid `ComplianceRequest` data are not provider
failures and must be reported separately.

## Test topology and dependency injection

Use `httpx.AsyncClient` with `httpx.ASGITransport(app=app)`; do not start Uvicorn
or bind a network port. Before the run, set one stable FastAPI dependency
override for `get_decision_service` that returns:

```text
CompositeDecisionService(
    StaticPolicyService(),
    instrumented_fake_llm_analyzer,
)
```

Install this override once before concurrent traffic starts and remove it in a
`finally` block after all requests complete. Never mutate
`app.dependency_overrides` while requests are in flight.

The instrumented fake must:

- count total `analyze()` calls and calls by scenario;
- return deterministic `LLMAnalysis` and token values for successful calls;
- select failure behavior from a synthetic correlation identifier held by the
  harness, not from real transaction data;
- retain only aggregate counters and synthetic request identifiers;
- protect shared counters for concurrent async access;
- expose no fallback or business-decision logic of its own.

Run with `GROQ_API_KEY` absent and patch `AsyncGroq` construction to fail the
experiment if reached. Network access should also be denied by the execution
environment as a second guard.

## Load profile

The standalone harness defaults to 30 seconds and accepts any positive duration
up to five minutes. It runs at a target arrival rate of 10 requests per second
with a maximum concurrency of 20. A five-minute run schedules approximately
3,000 requests. Use a monotonic clock for pacing and latency measurement; do not
convert the test into an unconstrained throughput benchmark.

Use the following deterministic traffic mix:

| Share | Scenario | Synthetic input/result |
| ---: | --- | --- |
| 40% | Static `SKIP` | `risk=0.8`; fake must not be called |
| 40% | Successful fake `ASSIST` | Valid `BASE-001` input with `NONE` |
| 10% | Timeout | Valid `ASSIST` input; fake raises `GroqTransportError` |
| 5% | Rate limit | Valid `ASSIST` input; fake raises `GroqRateLimitError` |
| 5% | Invalid output | Valid `ASSIST` input; fake raises `GroqSchemaValidationError` |

Concurrent request isolation is evaluated across the entire run rather than as
an additional traffic share. Keep concurrency at or below 20, wait for all
scheduled requests to finish, and record queueing separately from response
latency if the harness cannot maintain 10 RPS.

## Failure modes

All failures are injected by the in-process fake or mocked transport boundary.
No retry may invoke a real provider.

| Failure mode | Injection at the existing boundary | Required observation |
| --- | --- | --- |
| Timeout | Raise `GroqTransportError` representing `APITimeoutError` | HTTP 200 safe fallback; no leaked infrastructure detail |
| Rate limit | Raise `GroqRateLimitError` | HTTP 200 safe fallback; no leaked infrastructure detail |
| Invalid output | Raise `GroqSchemaValidationError`, matching failed `LLMAnalysis.model_validate_json()` | HTTP 200 safe fallback; no provider content in response |
| Connection/API error | Rotate `GroqTransportError` and `GroqUnexpectedProviderError` | HTTP 200 safe fallback for both categories |
| Concurrent request isolation | Assign unique synthetic IDs and deterministic fake results while up to 20 requests overlap | No result, tokens, reason, or `request_id` is attributed to another request |

For each of the first four modes, the response must contain
`LLM-UNAVAILABLE-001`, the generic safe reason, `input_tokens=null`,
`output_tokens=null`, and `estimated_cost_usd=null`. The response must not contain
the exception class, exception text, injected provider payload, or
`llm_routing`.

## Merge and response assertions

Every response must conform to `ComplianceResponse`. Verify the implemented
merge matrix during the load run:

| Static result | Fake result | Expected public result |
| --- | --- | --- |
| `FLAGGED` with `SKIP` | Not called | Preserve static decision; `decision_source="STATIC_RULE"`; tokens null; cost `0` |
| `FLAGGED` with `ASSIST` | Any valid action | `FLAGGED`; review true; `decision_source="LLM_ASSISTED"` |
| `APPROVED` | `NONE` | `APPROVED`; review false; `decision_source="LLM_ASSISTED"` |
| `APPROVED` | `FLAG` or `REVIEW` | `FLAGGED`; review true; `decision_source="LLM_ASSISTED"` |
| Any `ASSIST` static result | Internal provider error | `FLAGGED`; review true; generic unavailable metadata only |

For successful fake `ASSIST`, verify that `input_tokens` and `output_tokens`
match the per-request fake result and `estimated_cost_usd` is null. For `SKIP`,
verify both token fields are null and `estimated_cost_usd` equals
`Decimal("0")` after response validation.

## Metrics and report

The harness must produce aggregate data only:

- scheduled, started, completed, and cancelled request counts;
- achieved RPS and maximum observed concurrency;
- p50, p95, p99, and maximum end-to-end latency by `SKIP`, successful `ASSIST`,
  and provider-failure group;
- HTTP status counts, with provider-originated HTTP 500 count called out;
- injected failures and safe fallbacks by failure mode;
- expected versus actual fake-provider call count;
- duplicate `request_id` count;
- cross-request attribution or isolation violation count;
- response-schema validation failure count;
- confirmation that no external connection or `AsyncGroq` construction occurred.

Preserve the experiment configuration, random seed if one is used, dependency
versions from `uv.lock`, and aggregate results. Do not preserve full request or
response bodies.

## Running the harness

Run the 30-second default profile:

```bash
python3 -m uv run python -m scripts.offline_load_test
```

Run the maximum five-minute profile:

```bash
python3 -m uv run python -m scripts.offline_load_test --duration 300
```

The harness is not a pytest test and does not enable the opt-in Groq integration
test. It uses only `ASGITransport` and an in-process fake provider, blocks
`AsyncGroq` construction, and emits aggregate JSON without request bodies.

Run the separate contention profile with 20 simultaneous workers, 25 requests
per worker, and deterministic fake-provider delays from 200 to 500 ms:

```bash
python3 -m uv run python -m scripts.offline_load_test --mode concurrency-test
```

This mode sends 500 requests and reports peak request/provider concurrency,
duplicate or missing `request_id` values, cross-request state leakage, and
whether all SKIP work completed before the first slow provider response.

## Gaps between this specification and the current project

1. The standalone harness now provides arrival-rate scheduling, concurrency
   control, percentile aggregation, status/fallback counts, and JSON output. It
   is intentionally not a pytest test and has no test marker.
2. The current `ComplianceResponse.latency_ms` measures only
   `decision_service.decide(transaction)`; it excludes request validation,
   dependency resolution, response-model validation, and serialization. The
   harness therefore uses its own end-to-end monotonic timer.
3. Standard tests remain short and do not sustain load; the standalone harness
   must be invoked explicitly for 30 to 300 seconds.
4. The harness counts fake-provider calls by scenario and checks that `SKIP`
   produces zero calls, but these counters are experiment-local rather than
   production telemetry.
5. A bounded offline test now covers 20 simultaneous requests, unique IDs, and
   cross-request token/reason attribution. Sustained isolation under the full
   five-minute load profile is not yet tested.
6. `CompositeDecisionService` safely catches `GroqClientError`. An arbitrary
   exception from a nonconforming `LLMAnalyzer` implementation would currently
   escape and may become HTTP 500; the fake used by this experiment must inject
   the same internal exception types guaranteed by `GroqComplianceClient`.
7. There is no runtime metric distinguishing provider-attempt failures from
   other application failures. The current API infers an attempted unavailable
   LLM from the `LLM-UNAVAILABLE-001` policy reference when assigning null cost.
8. No automated network-denial guard exists in the standard test configuration;
   current offline safety relies on dependency overrides/mocks and the default
   deselection of the `integration` marker.
