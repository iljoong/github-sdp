# Zero-downtime demo app: readiness/liveness, SIGTERM drain, long task

Status: **DRAFT - design written 2026-11-19; implementation not approved**.
Baseline: current `main` working tree, inspected 2026-11-19.
Related: [index](README.md), [guarded revision cleanup](revision-cleanup.md),
[operator manual](../../manual/README.md).

## 1. Intent

The existing demo app (`app/server.py`) proves canary routing and SLO gates, but
it cannot show what an audience actually means by "zero downtime": a web page
that keeps calling the backend every second while a deployment happens, with a
version number that changes **without a single error**, and a long-running
request that **survives the old replica being shut down**.

This work makes the demo app realistic enough to show:

1. A verification web page (SPA style) polling the backend every 1 second; the
   displayed version changes v1 → v2 through 10% / 50% / 100% with zero errors.
2. A "start long task" button that starts a ~2-minute request on the **old**
   revision; the old revision is then deactivated (SIGTERM) and the task still
   completes because the app drains gracefully and the grace period is long
   enough.
3. Proper readiness vs liveness semantics and an explicit
   `terminationGracePeriodSeconds` instead of the 30-second default.

The implementation must be **well documented** so the owner can modify timings,
endpoints and screen layout later without reverse-engineering the code.

## 2. Goals, non-goals, anti-goals

Goals

- G1. Verification page matching the approved layout (section 5).
- G2. App endpoints for version, readiness, liveness, status and long task.
- G3. SIGTERM drain: stop being ready, reject new long tasks, finish in-flight
  work, exit before the grace deadline.
- G4. Candidate revisions get an explicit `terminationGracePeriodSeconds`
  (default 180 s) from a single policy file, guarded like `candidate_probes`.
- G5. A separate, manually dispatched **demo retire step** that deactivates the
  old revision after a successful 100% cutover, producing the SIGTERM.
- G6. Documentation: code-level module docs, this spec, a new operator manual
  chapter, and updates to INSTRUCTION guides.

Non-goals

- Production-grade job processing (queues, durable job state, resumable work).
- A JavaScript framework or front-end build step.
- Changing the canary gates, weights, bake times, or region order.
- The "grace 30 s fails vs 180 s succeeds" comparison run (kept possible by
  configuration, not built as a feature).
- Implementing the general [revision cleanup](revision-cleanup.md) spec.

Anti-goals (would be a failure even if it technically works)

- Long-task or status traffic leaking into business SLO telemetry and changing
  gate outcomes.
- Demo retire running automatically as part of `release.yml`.
- Breaking `restore` for releases that were **not** retired.
- Any error shown on the 1-second polling strip during a normal demo release.

## 3. Constraints and verified platform facts

| Fact | Source | Consequence |
| --- | --- | --- |
| ACA sends SIGTERM on scale-in, revision deactivation, app deletion; SIGKILL after grace | [ACA lifecycle](https://learn.microsoft.com/azure/container-apps/application-lifecycle-management) | Traffic shift to 0% alone never produces SIGTERM; deactivation is required |
| Default grace 30 s; `template.terminationGracePeriodSeconds` max 600 s | ARM/PowerShell model docs | 2-minute task needs ≥ ~150 s; use 180 s |
| Standard ingress HTTP request timeout 240 s | ACA ingress docs | Synchronous 120 s request is allowed |
| Pipeline keeps old revision active with `minReplicas: 1` | `rollout.py` `success.json` `old_revision_active: True` | SIGTERM must come from a new step |
| `restore` requires stable revision ready | `common.guarded_state` "Stable unavailable" | Retired release cannot be auto-restored (section 6.4) |
| Health gate counts only `kind == "business"` logs from `/work` | `metrics.query` | New endpoints must not emit `kind: business` |
| Dockerfile uses exec-form `CMD ["python", "server.py"]` | `app/Dockerfile` | Python is PID 1 and receives SIGTERM directly |
| ACA injects `CONTAINER_APP_NAME`, `CONTAINER_APP_REVISION`, `CONTAINER_APP_ENV_DNS_SUFFIX` | ACA runtime env | Page can derive revision FQDNs `<revision>.<dns suffix>` |

Repository constraints: Python standard library only, single container `web`,
port 8080, existing tests must keep passing, existing snapshots (without the new
fields) must keep their restore semantics.

## 4. Demo scenario (timeline)

```text
T0   Presenter opens /verify (served by the app) — strip is all blue (v1)
T1   gh workflow run release.yml -f profile=demo
     strip: blue → mixed (10%) → mixed (50%) → green (100%), errors = 0
T2   Region succeeds (success.json). v1 card: traffic 0%, SERVING, ready ✓
T3   Presenter clicks "긴 작업 시작" → POST to v1 revision FQDN
     v1 card: 1 in-flight task, elapsed counter
T4   Presenter runs demo-retire.yml for that region (protected approval)
     → az containerapp revision deactivate v1 → SIGTERM to v1 replica
T5   v1 card: DRAINING, ready ✗, grace remaining counting down from 180
T6   Long task returns 200 after ~120 s total → card shows "완료"
     v1 process exits; card becomes STOPPED (status unreachable after exit)
T7   strip stayed green the whole time, errors = 0
```

Timing rules (decision D8):

- Retire runs only **after the whole release run has completed**. It shares the
  `safe-aca-production` concurrency group, and `check_source.py` requires a
  completed source run; neither safety rule is relaxed. The release always
  runs KR → SEA → USW; use `profile=demo` (short bake) so the run completes
  quickly. No region selector is added.
- Step T3 (button) is done after the release completes and just before T4. The
  120 s task easily covers the retire approval plus deactivation.
- Retire must only target a region whose `success.json` exists.

Preparation (decision D9): v1 must itself run the new app code (drain logic,
`/status`, CORS) with the 180 s grace. On an existing environment that means
**one preparatory release** of the new code before the demo. That release also
switches readiness to `/readyz` and sets the grace through the candidate policy
(6.3). `region.bicep` changes only affect new bootstraps.

Platform risk (must be checked first, ZDD-R00): it is not yet confirmed that
(a) a deactivated revision's FQDN still routes to a draining replica, and
(b) an open request to that replica survives deactivation. Fallback design if
(a) fails: the card goes straight to `UNREACHABLE/STOPPED` and the evidence
comes from the long-task response itself (`drainObserved: true`,
`completed: true`) plus `kind: lifecycle` logs in Log Analytics. If (b) fails,
the demo claim is invalid on ACA as designed; stop and return to the owner
before building retire.

## 5. Verification page (approved layout "A + A2")

Served as a static page at `/verify` by the app itself (decision D2) and also
openable as a local file with a configurable base URL.

Top section (layout A)

- Large "현재 응답 버전" text (e.g. `v2`) with revision name and latency.
- Timeline strip: one cell per second, colour per version (palette assigned in
  order of first appearance), **red** for any error (non-200, timeout > 3 s,
  network failure, invalid JSON).
- Counters: 성공, 오류, 연속 성공.
- Polls `GET <shared FQDN>/api/version` every 1000 ms; never overlaps polls (if a
  call is still pending the next tick records it as pending, not error, until
  its own timeout).

Bottom section (layout A2)

- One card per revision seen by polling (normally v1 and v2), side by side.
- Each card polls `GET <revision FQDN>/status` every 1000 ms and shows:
  revision, replica, state (`SERVING` / `DRAINING` / `STOPPED`), traffic share
  observed by the top strip over the last 30 s, ready ✓/✗, in-flight long tasks
  with elapsed seconds, grace remaining while draining.
- `STOPPED` is shown when a draining revision's status becomes unreachable after
  having reported `DRAINING`; unreachable without prior draining is shown as
  `UNREACHABLE` (not an error on the strip).
- "긴 작업 시작" button on each card sends `POST <revision FQDN>/api/long-task`;
  the card then shows a progress bar (client-side elapsed / expected duration)
  and the final result (완료 / 실패 + reason).
- Multiple replicas (decision D10): a revision FQDN reaches a random replica,
  so the card does not depend on one replica. Card state is the "worst" state
  seen in the last 3 s (`DRAINING` beats `SERVING`; on SIGTERM every replica of
  the revision drains). Task progress comes from the page's own request
  (client timer + final response), not from `/status`. `/status` in-flight
  counts are shown with the replica name as a hint only. Scaling settings are
  not changed.

## 6. Design

### 6.1 App endpoints (`app/server.py`)

| Endpoint | Purpose | Behaviour while draining |
| --- | --- | --- |
| `GET /healthz` | Liveness: process alive and serving loop responsive | 200 |
| `GET /readyz` | Readiness | **503** |
| `GET /work` | Existing business contract and `kind: business` log — unchanged | unchanged |
| `GET /api/version` | `{version, release, revision, replica, dnsSuffix, app, time}` for the page | 200 |
| `GET /status` | `{state, ready, inflight:[{id, startedAt, elapsed}], drainStartedAt, graceSeconds, graceRemaining}` | 200 |
| `POST /api/long-task` | Sleeps `LONG_TASK_SECONDS` (default 120) in small steps, returns `{id, revision, replica, durationMs, completed:true, drainObserved}`; at most `LONG_TASK_MAX_CONCURRENT` (default 3) per replica, else **429** | **503** for new tasks; in-flight ones continue |
| `GET /verify` | Serves the static verification page | 200 |

- `version` is the human label shown in large text (decision D3). Source:
  `APP_VERSION` **baked into the image** via Dockerfile
  `ARG APP_VERSION=dev` / `ENV APP_VERSION=$APP_VERSION`. `release.yml` builds
  with `--build-arg APP_VERSION=r${{ github.run_number }}`; the bootstrap
  scripts use `bootstrap`. It is never set in the revision template env, so
  `expected_candidate` (which copies stable env) cannot make v2 inherit v1's
  label, and the snapshot schema is unchanged.
- Demo endpoints are always on (decision D7, owner). Abuse is bounded by the
  per-replica concurrency cap above; `LONG_TASK_SECONDS` is clamped to
  `GRACE_SECONDS - SHUTDOWN_MARGIN_SECONDS - 5` and to 230 s (ingress 240 s).
- CORS: `Access-Control-Allow-Origin: *` and `OPTIONS` preflight for `/api/*` and
  `/status` only (page calls revision FQDNs cross-origin).
- Long task and status requests log `kind: "long_task"` / no log respectively;
  never `kind: "business"`.

### 6.2 SIGTERM drain state machine

```text
SERVING --SIGTERM--> DRAINING --(inflight == 0) or (deadline)--> STOPPED (exit 0)
```

- Handler only sets a flag/event and records `drainStartedAt`; work happens in
  the main thread (signal-safe).
- Deadline = `GRACE_SECONDS - SHUTDOWN_MARGIN_SECONDS` (env, defaults 180 and
  10). `GRACE_SECONDS` must match the template value; the app reads it from env
  so `/status` can show the countdown.
- While draining, the HTTP server keeps accepting connections so in-flight and
  late probe/status requests are answered; readiness 503 removes the replica
  from load balancing.
- After in-flight reaches 0, wait `DRAIN_IDLE_SECONDS` (default 2) so the page
  can observe the final state, then `server.shutdown()` and exit 0.
- Second SIGTERM is ignored; SIGINT behaves like SIGTERM for local testing.
- Log one line per transition (`kind: "lifecycle"`).

### 6.3 Probes and grace policy

- `infra/health-probes.json`: Startup → `/healthz`, Readiness → `/readyz`,
  Liveness → `/healthz`. Timings unchanged.
- New `infra/revision-policy.json` (decision D4):
  `{"terminationGracePeriodSeconds": 180}`.
- `snapshot.py` records it as `candidate_termination_grace_seconds`.
- `common.expected_candidate` sets `template.terminationGracePeriodSeconds`
  when the snapshot field exists; older snapshots keep the stable value
  (same backward-compatibility rule as `candidate_probes`).
- `common.normalized_template` removes `terminationGracePeriodSeconds` when
  `None` (Azure may return null for unset).
- `infra/region.bicep` sets the same value for bootstrap revisions.
- The candidate env receives `GRACE_SECONDS` equal to the policy value.

### 6.4 Demo retire workflow

- New `.github/workflows/demo-retire.yml`, `workflow_dispatch`, inputs
  `source_run_id`, `region` (`kr`/`sea`/`usw`, no `all`), main only, same
  `safe-aca-production` concurrency group, environment `prod-<region>`
  (reuses existing deploy identity — it already has Container Apps Contributor).
- New `scripts/retire.py`:
  1. Validate source run with `check_source.py` rules and download the original
     baseline and **evidence** artifacts; require `success.json` for the region.
  2. `guarded_state(snapshot, {candidate: 100})` — fail on any drift.
  3. Require candidate ready; require stable has 0% traffic.
  4. `az containerapp revision deactivate` the stable revision.
  5. Poll up to 300 s until stable `active == false`; keep checking candidate
     ready and public `/work` via shared FQDN reaches candidate.
  6. Save `evidence/<region>/demo-retire.json`.
- Consequence: after retire, `restore` for that release fails safely with
  "Stable unavailable". Recovery = `az containerapp revision activate` of the
  old revision, then `restore`. This is documented in the manual chapter.
- This is a **demo-only** exception to [revision cleanup](revision-cleanup.md)
  CLEAN rules (it deactivates the LKG). It does not implement or replace that
  spec.

## 7. Requirements ledger

| ID | State | Requirement | Acceptance evidence required |
| --- | --- | --- | --- |
| ZDD-E01 | EXISTING | `/work` business contract and telemetry | Existing tests keep passing |
| ZDD-E02 | EXISTING | `candidate_probes` backward-compatible candidate policy | `test_safety.py` |
| ZDD-R00 | PROPOSED | Spike: confirm deactivated-revision FQDN reachability and in-flight request survival on ACA (section 4 platform risk) | Nonproduction run notes; decides fallback before ZDD-R05 |
| ZDD-R01 | PROPOSED | Endpoints of 6.1 incl. CORS and non-business logging | Unit tests per endpoint; log kind test |
| ZDD-R02 | PROPOSED | Drain state machine of 6.2 | Deterministic tests: ready→503 on drain, new long task 503, in-flight completes, exit before deadline, deadline exit with in-flight |
| ZDD-R03 | PROPOSED | Probe split and grace policy of 6.3 | Tests: expected candidate has grace; old snapshot unchanged; null normalization |
| ZDD-R04 | PROPOSED | Verification page of section 5 | Manual run against local app incl. SIGTERM (`kill -TERM`) |
| ZDD-R05 | PROPOSED | Demo retire workflow/script of 6.4 | Tests: refuses without success evidence, on drift, on non-zero stable traffic |
| ZDD-R06 | PROPOSED | Documentation of section 8 | Review |
| ZDD-R07 | PROPOSED | End-to-end nonproduction demo per section 4 | Run IDs and evidence; not run yet |

## 8. Documentation deliverables

- Module docstring at top of `app/server.py` explaining endpoints, drain state
  machine and every tunable env var with default.
- Header comment in the verification page script listing tunables
  (`POLL_INTERVAL_MS`, `REQUEST_TIMEOUT_MS`, strip length, colours) at one place.
- New `manual/05-zero-downtime-demo.md`: scenario, screen guide, presenter
  script, tunables table, how to change task duration/grace safely
  (`LONG_TASK_SECONDS < GRACE_SECONDS - margin`, grace ≤ 600, task < 240 s
  ingress timeout), troubleshooting, recovery after retire.
- Update `manual/03-scripts-reference.md` (retire.py), `02-pipeline-workflows.md`
  (demo-retire.yml), `README.md` index, `INSTRUCTION.md` and
  `INSTRUCTION.linux.md` (local SIGTERM test).
- Update this spec's ledger when implemented.

## 9. Decisions

| ID | Decision | By |
| --- | --- | --- |
| D1 | Layout A (top) + A2 revision cards (bottom), long-task button | Owner |
| D2 | Page served by app at `/verify`, vanilla HTML/JS, no build step | Assistant's call |
| D3 | Display version from image-baked `APP_VERSION` (`r<run_number>`), not template env | Assistant's call |
| D4 | Grace 180 s in `infra/revision-policy.json`, task 120 s | Assistant's call (owner asked for ~2 min task) |
| D5 | Long task triggered on v1 revision FQDN after 100%, then manual demo retire | Owner (accepted assistant's proposal) |
| D6 | Demo retire reuses `prod-<region>` environment/identity | Assistant's call |
| D7 | Demo endpoints always on, long tasks capped per replica (429) | Owner |
| D8 | Retire only after the whole release completes; no safety rule relaxed | Assistant's call |
| D9 | One preparatory release of the new code is part of demo setup | Assistant's call |
| D10 | No replica pinning; page aggregates per revision, task progress client-side | Assistant's call |
| D11 | ACA platform behaviour spiked first (ZDD-R00) before building retire | Assistant's call |

## 10. Left to the builder

Exact JSON field names beyond those listed, page styling, colour palette,
internal function decomposition, and test structure — following existing repo
conventions (`tests/test_safety.py` style, stdlib only).
