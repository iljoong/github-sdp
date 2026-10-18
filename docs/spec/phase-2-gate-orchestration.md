# Phase 2: deadline-aware gates, monitoring, and recovery

Status: **DRAFT - not approved for implementation**.
Baseline: `f5f22a7`, inspected 2026-10-18.
Dependency: reuse Phase 1 lifecycle/error contracts; see [index](README.md).

## 1. Objective

Keep health/control monitoring responsive during Azure and telemetry delays,
make promotion decisions from explicit evidence, and preserve reliable recovery.
Do not weaken quality gates or introduce concurrent Azure mutations.

## 2. Existing implementation ledger

| ID | State | Evidence at baseline | Preserve or extend |
| --- | --- | --- | --- |
| P2-E01 | EXISTING | `6050df6`; `IncompleteTelemetry` in [metrics.py](../../scripts/metrics.py) and retry logic in [bake](../../scripts/rollout.py) | Fixed closed windows; 30-second polls; additional 300 seconds after initial 120-second lag |
| P2-E02 | EXISTING | `test_bake_retries_same_closed_window_with_health_checks_and_bounded_deadline` | Recovery, expiry, same query arguments, health/drift failure, query error |
| P2-E03 | EXISTING | `test_incomplete_telemetry_never_masks_available_slo_failures` | Available absolute/relative failures must not be retried away |
| P2-E04 | EXISTING | `test_durable_baseline_upload_precedes_rollout` and [region workflow](../../.github/workflows/region.yml) | Upload baseline before candidate creation |
| P2-E05 | EXISTING | `test_restore_only_writes_original_explicit_weights`, `test_restore_does_not_mutate_on_drift` | Guarded original traffic restore and failed release result |
| P2-E06 | PARTIAL | `request`, `wait_ready`, `bake`, `restore` | Timeouts exist, but nested blocking operations can exceed nominal deadlines |
| P2-E07 | PARTIAL | Health and control probes exist in `bake` | All still share the blocking query/controller loop |
| P2-E08 | PARTIAL | Explicit ARM weight checks and revision endpoint probes | Final public candidate identity verification is absent |

## 3. Architecture and state

Prefer a small set of functions with explicit ownership over a generic workflow
framework. Separate these responsibilities:

1. **Controller:** sole owner of state transitions and Azure mutations.
2. **Probe scheduler:** bounded HTTP control probes using cached revision FQDNs.
3. **Telemetry evaluator:** queries immutable event windows with a deadline.
4. **Azure read/guard path:** bounded reads with freshness reported to controller.
5. **Demo load:** existing Phase 1 component; disabled for production.

No worker may independently promote, roll back, or refresh shared Azure CLI
authentication concurrently without coordination. The existing `_next_login`
and shared CLI account context in [common.py](../../scripts/common.py) need
serialized refresh/access if concurrency is introduced.

```text
BASELINE_CAPTURED -> BASELINE_UPLOADED -> PREFLIGHT_PASSED
 -> CANDIDATE_CREATED -> CANDIDATE_READY
 -> ROUTING_CONVERGING -> OBSERVING -> WAITING_FOR_TELEMETRY
 -> STAGE_PASSED -> next stage -> FINAL_VERIFY -> SUCCEEDED

Any post-intent failure -> STOP_WORKERS -> GUARDED_RESTORE
 -> RESTORE_VERIFIED or RESTORE_UNVERIFIED; deployment remains FAILED
```

Preflight failures remain read-only and must not enter traffic-writing recovery.

## 4. Proposed requirements

### P2-R01: responsive monitoring

Preserve the nominal five-second control schedule independently of telemetry
queries and slow Azure commands. Allow one in-flight probe per revision/endpoint
or another explicitly bounded policy; do not accumulate overdue probes.
Record scheduled, started, completed, and missed probe counts.

Keep periodic candidate health and drift checks, initially at the existing
60-second cadence. Record the age of the last successful guard. Before promotion,
perform a fresh guard and candidate-health check; stale cached success must not
authorize a write.

A slow Azure read must not block HTTP probing. It must also not result in
unbounded concurrent CLI commands. Probe/worker failures propagate to controller.

### P2-R02: explicit error categories

Classify failures through exception types or equally explicit structured results,
not message substring matching:

| Category | Retry policy | Promotion |
| --- | --- | --- |
| Incomplete telemetry | Same closed window until deadline | Block |
| Routing not yet converged | Approved convergence budget | Block |
| Observed SLO/contract failure | No retry to erase observation | Block; guarded recovery |
| Ownership/template/traffic drift | No blind mutation | Block; investigate |
| Malformed/ambiguous telemetry | Fail closed | Block |
| API authorization/configuration failure | No generic retry | Block |
| Transient API transport/throttling | Only classified, bounded, respecting server hints | Block until verified |

Replace the broad exception loops in routing verification and restore with this
policy. Distinguish a probe transport incident from an observed application
contract failure in evidence even if both stop the release.

### P2-R03: real deadlines

Use UTC only for event-window boundaries and evidence correlation. Use a
monotonic clock for elapsed budgets.

Preserve nominal existing budgets unless approved otherwise:

- Candidate readiness: 600 seconds.
- Initial ingestion lag: 120 seconds.
- Additional incomplete-telemetry allowance: 300 seconds.
- Telemetry retry interval: 30 seconds.
- Restore route verification: 120 seconds.

Every nested API/probe call must receive a timeout no greater than the remaining
budget. Check the deadline before and after a blocking call. Do not accept an
otherwise successful late result beyond the approved decision deadline.
Do not restart a window's allowance on each retry.

Account for the final telemetry query and worker shutdown when setting workflow
timeouts; leave explicit time for rollback and evidence upload. Reconcile
production's three one-hour stages with the 240-minute regional job limit.

### P2-R04: gate freshness and final verification

For each stage preserve fixed, nonoverlapping windows and configured bake time.
Record `last_evaluated_event_end`, decision time, and signal age.

The current scheme necessarily observes historical logs. Initial proposal:
maximum business-signal age at decision is **420 seconds**, matching lag plus
retry allowance, with fresh direct business probes and guard checks required.
This is an explicit residual-risk bound, not a claim that all live traffic
through the promotion instant was evaluated.

At 100%, verify the shared endpoint with five independent business requests that
all report the exact candidate revision. Verify ARM candidate=100, candidate
readiness, ownership, and unchanged app invariants before declaring success.
At mixed weights, validate that shared-endpoint responses identify only the
authorized stable/candidate set; do not demand an exact 90/10 split from a small
sample or make probabilistic routing a brittle deterministic gate.

Promotion must be blocked on worker errors, stale guards, unfinished gate
evaluation, or incomplete required evidence.

### P2-R05: recovery and evidence

Stop and drain workers before restoring traffic. Preserve the first failure.
Record recovery attempts and final status separately; a successful rollback
must not convert the deployment to success.

Keep the existing cleanup step as a safety net, but verify rollback before
writing traffic again if automatic rollback already succeeded. A prior success
file alone is insufficient: inspect current guarded routing.
If state no longer belongs to this release, stop without overwriting it.

Persist structured failed gate outcomes as well as successful/waiting records.
Currently a non-incomplete gate failure can leave rows without an explicit
failed gate status. Include category, phase, window, attempt, deadline, last
successful probe/guard times, and primary/secondary failures.

Evidence should be checkpointed at state transitions. Do not rely solely on the
normal end-of-stage path or imply that local files survive runner loss.

## 5. Acceptance plan

Proposed tests; existing P2-E tests must remain:

| ID | Fault/scenario | Acceptance |
| --- | --- | --- |
| P2-T01 | Telemetry query blocks for 180 seconds | Probe scheduler remains active; no unbounded query concurrency |
| P2-T02 | Guard or candidate health fails during query | Failure reaches controller; no subsequent promotion |
| P2-T03 | Result arrives immediately before/after deadline | Before accepted only if valid; after rejected |
| P2-T04 | UTC clock jumps | Event windows remain explicit; monotonic budget does not reset |
| P2-T05 | Traffic convergence vs ownership conflict | Only convergence retried; drift never overwritten |
| P2-T06 | ARM says 100%, public endpoint still serves stable | No final success until shared candidate verification passes |
| P2-T07 | Gate fails while load shutdown also fails | Original gate reason survives; both recorded |
| P2-T08 | SIGTERM during copy, bake, query, or drain | Best-effort bounded recovery; no deploy-success result |
| P2-T09 | Automatic restore already succeeded | Cleanup verifies current state without unnecessary write |
| P2-T10 | Runner disappears after mutation | Protected manual recovery demonstrated from uploaded baseline |
| P2-T11 | Token refresh becomes due with concurrent reads | One coordinated refresh; no account-context race |

Use virtual clocks for boundary tests, real threads for scheduler isolation, and
an approved nonproduction run for public routing and restore verification.

## 6. Requirement completion ledger

| Requirement | State | Implementation/test evidence | Remaining decision/work |
| --- | --- | --- | --- |
| P2-R01 | PARTIAL | P2-E07; synchronous checks exist | Introduce independent bounded monitoring |
| P2-R02 | PARTIAL | P2-E01 typed incomplete telemetry | Classify convergence, drift, and API failures |
| P2-R03 | PARTIAL | P2-E06 nominal timeout values | Enforce remaining budgets across calls |
| P2-R04 | PROPOSED | No final shared candidate verification | Approve freshness and final-probe policy |
| P2-R05 | PARTIAL | P2-E04/P2-E05 | Preserve root failures, structure evidence, verify-before-repeat cleanup |

Each row must be updated with commit, exact tests/results, operational evidence
or `not run`, and verification date before declaring completion.
Open decisions: signal-age limit, probe freshness budget, transport deadline
strategy inherited from Phase 1, and cancellation/recovery workflow budget.
