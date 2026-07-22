# Offline production-readiness experiment report

## Interpretation warning

This is a local, in-process experiment with a deterministic fake provider. It is
not a production benchmark and must not be used to predict internet, Groq,
Uvicorn, multi-process, container, or production-infrastructure performance.

## Environment and parameters

The experiment was run on 2026-07-22 from the project virtual environment.

| Item | Value |
| --- | --- |
| Operating system | macOS 26.5 (build 25F71), arm64 |
| Python | 3.12.4 |
| FastAPI | 0.139.2 |
| HTTPX | 0.28.1 |
| Pydantic | 2.13.4 |
| Transport | `httpx.ASGITransport`, in-process |
| Provider | Deterministic `OfflineFakeAnalyzer` only |
| External network | Not used; `AsyncGroq` construction blocked by the harness |
| Target rate | 10 RPS |
| Concurrency limit | 20 |
| Duration | 30 seconds |
| Scheduled requests | 300 |
| Traffic mix | 40% SKIP, 40% successful ASSIST, 10% timeout, 5% rate limit, 5% invalid output |

Command:

```bash
.venv/bin/python -m scripts.offline_load_test --duration 30
```

## Results

The run completed 300 requests in 29.909 seconds, producing 10.03 completed
requests per second. All responses were HTTP 200 and conformed to
`ComplianceResponse`.

| Metric | Result |
| --- | ---: |
| Completed requests | 300 |
| Throughput | 10.03 RPS |
| Maximum observed request concurrency | 1 |
| HTTP 200 | 300 |
| HTTP 500 | 0 |
| Other HTTP statuses | 0 |
| Response-schema failures | 0 |
| Injected provider failures | 60 |
| Safe fallbacks | 60 |
| Fake-provider calls | 180 |
| Fake-provider calls for SKIP | 0 |

### End-to-end latency

All latency values are milliseconds and were measured around the complete
in-process ASGI request.

| Scenario | Requests | p50 | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Overall | 300 | 9.774 | 53.793 | 54.865 | 54.992 |
| Static SKIP | 120 | 1.961 | 2.969 | 5.163 | 7.093 |
| Successful fake ASSIST | 120 | 13.822 | 14.706 | 15.234 | 15.240 |
| Timeout fallback | 30 | 53.793 | 54.886 | 54.992 | 54.992 |
| Rate-limit fallback | 15 | 8.015 | 9.774 | 9.774 | 9.774 |
| Invalid-output fallback | 15 | 8.145 | 9.424 | 9.424 | 9.424 |

## SLO evaluation

| SLO | Observation | Result |
| --- | --- | --- |
| Static SKIP p95 <= 100 ms | 2.969 ms | PASS |
| Successful fake ASSIST p95 <= 500 ms | 14.706 ms | PASS |
| Provider failure: 0 unhandled HTTP 500 | 0 HTTP 500 | PASS |
| 100% failures become `FLAGGED` plus human review | 60 fallbacks from 60 injected failures | PASS |
| 0 provider calls for SKIP | 0 calls across 120 SKIP requests | PASS |

The additional harness check requiring every response to match
`ComplianceResponse` also passed: 0 schema failures from 300 responses.

## Observed bottlenecks

No throughput bottleneck was observed at the requested 10 RPS. The artificial
timeout delay dominated the latency tail: timeout p95 was 54.886 ms and the
overall p95 was 53.793 ms, while successful fake ASSIST p95 was 14.706 ms.

The configured concurrency ceiling was not approached. Maximum observed request
and fake-provider concurrency were both 1 because requests arrived every 100 ms
and even the slowest fake response completed in approximately 55 ms. Therefore,
this run provides no evidence about queueing, shared-state contention, or
backpressure near concurrency 20.

## Experiment limitations

- The run used an in-process ASGI transport, not a listening HTTP server.
- The provider was deterministic and local; it did not model real network,
  service, TLS, DNS, retry, or Groq latency distributions.
- The 30-second run is shorter than the five-minute maximum profile and may miss
  slow degradation, resource growth, or rare scheduling outliers.
- Observed concurrency was 1, so the configured limit of 20 was not exercised.
- The experiment ran on one local machine and did not capture CPU, memory, event
  loop lag, file descriptors, or multi-process behavior.
- Percentiles use the harness's nearest-rank calculation over this single run;
  no confidence intervals or repeated-run variance were calculated.
- Only synthetic valid API requests and the three configured fake-provider
  failures were included. Authentication, refusal, malformed HTTP input, and
  arbitrary non-`GroqClientError` provider bugs were outside this run.

## Next recommended improvement

Repeat the contention profile over a longer duration while collecting event-loop
lag and process memory. Keep those measurements separate from the 10 RPS SLO
profile and continue to treat both as offline experiments rather than production
benchmarks.

## Separate offline in-process concurrency-test result

This is a second, independent offline in-process run using only
`SlowConcurrencyFakeAnalyzer` through `httpx.ASGITransport`. Every metric in
this section is a fake-provider result with no external network access. It does
not modify or replace the 30-second 10 RPS results above.

### Parameters

| Item | Value |
| --- | ---: |
| Workers | 20 |
| Requests per worker | 25 |
| Total requests | 500 |
| Fake-provider delay | 200–500 ms |
| SKIP | 200 |
| Successful ASSIST | 200 |
| Timeout failures | 50 |
| Rate-limit failures | 25 |
| Invalid-output failures | 25 |
| External network | Not used |

Command:

```bash
.venv/bin/python -m scripts.offline_load_test --mode concurrency-test
```

### Results

| Metric | Result |
| --- | ---: |
| Completed requests | 500 |
| Elapsed time | 9.084 s |
| Throughput | 55.044 RPS |
| Peak request concurrency | 20 |
| Peak fake-provider concurrency | 12 |
| HTTP 200 | 500 |
| HTTP 500 | 0 |
| Safe fallbacks | 100 of 100 failures |
| Duplicate request IDs | 0 |
| Missing request IDs | 0 |
| State-leakage violations | 0 |
| Response-schema failures | 0 |
| Provider calls for SKIP | 0 |

### End-to-end latency

| Scenario | Requests | p50 | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Overall | 500 | 207.383 | 505.983 | 508.763 | 521.639 |
| Static SKIP | 200 | 1.709 | 2.926 | 5.295 | 5.574 |
| Successful fake ASSIST | 200 | 309.052 | 507.327 | 508.763 | 512.665 |
| Timeout fallback | 50 | 306.441 | 505.357 | 505.983 | 505.983 |
| Rate-limit fallback | 25 | 307.846 | 508.329 | 508.821 | 508.821 |
| Invalid-output fallback | 25 | 403.138 | 512.032 | 521.639 | 521.639 |

### Isolation and non-blocking checks

All concurrency checks passed:

- actual request concurrency reached 20;
- all 500 `request_id` values were present and unique;
- every successful ASSIST response retained its own synthetic reason and token
  values;
- all failures returned only the generic safe fallback;
- no SKIP request called the fake provider;
- all 200 SKIP requests completed by 69.142 ms, before the first slow-provider
  request completed at 222.727 ms;
- maximum SKIP latency was 5.574 ms, below the fake provider's minimum 200 ms
  delay.

The peak provider concurrency of 12 is expected from the worker mix: 8 of the 20
workers execute only SKIP requests, while the remaining 12 exercise successful
ASSIST or provider-failure paths. These local results demonstrate isolation and
non-blocking static routing for this deterministic fake workload only; they are
not production performance measurements.
