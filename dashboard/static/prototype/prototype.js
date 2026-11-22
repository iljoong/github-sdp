// PROTOTYPE (throwaway, branch prototype/dashboard-layout): layout variants A/B/C over the real dashboard.js.
"use strict";
(() => {
  const VARIANTS = [["A", "Pipeline"], ["B", "Tabs"], ["C", "Inspector"]];
  const SVG = "http://www.w3.org/2000/svg";
  const ICONS = {
    logo: ["M4 17l5-5 4 4 7-8", "M15 8h5v5"],
    live: ["M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0", "M7.8 7.8a6 6 0 0 0 0 8.4", "M16.2 16.2a6 6 0 0 0 0-8.4",
           "M5 5a10 10 0 0 0 0 14", "M19 19a10 10 0 0 0 0-14"],
    pause: ["M9 5v14", "M15 5v14"],
    refresh: ["M20 11a8 8 0 1 0-2.3 5.7", "M20 4v7h-7"],
    bell: ["M6 16V11a6 6 0 0 1 12 0v5l2 2H4z", "M10 20a2 2 0 0 0 4 0"],
    github: ["M14 4h6v6", "M20 4l-9 9", "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"],
    plus: ["M12 5v14", "M5 12h14"],
    signout: ["M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3", "M10 17l-5-5 5-5", "M5 12h11"],
  };
  const doc = document;
  const byId = id => doc.getElementById(id);

  function icon(name) {
    const svg = doc.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    for (const d of ICONS[name] ?? []) {
      const path = doc.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }

  function paintIcons(root = doc) {
    for (const el of root.querySelectorAll("[data-icon]")) el.replaceChildren(icon(el.dataset.icon));
  }

  // The live toggle shows a pause glyph while live (click = pause) and the live glyph while paused.
  new MutationObserver(() => {
    const live = byId("live");
    const want = live.getAttribute("aria-pressed") === "true" ? "live" : "pause";
    if (live.dataset.icon !== want) { live.dataset.icon = want; live.replaceChildren(icon(want)); }
  }).observe(byId("live"), {attributes: true, attributeFilter: ["aria-pressed"]});

  // Identity pill: close the popover on outside click / Escape.
  const who = doc.querySelector(".who");
  doc.addEventListener("click", e => { if (who.open && !who.contains(e.target)) who.open = false; });
  doc.addEventListener("keydown", e => { if (e.key === "Escape") who.open = false; });

  // Variant switcher.
  const current = doc.body.dataset.variant;
  const at = VARIANTS.findIndex(([key]) => key === current);
  byId("proto-label").textContent = `${current} (${VARIANTS[at][1]})`;
  function go(step) {
    const url = new URL(location.href);
    url.searchParams.set("variant", VARIANTS[(at + step + VARIANTS.length) % VARIANTS.length][0]);
    location.replace(url);
  }
  byId("proto-prev").onclick = () => go(-1);
  byId("proto-next").onclick = () => go(1);
  doc.addEventListener("keydown", e => {
    if (e.target.closest?.("input, select, textarea, [contenteditable]") || byId("confirm").open) return;
    if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
  });

  // Tabs (variant B).
  for (const t of doc.querySelectorAll("[data-tab]")) {
    t.onclick = () => {
      for (const o of doc.querySelectorAll("[data-tab]")) o.setAttribute("aria-selected", String(o === t));
      for (const p of doc.querySelectorAll("[data-panel]")) p.hidden = p.dataset.panel !== t.dataset.tab;
    };
  }

  const statusText = {success: "성공", failure: "실패", cancelled: "취소됨", in_progress: "진행 중", queued: "대기열",
                      waiting: "승인 대기", completed: "완료"};
  function ago(iso) {
    if (!iso) return "시간 미확인";
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (s < 60) return "방금";
    if (s < 3600) return `${Math.floor(s / 60)}분 전`;
    if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
    return `${Math.floor(s / 86400)}일 전`;
  }
  function el(tag, cls, value) {
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (value != null) n.textContent = value;
    return n;
  }

  function sidebar(snapshot) {
    const runId = String(snapshot.run?.id ?? "");
    const byIdMap = new Map((snapshot.history ?? []).map(h => [String(h.id), h]));
    const ids = [...new Set([...(snapshot.runs ?? []).map(r => String(r.id)), ...byIdMap.keys()])];
    const runs = new Map((snapshot.runs ?? []).map(r => [String(r.id), r]));
    const list = byId("releases");
    list.replaceChildren(...ids.map(id => {
      const h = byIdMap.get(id) ?? {}, r = runs.get(id) ?? {};
      const state = h.conclusion || r.conclusion || h.status || r.status || "unknown";
      const li = el("li");
      const b = el("button", "release-item");
      b.type = "button";
      b.dataset.state = state;
      if (id === runId) b.setAttribute("aria-current", "true");
      b.onclick = () => selectRun(id); // dashboard.js global
      const top = el("span", "ri-top");
      top.append(el("span", "dot"), el("strong", null, `#${id}`), el("span", "ri-state", statusText[state] ?? state));
      b.append(top, el("span", "ri-title", h.title ?? r.title ?? "제목 없음"),
        el("small", null, [h.profile, h.commit?.slice(0, 7), ago(h.started_at)].filter(Boolean).join(" · ")),
        el("small", null, h.requester ? `요청자 ${h.requester}` : "요청자 미확인"));
      li.append(b);
      return li;
    }));
  }

  function decorate(snapshot) {
    const run = snapshot.run;
    if (run) {
      const entry = (snapshot.history ?? []).find(h => String(h.id) === String(run.id));
      byId("release-title").textContent = entry?.title ?? run.title ?? `릴리스 #${run.id}`;
      byId("meta").textContent = `#${run.id} · ${byId("meta").textContent}`;
    }
    const count = byId("inbox-count");
    if (count) count.textContent = snapshot.inbox?.length ? String(snapshot.inbox.length) : "";
    sidebar(snapshot);
  }

  // Wrap dashboard.js's global render() so every refresh (poll or SSE) also updates the prototype chrome.
  const original = window.render;
  window.render = snapshot => { original(snapshot); decorate(snapshot); };
  paintIcons();
})();
