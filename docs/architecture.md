# Decision pipeline architecture

The current implementation preserves deterministic static policy priority and
uses an optional LLM-assisted stage through a safe composite pipeline:

```text
Input validation
→ deterministic static policies
→ optional LLM-assisted analysis
→ structured-output validation
→ safe merge
→ response
```

## Safety and audit invariants

1. An LLM cannot override a static `FLAGGED` decision.
2. An LLM cannot independently issue a final automatic `APPROVED` decision.
3. An LLM may add policy references or an explanation, or require human review.
4. A timeout, API failure, refusal, or invalid output schema produces the safe
   result `FLAGGED` with `requires_human_review=true`.
5. The static result and LLM recommendation must be stored separately for audit.
6. Full transaction data must not be written to ordinary application logs.

`decision_source` identifies whether the final business decision came solely
from deterministic rules (`STATIC_RULE`) or includes safely merged LLM-assisted
analysis (`LLM_ASSISTED`). Static rules return `confidence=null`, because a rule
match is not a statistically calibrated estimate of real-world risk.

Country and currency validation currently checks only uppercase code shape (two
letters for countries and three for currencies). It does not verify membership
in ISO country or currency registries.

## Pipeline status

The implemented request path is currently:

```text
ComplianceRequest validation
→ CompositeDecisionService
→ StaticPolicyService
→ SKIP or GroqComplianceClient structured analysis
→ safe merge
→ ComplianceResponse
```

`GroqComplianceClient` remains an isolated transport boundary. The composite
service invokes it only for `ASSIST`; `SKIP` does not require a key and does not
create an SDK client. The pipeline is:

```text
Input validation
→ deterministic static policies
→ optional LLM-assisted analysis
→ structured-output validation
→ safe merge
→ response
```

## Safe-merge matrix

This matrix is implemented by `CompositeDecisionService`.

| Static result | Routing | LLM result | Final result |
| --- | --- | --- | --- |
| `COUNTRY-001` or `RISK-002`: `FLAGGED` | `SKIP` | Not called | Preserve static `FLAGGED` |
| `RISK-REVIEW-001`: `FLAGGED`, review required | `ASSIST` | Any valid action | Preserve `FLAGGED` and review; LLM may enrich explanation/references |
| `BASE-001`: `APPROVED` | `ASSIST` | `NONE` | Preserve static `APPROVED` |
| `BASE-001`: `APPROVED` | `ASSIST` | `REVIEW` or `FLAG` | `FLAGGED`, review required |
| Any `ASSIST` result | `ASSIST` | Timeout, refusal, transport error, or invalid schema | `FLAGGED`, review required |

The LLM never weakens a static decision. An `APPROVED` outcome in the matrix is
grounded in `BASE-001`, not independently granted by the LLM.

## Dependency injection

FastAPI depends on the `DecisionService` protocol through
`Depends(get_decision_service)`. The provider receives the lazy
`GroqComplianceClient` through `Depends(get_groq_client)` and returns
`CompositeDecisionService(StaticPolicyService(), groq_client)`. Offline tests use
`app.dependency_overrides` with a static service or fake analyzer and always
remove overrides after use.

## Tests

Standard tests are offline and exclude the integration marker:

```bash
python3 -m uv run pytest -v
```

The live Groq test is opt-in, uses a synthetic purpose, and makes a billable
external request. Run it only with explicit authorization and a configured key:

```bash
GROQ_API_KEY=... python3 -m uv run pytest -m integration -v tests/test_groq_integration.py
```

## Privacy warning

Never send real customer, payment, personal, confidential, or regulated data to
tests or external providers. Purpose text is untrusted data: it must remain out
of system prompts and ordinary logs. Secrets belong in an uncommitted `.env` or
the runtime environment. Static results and future LLM recommendations must be
retained separately in an approved audit store, with access and retention
controls defined before production.
