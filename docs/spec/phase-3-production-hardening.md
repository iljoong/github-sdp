# Phase 3: production quality gates and mutation ownership

Status: **DRAFT - not approved; production policy decisions required**.
Baseline: `f5f22a7`, inspected 2026-10-18.
Dependencies: [Phase 2](phase-2-gate-orchestration.md) verified and a named service
owner approves workload-specific production policy.

## 1. Scope and limitations

Harden the signal used for promotion and the ownership of Azure mutations.
This is not certification of the educational application, an exhaustive security
assessment, or a distributed transaction across regions.

Do not invent availability, sample size, error-budget, or latency requirements
for an unknown workload. Demo success cannot approve production readiness.

## 2. Verified starting point

| ID | State | Existing evidence | Limitation |
| --- | --- | --- | --- |
| P3-E01 | EXISTING | [metrics.py](../../scripts/metrics.py); `test_production_rejects_synthetic_only` | Production organic business streams are separate from demo/control |
| P3-E02 | EXISTING | `test_kql_has_run_revision_time_and_correct_ingestion_bounds` | Explicit app/revision/window and synthetic run filters, request/revision deduplication |
| P3-E03 | EXISTING | Production profile: 3,600-second bake, 300-second windows, 100 business samples, 30 control samples | Example values, not a justified confidence model |
| P3-E04 | EXISTING | `test_100_percent_requires_reference_and_live_stable_controls` | Frozen pre-cutover stable business reference; live stable controls continue |
| P3-E05 | EXISTING | `test_guard_rejects_another_releases_traffic_or_template`; [common.py](../../scripts/common.py) | Guard/read and mutation/write are separate operations |
| P3-E06 | EXISTING | [release](../../.github/workflows/release.yml) and [restore](../../.github/workflows/restore.yml) share `safe-aca-production` concurrency group | Does not coordinate Portal, CLI, or other repositories |
| P3-E07 | EXISTING | [app/server.py](../../app/server.py) logs business outcome and processing duration | Educational header classification; no trusted production request denominator |

Baseline gate values to preserve until policy approval: absolute errors <= 1%,
p95 <= 500 ms, candidate error regression <= 0.5 percentage points, candidate
p95 <= stable p95 * 1.5 + 50 ms. Do not silently redefine these during refactoring.

## 3. Proposed requirements

### P3-R01: telemetry validity, completeness, and freshness

Extend quality queries to report valid and invalid business records separately.
Records that declare business telemetry but lack required fields must not simply
disappear without a quality signal.

Specify and test:

- Required schema and version, valid status/duration/outcome domains.
- Duplicate/conflicting request-ID treatment and source/revision consistency.
- Ingestion lag, event-time skew, malformed counts, and missing-source indicators.
- A service-approved completeness rule based on a trusted request denominator
  or equivalent provider-supported evidence.

Passing a minimum sample count does not prove completeness or that error logs
are not delayed. If completeness cannot be established, report that limitation
and block production certification rather than labeling the data complete.

For the demo, P1 client request identifiers/counters can help reconcile requests;
do not assume that direct equality is possible without defining failed-before-app
requests, response loss, duplicate logs, and ingestion deadlines.

Production source classification must come from a trusted gateway/instrumentation
path, not arbitrary public request headers. Reuse app instrumentation only where
its trust and completeness assumptions are approved.

### P3-R02: statistical and workload policy

Define per-service policies for:

1. Minimum duration and usable samples at each traffic weight.
2. Absolute availability, business success, and latency objectives.
3. Relative regression and error-budget/burn-rate limits.
4. Tail latency appropriate to the workload, including end-to-end ingress latency.
5. Dependency failures, timeouts, queue age/depth, resource saturation, and named
   business KPIs where the service depends on them.
6. HTTP failure classification, including explicit treatment of 4xx, 429, 5xx,
   cancellations, and requests not reaching the application.

Production's current 100 samples and demo's 20 samples are smoke-test minima, not
a statistical demonstration of 99% success. Select a confidence or other approved
decision method, and document why the sample/duration policy is adequate.
Do not add metrics merely to populate a checklist; each must have an owner,
instrumentation, a threshold, a failure response, and acceptance tests.

Keep demo signals separate. Never substitute synthetic traffic for missing
organic traffic to force a production pass.

### P3-R03: baseline and reference quality

Reuse production preflight, rather than implementing a second baseline engine.
Apply approved bounded incomplete-telemetry retry semantics to the same closed
preflight window while preserving its read-only/no-rollback-before-mutation rule.

Record baseline interval, sample quality, workload conditions, and policy version.
Assess whether one prior five-minute window is representative; select the
reference period with the service owner.

At 100% retain an explicitly identified pre-cutover stable reference and continue
live stable controls. Detect common-mode degradation using independent absolute
service/platform signals; candidate-vs-stable comparison alone is insufficient.

### P3-R04: mutation concurrency and ownership

The existing guard/write sequence is not an atomic compare-and-swap.
First investigate the exact supported ACA API and mutation shape for conditional
updates. Do not assume ETags, `If-Match`, or a distributed lease are supported
or honored for the current traffic/copy commands.

Required research evidence:

- API version, provider documentation, and observed support.
- Successful conditional write, rejected stale write, and behavior on timeout.
- Whether updates affect only intended fields and preserve template/configuration.

If supported, use conditional mutations and re-read after ambiguous outcomes.
If unsupported, define an enforceable single-writer model covering all production
writers. An application-level lease is insufficient if Portal or external
automation can bypass it.

At minimum, minimize the guard/write gap, reverify afterward, stop on conflicts,
and document remaining race risk. Do not claim atomicity if no provider-enforced
condition is available. Preserve GitHub serialization and protected approvals.

### P3-R05: durable recovery and operational ownership

Keep the uploaded baseline as recovery input and the failed deployment result
even after successful rollback. Add machine-readable release summary containing
per-region deployed/stable revisions, final traffic, gate decision, rollback
verification, evidence references, and required operator action.

Define alerting/escalation for `RESTORE_UNVERIFIED`, runner loss, expired artifacts,
and stale ownership. A local mutation-intent file cannot survive runner loss;
evaluate durable mutation checkpoints or an external recovery controller only
if production recovery objectives require them.

Define separate procedures for:

- Partial multi-region success: retain healthy earlier regions by default.
- Rollback traffic when the original stable revision is unhealthy.
- Database/schema, secrets, and external side effects that traffic restore cannot undo.
- Artifact retention/access, incident retention, and recovery beyond retention.

Do not automatically bypass drift protection to meet a recovery target. Such
conflicts require a protected incident decision with evidence.

## 4. Evidence and versioning

All production gate artifacts should identify:

- Release run, commit, region, app, stable/candidate revision, immutable image.
- Policy/schema version and exact thresholds used.
- Event windows, query/decision times, sample counts, freshness/completeness state.
- Absolute and relative metric results; malformed and excluded record counts.
- Mutation ownership evidence and any conditional-write/lease result.
- Final routing and recovery status with primary/secondary errors.

Retain raw aggregate evidence sufficient to reproduce the decision without
retaining credentials or unnecessarily retaining request payloads.
Version evidence compatibly with protected restore; do not invalidate historical
baseline artifacts without an explicit compatibility strategy.

## 5. Acceptance and readiness matrix

These are proposed acceptance cases:

| ID | Scenario | Required outcome |
| --- | --- | --- |
| P3-T01 | Enough valid samples but malformed business records | Explicit telemetry-quality decision; no silent denominator reduction |
| P3-T02 | Healthy logs arrive before delayed failure logs | Completeness/freshness policy prevents unjustified production success |
| P3-T03 | Requests fail at ingress before app logging | Platform signal includes failure; app logs alone cannot pass |
| P3-T04 | Synthetic traffic only or forged source attribution | Cannot satisfy production organic acceptance |
| P3-T05 | Candidate and stable degrade together | Absolute/independent gate detects common-mode failure |
| P3-T06 | Low volume, missing baseline, or unrepresentative reference | Explicit blocked/failed decision, not a fabricated pass |
| P3-T07 | External writer changes state between guard/write | Conditional conflict rejected, or documented single-writer restriction demonstrated |
| P3-T08 | Mutation times out after Azure accepted it | Re-read ownership/state; no blind duplicate mutation |
| P3-T09 | Runner disappears after promotion | Recovery from durable evidence within approved recovery objectives |
| P3-T10 | Older protected restore artifact | Compatible restore or explicit unsupported-version rejection before mutation |
| P3-T11 | One region fails after earlier success | Accurate partial-release summary; no unintended global rollback |

Production readiness requires approved thresholds and policy, an operational
recovery exercise, and evidence of ownership enforcement. Unit tests alone do
not close these requirements.

## 6. Requirement completion ledger

| Requirement | State | Evidence at baseline | Remaining decision/work |
| --- | --- | --- | --- |
| P3-R01 | PARTIAL | P3-E01/P3-E02/P3-E07 | Trusted denominator, invalid-record reporting, completeness rule |
| P3-R02 | PROPOSED | Example thresholds only | Service owner and approved SLO/statistical policy |
| P3-R03 | PARTIAL | Read-only baseline and frozen 100% reference already exist | Retry/freshness and representative reference policy |
| P3-R04 | PARTIAL | Existing guards and GitHub lock | Provider capability research and enforced writer model |
| P3-R05 | PARTIAL | Baseline upload, protected restore, regional rollback | Durable summary, alerts, recovery objectives and exercise |

Update each row with implementation commit, code symbols, automated results,
controlled operational evidence, remaining limitations, and verification date.
If policy or API research blocks a requirement, mark it `BLOCKED` and name the
owner/decision; do not silently mark production ready.

Approval decisions: service owner, SLO/error-budget policy, traffic confidence
model, trusted instrumentation, provider concurrency support, single-writer
enforcement, recovery-time/data objectives, and evidence retention.
