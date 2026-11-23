# Deployment monitoring dashboard (A layout)

Status: **Approved 2026-11-20; implemented in `1be41b8`**. Nonproduction
console/receiver integration partially verified; regional rollback, demo retire,
reject/cancel and emergency approval path not verified through the console.
Baseline: `608afa1` (implementation base), inspected again on `main` 2026-11-22.
Amended 2026-11-23 by spec #21 (re-run gates, rollback after re-run, resume from
failed region, change-hint freshness); see
[ADR 0001](../adr/0001-rerun-prohibition-and-region-resume.md).
Related: [index](README.md), [implementation plan](../plan/2026-11-20-deployment-dashboard.md),
[operator guide](../../manual/07-deployment-dashboard.md),
[Azure permissions](../../manual/08-deployment-dashboard-permissions.md).

## 1. Intent

Non-developer release operators sign in with Microsoft Entra to a separate Azure
Container App, start a release, and follow **Build → KR → SEA → USW** together
with service state. They approve regional environments without a GitHub account
and request the existing protected restore/retire workflows. Only the A layout
(connected rollout rail) is provided; prototype switchers, scenario pickers and
simulated progress controls are excluded.

The existing app and the Python rollout/restore/retire scripts remain the
authority for deployment and verification. The dashboard never writes to Azure
target apps directly.

## 2. Goals and non-goals

Goals

- DD-G1. Rollout rail that separates GitHub run/gate state from Azure traffic.
- DD-G2. Regional detail: configured traffic, stable/candidate revision, Azure
  replica inventory, app readiness and `/api/version`/`/status` samples,
  allowlisted ENV, each with its source and observation time.
- DD-G3. Entra-only regional approval through a GitHub App custom deployment
  protection rule, with requester/approver separation.
- DD-G4. Protected operations: deploy, cancel, completed-source regional
  rollback, demo-only retire, resume of a failed/cancelled release from its
  first unfinished region.
- DD-G5. Durable audit with real Entra identities and duplicate-write protection.

Non-goals

- A new deployment engine or direct Azure mutation from the dashboard.
- Per-step (10/50/100) human approval or live bake-window progress.
- Production retire, automatic reactivation after retire, all-region restore,
  rollback of an in-progress source run, force cancel, deploy reruns. GitHub
  "Re-run" of a release stays forbidden; console Resume (a new `release.yml`
  run) is the supported way to finish a partially completed release
  ([ADR 0001](../adr/0001-rerun-prohibition-and-region-resume.md)).
- Arbitrary subscription/repository/URL selection by the client.
- Deploying the dashboard itself to production or switching GitHub policy as
  part of implementation approval.

## 3. Screen

- **Header:** target app, release/profile/commit, freshness, start deploy. Shows
  the current run by default; recent runs are selectable.
- **Rail:** Build/KR/SEA/USW. A candidate at 100% traffic does not imply job
  completion or gate success.
- **Region detail:** HTTP observation failure or absence is shown as
  `unobserved`, distinct from replica termination. A failure in one source
  (GitHub, Azure, app) never overwrites another source's successful state.
- **Action panel:** approve/reject, cancel, regional rollback, demo retire,
  resume from failed region, with disabled reasons, confirmation, requester and
  result. GitHub run links are optional; operators can finish without opening
  GitHub.
- **Re-runs:** a release with run attempt > 1 shows a re-run badge in the
  header, history and inbox; timeline approval events name the attempt. A resume
  run's history row links to its source release.
- **Freshness:** besides GitHub, audit, Azure and replica sources, a `hints`
  source shows the last received `workflow_run`/`workflow_job` change hint. It is
  stale only when a run is active and no hint arrived for 15 minutes (likely a
  missing App event subscription); polling continues regardless.

## 4. Platform and trust boundaries

| Component | Exposure | Identity and permissions |
| --- | --- | --- |
| Console (`dashboard.server:app`) | ACA Easy Auth, single tenant, Enterprise Application assignment required | Console MI: `Reader` on the three registered target apps, Key Vault `sign` on one GitHub App key, Entra/token-store secrets read, operations table read/write, inbox read |
| Receiver (`dashboard.webhook:app`) | Only `POST /webhooks/github` and `/healthz`; no Easy Auth, no UI or management API | Receiver MI: webhook secret read and inbox insert only; no signing, Actions or Azure target permissions |
| Storage | Azure Tables `dashboardops`, `dashboardinbox` (partition = repository ID); Blob `dashboardtokens` for the Easy Auth token store | Per-table role assignments; shared key disabled |
| GitHub App | Installed on the one target repository | Actions read/write, Deployments read/write only; webhook events Deployment protection rule, Workflow runs, Workflow jobs |

- The GitHub App private key is imported into Key Vault; the runtime signs RS256
  JWTs with `CryptographyClient` and never downloads the raw key. Installation
  tokens are cached in memory and requested for the single repository.
- The receiver verifies the raw-body HMAC, the registered repository,
  installation, event, run and environment, and stores each delivery ID once.
  The callback URL in the payload is untrusted; the console constructs GitHub
  calls from validated identifiers.
- Regional HTTP probes use only registered apps and revision FQDNs read from
  Azure. A response whose revision differs from the expected one is not
  attributed. Ingress of target apps is never opened for monitoring.
- Client requests never carry repositories, resource IDs, URLs, shell commands or
  credentials. No browser receives Azure or GitHub credentials.

## 5. Identity

- Roles: `ReleaseOperator` (deploy, cancel, rollback, demo retire, resume for
  every registered region and both profiles) and `ReleaseApprover` (approve/reject).
  One person may hold both but can never approve their own operation.
- Every request must present both the Easy Auth principal
  (`X-MS-CLIENT-PRINCIPAL`) and the token-store Entra ID token
  (`X-MS-TOKEN-AAD-ID-TOKEN`). The ID token is validated independently
  (signature, issuer, audience, lifetime, `tid`/`oid`, no `scp`, `roles` list)
  and must produce the same actor as the principal. Service principals, wrong
  tenant and role-less users are rejected.
- Writes additionally require the exact console `Origin` and
  `X-Dashboard-Write: 1`.
- Because the ID token comes from the Easy Auth token store, the console depends
  on a private Blob container and a user-delegation SAS (HTTPS, `rwd`, at most 7
  days) stored as a Key Vault secret and renewed before expiry
  ([manual 07](../../manual/07-deployment-dashboard.md)).

## 6. Operations

| Operation | Workflow | Preconditions rechecked server-side |
| --- | --- | --- |
| Deploy | `release.yml` on `main`, profile `demo`/`production` | Run ID obtained from `return_run_details`; ambiguous response is never re-dispatched or guessed |
| Approve/reject | GitHub App protection rule for `prod-{kr,sea,usw}` and `prod-rollback-*` | Gate bound to a console-started run and operation; approver differs from requester; pending state confirmed from the current attempt's jobs and a protection-rule row received at or after that attempt started; decision claim per run attempt. A deploy run's gate on attempt > 1 is a re-run gate: reject only |
| Cancel | GitHub run cancel | In-progress source release only; cancel success is not reported as recovery |
| Resume | `release.yml` on `main` with `profile`, `resume_from_run_id`, `start_region` | Completed source release (any attempt) with conclusion `failure`/`cancelled` and a built image (or, for a source that is itself a resume run, a successful `promote`); start region = first region not successful in the latest attempt, all earlier regions successful or, for a resume source, a leading prefix of `skipped` regions before its first executed region; later regions not run; start region live traffic exactly one active Healthy revision at 100% (the source baseline's stable or candidate when a baseline exists). Profile from the source's audit row (default `production`); client sends neither region nor profile |
| Regional rollback | `restore.yml` for one region | Completed source run (any run attempt), valid attempt-1 `baseline-<run>-<region>` evidence, stable revision active; workflow drift/health checks remain final |
| Demo retire | `demo-retire.yml` | Completed `demo` run with explicit profile evidence, attempt-1 region success evidence, candidate at 100%, and no later run attempt started that region's rollout; confirmation states that rollback then requires reactivation |

- Audit intent and a duplicate-prevention key are stored before any GitHub
  write. Reservations, claims and the `write` lock use one Azure Table partition
  transaction with ETags.
- An ambiguous GitHub write leaves the operation `confirmation_required`/
  `reserved`, keeps the write lock, and is resolved only by documented manual
  reconciliation. There is no force-unlock button.
- View-only GitHub reads may be cached up to 30 seconds; writes always recheck
  uncached state.
- Re-run gates: GitHub re-runs of deploy jobs are refused by the attempt-1
  guards, so a protection rule raised for attempt > 1 is listed as
  "Re-run blocked — reject only". Approve is refused server-side; reject closes
  the gate in GitHub. Claims are `review:<run>:<env>:<attempt>` (attempt 1 keeps
  `review:<run>:<env>`), so an attempt-1 decision never blocks or overwrites a
  later attempt's.
- Resume runs: `release.yml` skips `build` and runs `promote` (environment
  `build`), which verifies the source with `scripts/promote_source.py` and
  republishes its `release.json` with the unchanged image digest, the new run id
  and commit, `source_run_id`, and the original build's `build_run_id` and
  `source_commit` (kept when the source is itself a resume). The baseline and
  rollout success evidence copy these fields. Regions run from `start_region` onward. The
  run is recorded as a deploy-kind audit row with `source_run_id`, so normal
  per-region approval with requester separation applies. Workflow code comes
  from current `main`; the image comes from the source.
- Runs started directly in GitHub have no verifiable Entra requester and are
  never approved from the console. A GitHub-based emergency path must be
  designed and approved separately before production cutover.

## 7. Acceptance

| ID | Criterion | State | Evidence |
| --- | --- | --- | --- |
| DD-R01 | Unassigned, role-less, wrong-tenant, app-only users and self-approval rejected | `IMPLEMENTED` | `dashboard/tests/test_dashboard_auth_github.py`, `test_dashboard_operations.py`; live two-user separation not recorded |
| DD-R02 | Forged, replayed or foreign webhook cannot enable a gate | `IMPLEMENTED` | `test_dashboard_store_webhook.py`; signed GitHub `ping` redelivery returned HTTP 204 in nonproduction |
| DD-R03 | Lost dispatch response or crash after a write does not duplicate Azure-changing operations | `IMPLEMENTED` | `test_dashboard_operations.py` |
| DD-R04 | Traffic, gate state and HTTP samples displayed independently; partial source failure isolated | `IMPLEMENTED` | `test_dashboard_observations.py`, headless Edge smoke |
| DD-R05 | Missing/expired/wrong-attempt evidence or unverified profile blocks rollback/retire | `IMPLEMENTED` | `test_dashboard_observations.py`, `test_dashboard_operations.py` |
| DD-R06 | Real Entra login, Key Vault signing, installation token and Tables access from ACA | `PARTIAL` | Nonproduction console login and dashboard view confirmed by the owner; console MI table read/write and inbox read confirmed |
| DD-R07 | KR → SEA → USW approval through the App rule with a separate `ReleaseApprover` | `PARTIAL` | Owner confirmed approval on production run 44998140564 (all regions succeeded); reject/cancel not verified |
| DD-R08 | Regional rollback and demo retire through the console | `PROPOSED` | Not run through the console; requires separate approval |
| DD-R09 | Re-run gate listed reject-only; approve refused, reject sent; per-attempt claims; stale earlier-attempt rule rows ignored | `IMPLEMENTED` | `test_dashboard_observations.py`, `test_dashboard_operations.py`, UI smoke; not verified live |
| DD-R10 | Rollback eligible after a re-run; retire blocked when a later attempt ran the region | `IMPLEMENTED` | `test_dashboard_observations.py` |
| DD-R11 | Resume eligibility, server-derived dispatch inputs and audit row; workflow promote/verification fails closed | `IMPLEMENTED` | `test_dashboard_observations.py`, `test_dashboard_operations.py`, `tests/test_release_resume.py`, UI smoke; not run live |
| DD-R12 | Change-hint freshness turns stale while a run is active without hints | `IMPLEMENTED` | `test_dashboard_observations.py`, `test_dashboard_changes.py` |

## 8. Decisions

- User-selected: A layout, real shared ACA app, admin-only access, Easy Auth +
  managed identity, no operator GitHub accounts.
- Proposed and approved: existing workflows remain the only Azure write path;
  Key Vault signing; separate receiver; regional-only rollback; demo-only retire;
  one Entra approver distinct from the requester; live changes need separate
  approval. Accepting the GitHub custom protection rule public preview is part
  of this approval.
- 2026-11-22: keep Easy Auth + token-store SAS. App-native OIDC (no Blob/SAS
  but new session code) and principal-header-only (loses independent token
  validation) were not chosen.
- 2026-11-22: the principal `auth_typ` check was removed because ACA did not
  emit the expected `aad` value; independent ID token validation and actor
  matching are retained.
- 2026-11-23 (spec #21, [ADR 0001](../adr/0001-rerun-prohibition-and-region-resume.md)):
  GitHub re-runs stay forbidden; resume is a new `release.yml` run reusing the
  source image. Rollback drops the "first attempt" requirement; retire keeps
  attempt-1 evidence. Allowing re-runs and a separate resume workflow were
  rejected.
- Azure identities, Key Vault roles and cross-resource-group read grants are
  pre-provisioned outside `infra/dashboard.bicep`
  ([manual 08](../../manual/08-deployment-dashboard-permissions.md)).
- VNet integration, Private Endpoints/DNS and Storage public-network shutdown
  are not configured by the template and must be validated per installation.
