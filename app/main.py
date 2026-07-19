from decimal import Decimal
import time
from uuid import uuid4

from fastapi import Depends, FastAPI

from app.schemas import ComplianceRequest, ComplianceResponse
from app.services import DecisionService, get_decision_service


app = FastAPI()


@app.post("/api/v1/validate", response_model=ComplianceResponse)
async def validate_transaction(
    transaction: ComplianceRequest,
    decision_service: DecisionService = Depends(get_decision_service),
) -> ComplianceResponse:
    started_at = time.perf_counter()
    policy_decision = await decision_service.decide(transaction)
    request_id = uuid4()
    latency_ms = (time.perf_counter() - started_at) * 1_000
    llm_was_attempted = (
        policy_decision.decision_source == "LLM_ASSISTED"
        or "LLM-UNAVAILABLE-001" in policy_decision.policy_references
    )

    return ComplianceResponse(
        **policy_decision.model_dump(exclude={"llm_routing"}),
        request_id=request_id,
        latency_ms=latency_ms,
        estimated_cost_usd=(None if llm_was_attempted else Decimal("0")),
    )
