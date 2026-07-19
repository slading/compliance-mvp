# Compliance MVP

Use only synthetic transaction data in development, tests, examples, and
evaluation fixtures. Do not use real customer, payment, or personal data.

Local secrets belong in `.env`. The file is ignored by Git and must never be
committed.

## Pipeline

The application uses a composite pipeline. Static rules always run first;
`GroqComplianceClient` is called only for `ASSIST`, and `SKIP` requires neither a
Groq key nor SDK-client construction:

```text
Input validation
→ deterministic static policies
→ optional LLM-assisted analysis
→ structured-output validation
→ safe merge
→ response
```

## Merge matrix

| Static result | LLM recommendation | Safe outcome |
| --- | --- | --- |
| Static `FLAGGED` with `SKIP` | Not requested | Preserve static result |
| Static `FLAGGED` with `ASSIST` | Any valid result | Preserve `FLAGGED`; enrichment only |
| `BASE-001` `APPROVED` | `NONE` | Preserve static `APPROVED` |
| `BASE-001` `APPROVED` | `FLAG` or `REVIEW` | `FLAGGED`, human review required |
| Any assisted path | Timeout, refusal, API/schema failure | `FLAGGED`, human review required |

The matrix is implemented by `CompositeDecisionService`.

## Dependency injection

The endpoint receives `DecisionService` through `Depends(get_decision_service)`.
The provider receives a lazily initialized client through
`Depends(get_groq_client)` and returns `CompositeDecisionService` with
`StaticPolicyService`. Tests substitute a static service or fake provider at this
boundary and clean up overrides after every test.

## Tests

Install and run the default offline suite:

```bash
python3 -m uv sync --extra dev
python3 -m uv run pytest -v
```

The `integration` marker is excluded by default. The live Groq test must be run
only with explicit authorization because it makes an external, potentially
billable request:

```bash
GROQ_API_KEY=... python3 -m uv run pytest -m integration -v tests/test_groq_integration.py
```

## Privacy warning

Use synthetic data only. Never send real customer, payment, personal,
confidential, or regulated data to fixtures, logs, prompts, or external
providers. Treat purpose text as untrusted data and keep it out of system prompts
and ordinary logs. Keep API keys in the runtime environment or an uncommitted
`.env` file.
