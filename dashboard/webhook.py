import hashlib
import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .config import ENVIRONMENTS, OBSERVED_WORKFLOWS, SharedSettings, positive, repository
from .store import Inbox, table_clients

MAX_BODY = 1_048_576
OBSERVED_PATHS = frozenset(f".github/workflows/{path}" for path in OBSERVED_WORKFLOWS)
OBSERVED_NAMES = frozenset(OBSERVED_WORKFLOWS.values())
CHANGE_EVENTS = frozenset({"workflow_run", "workflow_job"})


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class RuleEvent:
    delivery_id: str
    run_id: int
    environment: str
    sha: str


@dataclass(frozen=True)
class ChangeEvent:
    delivery_id: str
    run_id: int
    run_attempt: int
    kind: str
    action: str


def verify_signature(headers, body, secret):
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    supplied = headers.get("x-hub-signature-256", "")
    if len(body) > MAX_BODY or not re.fullmatch(r"sha256=[0-9a-f]{64}", supplied) or not hmac.compare_digest(
            "sha256=" + signature, supplied):
        raise Rejected("Invalid webhook signature or size")


def parse_change(headers, body, secret, settings):
    """Return a refresh hint for an observed main-branch run/job, or None for other workflows/branches."""
    verify_signature(headers, body, secret)
    try:
        kind = headers["x-github-event"]
        delivery = str(UUID(headers["x-github-delivery"]))
        value = json.loads(body)
        repo, install, action = value["repository"], value["installation"], value["action"]
        if (kind not in CHANGE_EVENTS or not isinstance(action, str)
                or positive(install["id"]) != settings.installation_id or positive(repo["id"]) != settings.repo_id
                or repository(repo["full_name"]) != settings.repo):
            raise ValueError
        if kind == "workflow_run":
            run = value["workflow_run"]
            run_repo = run["repository"]
            if positive(run_repo["id"]) != settings.repo_id or repository(run_repo["full_name"]) != settings.repo:
                raise ValueError
            run_id, attempt = positive(run["id"]), positive(run["run_attempt"])
            observed = (run["head_branch"] == "main" and run["event"] == "workflow_dispatch"
                        and str(run["path"]).split("@", 1)[0] in OBSERVED_PATHS)
        else:
            job = value["workflow_job"]
            run_id, attempt = positive(job["run_id"]), positive(job["run_attempt"])
            observed = job["head_branch"] == "main" and job["workflow_name"] in OBSERVED_NAMES
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise Rejected("Unrecognized workflow change event") from None
    return ChangeEvent(delivery, run_id, attempt, kind, action[:32]) if observed else None


def parse_rule(headers, body, secret, settings):
    verify_signature(headers, body, secret)
    try:
        if headers["x-github-event"] == "ping":
            return None
        delivery = str(UUID(headers["x-github-delivery"]))
        value = json.loads(body)
        repo, install = value["repository"], value["installation"]
        env, sha = value["environment"], value["sha"]
        callback = re.fullmatch(
            r"https://api\.github\.com/repos/([^/]+/[^/]+)/actions/runs/([1-9][0-9]{0,18})/deployment_protection_rule",
            value["deployment_callback_url"])
        if (headers["x-github-event"] != "deployment_protection_rule" or value["action"] != "requested"
                or value["event"] != "workflow_dispatch" or value["ref"] not in ("main", "refs/heads/main")
                or env not in ENVIRONMENTS or not re.fullmatch(r"[a-f0-9]{40}", sha)
                or positive(install["id"]) != settings.installation_id or positive(repo["id"]) != settings.repo_id
                or repository(repo["full_name"]) != settings.repo or not callback
                or repository(callback[1]) != settings.repo):
            raise ValueError
        return RuleEvent(delivery, positive(callback[2]), env, sha)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise Rejected("Unrecognized protection rule event") from None


def receive(headers, body, secret, settings, inbox):
    if headers.get("x-github-event") in CHANGE_EVENTS:
        change = parse_change(headers, body, secret, settings)
        return inbox.put_change(change) if change else False
    rule = parse_rule(headers, body, secret, settings)
    return inbox.put(rule) if rule else False


@asynccontextmanager
async def lifespan(app):
    from azure.identity import ManagedIdentityCredential

    settings = SharedSettings.from_env()
    secret = os.environ["DASHBOARD_WEBHOOK_SECRET"].encode()
    if len(secret) < 32:
        raise ValueError("Configure a high-entropy GitHub webhook secret")
    credential = ManagedIdentityCredential(client_id=os.environ["DASHBOARD_MI_CLIENT_ID"])
    service = table_clients(settings, credential)
    app.state.receiver = (secret, settings, Inbox(service.get_table_client("dashboardinbox"), str(settings.repo_id)))
    try:
        yield
    finally:
        service.close()
        credential.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/webhooks/github")
async def webhook(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Webhook body is too large")
        body.extend(chunk)
    try:
        await run_in_threadpool(receive, request.headers, bytes(body), *request.app.state.receiver)
    except Rejected as error:
        raise HTTPException(403, str(error)) from None
    return Response(status_code=204)
