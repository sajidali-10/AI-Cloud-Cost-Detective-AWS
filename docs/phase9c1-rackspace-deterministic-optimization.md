# Phase 9C.1 — Rackspace Deterministic Optimization Engine

Status: complete (branch `phase-9c1-rackspace-optimization`)

Phase 9C.1 introduces an **internal, deterministic, evidence-driven
Rackspace optimization engine** on top of the Phase 9B canonical cost
+ resource layers.  It does **not** wire Rackspace findings into the
public `/api/cloud/recommendations` route — that integration is
Phase 9C.2.

## Architecture

```
Phase 9B.1 canonical cost layer        Phase 9B.2 canonical resource layer
       (CloudCostSummary)                         (List[CloudResource])
                  │                                          │
                  └────────────────┬─────────────────────────┘
                                   ▼
              app.services.cloud.rackspace.optimization
              ─────────────────────────────────────────
              • RackspaceFinding (internal Pydantic / dataclass model)
              • RS-VOL-001 — detached block storage review
              • RS-SRV-001 — non-active server review
              • RS-LB-001  — load balancer review (no-op for empty)
              • deterministic_finding_id() — SHA-256 over stable inputs
              • evaluate_rackspace_optimization() — pure orchestrator
              • Stable ordering: severity desc → rule_id → size desc
                                   → region → resource_id
              • Rightsizing gate (ACTIVE servers never rightsized
                without metrics evidence)
                                   │
                                   ▼
              app.services.cloud.rackspace.metrics_capability
              ─────────────────────────────────────────
              • RightsizingEvidenceStatus enum
              • probe_metrics_capability_sync() — Phase 9C.1 default:
                DEFERRED, note=RIGHTSIZING_METRICS_DEFERRED
              • Live catalog probe is opt-in
                (RACKSPACE_METRICS_PROBE_ENABLED=1); not enabled in 9C.1
```

The engine is **pure**:

* Consumes already-normalized canonical inputs (Phase 9B.1 cost,
  Phase 9B.2 resources).
* Issues **zero** Rackspace product API calls itself.  Phase 9B's
  inventory path is what populated the resources; the engine merely
  evaluates them.
* No `PUT` / `PATCH` / `DELETE` are emitted from this module — a
  static assertion in `test_phase9c1_rackspace_optimization_engine.py`
  enforces it.

## Finding model

```python
@dataclass(frozen=True)
class RackspaceFinding:
    finding_id: str              # rs-det-<rule>-<hash12>
    provider: str                # always "RACKSPACE"
    account_id: Optional[str]
    rule_id: str                 # RS-VOL-001 | RS-SRV-001 | RS-LB-001
    category: str                # STORAGE_REVIEW | SERVER_REVIEW | LOAD_BALANCER_REVIEW
    resource_type: str           # VOLUME | SERVER | LOAD_BALANCER
    resource_id: str
    resource_name: Optional[str]
    region: Optional[str]
    severity: RackspaceFindingSeverity     # LOW | MEDIUM | HIGH
    confidence: RackspaceFindingConfidence # LOW | MEDIUM | HIGH
    title: str
    summary: str
    recommended_action: str
    evidence: Tuple[Dict[str, Any], ...]
    estimated_monthly_savings: Optional[Decimal]   # None when unsupported
    savings_basis: str                           # UNAVAILABLE in 9C.1
    limitations: Tuple[str, ...]
    source: str                                  # RACKSPACE_CLOUD_API
```

The model is intentionally internal: it is **not** exported on
`app.schemas.*` because Phase 9C.1 does not yet publish Rackspace
findings on `/api/cloud/recommendations`.  Phase 9C.2 will map a
finding onto the canonical envelope.

## Rule IDs

| Rule ID        | Resource type | Trigger                                  | Action                                 |
|----------------|---------------|------------------------------------------|----------------------------------------|
| `RS-VOL-001`   | `VOLUME`      | `metadata.attachment_state == 'DETACHED'`| REVIEW — owner validation required     |
| `RS-SRV-001`   | `SERVER`      | `state ∈ {SHUTOFF, SUSPENDED, PAUSED, ERROR}` | REVIEW — purpose/state validation |
| `RS-LB-001`    | `LOAD_BALANCER`| `state == 'DELETED'` (only signal in scope) | REVIEW — confirm purge need          |

## Evidence requirements

* `RS-VOL-001` requires a `VOLUME` whose canonical `metadata.attachment_state`
  is `DETACHED`.  Defense in depth: if the API state contradicts DETACHED
  (`in-use` / `attaching` / `detaching`) the volume is skipped rather
  than emit a misleading finding.
* `RS-SRV-001` requires a non-`ACTIVE` server.  States outside the
  documented set are **not** a rule trigger.
* `RS-LB-001` requires `state == 'DELETED'`.  In Phase 9C.1 there is
  no reliable signal for an unused-but-present LB, so an empty
  inventory produces zero findings — that is correct, not a gap.

## Rightsizing evidence gate (critical)

ACTIVE servers **never** produce a rightsizing finding in Phase 9C.1.
The metrics capability probe is hard-wired to:

```python
RightsizingEvidenceStatus.DEFERRED
note="RIGHTSIZING_METRICS_DEFERRED"
```

Implications:

* A flavor label alone (e.g. `performance1-15`) does not trigger a
  finding.
* `ACTIVE + large flavor ≠ oversized` — explicitly documented in
  the engine limitations output.
* A future subphase may flip the probe on via
  `RACKSPACE_METRICS_PROBE_ENABLED=1`; until then, ACTIVE servers
  remain silent.

## Severity

| Rule         | Severity default                                | Escalation                         |
|--------------|-------------------------------------------------|------------------------------------|
| `RS-VOL-001` | `MEDIUM`                                        | `HIGH` when `size_gb > 100`        |
| `RS-SRV-001` | `LOW`                                           | none                               |
| `RS-LB-001`  | `LOW`                                           | none                               |

Security / availability severity is **never** confused with FinOps
priority.  A detached volume is a cost candidate, not an outage.

## Confidence

| Rule         | Confidence | Why                                                      |
|--------------|------------|----------------------------------------------------------|
| `RS-VOL-001` | `MEDIUM`   | State evidence is strong, duration is unknown            |
| `RS-SRV-001` | `LOW`      | State is observational but does not prove cost intent    |
| `RS-LB-001`  | `HIGH`     | The only trigger in scope is the authoritative DELETED state |

## Savings semantics

`estimated_monthly_savings` is **always `None`** in Phase 9C.1.
The Rackspace Billing V2 surface provides cycle-level totals only
and does not offer a reliable per-resource mapping.  Therefore:

* `savings_basis = "UNAVAILABLE"` on every finding.
* The engine summary reports `estimated_savings_total = null` when
  pricing is unsupported; it never returns `Decimal("0")` to mean
  unknown.
* Account-level current billing cycle cost (from the canonical
  cost summary) is exposed as **context only** in the limitations
  text — never allocated across resources.

**`unknown savings != zero savings`** is enforced by tests and the
live validator.

## Deterministic IDs

```python
deterministic_finding_id(
    provider="RACKSPACE",
    account_id="<tenant>",
    rule_id="RS-VOL-001",
    region="DFW",
    resource_type="VOLUME",
    resource_id="<volume-id>",
) -> "rs-det-rs-vol-001-<sha256[:12]>"
```

The hash is over `provider|account|rule|region|resource_type|resource_id`.
Same inputs always produce the same id; different inputs never collide
in practice.  The format intentionally does not match the public
recommendation id format so a future rotation of internal ids cannot
cascade into the canonical route.

## Deterministic ordering

Findings are sorted by:

1. `severity` descending
2. `rule_id` ascending
3. `priority_size` (volume `size_gb`) descending — 0 when N/A
4. `region` ascending
5. `resource_id` ascending
6. `finding_id` ascending (deterministic tie-breaker)

The engine summary also records `evaluated_at_iso`.  When omitted by
the caller, the engine sets the sentinel `"1970-01-01T00:00:00+00:00"`
so repeated invocations remain byte-identical.  The live validator
exercises this twice and asserts byte-identical payloads.

## Partial-data behavior

When the canonical resource inventory returns only some services:

* Each rule evaluates only the resources of its own type — a
  missing service simply produces zero findings for its rule.
* An empty resource list produces `status=EMPTY` with zero findings.
* The summary's `limitations` array documents the partial-data
  state explicitly.  The live validator confirms this on real data:
  the canonical resource path returns 50 resources (8 attached
  volumes, 8 detached volumes, 34 active servers, 0 LBs); the engine
  emits 8 findings, all `RS-VOL-001` review candidates.

## Security / read-only guarantees

* The optimization module imports no Rackspace product transport —
  a static assertion in the focused test suite enforces this.
* The live validator wraps `RackspaceHttpClient.get` /
  `post_json` with a verb-spy that captures only `(verb, host)` —
  never a full URL or path, so the Billing Account Number (RAN)
  path parameter never lands in captured output.
* Product write requests = 0 on every validator run.
* No finding payload contains an API key, X-Auth-Token, RAN, or any
  credential material.  The focused test suite asserts this against
  a known-secret fragment list.
* The metrics capability probe is offline by default; when enabled
  by env flag it consults the Identity catalog only (no metrics API
  calls in 9C.1).

## Live validation

```bash
scripts/phase9c1_rackspace_optimization_validation.sh
```

Drives the engine against the live canonical inputs and asserts:

* Engine executes; provider=RACKSPACE; status=SUCCESS|EMPTY.
* `resources_evaluated == len(canonical_resources)`.
* Detached volumes → `RS-VOL-001` (one per detached volume).
* Attached volumes → no `RS-VOL-001` finding.
* ACTIVE servers → zero rightsizing findings.
* Empty load balancer inventory → zero LB findings.
* Deterministic: two consecutive runs produce identical ids and
  byte-identical payloads.
* `estimated_savings_total = null` when pricing is unsupported.
* Product write requests = 0.
* No secret leakage.

Exit codes: `0` success, `1` validation failure, `2` precondition
failure.

## Focused tests

```bash
cd backend && .venv/bin/python -m pytest \
    tests/test_phase9c1_rackspace_optimization_engine.py -v
```

35 tests covering:

* Detached volumes (positive, attached negative, defense-in-depth).
* Volume severity escalation, evidence preservation, age-vs-duration
  limitation language.
* ACTIVE / non-ACTIVE / ERROR server states.
* Empty / active / deleted LB inventory.
* Engine: determinism, partial resources, empty resources,
  provider isolation, summary reconciliation, savings semantics.
* Security: forbidden imports, secret-fragment scan, finding-id
  sanity check.

## Limitations (deferred to Phase 9C.x or later)

* **No rightsizing candidates** — metrics capability is `DEFERRED`.
  ACTIVE servers are deliberately silent.
* **No per-resource price** — `estimated_monthly_savings` is always
  `None`.  Phase 9C.x may integrate per-resource pricing once the
  Billing API surface supports it.
* **No daily / per-region cost allocation** — the canonical cost
  surface does not provide these dimensions.
* **No LLM / natural-language advice** — deterministic titles and
  rationales only.
* **No public route wiring** — `/api/cloud/recommendations` is
  unchanged in Phase 9C.1.

## Phase 9C.2 handoff

Phase 9C.2 will:

1. Map `RackspaceFinding` rows onto the canonical
   `CloudRecommendation` envelope (and/or extend the existing
   `Recommendation` Pydantic model).
2. Wire `/api/cloud/recommendations?provider=RACKSPACE` to the
   engine — currently the route returns
   `UnsupportedProviderForCanonicalRoute` for RACKSPACE.
3. Decide which Rackspace fields ride in `extensions` on the public
   schema (the internal `RackspaceFinding.to_dict()` output is the
   reference shape).
4. Plumb account-level cost context into the envelope without
   allocating it across resources.
5. Re-run the regression suite.

## Important invariants

These three statements are tested explicitly:

* **detached now != detached for N days** — `RS-VOL-001` never
  implies duration; the limitations text names this distinction.
* **ACTIVE + large flavor != oversized** — ACTIVE servers never
  produce a finding without metrics evidence.
* **unknown savings != zero savings** — `estimated_monthly_savings`
  is `None` (not `Decimal("0")`) whenever pricing is unsupported.
