import argparse
import concurrent.futures
import json
import os
import signal
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

from common import (az, candidate_owned, cli_app, equal_traffic, guarded_state, iso, load,
                    props, ready, require, revision, save, set_traffic, utcnow, verify_context)
from metrics import PROFILES, gate, metric, query


def request(fqdn, path, source, run, expected_revision=None):
    request_value = urllib.request.Request(
        f"https://{fqdn}{path}", headers={"X-Safe-Source": source, "X-Safe-Run": run,
                                        "User-Agent": "safe-deployment-example/1"})
    with urllib.request.urlopen(request_value, timeout=10) as response:
        body = json.load(response)
        require(response.status == 200, "Endpoint not healthy")
        if expected_revision:
            require(body.get("revision") == expected_revision, "Request reached wrong revision")
        if path == "/work":
            require(body.get("ok") is True and body.get("result") == 42, "Business contract failed")
        return body


def wait_ready(snapshot):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        guarded_state(snapshot, {snapshot["stable"]: 100})
        value = candidate_owned(snapshot)
        if ready(value):
            fqdn = props(value)["fqdn"]
            request(fqdn, "/healthz", "probe", snapshot["run_id"], snapshot["candidate"])
            request(fqdn, "/work", "probe", snapshot["run_id"], snapshot["candidate"])
            return fqdn
        require(props(value).get("provisioningState") != "Failed", "Candidate provisioning failed")
        time.sleep(10)
    raise RuntimeError("Candidate readiness timed out")


def baseline_preflight(snapshot, profile):
    settings = PROFILES[profile]
    evidence_path = Path("evidence") / os.environ["REGION"] / "baseline-preflight.json"
    record = {"started_at": iso(utcnow()), "profile": profile, "run_id": snapshot["run_id"],
              "stable": snapshot["stable"], "status": "checking"}
    try:
        guarded_state(snapshot, snapshot["traffic"])
        fqdn = props(revision(snapshot["stable"]))["fqdn"]
        request(fqdn, "/healthz", "probe", snapshot["run_id"], snapshot["stable"])
        request(fqdn, "/work", "probe", snapshot["run_id"], snapshot["stable"])
        record["direct_business_contract"] = "passed"
        if profile == "production":
            end = utcnow() - timedelta(seconds=settings["lag"])
            start = end - timedelta(seconds=settings["window"])
            rows, kql = query(snapshot, start, end, os.environ["LOG_ANALYTICS_WORKSPACE_ID"])
            record.update({"start": iso(start), "end": iso(end), "rows": rows, "query": kql})
            record["organic_baseline"] = metric(rows, snapshot["stable"], "organic", settings["samples"])
        else:
            record["limitation"] = "Demo checks direct business probes only, not organic baseline SLO."
        guarded_state(snapshot, snapshot["traffic"])
        record["status"] = "passed"
    except Exception as error:
        record.update({"status": "failed", "error": str(error)})
        save(evidence_path, record)
        raise
    save(evidence_path, record)


def bake(snapshot, profile, weight, expected_weights, stable_reference):
    settings = PROFILES[profile]
    started = utcnow()
    stable_fqdn = props(revision(snapshot["stable"]))["fqdn"]
    candidate_fqdn = props(candidate_owned(snapshot))["fqdn"]
    shared_fqdn = props(guarded_state(snapshot, expected_weights)[0])["configuration"]["ingress"]["fqdn"]
    workspace = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
    next_end = started + timedelta(seconds=settings["window"])
    finish = started + timedelta(seconds=settings["bake"])
    next_control = next_guard = 0.0
    evidence = Path("evidence") / os.environ["REGION"]
    # Demo traffic is deliberately NOT used by the production gate.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        pending = []
        while next_end <= finish:
            now = time.monotonic()
            if now >= next_control:
                for rev, fqdn in [(snapshot["stable"], stable_fqdn), (snapshot["candidate"], candidate_fqdn)]:
                    request(fqdn, "/healthz", "control", snapshot["run_id"], rev)
                    request(fqdn, "/work", "control", snapshot["run_id"], rev)
                next_control = now + 5
            if now >= next_guard:
                guarded_state(snapshot, expected_weights)
                require(ready(candidate_owned(snapshot)), "Candidate health regressed")
                next_guard = now + 60
            if profile == "demo":
                # Reuse no cookies: ACA affinity is disabled in bootstrap.
                pending.append(pool.submit(request, shared_fqdn, "/work", "demo", snapshot["run_id"]))
                if len(pending) >= 20:
                    for future in pending:
                        future.result()
                    pending.clear()
            if utcnow() >= next_end + timedelta(seconds=settings["lag"]):
                begin = next_end - timedelta(seconds=settings["window"])
                rows, kql = query(snapshot, begin, next_end, workspace)
                record = {"start": iso(begin), "end": iso(next_end), "rows": rows, "query": kql,
                          "profile": profile, "run_id": snapshot["run_id"]}
                filename = evidence / f"gate-{weight}-{int(next_end.timestamp())}.json"
                save(filename, record)
                result = gate(rows, snapshot, profile, weight, stable_reference)
                record["result"] = result
                save(filename, record)
                stable_reference = result["stable_reference"]
                print(json.dumps({"window": iso(next_end), "gate": result}), flush=True)
                next_end += timedelta(seconds=settings["window"])
            time.sleep(0.1 if profile == "demo" else 1)
        for future in pending:
            future.result()
    guarded_state(snapshot, expected_weights)
    return stable_reference


def restore(snapshot, evidence_name="rollback"):
    verify_context(snapshot)
    require(snapshot["run_id"] == os.environ.get("SOURCE_RUN_ID", os.environ["GITHUB_RUN_ID"]),
            "Source run mismatch")
    current, weights = guarded_state(snapshot)
    # A not-yet-created candidate is allowed only when the app still matches the
    # captured stable template. Never hide an Azure API error as "not found".
    revisions = az("containerapp", "revision", "list", *cli_app())
    exists = any(r["name"] == snapshot["candidate"] for r in revisions)
    if exists:
        candidate_owned(snapshot)
    else:
        require(equal_traffic(weights, {snapshot["stable"]: 100}), "Missing candidate owns live traffic")
    fqdn = props(revision(snapshot["stable"]))["fqdn"]
    request(fqdn, "/healthz", "probe", snapshot["run_id"], snapshot["stable"])
    request(fqdn, "/work", "probe", snapshot["run_id"], snapshot["stable"])
    set_traffic(snapshot["traffic"])
    # Routing is eventually consistent: verify both ARM and multiple real requests.
    deadline = time.monotonic() + 120
    while True:
        try:
            value, _ = guarded_state(snapshot, snapshot["traffic"])
            public_fqdn = props(value)["configuration"]["ingress"]["fqdn"]
            for _ in range(5):
                request(public_fqdn, "/work", "probe", snapshot["run_id"], snapshot["stable"])
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)
    # Keep BOTH revisions active: no destructive operation, easy forensics.
    save(Path("evidence") / os.environ["REGION"] / f"{evidence_name}.json",
         {"restored_at": iso(utcnow()), "source_run": snapshot["run_id"],
          "stable": snapshot["stable"], "traffic": snapshot["traffic"], "verified": True})
    print(f"Restored exact baseline traffic to {snapshot['stable']}", flush=True)


def cancelled(signum, _frame):
    raise RuntimeError(f"Runner signal {signum}; attempting guarded rollback")


def deploy(snapshot, profile):
    require(os.environ["GITHUB_RUN_ATTEMPT"] == "1", "Deploy reruns forbidden")
    require(snapshot["run_id"] == os.environ["GITHUB_RUN_ID"], "Wrong deployment run")
    verify_context(snapshot)
    # Do not roll back when preflight has not passed: it may be somebody else's drift.
    guarded_state(snapshot, snapshot["traffic"])
    revisions = az("containerapp", "revision", "list", *cli_app())
    require(not any(r["name"] == snapshot["candidate"] for r in revisions),
            "Candidate exists: this run is not safely replayable")
    # Still read-only: unhealthy business/SLO baseline must not create a revision
    # or enter any automatic restore path that would write traffic.
    baseline_preflight(snapshot, profile)
    save(Path("evidence") / os.environ["REGION"] / "mutation-intent.json",
         {"at": iso(utcnow()), "candidate": snapshot["candidate"]})
    try:
        # Before copy, baseline has explicit stable=100, NEVER latest=100.
        # Copy inherits this app-scoped traffic: the new revision starts at 0%.
        az("containerapp", "revision", "copy", *cli_app(),
           "--from-revision", snapshot["stable"], "--container-name", "web",
           "--image", snapshot["image"], "--revision-suffix", f'r{snapshot["run_id"]}',
           "--set-env-vars", f'RELEASE_ID={snapshot["run_id"]}')
        guarded_state(snapshot, {snapshot["stable"]: 100})
        wait_ready(snapshot)
        reference = None
        expected_weights = {snapshot["stable"]: 100}
        for weight in (10, 50, 100):
            guarded_state(snapshot, expected_weights)
            require(ready(candidate_owned(snapshot)), "Candidate not Healthy before promotion")
            expected_weights = {snapshot["stable"]: 100 - weight, snapshot["candidate"]: weight}
            set_traffic(expected_weights)
            # Check routing before starting the new window.
            for attempt in range(12):
                try:
                    guarded_state(snapshot, expected_weights)
                    break
                except Exception:
                    if attempt == 11:
                        raise
                    time.sleep(5)
            reference = bake(snapshot, profile, weight, expected_weights, reference)
        save(Path("evidence") / os.environ["REGION"] / "success.json",
             {"at": iso(utcnow()), "revision": snapshot["candidate"], "image": snapshot["image"],
              "profile": profile, "old_revision_active": True})
    except BaseException as failure:
        # SIGINT/SIGTERM allow best effort only. SIGKILL, host loss and Azure outages
        # require the separate protected manual-restore workflow.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        try:
            save(Path("evidence") / os.environ["REGION"] / "failure.json",
                 {"at": iso(utcnow()), "type": type(failure).__name__, "message": str(failure),
                  "candidate": snapshot["candidate"], "run_id": snapshot["run_id"]})
        except OSError as evidence_error:
            print(f"Cannot persist failure evidence: {evidence_error}", flush=True)
        try:
            restore(snapshot, "automatic-rollback")
        except BaseException as rollback_error:
            print(f"ROLLBACK NOT VERIFIED: {rollback_error}. Run manual restore with original run ID.", flush=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["deploy", "restore"])
    parser.add_argument("--profile", choices=list(PROFILES), default="production")
    parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancelled)
    snapshot = load(args.snapshot)
    if args.operation == "deploy":
        deploy(snapshot, args.profile)
    else:
        restore(snapshot, "manual-rollback")


if __name__ == "__main__":
    main()
