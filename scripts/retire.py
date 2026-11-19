"""Demo-only: deactivate the previous stable revision after a completed release.

The release pipeline deliberately keeps the old revision active (instant rollback,
forensics). For the zero-downtime demo we want that revision to receive a real
SIGTERM while a long task is running on it, so this script deactivates it on
operator request (.github/workflows/demo-retire.yml).

Safety: it refuses unless the source run's region succeeded, the live traffic is
exactly {candidate: 100}, the app/template has not drifted and the candidate is
ready. After deactivation it keeps proving the candidate still serves /work on
the public FQDN until Azure reports the old revision inactive.

Usage: python scripts/retire.py --snapshot baseline-kr.json --success evidence/kr/success.json
"""
import argparse
import os
import time
from pathlib import Path

from common import (az, candidate_owned, cli_app, guarded_state, iso, load, props, ready, require, revision,
                    save, utcnow, verify_context)
from rollout import request


def retire(snapshot, success, timeout=300, interval=5):
    verify_context(snapshot)
    require(os.environ.get("SOURCE_RUN_ID") == snapshot["run_id"], "Source run mismatch")
    require(success.get("revision") == snapshot["candidate"],
            "Region success evidence does not match this release's candidate")
    stable, candidate = snapshot["stable"], snapshot["candidate"]
    # Also proves the old revision serves 0% of traffic and nothing drifted.
    current, _ = guarded_state(snapshot, {candidate: 100})
    require(ready(candidate_owned(snapshot)), "Candidate not ready; refusing to retire the old revision")
    public_fqdn = props(current)["configuration"]["ingress"]["fqdn"]
    print(f"Deactivating {stable}; {candidate} keeps 100% traffic", flush=True)
    az("containerapp", "revision", "deactivate", *cli_app(), "--revision", stable)
    deadline = time.monotonic() + timeout
    while True:
        require(ready(revision(candidate)), "Candidate became unready during retire")
        request(public_fqdn, "/work", "probe", snapshot["run_id"], candidate)
        if props(revision(stable)).get("active") is False:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Stable still active after {timeout}s; check the revision in Azure")
        time.sleep(interval)
    record = {"retired_at": iso(utcnow()), "source_run": snapshot["run_id"], "stable": stable,
              "candidate": candidate, "verified": True}
    save(Path("evidence") / os.environ["REGION"] / "demo-retire.json", record)
    print(f"Retired {stable}; {candidate} verified on {public_fqdn}", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--success", required=True)
    args = parser.parse_args()
    retire(load(args.snapshot), load(args.success))


if __name__ == "__main__":
    main()
