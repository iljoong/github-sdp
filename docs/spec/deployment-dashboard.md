# Deployment monitoring dashboard (A layout)

Status: **Approved 2026-11-20; implemented in `1be41b8`**. Nonproduction
console/receiver integration partially verified; regional rollback, demo retire,
reject/cancel and emergency approval path not verified through the console.
Baseline: `608afa1` (implementation base), inspected again on `main` 2026-11-22.
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
  rollback, demo-only retire.
- DD-G5. Durable audit with real Entra identities and duplicate-write protection.

Non-goals

- A new deployment engine or direct Azure mutation from the dashboard.
- Per-step (10/50/100) human approval or live bake-window progress.
- Production retire, automatic reactivation after retire, all-region restore,
  rollback of an in-progress source run, force cancel, deploy reruns.
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
- **Action panel:** approve/reject, cancel, regional rollback, demo retire, with
  disabled reasons, confirmation, requester and result. GitHub run links are
  optional; operators can finish without opening GitHub.

## 4. Platform and trust boundaries

| Component | Exposure | Identity and permissions |
| --- | --- | --- |
| Console (`dashboard.server:app`) | ACA Easy Auth, single tenant, Enterprise Application assignment required | Console MI: `Reader` on the three registered target apps, Key Vault `sign` on one GitHub App key, Entra/token-store secrets read, operations table read/write, inbox read |
| Receiver (`dashboard.webhook:app`) | Only `POST /webhooks/github` and `/healthz`; no Easy Auth, no UI or management API | Receiver MI: webhook secret read and inbox insert only; no signing, Actions or Azure target permissions |
| Storage | Azure Tables `dashboardops`, `dashboardinbox` (partition = repository ID); Blob `dashboardtokens` for the Easy Auth token store | Per-table role assignments; shared key disabled |
| GitHub App | Installed on the one target repository | Actions read/write, Deployments read/write only |

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

- Roles: `ReleaseOperator` (deploy, cancel, rollback, demo retire for every
  registered region and both profiles) and `ReleaseApprover` (approve/reject).
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
| Approve/reject | GitHub App protection rule for `prod-{kr,sea,usw}` and `prod-rollback-*` | Gate bound to a console-started run and operation; approver differs from requester; pending state and run attempt confirmed |
| Cancel | GitHub run cancel | In-progress source release only; cancel success is not reported as recovery |
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
- Azure identities, Key Vault roles and cross-resource-group read grants are
  pre-provisioned outside `infra/dashboard.bicep`
  ([manual 08](../../manual/08-deployment-dashboard-permissions.md)).
- VNet integration, Private Endpoints/DNS and Storage public-network shutdown
  are not configured by the template and must be validated per installation.
