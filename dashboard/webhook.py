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

from .config import ENVIRONMENTS, SharedSettings, positive, repository
from .store import Inbox, table_clients

MAX_BODY = 1_048_576


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class RuleEvent:
    delivery_id: str
    run_id: int
    environment: str
    sha: str


def parse_rule(headers, body, secret, settings):
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    supplied = headers.get("x-hub-signature-256", "")
    if len(body) > MAX_BODY or not re.fullmatch(r"sha256=[0-9a-f]{64}", supplied) or not hmac.compare_digest(
            "sha256=" + signature, supplied):
        raise Rejected("Invalid webhook signature or size")
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
