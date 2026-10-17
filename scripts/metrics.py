"""Closed, non-overlapping, run/time/revision-scoped business SLI windows."""
import math
from datetime import timedelta

from common import az, iso, require

PROFILES = {
    "production": {"bake": 3600, "window": 300, "lag": 120, "samples": 100, "controls": 30},
    "demo": {"bake": 180, "window": 60, "lag": 120, "samples": 20, "controls": 5},
}


def query(snapshot, start, end, workspace):
    # All interpolated strings are validated as identifiers before use.
    app_name = snapshot["app_id"].split("/")[-1]
    stable, candidate, run = snapshot["stable"], snapshot["candidate"], snapshot["run_id"]
    kql = f"""
ContainerAppConsoleLogs_CL
| where TimeGenerated between (datetime({iso(start)}) - 5m .. datetime({iso(end)}) + 30m)
| where ContainerAppName_s == '{app_name}'
| where RevisionName_s in ('{stable}', '{candidate}')
| extend d = parse_json(Log_s)
| where tostring(d.kind) == 'business'
| extend EventTime=todatetime(d.event_time), Revision=tostring(d.revision),
         Source=tostring(d.source), Run=tostring(d.monitor_run),
         Status=toint(d.status), Duration=todouble(d.duration_ms), Ok=tobool(d.business_ok),
         RequestId=tostring(d.request_id)
| where EventTime >= datetime({iso(start)}) and EventTime < datetime({iso(end)})
| where Revision == RevisionName_s and isnotempty(RequestId)
| where Source == 'organic' or (Source in ('demo','control') and Run == '{run}')
| where isnotnull(Status) and isnotnull(Duration) and isnotnull(Ok)
| summarize arg_max(TimeGenerated, *) by RequestId, Revision
| summarize Samples=count(), Errors=countif(Status >= 500 or Status < 200 or Ok == false),
            BusinessFailures=countif(Ok == false), P95=percentile(Duration,95)
            by Revision, Source
"""
    rows = az("monitor", "log-analytics", "query", "--workspace", workspace,
              "--analytics-query", kql, "--timespan", f"{iso(start - timedelta(minutes=5))}/{iso(end + timedelta(minutes=30))}")
    # CLI returns a flat row array (not the REST API's tables/rows envelope).
    require(isinstance(rows, list), "Unexpected Log Analytics response; fail closed")
    return rows, kql


def metric(rows, revision, source, minimum):
    matching = [r for r in rows if r.get("Revision") == revision and r.get("Source") == source]
    require(len(matching) == 1, f"Missing/ambiguous telemetry: {revision}/{source}")
    row = matching[0]
    n = int(row["Samples"])
    errors, failures, p95 = int(row["Errors"]), int(row["BusinessFailures"]), float(row["P95"])
    require(n >= minimum, f"Insufficient samples {revision}/{source}: {n} < {minimum}")
    require(0 <= errors <= n and 0 <= failures <= n and math.isfinite(p95) and p95 >= 0,
            "Invalid telemetry")
    error_rate = max(errors, failures) / n
    require(error_rate <= 0.01, f"Error/business-failure SLO exceeded: {revision}/{source}: {error_rate}")
    require(p95 <= 500, f"p95 SLO exceeded: {revision}/{source}: {p95} ms")
    return {"samples": n, "error_rate": error_rate, "p95": p95}


def gate(rows, snapshot, profile, weight, stable_reference=None):
    settings = PROFILES[profile]
    source = "organic" if profile == "production" else "demo"
    stable, candidate = snapshot["stable"], snapshot["candidate"]
    controls = {rev: metric(rows, rev, "control", settings["controls"]) for rev in (stable, candidate)}
    current = metric(rows, candidate, source, settings["samples"])
    if weight < 100:
        reference = metric(rows, stable, source, settings["samples"])
    else:
        require(stable_reference is not None, "Missing pre-cutover stable business reference")
        reference = stable_reference
    require(current["error_rate"] <= reference["error_rate"] + 0.005, "Candidate error regression")
    require(current["p95"] <= reference["p95"] * 1.5 + 50, "Candidate latency regression")
    return {"candidate": current, "stable_reference": reference, "stable_live_controls": controls[stable],
            "candidate_live_controls": controls[candidate], "weight": weight, "source": source}
