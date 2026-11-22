import asyncio
import json
import os
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from azure.core.exceptions import AzureError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from .auth import Denied, EntraSession, actor_from_principal, verify_write_origin
from .changes import ChangeFeed
from .config import Settings, positive
from .evidence import Evidence
from .github import GitHub, ReadCache, RemoteError, Tokens, close_transport
from .memo import AzureMemo
from .observations import DashboardReader, AzureReader
from .operations import Command, InvalidAction, Operations
from .store import Conflict, Inbox, OperationsTable, table_clients

STATIC = Path(__file__).with_name("static")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
SWITCH_ACCOUNT = "/.auth/logout?" + urlencode({
    "post_logout_redirect_uri": "/.auth/login/aad?" + urlencode({
        "post_login_redirect_uri": "/", "prompt": "select_account",
    }),
})


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["deploy", "cancel", "rollback", "retire"]
    request_id: str
    source_run_id: int | None = None
    region: Literal["kr", "sea", "usw"] | None = None
    profile: Literal["demo", "production"] | None = None


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    environment: str
    approved: bool


@asynccontextmanager
async def lifespan(app):
    from azure.identity import ManagedIdentityCredential
    from azure.keyvault.keys.crypto import CryptographyClient, SignatureAlgorithm

    settings = Settings.from_env()
    credential = ManagedIdentityCredential(client_id=os.environ["DASHBOARD_MI_CLIENT_ID"])
    service = table_clients(settings.shared, credential)
    signing = CryptographyClient(settings.signing_key, credential)
    azure = AzureReader(credential)
    try:
        sign = lambda digest: signing.sign(SignatureAlgorithm.rs256, digest).signature
        shared = settings.shared
        token = Tokens(settings.app_client_id, shared.installation_id, shared.repo_id, sign)
        github = GitHub(shared.repo, token)
        read_cache = ReadCache()
        observer = GitHub(shared.repo, token, read_cache)
        store = OperationsTable(service.get_table_client("dashboardops"), str(shared.repo_id))
        inbox = Inbox(service.get_table_client("dashboardinbox"), str(shared.repo_id))
        app.state.settings = settings
        app.state.read_cache = read_cache
        app.state.verify_session = EntraSession(settings.tenant_id, settings.entra_client_id)
        # Display reads share Azure observations briefly; write-time checks below read Azure directly.
        app.state.reader = DashboardReader(shared, settings.targets, observer, Evidence(observer), inbox, store,
                                           AzureMemo(azure), cache=read_cache)
        app.state.changes = ChangeFeed(inbox, read_cache.invalidate)
        app.state.operations = Operations(store, github, DashboardReader(
            shared, settings.targets, github, Evidence(github), inbox, store, azure), read_cache.invalidate)
        yield
    finally:
        azure.close()
        close_transport()
        signing.close()
        service.close()
        credential.close()


def create_app():
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # (poll interval, connection lifetime) in seconds; the browser's EventSource reconnects after the
    # lifetime, which re-checks the session and bounds how long one stream holds the server.
    app.state.stream_timing = (2.0, 300.0)

    def actor(request, write=False):
        settings = app.state.settings
        user = actor_from_principal(request.headers.get("x-ms-client-principal"), settings.tenant_id)
        session = app.state.verify_session(request.headers.get("x-ms-token-aad-id-token"))
        if user != session:
            raise Denied("A valid Entra session is required")
        if write:
            verify_write_origin(request.headers, settings.origin)
        return session

    @app.middleware("http")
    async def security_headers(request, next_handler):
        response = await next_handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'")
        return response

    def error_handler(status, message=None):
        async def handler(request, exc):
            return JSONResponse({"detail": message or str(exc)}, status)
        return handler

    app.add_exception_handler(Denied, error_handler(403))
    app.add_exception_handler(Conflict, error_handler(409))
    app.add_exception_handler(InvalidAction, error_handler(422))
    app.add_exception_handler(RequestValidationError, error_handler(422, "Invalid request"))
    for error in (RemoteError, AzureError):
        app.add_exception_handler(error, error_handler(503, "Upstream state unavailable; verify the operation before retrying"))

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/")
    def index(request: Request):
        user = actor(request)
        # PROTOTYPE (throwaway branch prototype/dashboard-layout): ?variant=A|B|C, only when app.state.prototype is set.
        prototype, variant = getattr(app.state, "prototype", None), request.query_params.get("variant", "A")
        if prototype is not None and variant in ("A", "B", "C"):
            short = {"ReleaseOperator": "Operator", "ReleaseApprover": "Approver"}
            page = (STATIC / "prototype" / f"index-{variant}.html").read_text(encoding="utf-8")
            for key, value in {"switch_account": SWITCH_ACCOUNT, "account_name": user.display_name or "이름 없는 계정",
                               "account_alias": prototype.get("alias") or (user.display_name or "user").split()[0],
                               "account_role": " · ".join(short.get(r, r) for r in sorted(user.roles))}.items():
                page = page.replace("{{%s}}" % key, escape(value))
            return HTMLResponse(page)
        roles = "".join(f'<span class="account-role">{escape(role)}</span>' for role in sorted(user.roles))
        page = INDEX.replace("{{switch_account}}", escape(SWITCH_ACCOUNT), 1)
        page = page.replace("{{account_roles}}", roles, 1)
        return HTMLResponse(page.replace("{{account_name}}", escape(user.display_name or "이름 없는 계정"), 1))

    @app.get("/assets/{name}")
    def asset(request: Request, name: Literal["dashboard.css", "dashboard.js", "prototype.css", "prototype.js"]):
        actor(request)
        path = STATIC / ("prototype" if name.startswith("prototype") else "") / name
        return FileResponse(path, media_type="text/javascript" if name.endswith(".js") else "text/css")

    @app.get("/api/dashboard")
    def dashboard(request: Request, run_id: int | None = None, region: Literal["kr", "sea", "usw"] = "kr"):
        user = actor(request)
        if run_id is not None:
            try:
                positive(run_id)
            except ValueError:
                raise InvalidAction("Invalid run ID") from None
        app.state.changes.poll()
        return app.state.reader.snapshot(user, run_id, region)

    @app.get("/api/changes")
    async def changes(request: Request):
        actor(request)
        feed, (interval, lifetime) = app.state.changes, app.state.stream_timing
        position = await run_in_threadpool(feed.poll)

        async def stream(position):
            # Hints only: messages name a run whose state may have changed; the browser re-reads the snapshot.
            yield "retry: 5000\n\n"
            deadline = asyncio.get_running_loop().time() + lifetime
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(interval)
                if await request.is_disconnected():
                    return
                await run_in_threadpool(feed.poll)
                position, runs = feed.since(position)
                for run_id in runs:
                    yield f"event: change\ndata: {json.dumps({'run_id': run_id})}\n\n"
                if not runs:
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(position), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/operations")
    def operation(request: Request, data: OperationInput):
        return app.state.operations.request(actor(request, True), Command(**data.model_dump()))

    @app.post("/api/operations/{operation_id}/review")
    def review(request: Request, operation_id: str, data: ReviewInput):
        return app.state.operations.review(actor(request, True), operation_id, data.environment, data.approved)

    return app


app = create_app()
