import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
import time
from typing import Any
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from app.groq_client import (
    GroqRateLimitError,
    GroqSchemaValidationError,
    GroqTransportError,
    LLMAnalysis,
    LLMProviderResult,
)
from app.main import app
from app.schemas import ComplianceResponse
from app.services import (
    CompositeDecisionService,
    StaticPolicyService,
    get_decision_service,
)


RPS = 10
MAX_CONCURRENCY = 20
DEFAULT_DURATION_SECONDS = 30.0
MAX_DURATION_SECONDS = 300.0
SCENARIO_CYCLE = (
    "skip",
    "assist",
    "skip",
    "assist",
    "timeout",
    "skip",
    "assist",
    "skip",
    "assist",
    "rate_limit",
    "skip",
    "assist",
    "skip",
    "assist",
    "timeout",
    "skip",
    "assist",
    "skip",
    "assist",
    "invalid_output",
)
FAILURE_SCENARIOS = {"timeout", "rate_limit", "invalid_output"}
CONCURRENCY_WORKERS = 20
REQUESTS_PER_WORKER = 25
MIN_SLOW_PROVIDER_DELAY_MS = 200.0


@dataclass(frozen=True)
class RequestResult:
    scenario: str
    latency_ms: float
    status_code: int
    fallback: bool
    schema_valid: bool


@dataclass(frozen=True)
class ConcurrencyRequestResult:
    scenario: str
    latency_ms: float
    completed_at_ms: float
    status_code: int
    fallback: bool
    schema_valid: bool
    request_id: str | None
    state_isolated: bool


class OfflineFakeAnalyzer:
    """Deterministic provider fake with no transport or business fallback."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.active_calls = 0
        self.max_active_calls = 0

    async def analyze(self, purpose: str) -> LLMProviderResult:
        scenario = purpose.split()[3]
        if scenario == "skip":
            raise AssertionError("SKIP must not call the fake provider")

        self.calls[scenario] += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if scenario == "assist":
                await asyncio.sleep(0.01)
                return LLMProviderResult(
                    analysis=LLMAnalysis(
                        suggested_action="NONE",
                        reason="Synthetic offline load analysis.",
                    ),
                    input_tokens=12,
                    output_tokens=8,
                )
            if scenario == "timeout":
                await asyncio.sleep(0.05)
                raise GroqTransportError("synthetic timeout")
            if scenario == "rate_limit":
                await asyncio.sleep(0.005)
                raise GroqRateLimitError("synthetic rate limit")
            if scenario == "invalid_output":
                await asyncio.sleep(0.005)
                raise GroqSchemaValidationError("synthetic invalid output")
            raise AssertionError(f"Unknown synthetic scenario: {scenario}")
        finally:
            self.active_calls -= 1


class SlowConcurrencyFakeAnalyzer:
    """Slow deterministic fake used only by the concurrency profile."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.active_calls = 0
        self.max_active_calls = 0

    async def analyze(self, purpose: str) -> LLMProviderResult:
        parts = purpose.split()
        scenario = parts[3]
        worker_id = int(parts[5])
        request_index = int(parts[7])
        correlation = worker_id * REQUESTS_PER_WORKER + request_index

        if scenario == "skip":
            raise AssertionError("SKIP must not call the slow fake provider")

        self.calls[scenario] += 1
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        delay_seconds = 0.2 + (correlation % 4) * 0.1
        try:
            await asyncio.sleep(delay_seconds)
            if scenario == "assist":
                return LLMProviderResult(
                    analysis=LLMAnalysis(
                        suggested_action="NONE",
                        reason=(
                            f"Synthetic concurrency result worker {worker_id} "
                            f"request {request_index}."
                        ),
                    ),
                    input_tokens=10_000 + correlation,
                    output_tokens=20_000 + correlation,
                )
            private_detail = (
                f"synthetic {scenario} worker {worker_id} "
                f"request {request_index}"
            )
            if scenario == "timeout":
                raise GroqTransportError(private_detail)
            if scenario == "rate_limit":
                raise GroqRateLimitError(private_detail)
            if scenario == "invalid_output":
                raise GroqSchemaValidationError(private_detail)
            raise AssertionError(f"Unknown synthetic scenario: {scenario}")
        finally:
            self.active_calls -= 1


def _duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a number") from exc
    if not 0 < duration <= MAX_DURATION_SECONDS:
        raise argparse.ArgumentTypeError("duration must be > 0 and <= 300 seconds")
    return duration


def _payload(scenario: str, request_number: int) -> dict[str, object]:
    return {
        "amount": 100.00,
        "risk": 0.8 if scenario == "skip" else 0.1,
        "source_country": "US",
        "target_country": "FI",
        "currency": "USD",
        "purpose": f"Synthetic load scenario {scenario} request {request_number}",
    }


def _concurrency_payload(
    scenario: str,
    worker_id: int,
    request_index: int,
) -> dict[str, object]:
    return {
        "amount": 100.00,
        "risk": 0.8 if scenario == "skip" else 0.1,
        "source_country": "US",
        "target_country": "FI",
        "currency": "USD",
        "purpose": (
            f"Synthetic concurrency scenario {scenario} worker {worker_id} "
            f"request {request_index}"
        ),
    }


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile / 100) - 1)
    return round(ordered[index], 3)


def _latency_summary(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": round(max(values), 3) if values else None,
    }


async def run_load_test(duration_seconds: float) -> dict[str, Any]:
    fake = OfflineFakeAnalyzer()
    service = CompositeDecisionService(StaticPolicyService(), fake)
    app.dependency_overrides[get_decision_service] = lambda: service

    total_requests = max(1, int(duration_seconds * RPS))
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    active_requests = 0
    max_active_requests = 0
    run_started = time.perf_counter()

    async def execute(
        client: httpx.AsyncClient,
        request_number: int,
    ) -> RequestResult:
        nonlocal active_requests, max_active_requests
        scheduled_at = run_started + request_number / RPS
        await asyncio.sleep(max(0.0, scheduled_at - time.perf_counter()))
        scenario = SCENARIO_CYCLE[request_number % len(SCENARIO_CYCLE)]

        async with semaphore:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            request_started = time.perf_counter()
            try:
                response = await client.post(
                    "/api/v1/validate",
                    json=_payload(scenario, request_number),
                )
            finally:
                active_requests -= 1

        latency_ms = (time.perf_counter() - request_started) * 1_000
        body: dict[str, Any]
        try:
            body = response.json()
        except ValueError:
            body = {}

        schema_valid = False
        if response.status_code == 200:
            try:
                ComplianceResponse.model_validate(body)
                schema_valid = True
            except ValidationError:
                pass

        fallback = (
            body.get("decision") == "FLAGGED"
            and body.get("requires_human_review") is True
            and "LLM-UNAVAILABLE-001" in body.get("policy_references", [])
        )
        return RequestResult(
            scenario=scenario,
            latency_ms=latency_ms,
            status_code=response.status_code,
            fallback=fallback,
            schema_valid=schema_valid,
        )

    try:
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://offline-test",
        ) as client:
            results = await asyncio.gather(
                *(execute(client, number) for number in range(total_requests))
            )
    finally:
        app.dependency_overrides.pop(get_decision_service, None)

    elapsed_seconds = time.perf_counter() - run_started
    status_distribution = Counter(result.status_code for result in results)
    scenario_distribution = Counter(result.scenario for result in results)
    latencies_by_scenario: defaultdict[str, list[float]] = defaultdict(list)
    for result in results:
        latencies_by_scenario[result.scenario].append(result.latency_ms)

    expected_failures = sum(
        count
        for scenario, count in scenario_distribution.items()
        if scenario in FAILURE_SCENARIOS
    )
    fallback_count = sum(result.fallback for result in results)
    schema_failures = sum(not result.schema_valid for result in results)
    skip_provider_calls = fake.calls["skip"]
    skip_p95 = _percentile(latencies_by_scenario["skip"], 95)
    assist_p95 = _percentile(latencies_by_scenario["assist"], 95)

    return {
        "profile": {
            "duration_seconds": duration_seconds,
            "target_rps": RPS,
            "max_concurrency": MAX_CONCURRENCY,
            "scheduled_requests": total_requests,
            "scenario_distribution": dict(sorted(scenario_distribution.items())),
        },
        "results": {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "throughput_rps": round(len(results) / elapsed_seconds, 3),
            "completed_requests": len(results),
            "max_observed_concurrency": max_active_requests,
            "status_distribution": {
                str(status): count
                for status, count in sorted(status_distribution.items())
            },
            "fallback_count": fallback_count,
            "schema_failure_count": schema_failures,
        },
        "latency_ms": {
            "overall": _latency_summary(
                [result.latency_ms for result in results]
            ),
            "by_scenario": {
                scenario: _latency_summary(values)
                for scenario, values in sorted(latencies_by_scenario.items())
            },
        },
        "fake_provider": {
            "calls_total": sum(fake.calls.values()),
            "calls_by_scenario": dict(sorted(fake.calls.items())),
            "skip_calls": skip_provider_calls,
            "max_observed_concurrency": fake.max_active_calls,
        },
        "checks": {
            "static_skip_p95_le_100_ms": (
                skip_p95 is not None and skip_p95 <= 100
            ),
            "successful_assist_p95_le_500_ms": (
                assist_p95 is not None and assist_p95 <= 500
            ),
            "zero_http_500": status_distribution[500] == 0,
            "all_failures_used_safe_fallback": (
                fallback_count == expected_failures
            ),
            "zero_provider_calls_for_skip": skip_provider_calls == 0,
            "all_responses_match_schema": schema_failures == 0,
        },
    }


async def run_concurrency_test() -> dict[str, Any]:
    fake = SlowConcurrencyFakeAnalyzer()
    service = CompositeDecisionService(StaticPolicyService(), fake)
    app.dependency_overrides[get_decision_service] = lambda: service

    start_barrier = asyncio.Barrier(CONCURRENCY_WORKERS)
    active_requests = 0
    max_active_requests = 0
    run_started = time.perf_counter()

    async def execute_request(
        client: httpx.AsyncClient,
        worker_id: int,
        request_index: int,
    ) -> ConcurrencyRequestResult:
        nonlocal active_requests, max_active_requests
        scenario = SCENARIO_CYCLE[worker_id]
        correlation = worker_id * REQUESTS_PER_WORKER + request_index
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        await asyncio.sleep(0)
        request_started = time.perf_counter()
        try:
            response = await client.post(
                "/api/v1/validate",
                json=_concurrency_payload(
                    scenario,
                    worker_id,
                    request_index,
                ),
            )
        finally:
            active_requests -= 1

        latency_ms = (time.perf_counter() - request_started) * 1_000
        completed_at_ms = (time.perf_counter() - run_started) * 1_000
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}

        schema_valid = False
        if response.status_code == 200:
            try:
                ComplianceResponse.model_validate(body)
                schema_valid = True
            except ValidationError:
                pass

        fallback = (
            body.get("decision") == "FLAGGED"
            and body.get("requires_human_review") is True
            and "LLM-UNAVAILABLE-001" in body.get("policy_references", [])
        )
        if scenario == "skip":
            state_isolated = (
                body.get("decision_source") == "STATIC_RULE"
                and body.get("input_tokens") is None
                and body.get("output_tokens") is None
                and body.get("policy_references") == ["RISK-002"]
            )
        elif scenario == "assist":
            expected_reason = (
                f"Synthetic concurrency result worker {worker_id} "
                f"request {request_index}."
            )
            state_isolated = (
                body.get("decision_source") == "LLM_ASSISTED"
                and body.get("input_tokens") == 10_000 + correlation
                and body.get("output_tokens") == 20_000 + correlation
                and expected_reason in body.get("reason", "")
            )
        else:
            state_isolated = (
                fallback
                and body.get("input_tokens") is None
                and body.get("output_tokens") is None
                and body.get("reason")
                == "LLM analysis is unavailable; human review is required."
            )

        return ConcurrencyRequestResult(
            scenario=scenario,
            latency_ms=latency_ms,
            completed_at_ms=completed_at_ms,
            status_code=response.status_code,
            fallback=fallback,
            schema_valid=schema_valid,
            request_id=body.get("request_id"),
            state_isolated=state_isolated,
        )

    async def worker(
        client: httpx.AsyncClient,
        worker_id: int,
    ) -> list[ConcurrencyRequestResult]:
        await start_barrier.wait()
        results: list[ConcurrencyRequestResult] = []
        for request_index in range(REQUESTS_PER_WORKER):
            results.append(
                await execute_request(client, worker_id, request_index)
            )
        return results

    try:
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://offline-test",
        ) as client:
            worker_results = await asyncio.gather(
                *(worker(client, worker_id) for worker_id in range(CONCURRENCY_WORKERS))
            )
    finally:
        app.dependency_overrides.pop(get_decision_service, None)

    results = [result for worker_result in worker_results for result in worker_result]
    elapsed_seconds = time.perf_counter() - run_started
    status_distribution = Counter(result.status_code for result in results)
    scenario_distribution = Counter(result.scenario for result in results)
    latencies_by_scenario: defaultdict[str, list[float]] = defaultdict(list)
    for result in results:
        latencies_by_scenario[result.scenario].append(result.latency_ms)

    request_ids = [result.request_id for result in results if result.request_id]
    duplicate_request_ids = len(request_ids) - len(set(request_ids))
    missing_request_ids = len(results) - len(request_ids)
    state_leakage_count = sum(not result.state_isolated for result in results)
    schema_failures = sum(not result.schema_valid for result in results)
    expected_failures = sum(
        count
        for scenario, count in scenario_distribution.items()
        if scenario in FAILURE_SCENARIOS
    )
    fallback_count = sum(result.fallback for result in results)
    skip_results = [result for result in results if result.scenario == "skip"]
    slow_results = [result for result in results if result.scenario != "skip"]
    last_skip_completion_ms = max(
        result.completed_at_ms for result in skip_results
    )
    first_slow_completion_ms = min(
        result.completed_at_ms for result in slow_results
    )
    skip_max_latency_ms = max(result.latency_ms for result in skip_results)

    return {
        "profile": {
            "mode": "concurrency-test",
            "workers": CONCURRENCY_WORKERS,
            "requests_per_worker": REQUESTS_PER_WORKER,
            "scheduled_requests": len(results),
            "fake_provider_delay_ms": {"min": 200, "max": 500},
            "scenario_distribution": dict(sorted(scenario_distribution.items())),
        },
        "results": {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "throughput_rps": round(len(results) / elapsed_seconds, 3),
            "completed_requests": len(results),
            "peak_request_concurrency": max_active_requests,
            "peak_provider_concurrency": fake.max_active_calls,
            "status_distribution": {
                str(status): count
                for status, count in sorted(status_distribution.items())
            },
            "fallback_count": fallback_count,
            "schema_failure_count": schema_failures,
            "duplicate_request_id_count": duplicate_request_ids,
            "missing_request_id_count": missing_request_ids,
            "state_leakage_count": state_leakage_count,
            "last_skip_completion_ms": round(last_skip_completion_ms, 3),
            "first_slow_completion_ms": round(first_slow_completion_ms, 3),
        },
        "latency_ms": {
            "overall": _latency_summary(
                [result.latency_ms for result in results]
            ),
            "by_scenario": {
                scenario: _latency_summary(values)
                for scenario, values in sorted(latencies_by_scenario.items())
            },
        },
        "fake_provider": {
            "calls_total": sum(fake.calls.values()),
            "calls_by_scenario": dict(sorted(fake.calls.items())),
            "skip_calls": fake.calls["skip"],
        },
        "checks": {
            "exactly_500_requests": len(results) == 500,
            "peak_request_concurrency_reached_20": max_active_requests == 20,
            "all_request_ids_unique_and_present": (
                duplicate_request_ids == 0 and missing_request_ids == 0
            ),
            "zero_state_leakage": state_leakage_count == 0,
            "skip_did_not_wait_for_slow_assist": (
                last_skip_completion_ms < first_slow_completion_ms
                and skip_max_latency_ms < MIN_SLOW_PROVIDER_DELAY_MS
            ),
            "zero_provider_calls_for_skip": fake.calls["skip"] == 0,
            "zero_http_500": status_distribution[500] == 0,
            "all_failures_used_safe_fallback": (
                fallback_count == expected_failures
            ),
            "all_responses_match_schema": schema_failures == 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the in-process offline compliance load profile."
    )
    parser.add_argument(
        "--mode",
        choices=("rate", "concurrency-test"),
        default="rate",
        help="profile to run (default: rate)",
    )
    parser.add_argument(
        "--duration",
        type=_duration,
        default=DEFAULT_DURATION_SECONDS,
        metavar="SECONDS",
        help="run duration, > 0 and <= 300 (default: 30)",
    )
    args = parser.parse_args()

    with patch(
        "app.groq_client.AsyncGroq",
        side_effect=RuntimeError("AsyncGroq is forbidden in the offline harness"),
    ):
        if args.mode == "concurrency-test":
            report = asyncio.run(run_concurrency_test())
        else:
            report = asyncio.run(run_load_test(args.duration))

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
