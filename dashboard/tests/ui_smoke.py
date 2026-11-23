"""Headless Edge smoke for the console UI against fixture APIs (never real Azure or GitHub).

Usage (system Python with Playwright; the fixture server runs under the dashboard venv):
  python dashboard/tests/ui_smoke.py --server-python <dashboard venv python> [--headed]
Covers: layout B (top bar with icon buttons, identity pill menu, sidebar release list, release name heading, ARIA tabs
with the inbox count badge and URL tab), approval inbox across regions + tab-title badge + notifications,
health/step/traffic-stage badges, per-source freshness, stream update, active-release polling with the stream open,
idle slow poll, inbox truncation note, polling fallback, Live/Pause/refresh, deep link + back button,
incremental render preserving open sections, scroll and the release list, resource tree, events timeline,
reject-only re-run gate with re-run badges, release history with per-entry actions, typed-region confirmation, keyboard shortcuts, dark theme, mobile width.
The fixture server logs one ConnectionAbortedError traceback when the smoke deliberately breaks the stream.
"""
import argparse
import base64
import json
import re
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


def revision(name, weight, sample_replica=None):
    return {"name": name, "active": True, "health": "Healthy", "running": "Running", "provisioning": "Provisioned",
            "replica_count": 1, "scale": {"minReplicas": 1, "maxReplicas": 3}, "env": {"APP_VERSION": name},
            "replicas": [{"name": f"{name}-rep", "containers": [{"name": "app", "ready": True, "restartCount": 0}]}],
            "sample": {"replica": sample_replica, "ready": True, "state": "ready"} if sample_replica else None,
            "weight": weight}


def region(job, weight, step=None, started=None, revisions=()):
    return {"job": job, "observed_at": iso(2), "candidate": "app--r7", "baseline": "app--r5", "app": "app",
            "candidate_weight": weight, "traffic": {"app--r7": weight, "app--r5": 100 - weight},
            "traffic_stage": max(s for s in (0, 10, 50, 100) if weight >= s), "progress": progress(step, started),
            "revisions": list(revisions)}


def history_entry(run_id, status, conclusion, started, profile, commit, requester, reason, title=None):
    return {"id": run_id, "status": status, "conclusion": conclusion, "head_sha": commit, "url": None,
            "display_title": title, "started_at": iso(started), "profile": profile, "commit": commit, "requester": requester,
            "duration_seconds": 300 if status != "completed" else 1500, "actions": {"rollback": reason, "retire": reason}}


def event(seconds_ago, kind, **fields):
    return {"at": iso(seconds_ago), "kind": kind, "name": None, "status": None, "environment": None, "region": None,
            "actor": None, "actor_name": None, **fields}


def gate(operation_id, run_id, region_key, reason=None, kind="deploy", attempt=1):
    return {"operation_id": operation_id, "run_id": run_id, "kind": kind, "region": region_key,
            "environment": f"prod-{region_key}", "reason": reason, "source_run_id": None if kind == "deploy" else 480,
            "run_attempt": attempt, "rerun": attempt > 1}


def source(age, state="fresh", **extra):
    return {"observed_at": iso(age), "age_seconds": age, "state": state, **extra}


TITLES = {501: "Release 2026.11.22 · checkout latency fix", 480: "Release 2026.11.20 · pricing API"}


def snapshot(**changes):
    data = {
        "run": {"id": 501, "status": "in_progress", "conclusion": None, "head_sha": "abcdef1234", "url": None,
                "profile": "demo", "run_attempt": 1, "display_title": TITLES[501]},
        "runs": [{"id": 501, "status": "in_progress", "conclusion": None, "display_title": TITLES[501]},
                 {"id": 480, "status": "completed", "conclusion": "success", "display_title": TITLES[480]}],
        "build": "success",
        "regions": {
            "kr": region("success", 100),
            "sea": region("in_progress", 50, "Bake at 50%", 65,
                          [revision("app--r7", 50, "app--r7-http"), revision("app--r5", 50, "app--r5-rep")]
                          + [revision(f"app--r{n}", 0) for n in (1, 2, 3)]),
            "usw": region("waiting", 0),
        },
        "history": [history_entry(501, "in_progress", None, 300, "demo", "abcdef1234", "Kim Operator",
                                  "A protected workflow is still active", TITLES[501]),
                    history_entry(480, "completed", "success", 7200, "production", "1234567abc", None,
                                  "Select this release to verify its recovery evidence", TITLES[480])],
        "timeline": [event(400, "deploy", status="accepted", actor="u1", actor_name="Kim Operator"),
                     event(350, "job_completed", name="build / build", status="success"),
                     event(200, "approval_requested", environment="prod-kr", region="kr"),
                     event(100, "approve", status="accepted", environment="prod-kr", region="kr", actor="u2",
                           actor_name="Lee Approver")],
        "pending": [], "inbox_truncated": True,
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
    base = snapshot()
    fx.call("PUT", "/__fixture/snapshot", base)
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
        def tab(name):
            page.locator(f"#tab-{name}").click()
            expect(page.locator(f"#tab-{name}")).to_have_attribute("aria-selected", "true")
            expect(page.locator(f"#panel-{name}")).to_be_visible()

        releases = page.locator("#releases > li")
        try:
            page.goto("/?run=501&region=sea&tab=inbox")
            expect(page.locator("[data-region=sea]")).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#releases li[data-key='501'] button")).to_have_attribute("aria-current", "true")
            expect(page.locator("#tab-inbox")).to_have_attribute("aria-selected", "true")
            expect(page.locator("#panel-inbox")).to_be_visible()
            expect(page.locator("#panel-overview")).to_be_hidden()
            assert {"run_id": 501, "region": "sea"} in fx.call("GET", "/__fixture/requests")
            check("deep link selects release, region and tab")

            expect(page.locator("#release-title")).to_have_text("Release 2026.11.22 · checkout latency fix")
            expect(page.locator("#meta")).to_have_text("#501 · main · demo · abcdef1 · 실행 중")
            expect(releases).to_have_count(2)
            first = page.locator("#releases li[data-key='501'] button")
            for fragment in ("#501", "실행 중", "checkout latency fix", "demo", "abcdef1", "5분 전", "요청자 Kim Operator"):
                expect(first).to_contain_text(fragment)
            expect(first.locator(".dot")).to_have_count(1)
            expect(page.locator("#releases li[data-key='480'] button")).not_to_have_attribute("aria-current", "true")
            expect(page.locator("#releases li[data-key='480']")).to_contain_text("요청자 미확인")
            check("heading shows the release name; sidebar lists releases with status, name, profile, commit, time, requester")

            expect(page).to_have_title("(1) Deployment Dashboard")
            expect(page.locator(".topbar")).to_contain_text("Deployment Dashboard")
            for name in ("실시간 갱신", "새로고침", "승인 요청 알림"):
                button = page.get_by_role("button", name=name, exact=True)
                expect(button).to_have_count(1)
                expect(button.locator("svg")).to_have_count(1)
                assert button.get_attribute("title"), name
                assert not button.inner_text().strip(), f"{name} shows text instead of an icon"
            expect(page.locator("#deploy svg")).to_have_count(1)
            pill = page.locator("#account summary")
            expect(pill.locator("#account-alias")).to_have_text("fixture.op")
            expect(pill.locator(".account-role")).to_have_text(["Approver", "Operator"])
            menu = page.locator("#account .who-menu")
            expect(menu).to_be_hidden()
            pill.click()
            expect(menu).to_be_visible()
            expect(menu.locator("#account-name")).to_have_text("Fixture Operator")
            expect(menu.get_by_role("link", name="계정 전환")).to_be_visible()
            expect(menu.get_by_role("link", name="Sign out")).to_have_attribute("href", "/.auth/logout")
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
            pill.click()
            page.locator("#release-title").click()
            expect(menu).to_be_hidden()
            check("top bar title, icon buttons with accessible names and tooltips, identity pill menu")

            items = page.locator("#inbox-list > li")
            expect(items).to_have_count(2)
            expect(items.filter(has_text="prod-usw")).to_have_count(1)
            mine = items.filter(has_text="prod-kr")
            expect(mine).to_contain_text("610")
            expect(mine).to_contain_text("다른 승인자")
            expect(mine.get_by_role("button", name="승인", exact=True)).to_be_disabled()
            expect(items.filter(has_text="prod-usw").get_by_role("button", name="승인", exact=True)).to_be_enabled()
            expect(page.locator("#inbox-count")).to_have_text("2")
            expect(page.locator("#tab-inbox")).to_have_attribute("data-attention", "")
            expect(page.locator("#inbox-truncated")).to_be_visible()
            check("inbox across regions, requester explanation, title + tab badges, truncation note")

            tabs = page.get_by_role("tab")
            expect(tabs).to_have_text(["개요", "승인 대기 2", "타임라인", "운영 이력", "릴리스 이력"])
            page.locator("#tab-inbox").focus()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#tab-timeline")).to_be_focused()
            expect(page.locator("#tab-timeline")).to_have_attribute("aria-selected", "true")
            expect(page.locator("#panel-timeline")).to_be_visible()
            expect(page.locator("#panel-inbox")).to_be_hidden()
            page.keyboard.press("End")
            expect(page.locator("#tab-history")).to_be_focused()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#tab-overview")).to_be_focused()
            expect(page.locator("#tab-overview")).to_have_attribute("tabindex", "0")
            expect(page.locator("#tab-inbox")).to_have_attribute("tabindex", "-1")
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=sea")
            page.keyboard.press("ArrowLeft")
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=sea&tab=history")
            page.keyboard.press("Home")
            expect(page.locator("#panel-overview")).to_be_visible()
            check("ARIA tabs with arrow/Home/End keys; the tab is kept in the URL")

            for key, health in (("kr", "healthy"), ("sea", "progressing"), ("usw", "waiting")):
                badge = page.locator(f"[data-region={key}] .badge")
                expect(badge).to_have_attribute("data-health", health)
                assert badge.inner_text().strip()[0] in "✓⟳✕⏸?", badge.inner_text()
            expect(page.locator("[data-region=sea] .step")).to_contain_text("Bake at 50%")
            expect(page.locator("[data-region=sea] .step")).to_contain_text("1분")
            expect(page.locator("[data-region=sea] .stages [data-reached]")).to_have_count(3)
            check("health badges, current step + elapsed, traffic stage")

            tab("audit")
            expect(page.locator("#freshness [data-source=github]")).to_contain_text("초 전")
            expect(page.locator("#freshness [data-source='runtime:app--r5']")).to_have_attribute("data-state", "stale")
            first = page.locator("#freshness [data-source=audit]").inner_text()
            page.wait_for_timeout(2100)
            assert page.locator("#freshness [data-source=audit]").inner_text() != first, "freshness does not tick"
            check("per-source freshness ticks and warns when stale")

            tab("overview")
            tree = page.locator("#tree")
            expect(tree.locator("[data-node=app]")).to_contain_text("app")
            expect(tree.locator("[data-node=revision]")).to_have_count(5)
            r7 = tree.locator("[data-node=revision]").filter(has_text="app--r7")
            r5 = tree.locator("[data-node=revision]").filter(has_text="app--r5")
            expect(r7.locator("[data-node=replica][data-observed=azure]")).to_contain_text("app--r7-rep")
            expect(r7.locator("[data-node=replica][data-observed=http]")).to_contain_text("app--r7-http")
            expect(r5.locator("[data-node=replica]")).to_have_count(1)
            expect(r5.locator("[data-node=replica]")).to_have_attribute("data-observed", "both")
            expect(r5.locator("[data-node=container]")).to_have_attribute("data-health", "healthy")
            expect(r5).to_have_attribute("data-health", "healthy")
            check("resource tree app > revision > replica > container; Azure-listed vs HTTP-observed")

            tab("timeline")
            events = page.locator("#timeline > li")
            expect(events).to_have_count(4)
            expect(events.first).to_contain_text("새 배포")
            expect(events.first).to_contain_text("Kim Operator")
            expect(events.nth(2)).to_contain_text("prod-kr 승인 요청")
            expect(events.last).to_contain_text("Lee Approver")
            expect(events.last).to_have_attribute("data-kind", "approve")
            check("events timeline in time order with display names")

            tab("history")
            entries = page.locator("#history > li")
            expect(entries).to_have_count(2)
            current = page.locator("#history > li[data-key='501']")
            expect(current).to_have_attribute("aria-current", "true")
            for fragment in ("Kim Operator", "demo", "abcdef1", "5분", "checkout latency fix"):
                expect(current).to_contain_text(fragment)
            past = page.locator("#history > li[data-key='480']")
            for fragment in ("production", "1234567", "25분", "릴리스를 선택"):
                expect(past).to_contain_text(fragment)
            expect(past.get_by_role("button", name="롤백 요청")).to_be_disabled()
            check("release history with start, profile, commit, requester, duration and disabled reasons")
            if screenshot:
                tab("overview")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.mouse.move(0, 0)
                page.screenshot(path=f"{screenshot}-default.png")
                tab("inbox")
                page.mouse.move(0, 0)
                page.screenshot(path=f"{screenshot}-inbox.png")
                page.set_viewport_size({"width": 1100, "height": 650})
            tab("overview")

            expect(page.locator("#connection")).to_have_attribute("data-mode", "stream")
            page.locator("#notify").click()
            expect(page.locator("#notify")).to_have_attribute("aria-pressed", "true")
            details = page.locator("#regional article details").first
            details.locator("summary").click()
            page.evaluate("""() => { window.__article = document.querySelector('#regional article');
                                     window.__release = document.querySelector('#releases li[data-key="480"]');
                                     window.scrollTo(0, 300); }""")
            scroll = page.evaluate("window.scrollY")
            assert scroll > 0, "page is not scrollable"
            update = snapshot(history=base["history"])
            update["regions"]["kr"]["job"] = "failure"
            update["inbox"].append(gate("op-sea-new", 777, "sea"))
            fx.call("PUT", "/__fixture/snapshot", update)
            fx.call("POST", "/__fixture/change", {"run_id": 501})
            expect(page.locator("[data-region=kr] .badge")).to_have_attribute("data-health", "degraded", timeout=4000)
            expect(page).to_have_title("(2) Deployment Dashboard")
            expect(page.locator("#inbox-count")).to_have_text("3")
            assert page.evaluate("window.__article === document.querySelector('#regional article')"), "article re-created"
            assert page.evaluate("window.__release === document.querySelector('#releases li[data-key=\"480\"]')"), "release list rebuilt"
            expect(details).to_have_js_property("open", True)
            assert page.evaluate("window.scrollY") == scroll, "scroll reset"
            page.wait_for_function("window.__notes.length === 1")
            check("stream update, incremental render, notification for new approval")

            update["regions"]["sea"]["progress"] = progress("Bake at 50% - verify", 70)
            fx.call("PUT", "/__fixture/snapshot", update)
            expect(page.locator("[data-region=sea] .step")).to_contain_text("verify", timeout=7000)
            expect(page.locator("#connection")).to_have_attribute("data-mode", "stream")
            check("active release keeps the 5 s poll while the stream is open")

            idle = snapshot(run={**update["run"], "status": "completed", "conclusion": "success"}, inbox=[],
                            inbox_truncated=False, regions={k: region("success", 100) for k in ("kr", "sea", "usw")})
            fx.call("PUT", "/__fixture/snapshot", idle)
            fx.call("POST", "/__fixture/change", {"run_id": 501})
            tab("inbox")
            expect(page.locator("#inbox-empty")).to_be_visible(timeout=4000)
            expect(page.locator("#inbox-truncated")).to_be_hidden()
            expect(page.locator("#inbox-count")).to_be_empty()
            expect(page.locator("#tab-inbox")).not_to_have_attribute("data-attention", "")
            page.wait_for_timeout(500)
            count = len(fx.call("GET", "/__fixture/requests"))
            page.wait_for_timeout(7000)
            assert len(fx.call("GET", "/__fixture/requests")) == count, "idle console with an open stream kept fast polling"
            fx.call("PUT", "/__fixture/snapshot", update)
            fx.call("POST", "/__fixture/change", {"run_id": 501})
            expect(items).to_have_count(3, timeout=4000)
            check("idle console with an open stream relies on the stream and a slow safety poll")

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
            expect(page).to_have_title("(1) Deployment Dashboard")
            check("approved request leaves the inbox immediately")

            rerun = snapshot(run={**update["run"], "run_attempt": 2}, inbox_truncated=False,
                             inbox=[gate("op-rerun", 501, "usw", "Re-run blocked — reject only", attempt=2)],
                             history=[{**base["history"][0], "run_attempt": 2}, base["history"][1]],
                             timeline=[event(300, "approval_requested", environment="prod-usw", region="usw", attempt=1),
                                       event(60, "approval_requested", environment="prod-usw", region="usw", attempt=2)])
            fx.call("PUT", "/__fixture/snapshot", rerun)
            page.locator("#refresh").click()
            gated = items.filter(has_text="prod-usw")
            expect(gated).to_have_count(1)
            expect(gated).to_have_attribute("data-rerun", "1")
            expect(gated).to_contain_text("재실행 · 시도 2")
            expect(gated).to_contain_text("승인할 수 없습니다")
            expect(gated.get_by_role("button", name="승인", exact=True)).to_be_disabled()
            expect(gated.get_by_role("button", name="거부", exact=True)).to_be_enabled()
            expect(page).to_have_title("(1) Deployment Dashboard")
            expect(page.locator("#meta .rerun")).to_have_text("재실행 · 시도 2")
            expect(page.locator("#history > li[data-key='501'] .rerun")).to_have_text("재실행 · 시도 2")
            expect(page.locator("#releases li[data-key='501'] .rerun")).to_have_count(1)
            tab("timeline")
            expect(page.locator("#timeline > li")).to_have_text([re.compile("prod-usw 승인 요청 · 시도 1"),
                                                                   re.compile("prod-usw 승인 요청 · 시도 2")])
            tab("inbox")
            gated.get_by_role("button", name="거부", exact=True).click()
            page.locator("dialog#confirm").get_by_role("button", name="확인", exact=True).click()
            expect(gated).to_have_count(0)
            assert fx.call("GET", "/__fixture/reviews")[-1] == {"operation_id": "op-rerun", "environment": "prod-usw",
                                                               "approved": False}
            fx.call("PUT", "/__fixture/snapshot", update)
            check("re-run gate is reject-only: approve disabled with reason, reject sent; re-run badges and timeline attempts")

            tab("overview")
            page.locator("[data-region=kr]").click()
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=kr")
            page.locator("#releases li[data-key='480'] button").click()
            expect(page).to_have_url(f"{fx.origin}/?run=480&region=kr")
            expect(page.locator("#releases li[data-key='480'] button")).to_have_attribute("aria-current", "true")
            expect(page.locator("#release-title")).to_have_text("Release 2026.11.20 · pricing API")
            page.go_back()
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=kr")
            expect(page.locator("#releases li[data-key='501'] button")).to_have_attribute("aria-current", "true")
            page.go_back()
            expect(page.locator("[data-region=sea]")).to_have_attribute("aria-pressed", "true")
            page.wait_for_timeout(500)
            assert fx.call("GET", "/__fixture/requests")[-1] == {"run_id": 501, "region": "sea"}
            page.locator("#releases li[data-key='480'] button").focus()
            page.keyboard.press("Enter")
            expect(page).to_have_url(f"{fx.origin}/?run=480&region=sea")
            page.go_back()
            expect(page).to_have_url(f"{fx.origin}/?run=501&region=sea")
            check("sidebar selects releases by click or keyboard; URL tracks release/region; back button restores them")

            recovery = snapshot(run={"id": 480, "status": "completed", "conclusion": "success", "head_sha": "1234567abc",
                                     "url": None, "profile": "production", "run_attempt": 1}, inbox=[],
                                actions={"deploy": None, "cancel": "No active release selected", "rollback": None,
                                         "retire": "Retire requires verified demo evidence and candidate traffic at 100%"})
            recovery["regions"]["kr"].update(candidate_weight=100, traffic={"app--r7": 100, "app--r5": 0})
            select = "Select this release to verify its recovery evidence"
            recovery["history"][0]["actions"] = {"rollback": select, "retire": select}
            recovery["history"][1]["actions"] = {"rollback": None, "retire": recovery["actions"]["retire"]}
            fx.call("PUT", "/__fixture/snapshot", recovery)
            tab("history")
            past.get_by_role("button", name="선택").click()
            expect(page).to_have_url(f"{fx.origin}/?run=480&region=sea&tab=history")
            page.evaluate("document.activeElement.blur()")
            page.keyboard.press("1")
            expect(page).to_have_url(f"{fx.origin}/?run=480&region=kr&tab=history")
            expect(page.locator("[data-region=kr]")).to_have_attribute("aria-pressed", "true")
            count = len(fx.call("GET", "/__fixture/requests"))
            page.keyboard.press("r")
            page.wait_for_function(f"fetch('/__fixture/requests').then(r => r.json()).then(r => r.length > {count})")
            page.keyboard.press("l")
            expect(page.locator("#live")).to_have_attribute("aria-pressed", "false")
            page.keyboard.press("l")
            expect(page.locator("#live")).to_have_attribute("aria-pressed", "true")
            check("keyboard shortcuts: region keys, R refresh, L live/pause")

            expect(past).to_have_attribute("aria-current", "true")
            expect(past.get_by_role("button", name="이전 리비전 종료")).to_be_disabled()
            expect(past).to_contain_text("demo 증거")
            past.get_by_role("button", name="롤백 요청").click()
            dialog = page.locator("dialog#confirm")
            for fragment in ("#480", "prod-kr", "app--r7 100%", "app--r5 100%", "기준 스냅샷"):
                expect(dialog.locator("#confirm-context")).to_contain_text(fragment)
            apply = dialog.locator("#confirm-apply")
            expect(apply).to_be_disabled()
            typed = dialog.locator("#typed")
            typed.press_sequentially("sea")
            expect(apply).to_be_disabled()
            typed.fill("")
            typed.press_sequentially("kr")
            expect(apply).to_be_enabled()
            expect(page).to_have_url(f"{fx.origin}/?run=480&region=kr&tab=history")
            apply.click()
            page.wait_for_function("fetch('/__fixture/writes').then(r => r.json()).then(w => w.length > 0)")
            assert fx.call("GET", "/__fixture/writes") == [{"action": "rollback", "source_run_id": 480, "region": "kr"}]
            check("history rollback needs the typed region code and shows full context")

            def luminance(selector):
                return page.evaluate("""s => { const [r, g, b] = getComputedStyle(document.querySelector(s))
                    .backgroundColor.match(/[0-9.]+/g).map(Number); return (r + g + b) / 3; }""", selector)

            assert luminance("body") > 180 and luminance(".panel") > 180, "light theme is not light"
            page.emulate_media(color_scheme="dark")
            assert luminance("body") < 60 and luminance(".panel") < 60, "dark theme does not follow the OS setting"
            assert page.evaluate("(([r, g, b]) => (r + g + b) / 3)(getComputedStyle(document.body).color.match(/[0-9.]+/g).map(Number))") > 180
            if screenshot:
                page.screenshot(path=f"{screenshot}-dark.png", full_page=True)
            page.emulate_media(color_scheme="light")
            check("prefers-color-scheme dark theme")

            page.set_viewport_size({"width": 375, "height": 740})
            page.wait_for_timeout(300)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "horizontal overflow on mobile"
            sidebar, content = page.locator(".sidebar").bounding_box(), page.locator(".content").bounding_box()
            assert sidebar["y"] + sidebar["height"] <= content["y"] + 1, "sidebar does not collapse above the content"
            expect(page.locator("#deploy")).to_be_visible()
            expect(page.locator("#account summary")).to_be_visible()
            for name, selector in (("history", "#history"), ("timeline", "#timeline"), ("overview", "#tree"), ("inbox", "#inbox")):
                tab(name)
                expect(page.locator(selector)).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"overflow on {name}"
            check("mobile width: sidebar above content, every tab without horizontal overflow")
            assert not violations, violations
            check("no CSP violations or page errors")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", default=sys.executable)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--screenshot", help="path prefix: saves <prefix>-default.png and <prefix>-inbox.png "
                                             "(1440x900, light) and <prefix>-dark.png")
    args = parser.parse_args()
    fixture = Fixture(args.server_python)
    try:
        run(fixture, args.headed, args.screenshot)
    finally:
        fixture.close()
    print("UI smoke passed")


if __name__ == "__main__":
    main()
