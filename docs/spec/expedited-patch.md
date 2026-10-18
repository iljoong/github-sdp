# Expedited patch deployment

Status: **DRAFT - documentation approved; implementation not approved**.
Baseline: `4cad565`, inspected 2026-10-18.
Related: [index](README.md), [gate orchestration](phase-2-gate-orchestration.md),
[production hardening](phase-3-production-hardening.md).

## 1. Objective and scope

Provide an explicit expedited release path for small, backward-compatible
application image fixes. Eliminate intentional bake delays, not readiness,
business verification, approval, or recovery protection.

Use the existing release entry point and shared regional deployment engine.
Do not copy the deployment implementation into a second workflow or simulate
patch behavior by setting bake durations to zero.

Out of scope: database migrations, external side effects, identity/network or
app-scoped configuration changes, and recovery from an unhealthy baseline.
Traffic rollback does not reverse these changes. Deployment-tooling-only fixes
do not inherently require an application deployment.

This specification is independent of approval of Phases 2/3. Reuse their approved
work where available; do not implicitly approve their proposed architecture.

## 2. Existing implementation ledger

| ID | State | Evidence at baseline | Gap |
| --- | --- | --- | --- |
| PATCH-E01 | EXISTING | [release workflow](../../.github/workflows/release.yml): manual dispatch, tests, one build/digest, sequential regions, production concurrency | Only production/demo profiles exist |
| PATCH-E02 | EXISTING | [regional workflow](../../.github/workflows/region.yml), [snapshot](../../scripts/snapshot.py): immutable baseline upload before mutation | Preserve for patch mode |
| PATCH-E03 | EXISTING | [rollout](../../scripts/rollout.py): preflight, candidate creation, readiness, staged bake, guarded restore | No direct 0% to 100% patch branch |
| PATCH-E04 | EXISTING | [state guards](../../scripts/common.py): template/traffic ownership and active healthy stable checks | Do not weaken for patches |
| PATCH-E05 | EXISTING | [source verification](../../scripts/check_source.py) accepts completed main release workflow runs | Retain source provenance; do not bypass for restore |

Existing success evidence is not proof of sustained service health. The current
patch path is absent; no patch acceptance or operational validation is claimed.

## 3. Proposed requirements

| ID | State | Requirement | Acceptance evidence required |
| --- | --- | --- | --- |
| PATCH-R01 | PROPOSED | Explicit audited execution mode | Input/default/approval tests |
| PATCH-R02 | PROPOSED | Shared guarded deployment preparation | Mutation-order and baseline tests |
| PATCH-R03 | PROPOSED | Direct promotion with bounded verification | Exact traffic-write and endpoint tests |
| PATCH-R04 | PROPOSED | Guarded recovery and region sequencing | Fault-injection and cancellation tests |
| PATCH-R05 | PROPOSED | Evidence and manual restore compatibility | Artifact/provenance and recovery tests |

### PATCH-R01: explicit authorization and inputs

Add an execution mode to the release workflow, separate from the existing
production/demo telemetry profile. Normal release remains the default and retains
its existing behavior. Patch mode requires a nonempty justification or incident
reference, records the actor and commit, and explicitly acknowledges skipped
staged promotion and telemetry-window gates.

Keep main-only dispatch, rerun rejection, protected build/regional environments,
OIDC identities, and `safe-aca-production` concurrency with
`cancel-in-progress: false`. Reject ambiguous combinations of patch mode and
demo-only behavior rather than silently interpreting them.

Patch mode must not implicitly use synthetic traffic as production evidence.
Production alerting remains enabled; smoke-check success is not SLO certification.

### PATCH-R02: preparation remains fail-closed

Run existing tests, build once, and deploy the same immutable digest to every
region. Capture and successfully upload each regional baseline before mutation.
Require the baseline stable revision to remain active and healthy.

Reuse read-only ownership/drift checks and direct baseline health/business
checks. Explicitly omit the normal production preflight telemetry-window gate
for this mode; record that omission rather than claiming the gate passed.
If baseline checks fail, do not create a candidate, change traffic, or enter a
traffic-writing rollback path.

Copy the exact stable-derived template using existing candidate identity,
image, environment, and frozen probe-policy rules. Create the candidate at
zero shared-endpoint traffic while preserving stable=100%.

### PATCH-R03: promote directly after readiness

```text
BASELINE_UPLOADED -> PREFLIGHT_PASSED -> CANDIDATE_CREATED_AT_0
 -> CANDIDATE_READY -> DIRECT_BUSINESS_PASSED
 -> CUTOVER_TO_100 -> PUBLIC_VERIFY -> SUCCEEDED
```

Wait for candidate provisioning/readiness and verify its revision-specific
`/healthz` and `/work` responses and revision identity. Perform a fresh ownership,
traffic, and health check immediately before cutover.

Write explicit stable=0%, candidate=100% weights in one promotion operation.
Never write 10%, 50%, or `latest` routing. Azure may omit zero-weight entries.

Verify both ARM routing and repeated shared-endpoint business responses reaching
the candidate. Define bounded routing convergence, per-request timeouts, and a
required successful-request count before implementation. Treat actual business
contract failures as failures, not convergence observations to retry away.

Do not run bake windows, windowed telemetry queries, ingestion-delay waits, or
demo load for this path. Startup and routing can still take time: do not promise
instant availability or zero request interruption.

### PATCH-R04: failure and cancellation

After mutation intent, use the same guarded regional recovery policy as normal
release. Stop later regions on any failure; retain successful earlier regions.
Leave both revisions active, even after rollback, for investigation.

Pre-cutover errors must not promote traffic. If recovery writes are attempted,
they must pass the existing ownership and stable-health guards. Post-cutover
verification failure triggers a guarded attempt to restore exact baseline
traffic. A successful rollback never turns the patch run into success.

Preserve cleanup after error/cancellation and a separately protected manual
restore path for runner loss, forced termination, or unverified recovery.
Do not bypass drift checks or automatically cancel an active production writer.

### PATCH-R05: evidence and restore compatibility

Record mode, justification, actor, commit, image digest, source run, region,
baseline/candidate identities, all skipped gates, readiness and business checks,
traffic transitions, final routing, and rollback status. Explicitly state
`sustained production SLOs not evaluated` in successful patch summaries.

Preserve baseline artifact naming, snapshot provenance, and the source workflow
identity accepted by manual restore. Validate that restoring a completed patch
run retrieves its pre-patch baseline. Reuse the existing source binding rather
than accepting arbitrary workflow artifacts.

Publish durable per-region success evidence usable by
[revision cleanup](revision-cleanup.md); smoke success and production-gated
success must remain distinguishable.

## 4. Acceptance and validation

Required automated cases:

- Normal production/demo behavior and default mode remain unchanged.
- Missing justification, invalid mode combinations, wrong branch, and reruns fail.
- Failed baseline upload or preflight produces no Azure mutation.
- Candidate is created at zero traffic; checks fail closed before cutover.
- Successful patch writes only direct 100% promotion, with no bake/telemetry/load
  calls or intentional bake sleeps.
- Public requests returning an old revision never authorize success.
- Timeout, API error, drift, or contract failure cannot become success-shaped
  fallback evidence; recovery errors are surfaced separately.
- KR/SEA failure prevents later regions; successful earlier regions are preserved.
- Patch baseline artifacts pass source verification and guarded manual restore.
- Normal release, patch, restore, and cleanup share serialization.

Controlled nonproduction validation requires separate approval. Exercise one
successful cutover and one failed post-cutover check with verified restoration.
Record real run/artifact IDs and observed timing without claiming an SLA.

## 5. Decisions required before implementation

- Approver and eligibility policy for expedited changes.
- Exact mode/profile input contract and operator acknowledgment.
- Readiness, convergence, request-count, call-timeout, and total job budgets,
  with time reserved for recovery and evidence.
- Whether existing environment approval arrangements meet incident response
  needs; do not silently remove approval.

Implementation ledger updates must follow the [index vocabulary](README.md).
Automated tests and operational validation for these requirements: **not run**.
Update both operator guides when behavior is implemented, not merely specified.
