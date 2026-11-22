import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from azure.core.exceptions import AzureError

from .auth import APPROVER, OPERATOR
from .config import REGIONS, positive
from .github import RemoteError
from .operations import InvalidAction, WORKFLOW, review_claim
from .store import stamp

ARM = "https://management.azure.com"
API_VERSION = "2024-03-01"
ENV_KEYS = frozenset({"RELEASE_ID", "GRACE_SECONDS", "LONG_TASK_SECONDS", "LONG_TASK_ABORT_ON_DRAIN"})
JOB_PREFIX = {"kr": "korea", "sea": "southeastasia", "usw": "westus"}
SOURCE_ERRORS = (AzureError, RemoteError, httpx.HTTPError, ValueError, TypeError, KeyError)
TIMELINE_LIMIT = 50
KINDS = frozenset({"deploy", "cancel", "rollback", "retire", "approve", "reject"})


def _env_region(environment):
    region = str(environment or "").rsplit("-", 1)[-1]
    return region if region in JOB_PREFIX else None


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _epoch(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def candidate(target, run_id):
    return f"{target.app_name}--r{positive(run_id)}"


def safe_env(items):
    return {row["name"]: row["value"] for row in items
            if row.get("name") in ENV_KEYS and not row.get("secretRef") and isinstance(row.get("value"), str)
            and re.fullmatch(r"[0-9]{1,19}|[0-9]{1,5}\.[0-9]{1,4}|[YN]", row["value"])}


def runtime_sample(data, revision):
    if data.get("revision") != revision or not isinstance(data.get("replica"), str):
        return None
    return {"replica": data["replica"], "ready": data.get("ready"), "state": data.get("state"),
            "inflight_count": len(data["inflight"]) if isinstance(data.get("inflight"), list) else None,
            "grace_remaining": data.get("graceRemaining"), "long_task_seconds": data.get("longTaskSeconds"),
            "abort_on_drain": data.get("abortOnDrain")}


def job_states(jobs, kind, region=None):
    if kind == "retire":
        names = {region: {"retire"}}
    elif kind == "rollback":
        names = {r: {f"{p} / restore"} for r, p in JOB_PREFIX.items()}
    else:
        names = {"build": {"build / build", "build"}}
        names.update({r: {f"{p} / rollout"} for r, p in JOB_PREFIX.items()})
    result = {}
    for key, matching in names.items():
        rows = [job for job in jobs if job.get("name", "").strip().lower() in matching]
        result[key] = "unknown" if len(rows) > 1 else "not_started"
        if len(rows) == 1:
            result[key] = ((rows[0].get("conclusion") or "unknown") if rows[0].get("status") == "completed"
                           else (rows[0].get("status") or "unknown"))
    return result


def _single_job(jobs, region):
    rows = [job for job in jobs if job.get("name", "").strip().lower() == f"{JOB_PREFIX[region]} / rollout"]
    return rows[0] if len(rows) == 1 else None


def job_progress(jobs, repo, run_id, now):
    """Per-region current step (with start and elapsed seconds), failed step and job log link from release job steps."""
    log = re.compile(rf"https://github\.com/{re.escape(repo)}/actions/runs/{positive(run_id)}/job/[0-9]+", re.IGNORECASE)
    result = {}
    for region in JOB_PREFIX:
        job = _single_job(jobs, region) or {}
        steps = [step for step in job.get("steps") or () if isinstance(step, dict)]
        current = next((step for step in steps if step.get("status") == "in_progress"), None)
        started = _epoch(current.get("started_at")) if current and job.get("status") == "in_progress" else None
        failed = next((step.get("name") for step in steps if step.get("conclusion") in ("failure", "timed_out")), None)
        url = job.get("html_url")
        result[region] = {
            "step": current.get("name") if started is not None else None,
            "started_at": _iso(started) if started is not None else None,
            "elapsed_seconds": int(max(0, now - started)) if started is not None else None,
            "failed_step": failed if job.get("status") == "completed" and job.get("conclusion") != "success" else None,
            "log_url": url if isinstance(url, str) and log.fullmatch(url) else None}
    return result


def traffic_stage(weight):
    """Rollout stage (0/10/50/100) reached by the candidate's Azure traffic weight."""
    if type(weight) is not int:
        return None
    return next(stage for stage in (100, 50, 10, 0) if weight >= stage)


def previous_revision(traffic, desired):
    weighted = sorted((-weight, name) for name, weight in traffic.items() if weight and name != desired)
    return weighted[0][1] if weighted else None


def _revision_name(name, target):
    return isinstance(name, str) and bool(re.fullmatch(re.escape(target.app_name) + r"--[a-z0-9-]{1,70}", name))


def _host(host):
    return isinstance(host, str) and bool(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.azurecontainerapps\.io", host))


class AzureReader:
    def __init__(self, credential, http=None, probe=None):
        self.credential = credential
        self.http = http or httpx.Client(follow_redirects=False, timeout=8, trust_env=False)
        self.probe = probe or httpx.Client(timeout=3, follow_redirects=False, trust_env=False)
        self.probes = ThreadPoolExecutor(max_workers=6)

    def close(self):
        self.probes.shutdown(wait=False)
        self.http.close()
        self.probe.close()

    def _arm(self, path):
        url = f"{ARM}{path}?api-version={API_VERSION}"
        token = self.credential.get_token(f"{ARM}/.default").token
        rows = []
        for _ in range(10):
            response = self.http.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            result = response.json()
            if "value" not in result:
                return result
            rows.extend(result["value"])
            url = result.get("nextLink")
            if not url:
                return rows
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "management.azure.com" or parsed.path.lower() != path.lower():
                raise ValueError("Unexpected Azure pagination URL")
        raise ValueError("Azure result exceeds pagination limit")

    def region(self, target, run_id, detail=False, baseline=None):
        app = self._arm(target.resource_id)["properties"]
        rows = self._arm(target.resource_id + "/revisions")
        revisions = {row["name"]: row["properties"] for row in rows if _revision_name(row.get("name"), target)}
        traffic = {}
        for row in app["configuration"]["ingress"]["traffic"]:
            name, weight = row.get("revisionName"), row.get("weight")
            if row.get("latestRevision") or not _revision_name(name, target) or type(weight) is not int or not 0 <= weight <= 100:
                raise ValueError("Unexpected traffic routing")
            traffic[name] = traffic.get(name, 0) + weight
        if sum(traffic.values()) != 100:
            raise ValueError("Incomplete traffic routing")
        desired = candidate(target, run_id) if run_id else None
        result = {"observed_at": stamp(), "traffic": traffic, "candidate": desired,
                  "candidate_weight": traffic.get(desired, 0) if desired else None}
        if not detail:
            return result
        # Without verified evidence (run still in progress), label the previous revision from live traffic.
        stable = baseline["stable"] if baseline else previous_revision(traffic, desired)
        chosen = dict.fromkeys([stable, desired, *traffic])
        result["baseline"] = stable
        result["revisions"] = []
        for name in chosen:
            if name not in revisions:
                continue
            fields = revisions[name]
            template = fields.get("template") or {}
            row = {"name": name, "active": fields.get("active"), "health": fields.get("healthState"),
                   "running": fields.get("runningState"), "provisioning": fields.get("provisioningState"),
                   "replica_count": fields.get("replicas"), "scale": {key: (template.get("scale") or {}).get(key)
                                                                    for key in ("minReplicas", "maxReplicas")},
                   "env": safe_env([env for c in template.get("containers", []) for env in c.get("env", [])])}
            result["revisions"].append(row)
            if fields.get("active") is not True:
                continue
            try:
                row["replicas"] = [{"name": replica["name"], "containers": [
                    {key: c.get(key) for key in ("name", "ready", "restartCount", "runningState")}
                    for c in replica.get("properties", {}).get("containers", [])]}
                    for replica in self._arm(f"{target.resource_id}/revisions/{name}/replicas")]
            except SOURCE_ERRORS:
                row["replicas"] = None
            row["sample"] = self._sample(fields.get("fqdn"), name)
        return result

    def _sample(self, host, revision):
        if not _host(host) or not host.startswith(revision + "."):
            return None
        try:
            status, version = (self.probes.submit(self.probe.get, f"https://{host}{path}")
                               for path in ("/status", "/api/version"))
            status, version = status.result(), version.result()
            status.raise_for_status()
            version.raise_for_status()
            sample = runtime_sample(status.json(), revision)
            ver = version.json()
            if sample is not None:
                if ver.get("revision") == revision:
                    sample["version"] = ver.get("version")
                    sample["version_replica"] = ver.get("replica")
                sample["observed_at"] = stamp()
            return sample
        except SOURCE_ERRORS:
            return None


def source(now, observed, stale_after, reason=None):
    """Freshness of one snapshot source; observed is epoch seconds or None when the source produced nothing."""
    if observed is None:
        return {"observed_at": None, "age_seconds": None, "state": "unavailable", "reason": reason or "upstream_error"}
    age = max(0, now - observed)
    return {"observed_at": _iso(observed), "age_seconds": int(age), "state": "stale" if age > stale_after else "fresh"}


class DashboardReader:
    def __init__(self, shared, targets, github, evidence, inbox, store, azure=None, cache=None, clock=time.time,
                 stale_after=15):
        self.shared, self.targets, self.github = shared, targets, github
        self.evidence, self.inbox, self.store, self.azure = evidence, inbox, store, azure
        # cache: the display ReadCache (observation ages and rate-limit back-off); None for uncached readers.
        self.cache, self.clock, self.stale_after = cache, clock, stale_after

    def _run_matches(self, run, run_id, workflow):
        path = f".github/workflows/{workflow}"
        return (run.get("id") == run_id and run.get("path") in (path, f"{path}@main", f"{path}@refs/heads/main")
                and run.get("head_branch") == "main" and run.get("event") == "workflow_dispatch"
                and (run.get("repository") or {}).get("id") == self.shared.repo_id)

    def gate(self, operation, env):
        run_id = operation["run_id"]
        workflow = WORKFLOW.get(operation["kind"])
        if workflow is None:
            return False
        region = env.rsplit("-", 1)[-1]
        expected = f"prod-rollback-{region}" if operation["kind"] == "rollback" else f"prod-{region}"
        if env != expected or (operation["kind"] != "deploy" and operation.get("region") != region):
            return False
        run = self.github.run(run_id)
        if (not self._run_matches(run, run_id, workflow) or run.get("run_attempt") != 1
                or run.get("status") == "completed"):
            return False
        return self._waiting_gate(operation, env, run)

    def _waiting_gate(self, operation, env, run):
        region = env.rsplit("-", 1)[-1]
        rows = self.inbox.rules(run["id"])
        if not any(row["environment"] == env and row["sha"] == run["head_sha"] for row in rows):
            return False
        jobs = self.github.jobs(run["id"], 1)
        # A custom-rule request can exist before a runner starts. Refuse states
        # known to be running or completed; use the signed rule request for the gate.
        return job_states(jobs, operation["kind"], operation.get("region")).get(region) in {"waiting", "pending"}

    def baseline(self, run, region, fresh=False):
        run_id = run["id"]
        data = self.evidence.file(run_id, f"baseline-{run_id}-{region}", f"baseline-{region}.json", fresh=fresh)
        target = self.targets[region]
        if (not data or data.get("run_id") != str(run_id) or str(data.get("repository", "")).lower() != self.shared.repo
                or data.get("region") != region or data.get("commit") != run.get("head_sha")
                or str(data.get("app_id", "")).lower() != target.resource_id.lower()
                or data.get("candidate") != candidate(target, run_id) or not _revision_name(data.get("stable"), target)):
            return None
        traffic = data.get("traffic")
        if (not isinstance(traffic, dict)
                or any(not _revision_name(name, target) or type(weight) is not int or not 0 <= weight <= 100
                       for name, weight in traffic.items())
                or {name: weight for name, weight in traffic.items() if weight} != {data["stable"]: 100}):
            return None
        return {"stable": data["stable"], "candidate": data["candidate"]}

    def _success(self, run, region, baseline, fresh=False):
        run_id = run["id"]
        data = self.evidence.file(run_id, f"evidence-{run_id}-{region}-1", f"{region}/success.json", fresh=fresh)
        return data if data and data.get("revision") == baseline["candidate"] else {}

    def recovery(self, command, run, baseline, azure, fresh=False):
        if run.get("status") != "completed" or run.get("run_attempt") != 1:
            return "The original release must be completed on its first attempt"
        if not baseline:
            return "The deployment baseline is unavailable, expired or unverified"
        rows = {r["name"]: r for r in azure.get("revisions", [])}
        stable = rows.get(baseline["stable"], {})
        if stable.get("active") is not True or stable.get("health") != "Healthy":
            return "The baseline revision is inactive, unhealthy or unverified"
        if any(name not in baseline.values() for name, weight in azure.get("traffic", {}).items() if weight):
            return "Current traffic belongs to a different release"
        if command == "rollback":
            if azure.get("traffic", {}).get(baseline["stable"]) == 100:
                return "The baseline already receives all traffic"
            return None
        success = self._success(run, azure["region"], baseline, fresh)
        if success.get("profile") != "demo" or azure.get("candidate_weight") != 100:
            return "Retire requires verified demo evidence and candidate traffic at 100%"
        if rows.get(baseline["candidate"], {}).get("health") != "Healthy":
            return "The candidate is not verified healthy"
        return None

    def _active(self):
        return bool(self.github.active_runs())

    def check(self, command):
        if command.action != "cancel" and self._active():
            raise InvalidAction("Another protected workflow has not completed")
        if command.action == "deploy":
            return
        run = self.github.run(command.source_run_id)
        if not self._run_matches(run, command.source_run_id, "release.yml"):
            raise InvalidAction("Select an original main release")
        if command.action == "cancel":
            if run.get("status") == "completed":
                raise InvalidAction("This release has already completed")
            return
        if run.get("status") != "completed" or run.get("run_attempt") != 1:
            raise InvalidAction("The original release must be completed on its first attempt")
        region = command.region
        # Write-time eligibility never trusts cached evidence.
        baseline = self.baseline(run, region, fresh=True)
        if not baseline:
            raise InvalidAction("The deployment baseline is unavailable, expired or unverified")
        azure = self.azure.region(self.targets[region], run["id"], True, baseline)
        reason = self.recovery(command.action, run, baseline, {**azure, "region": region}, fresh=True)
        if reason:
            raise InvalidAction(reason)

    def _view_run(self, run):
        summary = {k: run.get(k) for k in ("id", "status", "conclusion", "head_sha", "run_attempt", "created_at")}
        summary["url"] = f"https://github.com/{self.shared.repo}/actions/runs/{positive(run['id'])}"
        return summary

    def _regional(self, name, run, selected, baseline):
        try:
            return self.azure.region(self.targets[name], run["id"] if run else None, name == selected, baseline)
        except SOURCE_ERRORS:
            return {"unavailable": True, "observed_at": stamp()}

    def snapshot(self, actor, run_id=None, region="kr"):
        if region not in REGIONS:
            raise InvalidAction("Unknown region")
        now = self.clock()
        with self.cache.track() if self.cache else nullcontext() as seen:
            result = self._snapshot(actor, run_id, region, now)
        sources = {"github": self._github_source(now, result["github_available"], seen),
                   "audit": source(now, now if result.pop("_audit_read") else None, self.stale_after)}
        for name in REGIONS:
            observation = result["regions"][name]
            sources[f"azure:{name}"] = source(
                now, None if observation.get("unavailable") else _epoch(observation.get("observed_at")), self.stale_after)
            for revision in observation.get("revisions") or ():
                if "sample" in revision:
                    sample = revision["sample"]
                    sources[f"runtime:{revision['name']}"] = source(
                        now, _epoch(sample.get("observed_at")) if sample else None, self.stale_after, "not_produced")
        result["sources"] = sources
        return result

    def _github_source(self, now, available, seen):
        backoff = self.cache.backoff_remaining() if self.cache else 0
        if not available:
            entry = source(now, None, 0, "rate_limited" if backoff else "upstream_error")
        else:
            age = seen.age if seen is not None and seen.age is not None else 0
            # Reads are served from cache within their lifetime; only data older than that is stale.
            entry = source(now, now - age, (seen.ttl if seen is not None else 0) + self.stale_after)
            if backoff:
                entry["reason"] = "rate_limited"
        if backoff:
            entry["retry_after_seconds"] = math.ceil(backoff)
        return entry

    def _snapshot(self, actor, run_id, region, now):
        gh_available, audit_available = True, True
        run, baseline, current, states, audit, progress = None, None, None, {}, [], {}
        recent, pending, restored, retired, cancelled = [], [], None, None, False
        active_runs, inbox, jobs, inbox_truncated = [], [], [], False
        try:
            recent = [row for row in self.github.runs(recent=True)
                      if self._run_matches(row, row.get("id"), "release.yml")]
            if run_id:
                run = self.github.run(run_id)
                if not self._run_matches(run, run_id, "release.yml"):
                    raise InvalidAction("Select an original main release")
            elif recent:
                run = next((row for row in recent if row.get("status") != "completed"), recent[0])
            if run:
                jobs = self.github.jobs(run["id"], run["run_attempt"])
                states = job_states(jobs, "deploy")
                progress = job_progress(jobs, self.shared.repo, run["id"], now)
                # Evidence cannot change any action until the source run completes.
                if run.get("run_attempt") == 1 and run.get("status") == "completed":
                    baseline = self.baseline(run, region)
            active_runs = self.github.active_runs()
        except SOURCE_ERRORS:
            gh_available = False

        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks = {r: pool.submit(self._regional, r, run, region, baseline if r == region else None)
                     for r in REGIONS}
            regions = {r: {"job": states.get(r, "unknown"), **work.result()} for r, work in tasks.items()}
        empty = dict.fromkeys(("step", "started_at", "elapsed_seconds", "failed_step", "log_url"))
        for name, observation in regions.items():
            observation["progress"] = progress.get(name, dict(empty))
            observation["traffic_stage"] = traffic_stage(observation.get("candidate_weight"))
            observation["app"] = self.targets[name].app_name

        audit_read = False
        try:
            audit = self.store.recent()
            unresolved = self.store.unresolved()
            if run:
                cancelled = self.store.claimed(f"cancel:{run['id']}")
                current = self.store.by_run(run["id"])
            audit_read = True
        except SOURCE_ERRORS:
            audit, current, cancelled = [], None, False
        try:
            if not audit_read:
                raise ValueError("Audit store unavailable")
            if run:
                if current and current["kind"] == "deploy":
                    pending.extend(self._pending_view(actor, current, unresolved))
                for op in audit:
                    if op.get("kind") not in {"rollback", "retire"} or op.get("source_run_id") != run["id"] or not op.get("run_id"):
                        continue
                    pending.extend(self._pending_view(actor, op, unresolved))
                    if op.get("region") != region or not baseline:
                        continue
                    artifact_id = op["run_id"]
                    if op["kind"] == "rollback" and restored is None:
                        ev = self.evidence.file(artifact_id, f"restore-evidence-{artifact_id}-{region}-1", f"{region}/rollback.json")
                        restored = bool(ev and baseline and ev.get("verified") is True
                                        and ev.get("source_run") == str(run["id"]) and ev.get("stable") == baseline["stable"]
                                        and regions[region].get("traffic", {}).get(baseline["stable"]) == 100)
                    if op["kind"] == "retire" and retired is None:
                        ev = self.evidence.file(artifact_id, f"demo-retire-evidence-{artifact_id}-{region}", f"{region}/demo-retire.json")
                        retired = bool(ev and baseline and ev.get("verified") is True and ev.get("stable") == baseline["stable"]
                                       and ev.get("source_run") == str(run["id"]) and regions[region].get("candidate_weight") == 100
                                       and any(r["name"] == baseline["stable"] and r["active"] is False
                                               for r in regions[region].get("revisions", [])))
            inbox, inbox_truncated = self._inbox(actor, pending, active_runs, run, unresolved)
        except SOURCE_ERRORS:
            audit_available, unresolved = False, True
            pending, inbox, inbox_truncated = [], [], False

        active = bool(active_runs) if gh_available else True

        def blocked(action):
            if OPERATOR not in actor.roles:
                return "ReleaseOperator role required"
            if unresolved or not gh_available:
                return "A write is unresolved or GitHub state is unavailable"
            if action != "cancel" and active:
                return "A protected workflow is still active"
            return None

        def eligible(action):
            if action == "deploy":
                return None
            if action == "cancel":
                return ("A cancellation was already accepted" if cancelled else
                        None if run and run.get("status") != "completed" else "No active release selected")
            if not run:
                return "Select an original completed release"
            try:
                return self.recovery(action, run, baseline, {**regions[region], "region": region})
            except SOURCE_ERRORS:
                return "Recovery evidence is unavailable"

        reasons = {action: blocked(action) or eligible(action) for action in ("deploy", "cancel", "rollback", "retire")}
        public = ("operation_id", "kind", "status", "created_at", "actor", "actor_name", "source_run_id", "run_id",
                  "environment", "region")
        ops = [*audit, *([current] if current else [])]
        return {"run": ({**self._view_run(run), "profile": current.get("profile") if current else None} if run else None),
                "runs": [self._view_run(row) for row in recent[:20]], "regions": regions, "build": states.get("build", "unknown"),
                "history": self._history(recent[:20], run, ops, reasons, blocked, now),
                "timeline": self._timeline(run, jobs, ops) if run else [],
                "pending": pending, "inbox": inbox, "inbox_truncated": inbox_truncated, "actions": reasons, "audit": [{k: row[k] for k in public if k in row} for row in audit],
                "restored": restored is True, "retired": retired is True, "observed_at": stamp(),
                "github_available": gh_available, "audit_available": audit_available, "_audit_read": audit_read}

    def _history(self, rows, selected, ops, reasons, blocked, now):
        """Release history entries; recovery reasons are fully evaluated only for the selected release."""
        owners = {}
        for op in ops:
            if op.get("kind") == "deploy" and op.get("run_id"):
                owners.setdefault(op["run_id"], op)
        result = []
        for row in rows:
            op = owners.get(row["id"], {})
            start = _epoch(row.get("run_started_at") or row.get("created_at"))
            end = _epoch(row.get("updated_at")) if row.get("status") == "completed" else now
            actions = {}
            for action in ("rollback", "retire"):
                if selected and row["id"] == selected["id"]:
                    actions[action] = reasons[action]
                elif row.get("status") != "completed" or row.get("run_attempt") != 1:
                    actions[action] = blocked(action) or "The original release must be completed on its first attempt"
                else:
                    actions[action] = blocked(action) or "Select this release to verify its recovery evidence"
            result.append({**self._view_run(row), "started_at": _iso(start) if start is not None else None,
                           "profile": op.get("profile"), "commit": row.get("head_sha"),
                           "requester": op.get("actor_name"),
                           "duration_seconds": int(max(0, end - start)) if start is not None and end is not None else None,
                           "actions": actions})
        return result

    def _timeline(self, run, jobs, ops, limit=TIMELINE_LIMIT):
        """Bounded, time-ordered merge of job transitions, approval requests and console audit for one run."""
        events = []

        def add(at, kind, name=None, status=None, environment=None, region=None, op=None):
            moment = _epoch(at)
            if moment is not None:
                events.append((moment, {"at": _iso(moment), "kind": kind, "name": name, "status": status,
                                        "environment": environment, "region": region,
                                        "actor": (op or {}).get("actor"), "actor_name": (op or {}).get("actor_name")}))

        regions = {prefix: region for region, prefix in JOB_PREFIX.items()}
        for job in jobs:
            name = str(job.get("name", ""))
            region = regions.get(name.split(" / ", 1)[0].strip().lower())
            add(job.get("started_at"), "job_started", name, region=region)
            if job.get("status") == "completed":
                add(job.get("completed_at"), "job_completed", name, job.get("conclusion"), region=region)
        try:
            rules = self.inbox.rules(run["id"])
        except SOURCE_ERRORS:
            rules = []
        for rule in rules:
            env = rule.get("environment")
            add(rule.get("received_at"), "approval_requested", environment=env, region=_env_region(env))
        seen = set()
        for op in ops:
            if (op.get("operation_id") in seen or op.get("kind") not in KINDS
                    or run["id"] not in (op.get("run_id"), op.get("source_run_id"))):
                continue
            seen.add(op.get("operation_id"))
            env = op.get("environment")
            add(op.get("created_at"), op["kind"], status=op.get("status"), environment=env,
                region=op.get("region") or _env_region(env), op=op)
        events.sort(key=lambda event: event[0])
        return [event for _, event in events[-limit:]]

    def _inbox(self, actor, pending, active_runs, selected, unresolved, limit=10):
        """Waiting gates of the selected run plus those of up to `limit` other active console-started runs.

        Returns (gates, truncated); `truncated` is set when more active console runs exist than are listed.
        """
        gates = list(pending)
        known = {(gate["run_id"], gate["environment"]) for gate in gates}
        started = []
        for item in active_runs:
            if not isinstance(item.get("id"), int) or (selected and item["id"] == selected["id"]):
                continue
            op = self.store.by_run(item["id"])
            if op and op.get("kind") in WORKFLOW and op.get("run_id") == item["id"]:
                started.append(op)
        for op in started[:limit]:
            for gate in self._pending_view(actor, op, unresolved):
                if (gate["run_id"], gate["environment"]) not in known:
                    known.add((gate["run_id"], gate["environment"]))
                    gates.append(gate)
        return gates, len(started) > limit

    def _pending_view(self, actor, op, unresolved):
        if op.get("status") != "accepted":
            return []
        result = []
        for region in REGIONS if op["kind"] == "deploy" else (op["region"],):
            env = f"prod-{'rollback-' if op['kind'] == 'rollback' else ''}{region}"
            if self.store.claimed(review_claim(op["run_id"], env)) or not self.gate(op, env):
                continue
            reason = ("An unrelated write is unresolved" if unresolved else
                      "ReleaseApprover role required" if APPROVER not in actor.roles else
                      "A different Entra approver is required" if actor.object_id == op["actor"] else None)
            result.append({"operation_id": op["operation_id"], "run_id": op["run_id"], "kind": op["kind"],
                           "region": region, "environment": env, "reason": reason,
                           "source_run_id": op.get("source_run_id")})
        return result
