from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field


CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Decision = Literal["APPROVED", "FLAGGED"]
DecisionSource = Literal["STATIC_RULE", "LLM_ASSISTED"]


class ComplianceRequest(BaseModel):
    amount: Annotated[
        Decimal,
        Field(gt=0, max_digits=12, decimal_places=2),
    ]
    risk: Annotated[float, Field(ge=0, le=1)]
    source_country: CountryCode
    target_country: CountryCode
    currency: CurrencyCode
    purpose: Annotated[str, Field(min_length=5, max_length=500)]


class ComplianceResponse(BaseModel):
    request_id: UUID
    decision: Decision
    reason: str
    policy_references: list[str]
    decision_source: DecisionSource
    confidence: float | None
    requires_human_review: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: Annotated[float, Field(ge=0)]
    estimated_cost_usd: Decimal | None
