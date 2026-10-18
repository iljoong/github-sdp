# Guarded revision cleanup

Status: **DRAFT - documentation approved; implementation not approved**.
Baseline: `4cad565`, inspected 2026-10-18.
Related: [index](README.md), [expedited patch](expedited-patch.md),
[production ownership](phase-3-production-hardening.md).

## 1. Objective and scope

Deactivate obsolete revisions without sacrificing immediate rollback or
interfering with deployment and investigation. Use a separate protected cleanup
workflow, initially manually dispatched with dry-run as the default.

Do not add unconditional deactivation to the release success path. Cleanup
failure must remain visible without turning a successful release into a failed
deployment or triggering its rollback handler.

Deactivate revisions; do not delete them, delete registry images, change
templates, change traffic, alter scaling policy, or reactivate revisions.
Scheduling is a future explicitly approved extension, not an initial behavior.

Zero shared-endpoint traffic alone does not establish that a revision is unused.
Account for direct revision URLs, labels, event processing, and background work.
Initial eligibility is limited to this repository's owned HTTP demonstration
workload, with unsupported workloads/configurations blocked explicitly.

## 2. Existing implementation ledger

| ID | State | Evidence at baseline | Gap |
| --- | --- | --- | --- |
| CLEAN-E01 | EXISTING | [snapshot](../../scripts/snapshot.py) requires a healthy stable revision at 100% and minReplicas >= 1 | Historical active revisions can retain replicas |
| CLEAN-E02 | EXISTING | [restore](../../scripts/rollout.py) deliberately keeps stable and candidate active | No revision cleanup exists |
| CLEAN-E03 | EXISTING | [state guards](../../scripts/common.py) require saved stable to be active and healthy | Restore does not reactivate an inactive LKG |
| CLEAN-E04 | EXISTING | [release](../../.github/workflows/release.yml) and [restore](../../.github/workflows/restore.yml) share production concurrency | Cleanup must join the same serialization |
| CLEAN-E05 | PARTIAL | Baseline artifacts and regional success evidence identify deployment revisions | Durable authoritative retention/protection inventory is not implemented |

No cleanup tests or operational validation are claimed at this baseline.

## 3. Retention model

After successful deployment of C, keep:

| Revision role | Required action |
| --- | --- |
| C, current production | Keep active; never clean while receiving traffic |
| B, baseline LKG captured for successful release C | Keep active for immediate restore of C |
| A and older, with no protection | Eligible only after retention and safety checks |
| Failed candidate | Keep for investigation until retention expires and other protections clear |

Protect B regardless of its age until another verified successful deployment
establishes a new current/LKG pair or an explicitly approved policy replaces
that protection. Do not let a time threshold alone remove the only rollback
target.

The retention interval applies to unprotected revisions. A 24-48 hour interval
was suggested for discussion, not approved. Its exact value and age anchor
(such as recorded retirement from production or failure time) remain open.
Creation time alone is insufficient: a long-lived revision may just have retired.
Missing or untrusted timestamps must prevent eligibility, not default to old.

Retain successful-release evidence, baseline artifacts, and required image
digests for the promised recovery window. Artifact retention is not a guarantee
that arbitrary historical restores remain safe.

## 4. Proposed requirements

| ID | State | Requirement | Acceptance evidence required |
| --- | --- | --- | --- |
| CLEAN-R01 | PROPOSED | Separate protected dry-run-first workflow | Dispatch/default/concurrency tests |
| CLEAN-R02 | PROPOSED | Authoritative inventory and protected set | Provenance and revision-selection tests |
| CLEAN-R03 | PROPOSED | Retention and workload eligibility | Boundary and unsupported-workload tests |
| CLEAN-R04 | PROPOSED | Fresh guarded deactivation and verification | Race/error/partial-completion tests |
| CLEAN-R05 | PROPOSED | Evidence and recovery limitations | Dry-run/report and restore-compatibility tests |

### CLEAN-R01: workflow and authorization

Use main-only manual dispatch with explicit region selection, default dry-run,
and protected environment authorization for mutation. Record requesting actor
and run. Use OIDC and approved minimum permissions for reads/deactivation.

Serialize using `safe-aca-production`, `cancel-in-progress: false`, shared with
normal release, patch, and restore. Never interrupt another production writer
to clean resources. Rerunning must replan from fresh state rather than replay a
stale deactivation list.

### CLEAN-R02: protected set and provenance

Build the plan from current Azure state and verified durable release evidence.
Identify the current successful deployment and the exact baseline LKG captured
for it. Never infer LKG from the second-newest revision or name sorting.

Protect:

- Every revision with nonzero traffic, including unexpected traffic owners.
- The immediate previous LKG for the current successful release.
- Any revision referenced by an unfinished deployment or unresolved recovery.
- The current app template/head revision, even at zero traffic, because existing
  guards depend on its identity and recovery may still be unresolved.
- Explicit incident/forensic holds and any other approved protection.

Run IDs and matching names alone do not prove ownership. Bind repository, app,
region, commit/run, image/template identity, and completion evidence.
Account for successful normal and patch releases without labeling patch smoke
checks as sustained SLO validation.

Missing/conflicting success evidence, ambiguous current/LKG identity, unknown
ownership, unsupported traffic/labels, or unresolved deployment status must
block automated mutation and identify the required operator action.
Define the durable evidence/hold storage and authorization before implementing
this selection; do not invent success from missing data.

### CLEAN-R03: eligibility

Only deactivate an active, owned revision that is outside the protected set,
has zero traffic, has no other supported routing/workload use, and has exceeded
the approved retention interval.

Record an explicit keep/eligible/blocked decision and reason for every inspected
revision. Preserve current-production health and rollback availability.
Never rewrite minReplicas or a stable template to reduce cost; that would change
snapshot hashes and recovery assumptions.

### CLEAN-R04: guarded execution

Immediately before each mutation, refresh traffic, template/head identity,
ownership, current/LKG health, deployment status, holds, and retention evidence.
If the plan is no longer valid, stop rather than silently adopting a different
target. Keep mutations sequential and bounded.

Deactivate only the explicitly verified revision. Re-read and verify that it is
inactive and its replicas have stopped within an approved deadline, while
production traffic and protected revisions remain unchanged.

No broad catch, silent skip, or unconditional success on API failure. On timeout
or an ambiguous write outcome, inspect actual state and report uncertainty.
Stop further deactivations on unexpected drift or failure. Record completed
actions; do not reactivate them automatically as a cleanup rollback.

GitHub concurrency does not coordinate Portal, ad hoc CLI, or external writers.
Fresh checks reduce but do not eliminate guard/write races. Reuse approved
provider-conditional or enforceable single-writer protections if available;
do not claim atomicity without evidence.

### CLEAN-R05: evidence and recovery contract

Persist the plan before mutation and the observed result afterward. Include
source release/baseline references, actor, app/region, policy version, dry-run
flag, protected identities/reasons, eligibility timestamps, actions, API outcomes,
final activity/replica state, and any operator follow-up. Surface evidence-write
failures and keep cleanup failure separate from release success.

Dry-run must make no Azure mutations and clearly state that its plan is not
authorization to replay later. Execution must obtain a fresh plan.

Preserve the active healthy LKG required by existing restore. Deactivation is
not immediate historical deletion, but inactive revisions are subject to Azure
retention and restore currently cannot reactivate them. Recovery beyond the
protected pair requires a separately designed approved procedure, fresh health
checks, and ownership investigation. Do not promise arbitrary historical rollback.

## 5. Acceptance and validation

Required automated cases:

- Default dry-run performs zero mutations and reports selection reasons.
- Current traffic, LKG, head, in-flight candidates, and incident holds are protected.
- A newer failed candidate is never mistaken for the LKG.
- A zero-traffic revision is not automatically eligible.
- Missing/conflicting evidence and unsupported workloads/configuration fail closed.
- Retention boundary, recently retired old revision, missing timestamp, and expired
  incident hold cases follow the approved policy.
- Traffic/protection changes between plan and write prevent deactivation.
- API denial, timeout, runner cancellation, and partial completion preserve
  actionable evidence and never report unconditional success.
- Repeated runs safely replan; already inactive revisions are not new mutations.
- Cleanup preserves the restore path for the current release's baseline.
- Cleanup cannot overlap normal release, patch, or restore.

Controlled nonproduction acceptance requires separate approval: deploy at least
three owned revisions, preview protections, deactivate only an eligible older
revision, verify stopped replicas, and demonstrate restore to the protected LKG.
Record run/artifact IDs. Do not execute cleanup merely to validate this draft.

## 6. Decisions required before implementation

- Retention duration, trusted age anchor, and failed-candidate investigation window.
- Durable successful-release inventory and hold mechanism, including access,
  evidence expiration, and recovery from missing records.
- Protected environment/identity policy and supported workload detection.
- Call/deactivation/replica-stop budgets and partial-failure response.
- Whether and when scheduled cleanup is permitted.

Implementation ledger updates must follow the [index vocabulary](README.md).
Automated tests and operational validation for these requirements: **not run**.
Update both operator guides when cleanup is implemented.
