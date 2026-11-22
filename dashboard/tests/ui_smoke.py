"""Headless Edge smoke for the console UI against fixture APIs (never real Azure or GitHub).

Usage (system Python with Playwright; the fixture server runs under the dashboard venv):
  python dashboard/tests/ui_smoke.py --server-python <dashboard venv python> [--headed]
Covers: approval inbox across regions + tab-title badge + notifications, health/step/traffic-stage badges,
per-source freshness, stream update, polling fallback, Live/Pause/refresh, deep link + back button,
incremental render preserving open sections, scroll and the release picker.
The fixture server logs one ConnectionAbortedError traceback when the smoke deliberately breaks the stream.
"""
import argparse
import base64
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

FIXTURE = Path(__file__).with_name("ui_fixture.py")
# Same tenant/user as test_dashboard_auth_github (not imported: it needs the dashboard venv).
TENANT, USER = "24373433-0a85-45ce-96b6-baf8530bc884", "6a07c4ce-08fd-46aa-9e45-ad65560943c6"


def principal(*roles):
    claims = [{"typ": k, "val": v} for k, v in {"tid": TENANT, "oid": USER, "idtyp": "user"}.items()]
    return base64.b64encode(json.dumps({"auth_typ": "aad", "role_typ": "roles",
                                        "claims": claims + [{"typ": "roles", "val": r} for r in roles]}).encode()).decode()


def iso(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def progress(step=None, started=None):
    return {"step": step, "started_at": iso(started) if started is not None else None,
            "elapsed_seconds": started, "failed_step": None, "log_url": None}


def revision(name, weight):
    return {"name": name, "active": True, "health": "Healthy", "running": "Running", "provisioning": "Provisioned",
            "replica_count": 1, "scale": {"minReplicas": 1, "maxReplicas": 3}, "env": {"APP_VERSION": name},
            "replicas": [{"name": f"{name}-rep", "containers": [{"name": "app", "ready": True, "restartCount": 0}]}],
            "sample": None, "weight": weight}


def region(job, weight, step=None, started=None, revisions=()):
    return {"job": job, "observed_at": iso(2), "candidate": "app--r7", "baseline": "app--r5",
            "candidate_weight": weight, "traffic": {"app--r7": weight, "app--r5": 100 - weight},
            "traffic_stage": max(s for s in (0, 10, 50, 100) if weight >= s), "progress": progress(step, started),
            "revisions": list(revisions)}


def gate(operation_id, run_id, region_key, reason=None, kind="deploy"):
    return {"operation_id": operation_id, "run_id": run_id, "kind": kind, "region": region_key,
            "environment": f"prod-{region_key}", "reason": reason, "source_run_id": None if kind == "deploy" else 480}


def source(age, state="fresh", **extra):
    return {"observed_at": iso(age), "age_seconds": age, "state": state, **extra}


def snapshot(**changes):
    data = {
        "run": {"id": 501, "status": "in_progress", "conclusion": None, "head_sha": "abcdef1234", "url": None,
                "profile": "demo", "run_attempt": 1},
        "runs": [{"id": 501, "status": "in_progress", "conclusion": None},
                 {"id": 480, "status": "completed", "conclusion": "success"}],
        "build": "success",
        "regions": {
            "kr": region("success", 100),
            "sea": region("in_progress", 50, "Bake at 50%", 65,
                          [revision("app--r7", 50), revision("app--r5", 50)] + [revision(f"app--r{n}", 0) for n in (1, 2, 3)]),
            "usw": region("waiting", 0),
        },
        "pending": [],
        "inbox": [gate("op-usw", 501, "usw"),
                  gate("op-kr-rb", 610, "kr", "A different Entra approver is required", kind="rollback")],
        "actions": {"deploy": "A protected workflow is still active", "cancel": None,
                    "rollback": "Select an original completed release", "retire": "Select an original completed release"},
        "audit": [], "restored": False, "retired": False, "observed_at": iso(), "github_available": True,
        "audit_available": True,
        "sources": {"github": source(1), "audit": source(0), "azure:kr": source(2), "azure:sea": source(2),
                    "azure:usw": source(2), "runtime:app--r5": source(40, "stale")},
    }
    data.update(changes)
    return data


class Fixture:
    def __init__(self, python):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen([python, str(FIXTURE), str(self.port)])
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(self.origin + "/healthz", timeout=1)
                return
            except OSError:
                if time.monotonic() > deadline or self.process.poll() is not None:
                    raise RuntimeError("fixture server did not start")
                time.sleep(0.2)

    def call(self, method, path, body=None):
        request = urllib.request.Request(self.origin + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def close(self):
        self.process.terminate()
        self.process.wait(10)


def check(name):
    print(f"ok - {name}", flush=True)


def run(fixture, headed, screenshot=None):
    fx = fixture
    fx.call("PUT", "/__fixture/snapshot", snapshot())
    with sync_playwright() as play:
        browser = play.chromium.launch(channel="msedge", headless=not headed)
        context = browser.new_context(viewport={"width": 1100, "height": 650}, base_url=fx.origin,
                                      extra_http_headers={"x-ms-client-principal": principal("ReleaseOperator", "ReleaseApprover"),
                                                          "x-ms-token-aad-id-token": "fixture"})
        context.grant_permissions(["notifications"], origin=fx.origin)
        context.add_init_script("""(() => {
          window.__notes = [];
          const Real = window.Notification;
          window.Notification = class extends Real { constructor(title, opts) { super(title, opts); window.__notes.push(title); } };
          Object.defineProperty(window.Notification, "permission", {get: () => Real.permission});
          window.Notification.requestPermission = (...a) => Real.requestPermission(...a);
        })()""")
        page = context.new_page()
        violations = []
        page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
        page.on("pageerror", lambda e: violations.append(str(e)))
        try:
            page.goto("/?run=501&region=sea")
            expect(page.locator("[data-region=sea]")).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#release")).to_have_value("501")
            assert {"run_id": 501, "region": "sea"} in fx.call("GET", "/__fixture/requests")
            check("deep link selects release and region")

            items = page.locator("#inbox-list > li")
            expect(items).to_have_count(2)
            expect(items.filter(has_text="prod-usw")).to_have_count(1)
            mine = items.filter(has_text="prod-kr")
            expect(mine).to_contain_text("610")
            expect(mine).to_contain_text("다른 승인자")
            expect(mine.get_by_role("button", name="승인", exact=True)).to_be_disabled()
            expect(items.filter(has_text="prod-usw").get_by_role("button", name="승인", exact=True)).to_be_enabled()
            expect(page).to_have_title("(1) 배포 관제실")
            check("inbox across regions, requester explanation, title badge")

            for key, health in (("kr", "healthy"), ("sea", "progressing"), ("usw", "waiting")):
                badge = page.locator(f"[data-region={key}] .badge")
                expect(badge).to_have_attribute("data-health", health)
                assert badge.inner_text().strip()[0] in "✓⟳✕⏸?", badge.inner_text()
            expect(page.locator("[data-region=sea] .step")).to_contain_text("Bake at 50%")
            expect(page.locator("[data-region=sea] .step")).to_contain_text("1분")
            expect(page.locator("[data-region=sea] .stages [data-reached]")).to_have_count(3)
            check("health badges, current step + elapsed, traffic stage")

            expect(page.locator("#freshness [data-source=github]")).to_contain_text("초 전")
            expect(page.locator("#freshness [data-source='runtime:app--r5']")).to_have_attribute("data-state", "stale")
            first = page.locator("#freshness [data-source=audit]").inner_text()
            page.wait_for_timeout(2100)
            assert page.locator("#freshness [data-source=audit]").inner_text() != first, "freshness does not tick"
            check("per-source freshness ticks and warns when stale")
            if screenshot:
                page.screenshot(path=screenshot, full_page=True)

            expect(page.locator("#connection")).to_have_attribute("data-mode", "stream")
            page.locator("#notify").click()
            expect(page.locator("#notify")).to_have_attribute("aria-pressed", "true")
            details = page.locator("#regional article details").first
            details.locator("summary").click()
            page.evaluate("""() => { window.__article = document.querySelector('#regional article');
                                     window.__option = document.querySelector('#release option[value="480"]');
                                     window.scrollTo(0, 300); }""")
            scroll = page.evaluate("window.scrollY")
            assert scroll > 0, "page is not scrollable"
            update = snapshot()
            update["regions"]["kr"]["job"] = "failure"
            update["inbox"].append(gate("op-sea-new", 777, "sea"))
            fx.call("PUT", "/__fixture/snapshot", update)
            fx.call("POST", "/__fixture/change", {"run_id": 501})
            expect(page.locator("[data-region=kr] .badge")).to_have_attribute("data-health", "degraded", timeout=4000)
            expect(page).to_have_title("(2) 배포 관제실")
            assert page.evaluate("window.__article === document.querySelector('#regional article')"), "article re-created"
            assert page.evaluate("window.__option === document.querySelector('#release option[value=\"480\"]')"), "picker rebuilt"
            expect(details).to_have_js_property("open", True)
            assert page.evaluate("window.scrollY") == scroll, "scroll reset"
            page.wait_for_function("window.__notes.length === 1")
            check("stream update, incremental render, notification for new approval")

            fx.call("POST", "/__fixture/stream", {"enabled": False})
            expect(page.locator("#connection")).to_have_attribute("data-mode", "polling", timeout=15000)
            update["regions"]["sea"].update(candidate_weight=100, traffic_stage=100)
            fx.call("PUT", "/__fixture/snapshot", update)
            expect(page.locator("[data-region=sea] .stages [data-reached]")).to_have_count(4, timeout=15000)
            check("polling fallback when the stream fails")

            page.locator("#live").click()
            expect(page.locator("#live")).to_have_attribute("aria-pressed", "false")
            expect(page.locator("#connection")).to_have_attribute("data-mode", "paused")
            count = len(fx.call("GET", "/__fixture/requests"))
            update["regions"]["usw"]["job"] = "in_progress"
            fx.call("PUT", "/__fixture/snapshot", update)
            page.wait_for_timeout(8000)
            assert len(fx.call("GET", "/__fixture/requests")) == count, "paused console kept polling"
            expect(page.locator("[data-region=usw] .badge")).to_have_attribute("data-health", "waiting")
            page.locator("#refresh").click()
            expect(page.locator("[data-region=usw] .badge")).to_have_attribute("data-health", "progressing")
            page.locator("#live").click()
            expect(page.locator("#live")).to_have_attribute("aria-pressed", "true")
            check("pause stops updates; manual refresh still works")

            items.filter(has_text="prod-usw").get_by_role("button", name="승인", exact=True).click()
            page.locator("dialog#confirm").get_by_role("button", name="확인", exact=True).click()
            expect(items.filter(has_text="prod-usw")).to_have_count(0)
            expect(page).to_have_title("(1) 배포 관제실")
            check("approved request leaves the inbox immediately")

            page.locator("[data-region=kr]").click()
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=kr")
            page.locator("#release").select_option("480")
            expect(page).to_have_url(f"{fx.origin}/?run=480&region=kr")
            page.go_back()
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=kr")
            expect(page.locator("#release")).to_have_value("501")
            page.go_back()
            expect(page.locator("[data-region=sea]")).to_have_attribute("aria-pressed", "true")
            page.wait_for_timeout(500)
            assert fx.call("GET", "/__fixture/requests")[-1] == {"run_id": 501, "region": "sea"}
            check("URL tracks release/region; back button restores them")
            assert not violations, violations
            check("no CSP violations or page errors")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", default=sys.executable)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--screenshot", help="save a full-page screenshot after the first checks")
    args = parser.parse_args()
    fixture = Fixture(args.server_python)
    try:
        run(fixture, args.headed, args.screenshot)
    finally:
        fixture.close()
    print("UI smoke passed")


if __name__ == "__main__":
    main()
