# Phase 1: reliable demo load and failure accounting

Status: **IMPLEMENTED - automated validation complete; operational acceptance not run**.
Baseline: `f5f22a7`, inspected 2026-10-18.
Implementation approval: 2026-10-18, including 30-second saturation deadline,
20-second drain budget, and demo-only curl subprocess transport.
Prerequisites and status rules: [specification index](README.md).

## 1. Objective and non-goals

Maintain approximately 10 shared-endpoint demo requests per second while
monitoring blocks, tolerate short client-side congestion without an immediate
rollback, and preserve useful failure evidence.

Do not lower sample or SLO thresholds, send demo traffic directly to candidate
revision endpoints to manufacture samples, generate production synthetic load,
or expand the queue without bound.

## 2. Existing implementation: reuse, do not rebuild

| ID | State | Baseline evidence | Remaining work |
| --- | --- | --- | --- |
| P1-E01 | EXISTING | `f5f22a7`; `generate_demo_traffic` and `demo_traffic` in [rollout.py](../../scripts/rollout.py) | Preserve independent scheduling and shared endpoint routing |
| P1-E02 | EXISTING | `test_demo_scheduler_targets_600_shared_endpoint_requests_per_minute` | Keep approximately 600 submissions/minute under healthy capacity |
| P1-E03 | EXISTING | `test_demo_load_continues_during_blocking_monitoring_and_stops_on_exit` | Preserve independence and stop-on-exit |
| P1-E04 | EXISTING | `test_production_never_starts_synthetic_load` | Keep production path free of demo workers |
| P1-E05 | PARTIAL | `demo-load-<weight>.json` records aggregate counts/RPS; tests cover single request error and monitoring-error shutdown | Missing per-interval pressure, latency, and complete multi-error accounting |
| P1-E06 | PARTIAL | 16 request workers and 32 outstanding slots; `test_demo_load_queue_is_bounded_and_saturation_fails` | Replace immediate failure test with backpressure/deadline tests |

The index records historical suite results. This table describes the historical baseline, not the implementation below.

## 3. Approved behavior

### P1-R01: bounded backpressure

Keep the current 16 workers, 32 outstanding slots, and target 10 RPS initially.
When slots are full:

1. Stop submitting; do not enqueue, drop an already submitted request, or create
   extra workers.
2. Wait interruptibly for completion while monitoring remains independent.
3. Resume paced submission after capacity becomes available; do not replay the
   entire missed schedule as a burst.
4. Track both continuous saturation duration and cumulative saturation time.
5. Fail with a distinct load-capacity reason if continuous saturation reaches
   the approved deadline: **30 seconds**.

Capacity recovery resets the continuous timer, not the cumulative statistic.
Repeated short saturation must remain visible in interval throughput evidence.
The existing server-side sample gate remains authoritative; successful load
generation does not by itself authorize promotion.

### P1-R02: request outcomes and latency

Assign a client request identifier and record monotonic submission, actual start,
and completion times. Aggregate:

- Submitted, succeeded, failed, cancelled-before-start, and outstanding counts.
- Worker queue delay and end-to-end request duration, separate from app log p95.
- Target/achieved request rate per complete 60-second interval and whole stage.
- Current/high-water outstanding count, saturation episodes, and total duration.
- Network/timeout, HTTP, response parsing, and business-contract failure categories.

Keep memory bounded through interval aggregation or bounded histograms; do not
retain every future or response body for the entire stage.
At termination, submitted requests must reconcile to terminal outcomes plus
explicit unresolved requests. Do not count a failed request as a successful
completion or silently lose requests after the first exception.

P1 does not require changing the application's business log schema. If client
and server request-ID correlation is added later, coordinate it with P3-R01.

### P1-R03: primary and secondary failures

Preserve the first controller/gate failure as the deployment's primary reason.
Record subsequent generator, drain, or evidence-write errors as secondary
failures. If the generator fails first, surface that as the primary load failure.

Every submitted future must be inspected or explicitly marked unresolved.
Iteration order of the pending set must not determine the reported root cause.
Do not suppress errors merely to permit promotion.

### P1-R04: bounded termination

On normal exit, controller failure, SIGINT, or SIGTERM:

1. Stop scheduling immediately.
2. Cancel tasks that have not started.
3. Drain running requests within an approved shutdown budget.
4. Persist reconciled statistics and both primary/secondary errors.
5. Return control to the existing guarded rollback path.

Approved drain budget: **20 seconds**. Request operations must have a demonstrable
whole-operation bound compatible with this budget. `urlopen(timeout=10)` and
`Future.cancel()` alone do not establish a total bound for a running request.
Do not claim a thread was terminated when only its future was cancelled.
The approved demo transport is curl >= 8.4.0: a 10-second whole-transfer deadline,
a 12-second process watchdog, and at most 1 additional second to reap after kill.
Response streaming is limited to 64 KiB. The read-only baseline preflight checks
curl availability/version. Readiness/control probes retain their existing transport.
Hard process termination remains an explicitly unsupported in-process cleanup
case and must use the protected recovery workflow.

## 4. State machine

```text
STARTING -> RUNNING -> BACKPRESSURED -> RUNNING
                |           |
                |           +-> FAILED (continuous deadline)
                +-> FAILED (request/contract failure)
RUNNING/BACKPRESSURED/FAILED -> STOPPING -> DRAINING -> STOPPED
```

Generator state is distinct from deployment state: `STOPPED` can accompany a
failed gate. A healthy generator must not write a misleading release-success flag.

## 5. Evidence contract

Extend the existing `evidence/<region>/demo-load-<weight>.json`; do not create
a competing output for the same stage. Retain existing field meanings and add
an explicit schema version if semantics change.

Required additions: `region`, `profile`, implementation commit, generator state,
primary/secondary failures, interval summaries, latency/queue summaries,
saturation timing, cancelled/unresolved counts, and shutdown outcome.
Include UTC times for correlation and monotonic elapsed durations for deadlines.
Never include tokens, headers containing credentials, or response bodies.

Console summaries should report target/actual RPS and queue pressure periodically,
not just at stage exit. Evidence-write failure must be visible and must not
replace the original runtime failure.

## 6. Acceptance tests

The following acceptance scenarios map to automated tests in section 7.

| ID | Test scenario | Required result |
| --- | --- | --- |
| P1-T01 | Queue fills briefly, then requests complete | Pause/resume, no rollback, high-water <= 32 |
| P1-T02 | Queue remains full through 30-second deadline | Typed capacity failure at boundary, no unbounded queue |
| P1-T03 | Repeated pressure episodes | Separate continuous/cumulative counters; no burst catch-up |
| P1-T04 | Monitoring blocks while load runs | Rate remains near target under healthy request latency |
| P1-T05 | Multiple request failures plus controller failure | Original controller failure preserved; all outcomes reconciled |
| P1-T06 | Stop with queued/running requests | No new submissions; cancellation/drain within approved budget |
| P1-T07 | Slow connect/read/body and trickle response | Whole-operation deadline demonstrated, not assumed |
| P1-T08 | Production profile | No scheduler, synthetic requests, or demo evidence |
| P1-T09 | Healthy virtual minute and short real concurrency run | Approximately 600 submissions; no leaked workers |

Operational acceptance: controlled demo in a nonproduction region, including
injected temporary network slowdown. Attach load evidence and gate artifacts.
A successful release alone does not prove backpressure or failure accounting.

## 7. Implementation ledger and completion

| Requirement | State | Implementation/test evidence | Next action |
| --- | --- | --- | --- |
| P1-R01 | IMPLEMENTED | `demo_load.Generator.run/pressure`; P1-T01/T02/T03/T04/T09 tests below pass | Controlled nonproduction slowdown run |
| P1-R02 | IMPLEMENTED | `Generator.collect/report/finish_evidence`; schema v2, fixed histograms, reconciled outcomes; P1-T05/T06/T09 pass | Attach operational stage evidence |
| P1-R03 | IMPLEMENTED | `Generator.failure/check`, `rollout.demo_traffic`; P1-T05 and evidence-error tests pass | Attach operational gate/failure artifacts |
| P1-R04 | IMPLEMENTED | `demo_load.request`, `Generator.run` drain, signal cleanup; P1-T06/T07/T08 pass | Controlled nonproduction shutdown acceptance |

Implementation commit: the commit introducing
[`scripts/demo_load.py`](../../scripts/demo_load.py) and this ledger update,
based on `f5f22a7`. Resolve its exact SHA with
`git log -1 --format=%H --diff-filter=A -- scripts/demo_load.py`.
Runtime evidence records `GITHUB_SHA` (or local Git HEAD); local runs with
uncommitted changes do not identify the complete implementation.

Code:
[demo_load.py](../../scripts/demo_load.py),
[rollout.py](../../scripts/rollout.py).
Tests:
[test_demo_load.py](../../tests/test_demo_load.py),
[test_safety.py](../../tests/test_safety.py).

Automated acceptance mapping (test method names):

| Scenario | Automated evidence |
| --- | --- |
| P1-T01/T03 | `test_repeated_short_pressure_resumes_without_catchup_bursts` |
| P1-T02 | `test_capacity_failure_occurs_at_deadline_and_unresolved_is_explicit`, `test_demo_load_queue_is_bounded_and_capacity_deadline_fails` |
| P1-T04 | `test_demo_load_continues_during_blocking_monitoring_and_stops_on_exit` |
| P1-T05 | `test_controller_primary_survives_multiple_worker_and_evidence_errors`, `test_generator_failure_precedes_later_controller_error`, `test_evidence_failure_alone_prevents_success` |
| P1-T06 | `test_stop_cancels_queued_tasks_and_drains_running_requests`, `test_sigint_and_sigterm_stop_drain_and_preserve_failure` |
| P1-T07 | `test_real_curl_slow_headers_body_and_trickle_obey_total_deadline`, `test_real_curl_stalled_tls_handshake_obeys_total_deadline`, `test_process_deadline_kills_stalled_transport_including_dns_or_connect`, `test_process_reap_deadline_is_not_replaced_by_unbounded_context_exit` |
| P1-T08 | `test_production_never_starts_synthetic_load`, `test_production_does_not_create_generator_or_evidence` |
| P1-T09 | `test_demo_scheduler_targets_600_shared_endpoint_requests_per_minute`, `test_real_scheduler_and_curl_stage_persist_reconciled_evidence` |

Validation on 2026-10-18:

- `python -m unittest discover -s tests -q`: **67 tests passed**.
- `python -m compileall -q app scripts tests`: passed.
- `git -c core.whitespace=cr-at-eol diff --check`: passed (existing CRLF files).
- Pylance syntax checks: no syntax errors in the new generator and test module.
- Default 30-second capacity and 20-second drain boundaries use virtual time.
  Real curl tests use a shortened 0.25-second total-transfer limit and assert
  completion within 0.9 seconds for stalled TLS, headers, body, and trickle traffic.
  The 12-second watchdog/1-second reap behavior is fault-injected; default curl
  deadline flags are asserted separately.
- Operational run/artifact: **not run**. No Azure release or infrastructure mutation.

Remaining acceptance: explicitly approved nonproduction regional run with injected
temporary network slowdown; attach demo-load and gate artifacts before marking
requirements VERIFIED. Host loss/SIGKILL and pathological OS-level inability to
reap a killed process cannot provide in-process cleanup guarantees; failures and
unresolved requests are explicit, not successful completions or terminated threads.
The existing protected recovery workflow remains required in those cases.

Open implementation decisions: none.
Out of scope: changing production SLOs, adding distributed orchestration, or
automatically deleting old candidate revisions.
