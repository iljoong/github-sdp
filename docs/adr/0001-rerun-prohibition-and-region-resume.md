# ADR 0001: GitHub re-runs stay forbidden; region resume is a new release run

Status: **Accepted 2026-11-23** (spec [#21](https://github.com/iljoong/github-sdp/issues/21);
implemented by #22–#26, recorded by #27).
Related: [dashboard spec](../spec/deployment-dashboard.md),
[operator guide](../../manual/07-deployment-dashboard.md),
[pipeline workflows](../../manual/02-pipeline-workflows.md).

## Context

Every deploy job rejects `GITHUB_RUN_ATTEMPT != 1` (`build.yml`,
`scripts/snapshot.py`, `scripts/rollout.py`). The candidate revision is named
`<app>--r<GITHUB_RUN_ID>` and the per-region baseline
`baseline-<run>-<region>` is never overwritten. Together these make each run the
provable owner of the revision it created and of the traffic it may restore.

Incident on release run #45021576545 (times UTC):

1. KR and SEA succeeded. USW was approved for attempt 1 at 13:20:35.
2. One demo request failed during the USW 50% bake. The guarded automatic
   rollback then refused with `Stable unavailable`, leaving the candidate at
   100% traffic.
3. An operator used GitHub **Re-run failed jobs**. GitHub created attempt 2 and
   raised a new `prod-usw` deployment protection rule request. The receiver
   accepted it (HTTP 204 at 13:46:26), but the console filtered it out: gate
   verification only accepted attempt 1, read attempt-1 jobs (where USW looked
   completed), and the review claim keyed by run and environment was already
   taken by the attempt-1 approval.
4. Nobody could approve or reject the gate in the console. Attempt 2 waited
   until it was cancelled at 13:55.

Approving would not have helped. The rerun guards refuse attempt 2 and the
candidate name already exists. There was no supported way to finish a partially
completed release short of a full new release that re-deploys KR and SEA. The
re-run also disabled console rollback, which required the source to have
completed "on its first attempt" even though `restore.yml` only needs the
verified attempt-1 baseline. The receiver also logged no `workflow_run` or
`workflow_job` deliveries because the GitHub App was not subscribed to them.

## Decision

1. **GitHub re-runs of deploy workflows remain forbidden.** The attempt-1
   guards, run-id candidate naming and never-overwrite-baseline rule are
   unchanged. The new `promote` job in `release.yml` has the same guard.
2. **The console is honest about re-runs.** A protection rule raised for attempt
   N > 1 of a console-started release is listed in the approval inbox as
   **"Re-run blocked — reject only"**. Approve is refused; a `ReleaseApprover`
   can reject, which closes the gate in GitHub immediately. Gate verification
   reads the current attempt's jobs and only accepts protection-rule rows
   received at or after that attempt started. Review claims include the attempt
   (`review:<run>:<env>:<attempt>`; attempt 1 keeps the legacy key
   `review:<run>:<env>`), and approve/reject audit rows record `run_attempt`.
3. **Resume is a new run of `release.yml`.** The dispatch inputs
   `resume_from_run_id` and `start_region` (`kr`/`sea`/`usw`, default `kr`)
   skip `build` and run a `promote` job on the `build` environment instead.
   `scripts/promote_source.py` verifies the source run and rebinds its
   `release.json` to the new run. Regions run from `start_region` onward in
   KR → SEA → USW order. The new run is attempt 1 with a fresh run id, candidate
   name and baselines, so `region.yml`, `snapshot.py`, `rollout.py` and the
   per-region approval flow run unchanged. The concurrency group
   `safe-aca-production` stays shared. An empty `resume_from_run_id` is the
   unchanged normal release.
4. **The console offers resume ("실패 지역부터 재개").** The client sends only the
   request id and source run id. The server derives the start region and reuses
   the source's profile from its audit row (default `production`), with the
   same request-id idempotency, write lock and audit row as other operations.
5. **Rollback no longer requires attempt 1.** It still requires a completed
   source, the verified attempt-1 baseline, and passing live traffic and health
   checks. Retire still requires attempt-1 success evidence and additionally
   refuses when a later attempt started that region's rollout.
6. The GitHub App must subscribe to **Deployment protection rule**, **Workflow
   runs** and **Workflow jobs**. The console's `hints` freshness source turns
   stale when a run is active and no change hint arrived for 15 minutes.

## Alternatives rejected

- **Allow GitHub re-runs to deploy.** This needs attempt-aware candidate and
  baseline naming and relaxes the core ownership invariant in `build.yml`,
  `snapshot.py`, `rollout.py` and the restore path. High risk for a rare event.
- **A separate resume workflow.** It would split release history and the
  console's single release view and duplicate the regional chain. Reusing
  `release.yml` keeps one release model, one concurrency group and one set of
  approvals.

## Consequences

- A resume run executes **workflow and script code from current `main`**, which
  may differ from the source commit, while the deployed image is the source's.
- **Commit binding.** The rebound `release.json` carries the resume run's own
  `commit` (so `snapshot.py` keeps requiring `release.commit == GITHUB_SHA`),
  the unchanged `image`, `source_run_id` (the immediate source run),
  `build_run_id` (the run that built the image) and `source_commit` (that build's
  commit). Re-rebinding a resume record keeps its `build_run_id` and
  `source_commit`. `snapshot.py` copies these optional fields into the baseline
  and `rollout.py` into its success evidence. `promote_source.py` requires the
  source record's repository, run id and commit to match the verified source
  run, the image to be an immutable digest
  `<ACR_NAME>.azurecr.io/safe-demo@sha256:...`, and a rebound source record to
  carry its build provenance.
- **Resume of a resume is allowed (skipped-prefix rule).** A resume run skipped
  `build` and the regions before its start region. Those regions count as
  satisfied when the source is itself a resume run (its `promote` job succeeded
  and `build` was skipped) and they form a leading prefix of `skipped` regions
  before the source's first executed region; otherwise each earlier region must
  have concluded `success`. A source that ran no region is refused.
- The console only offers resume for a completed release with conclusion
  `failure` or `cancelled` that built (or, for a resume source, promoted) its
  image, whose regions before the start region succeeded in the latest attempt
  or were skipped by the skipped-prefix rule, and whose later regions were not
  started, skipped or cancelled. Every refusal reason is a static string the UI
  translates. Independently, `promote_source.py` checks the source repository,
  `release.yml` path, `workflow_dispatch` event, `main` branch, `completed`
  status and, for every earlier region in the latest attempt
  (`filter=latest`), `"<caller> / rollout"` success or the skipped-prefix rule.
- **A start region without a source baseline** (never snapshotted) is accepted
  when its live traffic is exactly one active, Healthy revision at 100%; the
  resume run captures that revision as its baseline. With a source baseline, the
  live revision must also be that baseline's stable or candidate. For the
  incident run, USW (candidate at 100%) becomes resumable once that revision is
  Healthy; console rollback is the other option.
- A resume run is recorded as a deploy-kind audit row with `source_run_id`,
  `profile` and `start_region`, so inbox, history and timeline treat it as a
  release. Each region still needs a `ReleaseApprover` other than the requester.
  History shows a "resumed from" link and a re-run badge for attempt > 1.
- Re-run gates are only listed for deploy runs; a re-run of a rollback or retire
  run is not decidable in the console.
- Out of scope: resuming `release-kr.yml`, automatic rejection of re-run gates,
  the `Stable unavailable` automatic-rollback refusal, and the actual App
  subscription change (an operator action, see manual 07/08).
