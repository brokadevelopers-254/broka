# Administrative Zeno (Jarvis Mode) — Phase 1.1 architecture

Zeno in Jarvis Mode is a dedicated, read-only administrative intelligence
system at `/admin/zeno`. It answers an administrator's question by planning a
bounded investigation, running only registered evidence tools, and
synthesizing an answer in which every figure is traceable to retrieved
evidence.

Phase 1 is a foundation, not the full Jarvis roadmap: no administrative
mutations, no autonomous agents, no proactive triggers, no long-term memory
and no frontend.

## Module map

Code: `backend/api/domains/admin_zeno/`

| Module | Responsibility |
|---|---|
| `router.py` | The `/admin/zeno` API, feature flag and permission dependencies, rate limiting |
| `schemas.py` | Typed request/response contracts, `Evidence`, `BriefingResponse` |
| `orchestrator.py` | Plan → validate → execute → synthesize → validate → persist |
| `tool_registry.py` | The closed registry: typed arguments, permission check, timeout, sequential execution |
| `model_gateway.py` | Administrative model adapter over the existing provider chain and cost ledger |
| `prompts.py` | Administrative system prompt, planning, synthesis and repair prompts |
| `evidence.py` | Evidence construction, availability rules, reference validation, safe fallback |
| `conversation_service.py` | Administrator-owned conversations, bounded context, 30-day retention |
| `briefing_service.py` | Deterministic intelligence briefing with a short TTL cache |
| `models.py` | `admin_zeno_conversations`, `admin_zeno_turns` |
| `tools/` | The eight intelligence domains (below) |

It is independent of `zeno_assistant`. Shared code is limited to the AI
provider transport, circuit breakers, `record_ai_cost`, the existing analytics
services and the permission system. Administrative prompts and conversations
never enter the customer-facing Zeno routes.

## Request flow

```
Administrator question
  → authenticated admin with ADMIN_SUMMARY (and the feature flag)
  → conversation ownership checked (or a new conversation)
  → bounded recent context loaded
  → release the DB connection before the model call
  → model plans: action + up to 4 registered tools + arguments
  → registry validates each name/argument and checks the tool permission
  → tools run sequentially on the request's AsyncSession
  → evidence deduplicated, unavailable metrics kept as unavailable
  → release the DB connection again
  → model synthesizes JSON from that evidence only
  → every evidence reference validated against what was supplied
  → one bounded repair attempt on malformed output, else deterministic fallback
  → question + answer persisted; usage metadata recorded by the gateway
```

No private chain-of-thought is requested, stored or returned. The response
carries an `investigation` object with tool names, provider, model, latency and
`synthesis` (`model`, `fallback`, `clarify` or `no_evidence`).

## Execution limits

Defined centrally in `constants.py`:

| Limit | Default |
|---|---|
| Tool executions per turn | 4 |
| Model stages | planning + synthesis, plus at most one format-repair call |
| Per-tool timeout | 8–15 s (tool-specific; default 8 s) |
| Per-model-call timeout | 25 s (`ADMIN_ZENO_MODEL_TIMEOUT_SECONDS`) |
| Whole-turn timeout | 45 s (`ADMIN_ZENO_TOTAL_TIMEOUT_SECONDS`) |
| Context turns | 6 (`ADMIN_ZENO_CONTEXT_TURNS`) |
| Characters per context turn | 1,200 |
| Evidence objects sent to the model | 40 |
| Conversation retention | 30 days |
| Rate limit | 10 turns/minute per administrator |
| Briefing cache | 60 s (`ADMIN_ZENO_BRIEFING_CACHE_SECONDS`) |

Tools execute sequentially against one `AsyncSession`; no two statements share
the session concurrently. A failed or slow tool cannot abort the turn: it is
reported as a limitation and the remaining tools still run.

## Tool registry (23 tools, eight domains)

Every tool name is stable, every argument model uses `extra="forbid"`, every
tool declares a permission, and every handler wraps an existing analytics
service — there is no second analytics implementation in this package.

### Business (`ADMIN_SUMMARY`)

| Tool | Reuses |
|---|---|
| `business_overview` | `api.core.command_center.command_center` |
| `founder_insights` | `api.core.intelligence.pulse` + `api.core.insights.build_insights` |
| `marketplace_liquidity` | `api.core.intelligence.pulse` |
| `seller_responsiveness` | `api.core.intelligence.pulse` |
| `business_targets` | `api.core.intelligence.pulse` (targets, break-even) |

### Marketplace (`ADMIN_SUMMARY`)

| Tool | Reuses |
|---|---|
| `marketplace_demand` | `api.core.market_intel.demand` |
| `listing_rankings` | `api.core.market_intel.rankings` |
| `marketplace_performance` | `api.core.market_intel.performance` |
| `marketplace_anomalies` | `api.core.market_models.detect` |
| `marketplace_forecasts` | `api.core.market_models.forecasts` (validated only; withheld reported) |

### Operations (`ADMIN_SUMMARY`)

| Tool | Reuses |
|---|---|
| `operations_overview` | `api.core.operations.operations_report` |
| `operational_anomalies` | `api.core.ops_anomalies.detect` |

### Finance (`VIEW_TRANSACTIONS`)

| Tool | Reuses |
|---|---|
| `platform_revenue` | `api.core.revenue.revenue_report` |
| `platform_costs` | `api.core.revenue.revenue_report` + cost ledger |
| `break_even` | `api.core.intelligence.pulse` |

### Trust and safety (`VIEW_ALL_DISPUTES`)

| Tool | Reuses |
|---|---|
| `trust_overview` | `api.core.intelligence.complaints_overview` + aggregate counts of reports, disputes, fraud flags and moderation events |

Aggregates only. Message contents, call contents, phone numbers and
customer-identifying detail are not read into a prompt. Individual account
investigation remains out of scope.

### Growth (`ADMIN_SUMMARY`)

| Tool | Reuses |
|---|---|
| `growth_registrations` | `api.core.intelligence.pulse` + `api.core.activity.registration_retention` |
| `growth_engagement` | `api.core.activity.engagement_summary` |
| `growth_cohorts` | `api.core.intelligence.cohorts` |
| `growth_funnel` | `api.core.intelligence.funnel` |

### Zeno (`ADMIN_SUMMARY`)

| Tool | Reuses |
|---|---|
| `zeno_usage` | `zeno_conversations` / `zeno_turns` counts and `platform_costs` AI rows |

Customer-facing and administrative AI usage are reported separately.
Response quality is not measured because BROKA has no evaluation mechanism.

### Platform (`ADMIN_SUMMARY`)

| Tool | Reuses |
|---|---|
| `platform_health` | `api.core.operations.operations_report` |
| `platform_integrations` | configuration presence booleans only (never values) |

## API contracts

All routes require an authenticated administrator and `ADMIN_SUMMARY`; the
feature flag is checked first. When `ADMIN_ZENO_ENABLED` is false every route
answers `503` with `{"code": "ADMIN_ZENO_DISABLED"}`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/zeno/status` | Readiness: enabled, gateway ready, configured provider **names**, registry count, last error category, timestamp |
| `GET` | `/admin/zeno/briefing?window_days=7` | Deterministic briefing; no model call |
| `POST` | `/admin/zeno/chat` | One investigation turn |
| `GET` | `/admin/zeno/conversations?limit&offset` | The caller's conversations, bounded pagination |
| `GET` | `/admin/zeno/conversations/{id}?limit&offset` | One conversation and its turns, owner only |
| `DELETE` | `/admin/zeno/conversations/{id}` | Delete the caller's conversation and its turns |

`POST /admin/zeno/chat` request:

```json
{
  "conversation_id": null,
  "message": "Why has seller responsiveness declined over the last 7 days?",
  "context": {"page": "command-center", "period_days": 7}
}
```

`message` is 1–2000 characters; `extra="forbid"` rejects unknown fields.

Response (abridged):

```json
{
  "conversation_id": "<uuid>",
  "answer": {"headline": "...", "summary": "..."},
  "findings": [{"statement": "...", "evidence": ["business.seller_response.rate_24h"],
                "confidence": "measured"}],
  "possible_causes": [{"statement": "..."}],
  "recommendations": [{"action": "...", "rationale": "...", "page": "/intelligence"}],
  "evidence": [{"id": "ev.business.seller_response.rate_24h",
                "metric_key": "business.seller_response.rate_24h",
                "label": "Seller replies within 24 hours", "value": 63.2,
                "unit": "percentage", "previous_value": 71.0,
                "period": {"start": "...", "end": "...", "timezone": "UTC"},
                "source": "api.core.intelligence.pulse", "definition": "...",
                "generated_at": "...", "available": true,
                "unavailable_reason": null, "confidence": "measured"}],
  "limitations": ["..."],
  "related_pages": ["/intelligence"],
  "suggested_questions": ["..."],
  "generated_at": "...",
  "investigation": {"tools_used": ["seller_responsiveness"], "tool_errors": [],
                    "provider": "deepseek", "model": "deepseek-flash",
                    "latency_ms": 1830, "synthesis": "model"}
}
```

Failure responses:

| Status | Code | Meaning |
|---|---|---|
| 401/403 | — | Not signed in / not an administrator |
| 404 | — | Conversation missing **or** owned by another administrator |
| 429 | — | Per-administrator rate limit |
| 503 | `ADMIN_ZENO_DISABLED` | Feature flag off |
| 503 | `MODEL_UNAVAILABLE` | No provider produced a usable response; no answer was generated |
| 504 | `INVESTIGATION_TIMEOUT` | Whole-turn limit exceeded |

## Model-context privacy boundary

No tool payload reaches a provider directly. `sanitizer.sanitize_model_context`
runs on the serialized evidence and tool data immediately before the
synthesis and repair prompts, and removes anything that could identify a
customer:

- An explicit allowlist (`SAFE_TOOL_DATA_FIELDS`) of the supplementary `data`
  fields each registered tool may expose. A tool without an entry exposes no
  data, and `tests/test_admin_zeno_phase11.py` fails if a registered tool has
  no reviewed entry.
- Recursive key filtering at every depth: `id`, `*_id`, names, e-mail, phone,
  credentials, tokens, message and call content, plus any key that looks like
  an opaque identifier.
- Recursive value inspection: strings that look like an e-mail, UUID, phone
  number or PEM key block are dropped; lists and dictionaries are bounded.
- Evidence whose label, definition or value cannot be proven safe is dropped
  whole rather than partially rewritten, so it cannot be cited.

The tools also redact at the source, so the boundary is not the only guard:
the trust tool reduces `repeat_reported_users` to a count and a maximum, the
marketplace tools rank sellers and listings instead of naming them, and
`founder_insights` summarises per-account insights as counts. Only the number
of removed fields is reported (as a limitation), never their contents. Nothing
removed is logged.

## Evidence rules

- Every quantitative statement must cite evidence supplied in that turn.
  `orchestrator._validate_payload` rejects unknown `metric_key` references.
- A citation alone is not proof of a figure. Each numerical finding carries
  structured `claims` (`claims.py`): `metric_key`, `asserted_value`, `unit`,
  `evidence_reference`, `derivation` and `confidence`. The backend verifies:
  - the reference exists in this investigation and is available;
  - `metric_key` matches the referenced evidence;
  - the asserted value equals the evidence value (or a permitted derivation);
  - the unit matches exactly;
  - the confidence is not stronger than the evidence's confidence.
- Permitted derivations are only `value`, `previous_value`, `delta`
  (current − previous) and `pct_change` (rounded to one decimal). The model
  cannot invent its own arithmetic.
- Free text is scanned for numbers. Headline, summary, causes and
  recommendations may only repeat figures traceable to validated evidence or
  server-authored metric labels/definitions/limitations; a finding's statement
  may only use the figures of the evidence that finding cites.
- A metric that cannot be measured is `available: false`, `value: null`, with
  an `unavailable_reason`. It is never zero.
- Confidence is one of `measured`, `derived`, `estimated`, `hypothesis` or
  `unavailable`. Bytes priced from assumed rates are `estimated`.
- A synthesis that fails validation gets one bounded repair attempt and then
  `evidence.fallback_response`: verified figures only (each with a structured
  claim), no interpretation. An invalid synthesis is never returned.
- If no tool returns evidence, the answer says so and reports no numbers.
- A failed database tool rolls back to its savepoint (see “Tool execution and
  session recovery”), so the rest of the investigation can still produce an
  evidence-backed answer.

## Model gateway and fallback

`model_gateway.AdminModelGateway`:

- Provider chain: Gemini → DeepSeek → OpenRouter → Groq, reusing the existing
  `api.core.circuit_breaker` breakers and the configured models. A provider
  with no key is skipped; an open circuit is skipped.
- Per-call timeout (25 s) and a shared circuit breaker; no cached customer
  answer is ever served for an administrative question.
- Gemini is asked for JSON (`responseMimeType`) and providers get a JSON-only
  prompt; `extract_json` accepts fences but never repairs malformed JSON.
- A successful call records `cost_ledger.record_ai_cost` with
  `call_site="admin_zeno.chat"`, so Jarvis spend is separately identifiable.
- Failed calls record nothing and are not counted as successful answers.
- The administrative system prompt (`prompts.ADMIN_SYSTEM_PROMPT`) is used for
  every call. It contains no buyer/seller persona or action vocabulary.

`GET /admin/zeno/status` reports configured provider **names** only. A
configured provider is not claimed to be healthy.

## Conversation persistence

Two new tables, created by `Base.metadata.create_all()` (registered in
`api/database.py`). No existing table is altered, so no migration statement is
needed.

`admin_zeno_conversations`: `id` (UUID4), `admin_user_id` (FK users), `title`,
`created_at`, `updated_at`, `expires_at`.

`admin_zeno_turns`: `id` (UUID4), `conversation_id` (FK), `role`, `content`,
`evidence_json`, `metadata_json`, `created_at`.

- Ownership is part of every query; another administrator's conversation is
  `404`, indistinguishable from a missing one.
- Deletion removes the turns explicitly, then the conversation.
- `expires_at` is refreshed only when a new valid turn is persisted, and is
  30 days out. Touching an expired row never extends it.
- Expiry is enforced at read time: `get_owned` and `list_conversations`
  (including its total count) exclude `expires_at <= now`, so an expired
  conversation is indistinguishable from a missing one immediately.
- `conversation_service.cleanup_expired` removes expired conversations and
  their turns; it is safe to run repeatedly. The sweep
  (`workers.task_cleanup_admin_zeno_conversations`) runs regardless of
  `ADMIN_ZENO_ENABLED`: the flag controls access, not retention.
- Only the question, the user-facing answer, its evidence references and safe
  metadata are stored. No prompt, credential, reasoning trace or customer
  personal data.
- Customer-facing `zeno_conversations` / `zeno_turns` are untouched.

## Security and permissions

- `ADMIN_SUMMARY` for the interface; narrower existing permissions for
  sensitive tools (`VIEW_TRANSACTIONS` for finance, `VIEW_ALL_DISPUTES` for
  trust).
- The registry enforces tool permissions in the backend. Prompt text cannot
  grant a capability.
- The actor is always the signed-in user; no client-supplied administrator id
  is trusted.
- Tools are read-only and chosen from a closed registry; no SQL, shell, file
  or arbitrary Python execution exists.
- Business-model guard: BROKA is not a payment processor or escrow custodian;
  deal value and escrow principal are never BROKA revenue, and prompts and
  finance tool limitations state this.
- Prompt-injection guard: retrieved content and administrator text are data.
  An unregistered tool name is rejected as `unknown_tool`, and extra arguments
  are rejected by schema validation.
- No administrative mutation exists in Phase 1. The model may recommend an
  action and name its dashboard page, never perform it.

## Rate limits, cost and performance

- `admin_zeno` limiter, keyed by the authenticated administrator, 10/minute
  (`ADMIN_ZENO_RATE_LIMIT_PER_MINUTE`), separate from every customer bucket.
- AI cost is attributed to `admin_zeno.chat` in `platform_costs` with
  `call_site` in `meta`, and token counts where the provider supplies them.
  Unpriced calls are recorded as unpriced, never as zero.
- Tools reuse bounded analytics services; the briefing aggregates existing
  deterministic analysis and is cached for 60 s per process. Briefings never
  need a model call.

## Known limitations

- No proactive investigation, anomaly interpretation loop or scheduled
  briefing: the briefing is computed on request (with a short cache).
- No individual account investigation, no message/call content access and no
  personal-data search.
- Response quality is not evaluated; only provider usage is measured.
- Forecasts are reported only when the existing model's backtest published
  them; otherwise they are withheld.
- Availability is not measured from outside; application-level samples cannot
  prove end-to-end uptime.
- Retention cleanup runs in the existing 5-minute periodic sweep
  (`workers.task_cleanup_admin_zeno_conversations`) even while the feature
  flag is off. Reads already exclude expired rows; the sweep bounds storage.
- Financial cost categories distinguish a measured zero from no recorded
  rows, incomplete tracking, assumed rates and unpriced rows; only the first
  is reported as a measured zero.
- Zeno AI cost totals come from exact database aggregates. The customer /
  administrative split needs row metadata, so it is read in bounded pages;
  when the ledger exceeds the bound the split is reported unavailable rather
  than from a partial sample.
- Phase 1 has no semantic/vector memory and no cross-conversation learning.

## Tool execution and session recovery

Tools run sequentially on the request's `AsyncSession`, each inside a
savepoint. A handler that raises a database error rolls back to that savepoint
instead of poisoning the shared session, so the next tool in the same turn
executes normally and the investigation can still answer from usable evidence.
The registry logs the exception type only (never SQL, parameters or row data)
and reports the generic category `data_unavailable`. Cancellation is re-raised,
and a full session rollback is used only if the savepoint itself could not
recover. Effective permissions are snapshotted before the tool loop and the
conversation id is captured as a plain value, so recovery never touches an
expired ORM object.

## Future extension points

- Add new tools by registering them in `tools/` and adding its model-facing
  data allowlist to `sanitizer.SAFE_TOOL_DATA_FIELDS` (a test enforces this).
- Add proactive briefings by invoking `briefing_service.build_briefing` from a
  worker and persisting its output.
- Add a narrower administrator role by giving tools narrower permissions; the
  registry already enforces them per tool.
- Add response-quality evaluation before claiming any quality metric.

## Configuration

`ADMIN_ZENO_ENABLED` defaults off in production and on in development/test
(`config._on_unless_production`). Other tunables: `ADMIN_ZENO_TOTAL_TIMEOUT_SECONDS`,
`ADMIN_ZENO_MODEL_TIMEOUT_SECONDS`, `ADMIN_ZENO_CONTEXT_TURNS`,
`ADMIN_ZENO_BRIEFING_CACHE_SECONDS`, `ADMIN_ZENO_RATE_LIMIT_PER_MINUTE`.

## Tests

`backend/tests/test_admin_zeno.py` (36 tests) covers authentication and
authorisation, backend-enforced tool permissions, conversation ownership and
deletion scoping, the closed registry (unknown tools, invalid arguments,
timeouts, failures), tool limits, evidence availability and reference
validation, malformed model output and bounded repair, prompt injection,
provider outage, deterministic briefing, alert deduplication, cache behaviour
and preservation of `POST /admin/assistant/ask`.

`backend/tests/test_admin_zeno_phase11.py` (43 tests) covers the remediation:
model-context sanitisation and allowlist completeness, trust/marketplace/
founder-insight redaction, serialized prompt inspection, claim validation
(value, unit, confidence, derivations, unavailable and foreign evidence),
free-text figure rejection, evidence-only fallback, briefing reference
integrity and periods, read-time retention, cleanup while disabled, database
session recovery, the five financial cost states and bounded Zeno cost
aggregation with truncation reporting.

The suite passes on SQLite and on PostgreSQL (via `tests.postgres_plugin`).
Model calls are scripted in tests; CI never calls a live provider.
