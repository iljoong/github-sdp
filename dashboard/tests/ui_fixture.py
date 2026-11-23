"""Offline console for the headless Edge smoke (ui_smoke.py): the real app and static UI over fixture data.

Run with the dashboard venv: python dashboard/tests/ui_fixture.py PORT
Fixture controls (unauthenticated, fixture only):
  PUT  /__fixture/snapshot   replace the snapshot returned by /api/dashboard
  POST /__fixture/change     {"run_id": n} announce a change on the SSE stream
  POST /__fixture/stream     {"enabled": bool} break (and refuse) or restore the SSE stream
  GET  /__fixture/requests   /api/dashboard calls so far as [{"run_id", "region"}]
  GET  /__fixture/writes     POST /api/operations commands so far as [{"action", "source_run_id", "region"}]
  GET  /__fixture/reviews    review decisions so far as [{"operation_id", "environment", "approved"}]
"""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parent)]

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse

from dashboard.auth import APPROVER, Actor, OPERATOR
from dashboard.server import create_app
from test_dashboard_auth_github import TENANT, USER


class Feed:
    def __init__(self):
        self.runs, self.enabled = [], True

    def poll(self):
        return len(self.runs)

    def since(self, position):
        if not self.enabled:
            raise ConnectionAbortedError("fixture stream broken")
        return len(self.runs), self.runs[position:]


def fixture_app(port):
    app = create_app()
    state = {"snapshot": None, "requests": [], "writes": [], "reviews": []}
    feed = Feed()
    app.state.settings = SimpleNamespace(tenant_id=TENANT, origin=f"http://127.0.0.1:{port}")
    app.state.verify_session = lambda token: Actor(
        TENANT, USER, frozenset({OPERATOR, APPROVER}), display_name="Fixture Operator",
        username="fixture.op@example.test") if token == "fixture" else None
    app.state.changes = feed
    app.state.stream_timing = (0.2, 300.0)

    def request(actor, command):
        state["writes"].append({"action": command.action, "source_run_id": command.source_run_id,
                                "region": command.region})
        return ({"status": "accepted", "run_id": {"deploy": 901, "resume": 902}[command.action]}
                if command.action in ("deploy", "resume") else {"status": "accepted"})

    def review(actor, operation_id, environment, approved):
        state["reviews"].append({"operation_id": operation_id, "environment": environment, "approved": approved})
        return {"status": "accepted"}

    app.state.operations = SimpleNamespace(request=request, review=review)

    def snapshot(actor, run_id, region):
        state["requests"].append({"run_id": run_id, "region": region})
        data = copy.deepcopy(state["snapshot"])
        if run_id is not None and data.get("run"):
            data["run"]["id"] = run_id
            data["run"]["display_title"] = next((row.get("display_title") for row in data.get("runs", [])
                                                 if row["id"] == run_id), None)
        return data

    app.state.reader = SimpleNamespace(snapshot=snapshot)

    @app.middleware("http")
    async def refuse_stream(request: Request, next_handler):
        if request.url.path == "/api/changes" and not feed.enabled:
            return JSONResponse({"detail": "stream disabled"}, 503)
        return await next_handler(request)

    @app.put("/__fixture/snapshot")
    async def put_snapshot(request: Request):
        state["snapshot"] = await request.json()
        return {"ok": True}

    @app.post("/__fixture/change")
    async def change(request: Request):
        feed.runs.append((await request.json())["run_id"])
        return {"ok": True}

    @app.post("/__fixture/stream")
    async def stream(request: Request):
        feed.enabled = (await request.json())["enabled"]
        return {"ok": True}

    @app.get("/__fixture/requests")
    def requests():
        return state["requests"]

    @app.get("/__fixture/writes")
    def writes():
        return state["writes"]

    @app.get("/__fixture/reviews")
    def reviews():
        return state["reviews"]

    return app


if __name__ == "__main__":
    port = int(sys.argv[1])
    uvicorn.run(fixture_app(port), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
