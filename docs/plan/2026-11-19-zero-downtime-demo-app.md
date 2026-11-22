# Zero-downtime Demo App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `app/server.py` into a realistic zero-downtime demo app (readiness/liveness split, SIGTERM drain, 120 s long task, `/verify` page) and add the grace policy plus a manual demo retire step that produces a real SIGTERM.

**Architecture:** A small `Lifecycle` object in `app/server.py` owns the drain state machine and in-flight long tasks; the HTTP handler reads it from `self.server.lifecycle`. The grace period follows the existing frozen-policy pattern (`infra/*.json` → snapshot field → `common.expected_candidate`). `scripts/retire.py` + `.github/workflows/demo-retire.yml` deactivate the old revision only after a completed release.

**Tech Stack:** Python 3.13 standard library only, `unittest`, vanilla HTML/JS (no build), GitHub Actions, Azure CLI, Bicep.

**Spec:** `docs/spec/zero-downtime-demo-app.md` (read it first; section numbers below refer to it).

## Global Constraints

- Python standard library only; single container `web`; port 8080.
- `python -m unittest discover -s tests -v` must pass (it runs in `release.yml`); `python -m compileall -q app scripts tests` must pass.
- `/work` contract and its `kind: "business"` log line are unchanged. No new endpoint may emit `kind: "business"`.
- Old snapshots without new fields keep their exact restore semantics.
- Defaults: `LONG_TASK_SECONDS=120`, `GRACE_SECONDS=180`, `SHUTDOWN_MARGIN_SECONDS=10`, `DRAIN_IDLE_SECONDS=2`, `LONG_TASK_MAX_CONCURRENT=3`.
- Effective long task = `min(LONG_TASK_SECONDS, GRACE_SECONDS - SHUTDOWN_MARGIN_SECONDS - 5, 230)`; grace 1..600.
- `terminationGracePeriodSeconds` = 180 in `infra/revision-policy.json` (ARM `2024-03-01` `Template` supports it — checked in `CommonDefinitions.json`).
- `APP_VERSION` is baked into the image (Dockerfile `ARG`/`ENV`), never set in template env.
- Retire never relaxes the `safe-aca-production` concurrency group or `check_source.py`.
- Commits end with `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
- Tests run on Windows locally and Ubuntu in CI; signal/subprocess tests use `@unittest.skipUnless(os.name == "posix", ...)`.

## Review Focus

1. A client that disconnects mid long-task (browser tab closed) must still free its slot — `finish_task` in `finally`; test in Task 2.
2. A second SIGTERM, or SIGTERM with zero in-flight tasks, must exit cleanly after `DRAIN_IDLE_SECONDS`, never hang — tests in Task 1.
3. Non-numeric or out-of-range env values (`GRACE_SECONDS=abc`, `GRACE_SECONDS=900`) must fail fast at startup with a clear message, so a bad candidate never becomes ready — test in Task 1.
4. Azure returning `terminationGracePeriodSeconds: null` on a legacy stable, and snapshots without the grace field, must not cause drift errors — tests in Task 3.
5. The page must treat a slow poll as pending (not error) and never overlap polls; CORS preflight (`OPTIONS`) to a revision FQDN must succeed — `OPTIONS` test in Task 2, page behaviour in Task 4's manual check.

---

### Task 1: Lifecycle state machine and settings

**Files:**
- Modify: `app/server.py` (module docstring, `Settings`, `Lifecycle`)
- Test: `tests/test_zero_downtime.py` (new; load `server` the same way as `tests/test_safety.py:27-29`)

**Interfaces:**
- Produces:
  - `Settings.from_env(env: Mapping[str, str]) -> Settings`, frozen dataclass with `long_task_seconds: float` (already clamped), `grace_seconds: int`, `margin_seconds: int`, `idle_seconds: float`, `max_concurrent: int`. Raises `ValueError` naming the variable on non-numeric input, `GRACE_SECONDS` outside 1..600, or margin ≥ grace.
  - `Lifecycle(settings: Settings, clock: Callable[[], float] = time.monotonic)`
  - `Lifecycle.state -> str`: `"SERVING" | "DRAINING" | "STOPPED"`
  - `Lifecycle.begin_drain() -> bool` (True only on the first call)
  - `Lifecycle.start_task() -> tuple[int, str | None]`: `(200, task_id)`, `(503, None)` when draining, `(429, None)` at `max_concurrent`
  - `Lifecycle.finish_task(task_id: str) -> None`
  - `Lifecycle.status() -> dict` with keys `state, ready, inflight` (list of `{id, startedAt, elapsed}`), `drainStartedAt, graceSeconds, graceRemaining` (`None` while serving)
  - `Lifecycle.should_exit() -> bool`: True when draining and either (no in-flight and `idle_seconds` elapsed since the later of drain start / last finish) or `clock() >= drain_start + grace - margin`; sets state `STOPPED` when it returns True.
  - Each transition prints one JSON line `{"kind": "lifecycle", "event", "revision", "time"}`.

- [ ] **Step 1: Write failing tests** — class `LifecycleTests`, fake clock (`now = [0.0]`, `clock=lambda: now[0]`), `patch("builtins.print")`:
  - `test_settings_defaults_and_clamp`: `from_env({})` → `long_task_seconds == 120`, `grace_seconds == 180`; `{"LONG_TASK_SECONDS": "500"}` → `165`; `{"GRACE_SECONDS": "600", "LONG_TASK_SECONDS": "500"}` → `230`.
  - `test_settings_reject_invalid`: `GRACE_SECONDS` = `"abc"`, `"0"`, `"601"`, and `{"GRACE_SECONDS": "10", "SHUTDOWN_MARGIN_SECONDS": "10"}` each raise `ValueError` containing the variable name.
  - `test_drain_rejects_new_tasks_keeps_inflight`: start → 200; `begin_drain()` True, again False; `start_task()` → `(503, None)`; `status()` has `ready is False`, `state == "DRAINING"`, one in-flight.
  - `test_concurrency_cap`: three starts → 200, fourth → `(429, None)`; after one `finish_task`, a start → 200.
  - `test_exit_after_last_task_plus_idle`: drain at t=0 with one task; t=50 False; finish at t=120; t=121 False; t=122.1 True and `state == "STOPPED"`.
  - `test_exit_immediately_when_idle`: no tasks, drain at t=0 → t=1 False, t=2.1 True.
  - `test_exit_at_deadline_with_inflight`: task never finishes; t=100 `graceRemaining == 80`; t=169.9 False; t=170 True.
  - `test_lifecycle_events_are_not_business`: every printed JSON line has `kind == "lifecycle"`.

- [ ] **Step 2: Run** `python -m unittest tests.test_zero_downtime -v` — expected FAIL (`AttributeError: ... 'Settings'`).

- [ ] **Step 3: Implement** `Settings` and `Lifecycle` in `app/server.py`; one `threading.Lock` guards mutable state; task ids `uuid.uuid4().hex[:8]`. Add the module docstring required by spec §8 (endpoints, state diagram, every env var with default, clamp rule).

- [ ] **Step 4: Run** `python -m unittest tests.test_zero_downtime tests.test_safety -v` — expected PASS.

- [ ] **Step 5: Commit** `feat(app): add drain lifecycle and validated settings`.

### Task 2: HTTP endpoints, CORS and SIGTERM main loop

**Files:**
- Modify: `app/server.py` (`Handler`, new `run(settings, port) -> int`, `__main__`)
- Create: `app/verify.html` placeholder containing `<!-- POLL_INTERVAL_MS -->` (Task 4 replaces it)
- Test: `tests/test_zero_downtime.py`

**Interfaces:**
- Consumes: Task 1 `Settings`, `Lifecycle`.
- Produces:
  - Handler resolves `getattr(self.server, "lifecycle", None)`, falling back to a module-level `Lifecycle(Settings.from_env({}))`, so existing tests that build `ThreadingHTTPServer(..., server.Handler)` keep working.
  - Endpoints per spec §6.1:
    - `/healthz`: always 200, existing body.
    - `/readyz`: 200 or 503, body `lifecycle.status()`.
    - `/work`: unchanged.
    - `/api/version`: `{version, release, revision, replica, dnsSuffix, app, time}` from `APP_VERSION` (default `"dev"`), `RELEASE_ID`, `CONTAINER_APP_REVISION`, `CONTAINER_APP_REPLICA_NAME` (default `socket.gethostname()`), `CONTAINER_APP_ENV_DNS_SUFFIX` (default `""`) and `CONTAINER_APP_NAME`.
    - `/status`: `lifecycle.status()` plus `revision` and `replica`.
    - `POST /api/long-task`: 200 `{id, revision, replica, durationMs, completed: true, drainObserved}`, or 503/429 `{error}`.
    - `GET /verify`: `app/verify.html` (path relative to `server.py`) as `text/html; charset=utf-8`; 404 JSON if missing.
    - `OPTIONS` on `/api/*` and `/status`: 204.
  - CORS headers (`Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: GET, POST, OPTIONS`, `Access-Control-Allow-Headers: Content-Type`) are sent only on `/api/*` and `/status`.
  - The long task sleeps in ≤1 s steps until `settings.long_task_seconds`. It logs `kind: "long_task"` start and finish, and calls `finish_task` in `finally`.
  - `run(settings, port) -> int`:
    1. Build a `ThreadingHTTPServer` with `daemon_threads = True` and set `httpd.lifecycle`.
    2. Install SIGTERM/SIGINT handlers that only call `begin_drain()`.
    3. Serve in a thread and loop `while not lifecycle.should_exit(): time.sleep(0.2)`.
    4. Call `httpd.shutdown()` and return 0.
  - `__main__`: `sys.exit(run(Settings.from_env(os.environ), int(os.environ.get("PORT", "8080"))))`.

- [ ] **Step 1: Write failing tests** — class `EndpointTests`, real `ThreadingHTTPServer` on port 0, `httpd.lifecycle = Lifecycle(Settings.from_env({"LONG_TASK_SECONDS": "0.3"}))`, `patch("builtins.print")`:
  - `test_readyz_flips_on_drain`: `/readyz` 200 → after `begin_drain()` 503 with `state == "DRAINING"`; `/healthz` still 200.
  - `test_version_payload`: env `APP_VERSION=r7`, `CONTAINER_APP_ENV_DNS_SUFFIX=x.koreacentral.azurecontainerapps.io` → `version == "r7"`, `dnsSuffix` equal.
  - `test_long_task_completes_and_reports_drain`: POST in a thread, `begin_drain()` while running → 200, `completed is True`, `drainObserved is True`; a new POST during drain → 503.
  - `test_client_disconnect_frees_slot`: raw socket sends POST then closes; `status()["inflight"]` empty within 2 s.
  - `test_cors_only_on_api_and_status`: `OPTIONS /api/long-task` → 204 with `Access-Control-Allow-Origin: *`; `GET /work` lacks it.
  - `test_verify_page_served`: `GET /verify` 200, `Content-Type` starts `text/html`, body contains `POLL_INTERVAL_MS`.
  - `test_new_endpoints_never_log_business`: hit every new endpoint; no printed line has `kind == "business"`.
  - `test_sigterm_drains_then_exits` (posix only): spawn `python app/server.py` with a free `PORT`, `LONG_TASK_SECONDS=2`, `GRACE_SECONDS=20`, `SHUTDOWN_MARGIN_SECONDS=5`, `DRAIN_IDLE_SECONDS=0.5`; start a long task; `send_signal(SIGTERM)`; `/readyz` → 503; long task → 200; exit code 0 within 6 s.
  - Existing `test_http_contract_and_request_log` in `tests/test_safety.py` stays unchanged and passing.

- [ ] **Step 2: Run** `python -m unittest tests.test_zero_downtime -v` — expected FAIL on new endpoint tests.

- [ ] **Step 3: Implement** per Interfaces; keep `/work` body and its `finally` logging identical.

- [ ] **Step 4: Run** `python -m unittest discover -s tests -v` — expected PASS (posix test skipped on Windows).

- [ ] **Step 5: Commit** `feat(app): readiness, status, long task, CORS and SIGTERM drain`.

### Task 3: Probe split and grace policy

**Files:**
- Create: `infra/revision-policy.json` = `{"terminationGracePeriodSeconds": 180}`
- Modify: `infra/health-probes.json` (Readiness path → `/readyz`), `infra/region.bicep` (template `terminationGracePeriodSeconds: loadJsonContent('./revision-policy.json').terminationGracePeriodSeconds`; env `GRACE_SECONDS` with the same value as string), `scripts/snapshot.py`, `scripts/common.py`
- Test: `tests/test_safety.py`

**Interfaces:**
- Produces:
  - Snapshot field `candidate_termination_grace_seconds: int`, read from `infra/revision-policy.json` next to `candidate_probes`.
  - `common.expected_candidate(snapshot)`: when the field exists, set `template["terminationGracePeriodSeconds"]` and replace/add env `GRACE_SECONDS=str(value)`; when absent, output is identical to today.
  - `common.normalized_template`: delete `terminationGracePeriodSeconds` when `None`.

- [ ] **Step 1: Write/adjust failing tests** (the shared `snapshot()` fixture stays without the new field; tests that need it set it on their copy):
  - In `test_shared_http_probe_policy_has_explicit_valid_timeouts_and_thresholds`, change the path assertion: Startup and Liveness `/healthz`, Readiness `/readyz`, all `port == 8080`, `scheme == "HTTP"`.
  - `test_grace_policy_file_is_valid`: value is `int`, `== 180`, within 1..600; `region.bicep` contains `revision-policy.json`.
  - `test_candidate_grace_override_and_legacy_snapshot`: with field 180 → `terminationGracePeriodSeconds == 180`, env contains `{"name": "GRACE_SECONDS", "value": "180"}` and `RELEASE_ID`, stable template unchanged; without the field → no `terminationGracePeriodSeconds` key and env `== [{"name": "RELEASE_ID", "value": "123"}]`.
  - `test_null_grace_is_normalized`: `normalized_template({"terminationGracePeriodSeconds": None, "containers": []}) == {"containers": []}`; value `30` is kept.
  - `test_candidate_grace_drift_is_rejected`: `candidate_owned` with returned template grace 30 (snapshot field 180) raises `Candidate template drift`.
  - Extend `test_snapshot_freezes_candidate_probes_without_modifying_stable`: `captured["candidate_termination_grace_seconds"] == 180`.

- [ ] **Step 2: Run** `python -m unittest tests.test_safety -v` — expected FAIL on new/changed tests.

- [ ] **Step 3: Implement** the file changes. `rollout.py` needs no change (it already submits `expected_candidate(snapshot)`).

- [ ] **Step 4: Run** `python -m unittest discover -s tests -v` — expected PASS.

- [ ] **Step 5: Commit** `feat(infra): readiness on /readyz and frozen 180s termination grace policy`.

### Task 4: Verification page and version label

**Files:**
- Modify: `app/verify.html` (full page, one file, inline `<style>`/`<script>`), `app/Dockerfile` (`ARG APP_VERSION=dev`, `ENV APP_VERSION=$APP_VERSION`, `COPY server.py verify.html ./`), `.github/workflows/release.yml` build line (`--build-arg APP_VERSION="r${GITHUB_RUN_NUMBER}"`), `scripts/bootstrap.sh` and `scripts/bootstrap.ps1` build lines (`--build-arg APP_VERSION=bootstrap`)
- Test: `tests/test_zero_downtime.py`

**Interfaces:**
- Consumes: `/api/version`, `/status`, `POST /api/long-task` (Task 2).
- Page (spec §5, layouts A + A2):
  - **Tunables:** a single `const CONFIG = {...}` at the top of the script, under a header comment: `POLL_INTERVAL_MS: 1000`, `REQUEST_TIMEOUT_MS: 3000`, `STRIP_CELLS: 120`, `TRAFFIC_WINDOW_S: 30`, `CARD_STATE_WINDOW_S: 3`, `PALETTE`, `ERROR_COLOR`.
  - **URLs:** base comes from the `?base=` query parameter, else `location.origin`. The revision URL is `https://<revision>.<dnsSuffix>` when `dnsSuffix` is non-empty, else the base (local mode).
  - **Top section:**
    - Large version text with the revision name and latency.
    - Strip: one cell per second, coloured by version in first-seen order. Red for non-200, timeout, network failure or bad JSON. Grey "pending" when the previous poll is still open (no overlapping polls).
    - Counters: 성공 / 오류 / 연속 성공.
  - **Revision cards (bottom):** one card per revision seen, each polling `/status` every second.
    - State is the worst seen in the last `CARD_STATE_WINDOW_S` (`DRAINING` > `SERVING`).
    - When unreachable: `STOPPED` if it was `DRAINING`, else `UNREACHABLE`. Neither turns the strip red.
    - Also shows: traffic share over the last 30 s, ready ✓/✗, grace remaining, and the replica with an in-flight hint.
  - **"긴 작업 시작" button:** POSTs to the revision URL. Progress comes from a client timer, using 120 s as the expected duration until the first response. The result shows 완료 / 실패 + reason, including `drainObserved`.

- [ ] **Step 1: Write failing tests:**
  - `test_verify_page_declares_tunables_and_endpoints`: file contains `POLL_INTERVAL_MS: 1000`, `REQUEST_TIMEOUT_MS`, `/api/version`, `/status`, `/api/long-task`, `긴 작업 시작`.
  - `test_dockerfile_bakes_version_and_page`: Dockerfile contains `ARG APP_VERSION`, `ENV APP_VERSION=$APP_VERSION`, `verify.html`; `release.yml` contains `--build-arg APP_VERSION=`; `region.bicep` does not contain `APP_VERSION`.

- [ ] **Step 2: Run** `python -m unittest tests.test_zero_downtime -v` — expected FAIL.

- [ ] **Step 3: Implement** the page and build changes.

- [ ] **Step 4: Verify.** First run `python -m unittest discover -s tests -v`; expected PASS. Then do the manual check (ZDD-R04):
  1. Start `python app/server.py` with `LONG_TASK_SECONDS=20` and `GRACE_SECONDS=40`, and open `http://localhost:8080/verify`.
  2. Expect one coloured cell per second and rising counters; the button completes in about 20 s.
  3. Stop the server: expect red cells and the card shows `UNREACHABLE`.
  4. On posix, repeat with `kill -TERM <pid>` during a task: the card shows `DRAINING`, the task shows 완료, then the card shows `STOPPED`.
  5. Note the observations in the commit body.

- [ ] **Step 5: Commit** `feat(app): /verify zero-downtime page and image-baked APP_VERSION`.

### Task 5: Platform spike ZDD-R00 (operator, needs Azure)

**Files:**
- Modify: `docs/spec/zero-downtime-demo-app.md` (record result in §4 "Platform risk" and ledger row ZDD-R00)

- [ ] **Step 1:** Release Tasks 1–4 to nonproduction with one completed `release.yml` run (`profile=demo`); note the run ID.
- [ ] **Step 2:** Open `https://<shared FQDN>/verify`, click "긴 작업 시작" on the old revision card, then run `az containerapp revision deactivate -g <rg> -n <app> --revision <old>`.
- [ ] **Step 3:** Record:
  - (a) whether `/status` on the old revision FQDN kept answering `DRAINING`;
  - (b) whether the long task returned 200 with `drainObserved: true`;
  - (c) the `lifecycle` lines in `ContainerAppConsoleLogs_CL`.

  Afterwards run `az containerapp revision activate` on the old revision.
- [ ] **Step 4: Decide based on the result.**
  - (b) failed: stop and report to the owner (spec §4).
  - Only (a) failed: the page keeps working via the fallback (card shows `STOPPED`; evidence comes from the response and logs). Note this in the spec.
  - No Azure access for the executor: mark ZDD-R00 blocked and ask the owner whether to build Task 6 before the spike (spec D11).
- [ ] **Step 5: Commit** `docs(spec): record ZDD-R00 platform spike result`.

### Task 6: Demo retire script and workflow

**Files:**
- Create: `scripts/retire.py`, `.github/workflows/demo-retire.yml`
- Test: `tests/test_safety.py`

**Interfaces:**
- Consumes: from `common`: `verify_context`, `guarded_state`, `candidate_owned`, `ready`, `revision`, `props`, `az`, `cli_app`, `save`, `iso`, `utcnow`, `load`, `require`. From `rollout`: `request(fqdn, path, source, run, expected_revision)`.
- Produces `retire.retire(snapshot: dict, success: dict, timeout: float = 300, interval: float = 5) -> dict` and `main()` (args `--snapshot PATH --success PATH`). Steps, in order:
  1. `verify_context`.
  2. Require `os.environ["SOURCE_RUN_ID"] == snapshot["run_id"]` and `success["revision"] == snapshot["candidate"]`.
  3. `current, _ = guarded_state(snapshot, {candidate: 100})`. This also proves the stable revision has 0% traffic.
  4. Require `ready(candidate_owned(snapshot))`.
  5. Run `az("containerapp", "revision", "deactivate", *cli_app(), "--revision", snapshot["stable"])`.
  6. Poll until `props(revision(stable)).get("active") is False`. Each loop requires `ready(revision(candidate))` and `request(public_fqdn, "/work", "probe", run_id, candidate)`. On timeout, raise `RuntimeError("Stable still active after ...")`.
  7. Save `evidence/<REGION>/demo-retire.json` with `{retired_at, source_run, stable, candidate, verified: True}`.
- Produces the workflow `demo-retire.yml`:
  - Trigger: `workflow_dispatch` with inputs `source_run_id` (string) and `region` (choice `kr`/`sea`/`usw`).
  - `concurrency: {group: safe-aca-production, cancel-in-progress: false}`.
  - Job: `if: github.ref == 'refs/heads/main'`, `environment: prod-${{ inputs.region }}`, `timeout-minutes: 15`.
  - Steps mirror `restore-region.yml`:
    1. checkout; setup-python 3.13.
    2. Run `check_source.py`.
    3. Download `baseline-<run>-<region>` into `retire-input`.
    4. Download `evidence-<run>-<region>-1` into `retire-input/evidence`.
    5. Bind the snapshot to the source SHA and run.
    6. `azure/login` at the same pinned SHA.
    7. Run `python scripts/retire.py --snapshot retire-input/baseline-$REGION.json --success retire-input/evidence/$REGION/success.json`.
    8. Upload `evidence/` as `demo-retire-evidence-${{ github.run_id }}-${{ inputs.region }}`.

- [ ] **Step 1: Write failing tests.** Patch `retire.guarded_state`, `retire.candidate_owned`, `retire.ready`, `retire.revision`, `retire.az`, `retire.request`, `retire.save` and `time.sleep`; env `SOURCE_RUN_ID=123`, `REGION=kr`.
  - `test_retire_refuses_without_matching_success`: `success={"revision": "other"}` → raises; `az` not called.
  - `test_retire_refuses_on_drift`: `guarded_state` raises `Traffic drift` → re-raised; `az` not called; it was called with `{"myapp--r123": 100}`.
  - `test_retire_refuses_unready_candidate`: `ready` returns False → raises; `az` not called.
  - `test_retire_deactivates_then_verifies`: stable is active once, then inactive.
    - `az` is called once with `"deactivate"`, `"--revision", "myapp--old"`.
    - `request` is called with `"/work"` and `"myapp--r123"`.
    - The saved record has `verified is True`.
  - `test_retire_times_out_if_stable_stays_active`: `retire(..., timeout=0, interval=0)` with stable always active → raises `Stable still active`.
  - Extend `test_region_workflows_are_sequential_and_share_restore_lock` to include `demo-retire.yml`. It must contain `prod-${{ inputs.region }}` and `check_source.py`, and not `[all,`.

- [ ] **Step 2: Run** `python -m unittest tests.test_safety -v` — expected FAIL (`ModuleNotFoundError: retire`).

- [ ] **Step 3: Implement** `scripts/retire.py` and the workflow.

- [ ] **Step 4: Run** `python -m unittest discover -s tests -v` and `python -m compileall -q app scripts tests` — expected PASS.

- [ ] **Step 5: Commit** `feat(ops): demo retire workflow that deactivates the old revision`.

### Task 7: Documentation

**Files:**
- Create: `manual/05-zero-downtime-demo.md`
- Modify:
  - `manual/README.md`: index.
  - `manual/02-pipeline-workflows.md`: `demo-retire.yml`.
  - `manual/03-scripts-reference.md`: `retire.py`, the snapshot field, probes/grace policy.
  - `manual/04-operations-runbook.md`: recovery after retire.
  - `README.md` components: `verify.html`, `retire.py`, `demo-retire.yml`, `revision-policy.json`.
  - `INSTRUCTION.md` and `INSTRUCTION.linux.md`: local SIGTERM test.
  - `docs/spec/zero-downtime-demo-app.md`: ledger R01–R06 → IMPLEMENTED with test names; R07 stays PROPOSED until run.
  - `docs/spec/README.md`: index row.

- [ ] **Step 1: Write `manual/05-zero-downtime-demo.md`** in Korean, in the style of `manual/04-operations-runbook.md`. Sections:
  1. Purpose.
  2. State machine diagram.
  3. Endpoint table.
  4. Tunables table: every env var, its default, and where it is set (image, template, policy file or page `CONFIG`).
  5. Safe-change rules:
     - `LONG_TASK_SECONDS < GRACE_SECONDS - margin - 5`
     - grace ≤ 600
     - task ≤ 230 s
     - change grace only via `infra/revision-policy.json` plus a new release
  6. Preparation release (D9).
  7. Presenter script T0–T7.
  8. Screen guide: what each card state means.
  9. Troubleshooting: card `UNREACHABLE`, 429, 503, retire waiting on concurrency.
  10. Recovery after retire: `az containerapp revision activate`, then restore.
  11. File map for future edits: which file or function to change for each tunable or behaviour.
- [ ] **Step 2:** Apply the other file updates listed above.
- [ ] **Step 3: Verify links.** Write a short Python check that scans `manual/*.md`, `README.md` and `docs/spec/*.md` for relative `](...)` links and checks each with `Path.exists()`. Expected: no missing paths.
- [ ] **Step 4: Commit** `docs: zero-downtime demo operator guide and ledger update`.

### Task 8: End-to-end demo ZDD-R07 (operator, needs Azure)

- [ ] **Step 1:** Follow `manual/05-zero-downtime-demo.md` exactly: preparation release, demo release, button, `demo-retire.yml` for `kr`.
- [ ] **Step 2:** Pass criteria:
  - strip error count is 0 throughout;
  - the long task returns 200 with `drainObserved: true`;
  - the card goes `DRAINING` → 완료 → `STOPPED`;
  - `demo-retire.json` has `verified: true`.
- [ ] **Step 3:** Record run IDs in the ZDD-R07 ledger row; commit `docs(spec): record ZDD-R07 end-to-end evidence`.
