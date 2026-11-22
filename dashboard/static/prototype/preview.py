"""Local preview of the deployment dashboard with fake data (no Azure/GitHub/Entra).

Run with the dashboard venv:
  .scratch\\worktrees\\deployment-dashboard\\.venv\\Scripts\\python.exe .scratch\\local-preview\\preview.py [PORT]
Open http://127.0.0.1:PORT/ . Every request gets fake Easy Auth headers (Operator + Approver).
"""
import sys
import threading
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1] / "worktrees" / "preview"
sys.path[:0] = [str(REPO), str(REPO / "dashboard" / "tests")]

# ui_smoke imports Playwright at module level; only its data helpers are needed here.
stub = types.ModuleType("playwright.sync_api")
stub.expect = stub.sync_playwright = None
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
sys.modules["playwright.sync_api"] = stub

import uvicorn  # noqa: E402
import ui_smoke  # noqa: E402
from ui_fixture import fixture_app  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HEADERS = [(b"x-ms-client-principal", ui_smoke.principal("ReleaseOperator", "ReleaseApprover").encode()),
           (b"x-ms-token-aad-id-token", b"fixture")]


class InjectAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, headers=[h for h in scope["headers"] if h[0] not in dict(HEADERS)] + HEADERS)
        await self.app(scope, receive, send)


app = fixture_app(PORT)
# PROTOTYPE layout variants (?variant=A|B|C) with demo identity and release titles.
from dashboard.auth import APPROVER, Actor, OPERATOR  # noqa: E402
app.state.prototype = {"alias": "iljoong"}
app.state.verify_session = lambda token: Actor(ui_smoke.TENANT, ui_smoke.USER, frozenset({OPERATOR, APPROVER}),
                                               display_name="Iljoong Kim") if token == "fixture" else None


def demo_snapshot():
    data = ui_smoke.snapshot()
    titles = {501: "Release 2026.11.22 · checkout latency fix", 480: "Release 2026.11.20 · pricing API v2",
              455: "Release 2026.11.18 · KR cache warm-up", 431: "Release 2026.11.15 · hotfix login redirect"}
    data["run"]["title"] = titles[501]
    data["runs"] += [{"id": 455, "status": "completed", "conclusion": "success"},
                     {"id": 431, "status": "completed", "conclusion": "failure"}]
    data["history"] += [ui_smoke.history_entry(455, "completed", "success", 4 * 86400, "production", "9f8e7d6c5b",
                                               "Park Operator", "Select this release to verify its recovery evidence"),
                        ui_smoke.history_entry(431, "completed", "failure", 7 * 86400, "demo", "1a2b3c4d5e",
                                               "Kim Operator", "The release did not complete successfully")]
    for row in data["runs"] + data["history"]:
        row["title"] = titles.get(row["id"])
    return data


def seed():
    import json
    import urllib.request
    for _ in range(50):
        try:
            body = json.dumps(demo_snapshot()).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/__fixture/snapshot", body, method="PUT",
                                         headers={"content-type": "application/json"})
            urllib.request.urlopen(req, timeout=2)
            print(f"Dashboard preview ready: http://127.0.0.1:{PORT}/", flush=True)
            return
        except OSError:
            time.sleep(0.2)


threading.Thread(target=seed, daemon=True).start()
uvicorn.run(InjectAuth(app), host="127.0.0.1", port=PORT, log_level="warning", lifespan="off")
