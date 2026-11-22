# Deployment Monitoring Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Tasks 1–6 complete and merged in `1be41b8` (base `608afa1`). Live
verification items are tracked in the spec's acceptance table, not here.

**Goal:** Build the approved Entra-only ACA console with the A rollout rail.

**Architecture:** A management API/UI and a narrow GitHub webhook receiver share
durable Azure Table data. The console mutates only through existing GitHub
Actions workflows; Azure and application runtime clients observe state without
writing to the deployed workload.

**Tech Stack:** Python 3.13, FastAPI + Uvicorn, httpx, PyJWT, Azure Identity /
Key Vault Keys / Data Tables SDKs, HTML/CSS/JS without a frontend build, Bicep,
unittest.

**Spec:** [`docs/spec/deployment-dashboard.md`](../spec/deployment-dashboard.md).
Operator setup: [manual 07](../../manual/07-deployment-dashboard.md),
[manual 08](../../manual/08-deployment-dashboard-permissions.md).

## Global Constraints

- One configured repository, three named regions `kr`, `sea`, `usw`, fixed
  `main` and approved workflows. No client-directed endpoints or shell commands.
- Single-tenant ACA Easy Auth and Entra app roles `ReleaseOperator` /
  `ReleaseApprover`. Separate approver; no GitHub account linkage. No human
  reviewer impersonation.
- No modification of existing workflow or application code. No Azure resource
  deployment, GitHub App installation, approval policy change or live mutation
  under this plan.
- Separately testable modules; injected fakes in tests only. No runtime
  mock-login or dummy credential fallback.
- Persist operation intent before dispatch or rule approval. Never auto-replay
  ambiguous GitHub writes. HTTP observations are not authoritative inventory.
- Rollback is regional and completed-source only. Retire is demo-only with
  explicit evidence. No force cancel, `all` restore, or reactivation.
- Dashboard tests live in `dashboard/tests`, not `tests`: `release.yml` runs the
  stdlib-only `tests` suite and must not pick up FastAPI/Azure dependencies.

## Review Focus

1. Same Entra actor with both roles cannot approve their own run (task 3).
2. Validly signed webhook for a different install/repo or a repeated delivery
   cannot enable an unrelated gate (task 2).
3. Lost dispatch response or crashed worker after sending cannot cause duplicate
   Azure-changing operations (task 3).
4. Candidate traffic at 100% while the GitHub gate is incomplete and one replica
   sample is missing must display separately (tasks 4–5).
5. Evidence absent, expired, wrong attempt, or unverified production profile must
   not unlock rollback/retire (tasks 3–4).

Test command for every task (from the repository root, in a venv with
`dashboard/requirements.txt`):

```powershell
python -m unittest discover -s dashboard\tests -p <file> -v
```

## 1. Identity and allowlisted GitHub transport

**Files:** `dashboard/__init__.py`, `dashboard/config.py`, `dashboard/auth.py`,
`dashboard/github.py`, `dashboard/requirements.txt`,
`dashboard/tests/test_dashboard_auth_github.py`.

**Interfaces:** `Settings.from_env()`; `actor_from_principal(header, tenant_id) -> Actor`
with `Actor(tenant_id, object_id, roles, display_name)`; `EntraSession(tenant_id, audience)(id_token) -> Actor`;
`authorize(actor, role)`; `verify_write_origin(headers, origin)`; `GitHub` methods for
dispatch, run, jobs, artifacts, cancel and protection-rule review with injectable
HTTP, clock and signer.

- [x] Tests: Easy Auth role claim validation (wrong tenant, app-only, no role),
  ID token signature/issuer/audience validation, invalid origin rejected, exact
  dispatch arguments with `return_run_details`, narrow installation token
  request, expired token refresh with RS256 assertion, no bearer header on
  cross-origin redirects.
- [x] Implement startup config validation, actor/origin validation, fixed GitHub
  API origin and version, bounded pagination/timeouts, sanitized errors, in-memory
  installation token cache, Key Vault `CryptographyClient` RS256 signing via MI.

## 2. Durable operation store and webhook receipt

**Files:** `dashboard/store.py`, `dashboard/webhook.py`,
`dashboard/tests/test_dashboard_store_webhook.py`.

**Interfaces:** `Inbox.put(rule) -> bool`, `Inbox.rules(run_id)`;
`OperationsTable.reserve/finish/get/by_run/recent/unresolved/claimed`. Tables
`dashboardinbox` and `dashboardops`, partition = repository ID, ETag-guarded
transactions.

- [x] Tests: missing/invalid `X-Hub-Signature-256`, authentic wrong
  repo/install/environment, matching `deployment_protection_rule`, replayed
  delivery ID, oversized payload, competing reservations, repeated rule requests.
- [x] Implement the receiver app (`POST /webhooks/github`, `/healthz` only) with
  bounded raw body and verified event normalization. The receiver has no GitHub
  writer or UI.
- [x] Store GitHub run IDs as decimal text (the Tables SDK encodes Python `int`
  as Int32) and decode in the adapter; regression test included.

## 3. Protected operations

**Files:** `dashboard/operations.py`, `dashboard/server.py`,
`dashboard/tests/test_dashboard_operations.py`.

**Interfaces:** `Operations.request(actor, command)` for deploy / cancel /
regional rollback / demo retire; `Operations.review(actor, operation_id, environment, approved)`;
`POST /api/operations`, `POST /api/operations/{id}/review`; `GET /healthz`.

- [x] Tests: self-approval and role separation, unrelated or already-decided
  gates, duplicate command or concurrent review, unregistered targets,
  active-source rollback blocked, unknown profile/evidence blocks retire, stale
  eligibility rechecked, ambiguous dispatch stays `confirmation_required`, cancel
  does not mark restored, audit intent precedes the GitHub call, no automatic
  replay after a crash.
- [x] Implement typed commands and environment mapping; bind gates to the
  recorded run and operation; recheck run/attempt/status/evidence before writes;
  claim decisions atomically; separate retryable audit attempts from accepted
  review/cancel claims.

## 4. Authoritative reads and workflow evidence

**Files:** `dashboard/observations.py`, `dashboard/evidence.py`,
`dashboard/server.py`, `dashboard/tests/test_dashboard_observations.py`.

**Interfaces:** `DashboardReader.snapshot(actor, run_id, region)`,
`DashboardReader.check(command)`; `GET /api/dashboard?run_id=…&region=…`. Each
source carries its own observation time and availability.

- [x] Tests: reusable-workflow job fixtures (skipped, waiting, running, success),
  exact run/attempt/region evidence names, absent/expired/malformed/oversized
  evidence, partial Azure failure, redacted ENV, wrong-revision HTTP response,
  candidate 100% while baking, replica list independent from sampled responses.
- [x] Implement bounded GitHub reads with a 30-second conditional cache for
  view-only traffic (writes never use the cache), `main`-ref workflow paths,
  explicit Azure zero-weight entries, trusted artifact download with ZIP member
  validation, MI-based Azure REST reads, and probes limited to verified revision
  hosts. Never poll `/work`.

## 5. Production A layout

**Files:** `dashboard/static/index.html`, `dashboard/static/dashboard.css`,
`dashboard/static/dashboard.js`, `dashboard/server.py`,
`dashboard/tests/test_dashboard_http.py`.

- [x] Tests: authenticated/unauthenticated index and API requests, no simulator
  controls, JSON shapes, no credentials in HTML.
- [x] Build the connected rollout rail, region inspection, accessible
  confirmation, action reasons and audit history; DOM text only, no HTML
  interpolation of dynamic content.
- [x] Headless Edge smoke: A layout, approval request, mobile width, independent
  100% traffic and waiting state.

## 6. Deployable packaging and cutover guide

**Files:** `dashboard/Dockerfile`, `infra/dashboard.bicep`,
`manual/07-deployment-dashboard.md`, `manual/08-deployment-dashboard-permissions.md`,
`manual/README.md`.

- [x] One image runs `dashboard.server:app` and `dashboard.webhook:app` as two
  Container Apps; Bicep outputs `consoleUrl` and `githubCallback`.
- [x] Document identities, Key Vault/Table/Blob permissions, Easy Auth token
  store SAS lifecycle, webhook redelivery, manual reconciliation of `reserved`
  operations, key rotation and staged cutover. Identity creation and
  cross-resource-group grants are pre-provisioned (manual 08), not in the template.
- [x] Validate: `python -m unittest discover -s dashboard\tests -v` (25 passed
  at plan completion; 28 passed on `main` 2026-11-22),
  `python -m unittest discover -s tests -v` (113 run, 1 skipped),
  `python -m compileall -q dashboard`, `az bicep build --file infra\dashboard.bicep --stdout`,
  Docker build with both `/healthz` returning 200.

## Changes after the original plan

Recorded 2026-11-22 during nonproduction integration:

- The console validates the Easy Auth token-store ID token
  (`X-MS-TOKEN-AAD-ID-TOKEN`) independently and requires it to match the
  principal header. This makes the Blob token store and its SAS renewal a
  runtime dependency. Decision: keep Easy Auth + SAS.
- The principal `auth_typ` check was removed because ACA did not emit `aad`;
  signature validation and actor matching remain.
- Account display name and role badges were added to the header.

## Not covered by this plan

Live two-user role separation, reject/cancel, console-driven regional rollback
and demo retire, private network validation per installation, and a GitHub-based
emergency approval path. These require separately approved environments and
operators; see the spec acceptance table.
