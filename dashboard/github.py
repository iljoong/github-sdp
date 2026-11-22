import base64
import hashlib
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime

from .config import ENVIRONMENTS, REGIONS, positive, repository

API = "https://api.github.com"
WORKFLOWS = frozenset({"release.yml", "restore.yml", "demo-retire.yml"})
OBSERVED_WORKFLOWS = WORKFLOWS | {"release-kr.yml"}


class RemoteError(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"GitHub request failed ({status or 'unconfirmed'})")


def transport(method, url, headers, body):
    import httpx

    try:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
            response = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError:
        raise RemoteError() from None
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, response.headers, data


class ReadCache:
    def __init__(self, send=transport, clock=time.monotonic):
        self.send, self.clock = send, clock
        self.cache, self.lock = OrderedDict(), threading.Lock()
        self.retry_at = 0

    def __call__(self, method, url, headers, body):
        if method != "GET":
            raise ValueError("Observer only supports GET")
        with self.lock:
            now = self.clock()
            cached = self.cache.get(url)
            if cached and now < cached[0]:
                self.cache.move_to_end(url)
                return cached[1]
            if now < self.retry_at:
                raise RemoteError(429)
            if cached and cached[1][1].get("etag"):
                headers = {**headers, "If-None-Match": cached[1][1]["etag"]}
            result = self.send(method, url, headers, body)
            status, h, body = result
            if status == 304 and cached:
                result, status = cached[1], 200
            if status == 200:
                self.cache[url] = (self.clock() + 30, result)
                self.cache.move_to_end(url)
                if len(self.cache) > 256:
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
            return result


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
