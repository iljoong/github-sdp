import base64
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime

from .config import ENVIRONMENTS, REGIONS, positive, repository

API = "https://api.github.com"
WORKFLOWS = frozenset({"release.yml", "restore.yml", "demo-retire.yml"})
OBSERVED_WORKFLOWS = WORKFLOWS | {"release-kr.yml"}
RUN_PATH = re.compile(r"/actions/runs/(\d+)(/[^?]*)?(?:\?|$)")
RUN_LIST = re.compile(r"/actions/(?:workflows/[^/?]+/)?runs(?:\?|$)")


class RemoteError(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"GitHub request failed ({status or 'unconfirmed'})")


_client, _client_lock = None, threading.Lock()


def _shared_client():
    global _client
    import httpx

    with _client_lock:
        if _client is None:
            # Long-lived so connections (TLS sessions) are reused; strict: no redirects, no proxy environment.
            _client = httpx.Client(timeout=12, follow_redirects=False, trust_env=False)
        return _client


def close_transport():
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def transport(method, url, headers, body):
    import httpx

    try:
        response = _shared_client().request(method, url, headers=headers, json=body)
    except httpx.HTTPError:
        raise RemoteError() from None
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, response.headers, data


class ReadCache:
    """GET cache whose lifetime follows the observed run status (short while active, long once completed)."""

    def __init__(self, send=transport, clock=time.monotonic, active_ttl=5, completed_ttl=300, limit=512):
        self.send, self.clock = send, clock
        self.active_ttl, self.completed_ttl, self.limit = active_ttl, completed_ttl, limit
        self.cache, self.lock = OrderedDict(), threading.Lock()
        self.status, self.flights = OrderedDict(), {}
        self.retry_at = 0
        self.local = threading.local()

    @contextmanager
    def track(self):
        """Report the age of the oldest GitHub read served to this thread inside the block."""
        seen, previous = Observed(), getattr(self.local, "seen", None)
        self.local.seen = seen
        try:
            yield seen
        finally:
            self.local.seen = previous

    def _served(self, fetched, ttl):
        seen = getattr(self.local, "seen", None)
        if seen is not None:
            seen.add(self.clock() - fetched, ttl)

    def _remember(self, run_id, status):
        self.status[run_id] = status
        self.status.move_to_end(run_id)
        if len(self.status) > self.limit:
            self.status.popitem(last=False)

    def _ttl(self, url, body):
        match = RUN_PATH.search(url)
        if not match:
            return self.active_ttl
        run_id = int(match.group(1))
        if not match.group(2) and isinstance(body, dict) and isinstance(body.get("status"), str):
            self._remember(run_id, body["status"])
        return self.completed_ttl if self.status.get(run_id) == "completed" else self.active_ttl

    def _affected(self, url, run_id):
        match = RUN_PATH.search(url)
        return bool(RUN_LIST.search(url) or (match and run_id is not None and int(match.group(1)) == int(run_id)))

    def invalidate(self, run_id=None):
        """Expire cached reads of a run (and all run lists) after a console write or change hint.

        Entries keep their ETag so the next read is a conditional request; reads already in flight
        are detached so their (possibly older) result is not stored as fresh.
        """
        with self.lock:
            if run_id is not None:
                self.status.pop(int(run_id), None)
            for url, (expires, *entry) in self.cache.items():
                if self._affected(url, run_id):
                    self.cache[url] = (float("-inf"), *entry)
            for url in [url for url in self.flights if self._affected(url, run_id)]:
                del self.flights[url]

    def backoff_remaining(self):
        """Seconds until GitHub reads are attempted again after a rate-limit response (0 when not limited)."""
        with self.lock:
            return max(0, self.retry_at - self.clock())

    def __call__(self, method, url, headers, body):
        if method != "GET":
            raise ValueError("Observer only supports GET")
        with self.lock:
            now = self.clock()
            cached = self.cache.get(url)
            if cached and now < cached[0]:
                self.cache.move_to_end(url)
                self._served(cached[2], cached[3])
                return cached[1]
            if now < self.retry_at:
                raise RemoteError(429)
            flight = self.flights.get(url)
            leader = flight is None
            if leader:
                flight = self.flights[url] = _Flight()
                if cached and cached[1][1].get("etag"):
                    headers = {**headers, "If-None-Match": cached[1][1]["etag"]}
        if not leader:
            result = flight.join()
            if result[0] in (200, 304):
                self._served(self.clock(), self.active_ttl)
            return result
        try:
            result = self.send(method, url, headers, body)
        except BaseException as error:
            with self.lock:
                if self.flights.get(url) is flight:
                    del self.flights[url]
            flight.fail(error)
            raise
        status, h, body = result
        with self.lock:
            current = self.flights.get(url) is flight
            if current:
                del self.flights[url]
            if status == 304 and cached:
                result, status = cached[1], 200
            if status == 200:
                now, ttl = self.clock(), self._ttl(url, result[2]) if current else self.active_ttl
                self._served(now, ttl)
                if current:
                    self.cache[url] = (now + ttl, result, now, ttl)
                    self.cache.move_to_end(url)
                    if len(self.cache) > self.limit:
                        self.cache.popitem(last=False)
            elif status == 429 or (status == 403 and (
                    h.get("retry-after") is not None or h.get("x-ratelimit-remaining") == "0"
                    or (isinstance(body, dict) and "rate limit" in str(body.get("message", "")).lower()))):
                try:
                    delay = (int(h["retry-after"]) if "retry-after" in h else
                             int(h["x-ratelimit-reset"]) - time.time() if h.get("x-ratelimit-remaining") == "0" else 60)
                except (KeyError, ValueError):
                    delay = 60
                self.retry_at = self.clock() + max(1, delay)
        flight.finish(result)
        return result


class Observed:
    """Oldest served read age (seconds) and the longest cache lifetime that applied to it."""

    def __init__(self):
        self.age, self.ttl = None, 0

    def add(self, age, ttl):
        self.age = max(age, self.age or 0)
        self.ttl = max(ttl, self.ttl)


class _Flight:
    def __init__(self):
        self.done, self.result, self.error = threading.Event(), None, None

    def finish(self, result):
        self.result = result
        self.done.set()

    def fail(self, error):
        self.error = error
        self.done.set()

    def join(self, timeout=30):
        if not self.done.wait(timeout) or self.error is not None:
            raise RemoteError(getattr(self.error, "status", None))
        return self.result


def headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10"}


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


class Tokens:
    def __init__(self, client_id, installation_id, repo_id, sign, send=transport, clock=time.time):
        self.client_id, self.installation_id, self.repo_id = client_id, positive(installation_id), positive(repo_id)
        self.sign, self.send, self.clock = sign, send, clock
        self.lock = threading.Lock()
        self.token, self.expires = "", 0

    def __call__(self):
        with self.lock:
            now = int(self.clock())
            if self.expires > now + 60:
                return self.token
            claims = {"iss": self.client_id, "iat": now - 60, "exp": now + 480}
            text = b64(b'{"alg":"RS256","typ":"JWT"}') + b"." + b64(json.dumps(claims).encode())
            jwt = (text + b"." + b64(self.sign(hashlib.sha256(text).digest()))).decode()
            status, _, value = self.send("POST", f"{API}/app/installations/{self.installation_id}/access_tokens",
                                         headers(jwt), {"repository_ids": [self.repo_id], "permissions":
                                                        {"actions": "write", "deployments": "write"}})
            if status != 201:
                raise RemoteError(status)
            try:
                token = value["token"]
                expires = datetime.fromisoformat(value["expires_at"]).timestamp()
                if not isinstance(token, str) or not token or expires <= now:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise RemoteError() from None
            self.token, self.expires = token, expires
            return token


class GitHub:
    def __init__(self, repo, token, send=transport):
        self.repo, self.token, self.send = repository(repo), token, send

    def _call(self, method, path, body=None, expected=(200,)):
        status, response_headers, value = self.send(method, f"{API}/repos/{self.repo}/actions{path}",
                                                    headers(self.token()), body)
        if status not in expected:
            raise RemoteError(status)
        return response_headers, value

    def dispatch(self, workflow, inputs):
        if workflow not in WORKFLOWS:
            raise ValueError("Workflow not allowed")
        if workflow == "release.yml":
            if set(inputs) != {"profile"} or inputs["profile"] not in ("demo", "production"):
                raise ValueError("Invalid release profile")
        else:
            if set(inputs) != {"region", "source_run_id"} or inputs["region"] not in REGIONS:
                raise ValueError("Invalid recovery target")
            positive(inputs["source_run_id"])
        _, value = self._call("POST", f"/workflows/{workflow}/dispatches",
                              {"ref": "main", "inputs": inputs, "return_run_details": True})
        try:
            return positive(value["workflow_run_id"])
        except (KeyError, TypeError, ValueError):
            raise RemoteError() from None

    def run(self, run_id):
        return self._call("GET", f"/runs/{positive(run_id)}")[1]

    def _pages(self, path, key, initial="?", pages=5):
        rows = []
        for page in range(1, pages + 1):
            _, data = self._call("GET", f"{path}{initial}per_page=100&page={page}")
            try:
                batch = data[key]
                if not isinstance(batch, list):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise RemoteError() from None
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        return rows

    def runs(self, workflow="release.yml", recent=False):
        if workflow not in OBSERVED_WORKFLOWS:
            raise ValueError("Workflow not allowed")
        return self._pages(f"/workflows/{workflow}/runs?branch=main&event=workflow_dispatch", "workflow_runs",
                           "&", pages=1 if recent else 5)

    def active_runs(self):
        """One repository-wide query for unfinished main dispatch runs of the observed workflows."""
        paths = {f".github/workflows/{name}" for name in OBSERVED_WORKFLOWS}
        return [run for run in self._pages("/runs?branch=main&event=workflow_dispatch", "workflow_runs", "&", pages=1)
                if run.get("status") != "completed" and str(run.get("path", "")).split("@", 1)[0] in paths]

    def jobs(self, run_id, attempt):
        return self._pages(f"/runs/{positive(run_id)}/attempts/{positive(attempt)}/jobs", "jobs")

    def artifacts(self, run_id):
        return self._pages(f"/runs/{positive(run_id)}/artifacts", "artifacts")

    def artifact_location(self, artifact_id):
        h, _ = self._call("GET", f"/artifacts/{positive(artifact_id)}/zip", expected=(302,))
        return h.get("location")

    def pending(self, run_id):
        return self._call("GET", f"/runs/{positive(run_id)}/pending_deployments")[1]

    def cancel(self, run_id):
        self._call("POST", f"/runs/{positive(run_id)}/cancel", expected=(202,))

    def review_rule(self, run_id, environment, approved, comment):
        if environment not in ENVIRONMENTS:
            raise ValueError("Environment not allowed")
        self._call("POST", f"/runs/{positive(run_id)}/deployment_protection_rule",
                   {"environment_name": environment, "state": "approved" if approved else "rejected",
                    "comment": comment}, expected=(204,))
