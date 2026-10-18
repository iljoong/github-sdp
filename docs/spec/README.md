# Safe deployment implementation specifications

## Status and baseline

- Created: 2026-10-18.
- Specification status: **Phase 1 approved and implemented; Phases 2/3 and
  expedited patch/revision cleanup DRAFT**.
- Inspected implementation: `f5f22a75d50daf94296062720c610be61264d44b`.
- Phase 1 implementation and its 30-second saturation deadline, 20-second drain
  budget, and curl transport were approved on 2026-10-18. No deployment or
  infrastructure changes are authorized; Phases 2/3 still require approval.
- Existing implementation is recorded separately from proposed requirements.
- The reviewed baseline passed 49 unit tests. This is historical evidence, not
  evidence that the proposed work has been implemented.

| Phase | Specification | Current state | Prerequisite |
| --- | --- | --- | --- |
| 1 | [Demo load reliability](phase-1-demo-load-reliability.md) | Implemented; automated validation recorded, operational acceptance not run | Approved 2026-10-18 |
| 2 | [Gate orchestration and recovery](phase-2-gate-orchestration.md) | Draft; bounded telemetry retry exists, independent monitoring does not | Phase 1 interfaces verified; approval of Phase 2 |
| 3 | [Production quality and ownership](phase-3-production-hardening.md) | Draft; existing production guards are not production certification | Phase 2 verified; approved service-specific policy |

## Additional operational specifications

The following drafts were requested on 2026-10-18. Documentation approval does
not authorize implementation, deployment, or cleanup. They use `4cad565` as their
inspected baseline; the historical phase baseline and validation above remain
unchanged.

| Specification | Current state | Prerequisite |
| --- | --- | --- |
| [Expedited patch deployment](expedited-patch.md) | Draft; explicit 0% to 100% mode without bake windows, not implemented | Approved eligibility, input, verification, and authorization policy |
| [Guarded revision cleanup](revision-cleanup.md) | Draft; separate manual dry-run-first deactivation workflow, not implemented | Approved retention, durable protection inventory, and mutation policy |

These specifications are not new numbered phases and do not implicitly approve
Phases 2/3. Each records existing evidence, proposed requirements, decisions, and
acceptance criteria using the shared ledger vocabulary.

## Read before implementing

1. Read this index and the applicable phase's implementation ledger.
2. Compare the ledger's baseline with the current branch. Inspect the referenced
   functions and tests; do not assume an old status is still accurate.
3. Search the repository and Git history for the requirement ID and equivalent
   behavior. Reuse existing helpers and tests rather than creating a parallel path.
4. Run the relevant existing tests. Record their names and actual outcomes.
5. If behavior already satisfies a requirement, record the code/test evidence and
   implement only any missing acceptance criteria.
6. Implement approved requirements in dependency order. Keep runtime code and
   tests separate from speculative production architecture.
7. Update the same requirement row with implementation commit, tests, and
   validation evidence. Do not create a second requirement for the same work.

The ledger prevents accidental repeated work through review discipline; it is
not a runtime lock, a release checkpoint, or an automatic idempotency mechanism.

## Ledger vocabulary

| State | Meaning |
| --- | --- |
| `EXISTING` | Behavior is present at the inspected baseline; preserve it |
| `PARTIAL` | Some behavior exists, but listed acceptance criteria remain open |
| `PROPOSED` | Not implemented; approval is required |
| `IN_PROGRESS` | Approved work is actively being implemented |
| `IMPLEMENTED` | Code and relevant automated tests exist; operational verification may remain |
| `VERIFIED` | Required automated and controlled operational acceptance evidence is recorded |
| `BLOCKED` | Named dependency or decision prevents completion |
| `SUPERSEDED` | Replaced by a linked requirement; do not implement again |

An implementation row must include:

```text
Requirement ID:
State:
Inspected/implementation commit:
Code symbols:
Automated tests and command:
Observed result:
Operational run/artifact, or "not run":
Remaining work/blocker:
Last verified date:
```

Do not promote a row to `VERIFIED` based solely on a passing mocked test or an
unrelated successful release. A commit message is supporting evidence, not proof
that every acceptance criterion is met.

## Shared safety invariants

All phases must preserve:

- One image build and immutable digest across regions.
- Uploaded immutable regional baseline before any Azure mutation.
- Candidate copied from the exact stable revision, initially at zero traffic.
- Explicit 10%, 50%, and 100% weights; no `latest` routing.
- Korea Central -> Southeast Asia -> West US release order.
- Protected environment approval and separately protected manual restore.
- Organic business telemetry for production; synthetic traffic must not satisfy it.
- No deploy reruns, blind overwrite of drift, or automatic deletion of revisions.
- Failed deployment remains failed even when rollback succeeds.
- No inference that reverting traffic reverts databases or external side effects.

The additional drafts define narrow proposed extensions, not changes to these
implemented defaults. Patch mode would explicitly replace staged weights and
windowed production gates with direct cutover and smoke verification, recording
that sustained SLOs were not evaluated. Cleanup would deactivate only eligible
unprotected revisions while preserving the active current/previous-LKG pair;
it would not delete revisions or run unconditionally after release. All other
applicable safety and ownership protections remain required.

Primary implementation surfaces:
[rollout](../../scripts/rollout.py),
[metrics](../../scripts/metrics.py),
[state guards](../../scripts/common.py),
[snapshot](../../scripts/snapshot.py),
[regional workflow](../../.github/workflows/region.yml),
[restore workflow](../../.github/workflows/restore-region.yml),
[tests](../../tests/test_safety.py).

## Incident evidence motivating the work

| Release run | Verified observation | Design consequence |
| --- | --- | --- |
| [39088367556](https://github.com/iljoong/github-sdp/actions/runs/39088367556) | SEA candidate had 14 visible samples; later requery of the same window returned 23 | Preserve bounded same-window ingestion retries |
| [39099190623](https://github.com/iljoong/github-sdp/actions/runs/39099190623) | KR candidate remained at 14 samples through 11 queries; 180 total demo samples were recorded | Preserve independent, rate-controlled shared-endpoint load |
| [39100732686](https://github.com/iljoong/github-sdp/actions/runs/39100732686) | SEA 10% passed near 10 RPS; 50% aborted at 32 outstanding requests; all 1,917 submitted requests completed | Replace instantaneous saturation failure with bounded backpressure |

These runs demonstrate specific failure modes, not universal Azure latency
guarantees. Artifact availability depends on repository access and retention.
Store only sanitized evidence summaries here; never store bearer tokens, secrets,
or complete environment dumps.

## Approval and delivery

- Each phase can be approved independently; open decisions must be resolved before
  implementing the affected requirement.
- Phase specifications define desired outcomes, not a mandate to introduce a
  framework, package, or configuration subsystem.
- Add only the smallest shared helpers needed by approved behavior.
- Use deterministic clock/fault-injection tests for deadlines and a small real
  concurrency test for scheduling and shutdown.
- Operational validation uses a nonproduction environment with explicit approval.
- Update [the operator guide](../../INSTRUCTION.md) and
  [the Linux guide](../../INSTRUCTION.linux.md) whenever behavior changes.
- Do not start a release merely because a specification or commit was created.
