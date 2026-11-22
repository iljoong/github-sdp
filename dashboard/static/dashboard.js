"use strict";

const $ = id => document.getElementById(id);
const regionNames = {kr: "한국 중부", sea: "동남아시아", usw: "미국 서부"};
const statuses = {
  waiting: "승인 대기", pending: "대기", not_started: "미실행", queued: "실행 대기",
  in_progress: "실행 중", success: "완료", failure: "실패", cancelled: "취소됨",
  completed: "종료", skipped: "건너뜀", unknown: "미확인",
  accepted: "GitHub 요청 접수", reserved: "요청 처리 중", blocked: "조건 미충족",
  rejected: "GitHub에서 요청 거부", confirmation_required: "결과 확인 필요"
};
const labels = {deploy: "새 배포", cancel: "배포 취소", rollback: "롤백 요청",
                retire: "이전 리비전 종료", approve: "승인", reject: "거부"};
const descriptions = {
  deploy: "main 브랜치에서 시작합니다. 각 지역은 별도 운영자의 승인이 필요합니다.",
  cancel: "릴리스를 취소합니다. 취소만으로 트래픽이 복구되었다고 볼 수 없습니다.",
  rollback: "원본 릴리스의 이 지역 기준 상태로 복구를 요청합니다. 복구 환경의 승인이 필요합니다.",
  retire: "후보가 트래픽 100%를 유지하는 동안 이전 리비전을 비활성화합니다. 재활성화 전에는 기존 복구가 불가능합니다.",
  approve: "Entra 사용자로 이 환경의 GitHub App 보호 규칙을 승인합니다.",
  reject: "이 환경의 보호 규칙을 거부합니다."
};
const reasonText = {
  "ReleaseOperator role required": "배포 운영자 역할이 필요합니다.",
  "ReleaseApprover role required": "배포 승인자 역할이 필요합니다.",
  "A different Entra approver is required": "요청자 본인은 승인할 수 없습니다. 다른 승인자가 승인해야 합니다.",
  "A write is unresolved or GitHub state is unavailable": "이전 요청 결과가 미확인 상태이거나 GitHub를 조회할 수 없습니다.",
  "Upstream state unavailable; verify the operation before retrying": "연결된 서비스에서 결과를 확인하지 못했습니다. 다시 요청하기 전에 운영 이력을 확인하세요.",
  "A protected workflow is still active": "다른 보호된 실행이 완료될 때까지 기다리세요.",
  "An unrelated write is unresolved": "이전 변경 요청의 결과를 확인해야 합니다.",
  "No active release selected": "진행 중인 릴리스를 선택하세요.",
  "Select an original completed release": "완료된 원본 릴리스를 선택하세요.",
  "The original release must be completed on its first attempt": "원본 릴리스의 첫 번째 실행이 종료되어야 합니다.",
  "The deployment baseline is unavailable, expired or unverified": "원본 배포의 기준 스냅샷을 확인할 수 없습니다.",
  "The baseline revision is inactive, unhealthy or unverified": "이전 리비전의 활성 상태와 헬스를 확인할 수 없습니다.",
  "The baseline already receives all traffic": "이미 이전 리비전이 모든 트래픽을 받고 있습니다.",
  "A cancellation was already accepted": "GitHub에 취소 요청이 이미 접수되었습니다.",
  "Current traffic belongs to a different release": "현재 트래픽이 다른 릴리스에 연결되어 있습니다.",
  "Retire requires verified demo evidence and candidate traffic at 100%": "검증된 demo 증거와 후보 트래픽 100%가 필요합니다.",
  "The candidate is not verified healthy": "후보 리비전의 헬스를 확인할 수 없습니다.",
  "Select this release to verify its recovery evidence": "이 릴리스를 선택하면 복구 근거를 확인합니다."
};
const allowedBecause = {
  rollback: "검증된 기준 스냅샷 · 이전 리비전 활성/정상 · 현재 트래픽이 이 릴리스에 속함",
  retire: "검증된 demo 증거 · 후보 트래픽 100% · 후보와 이전 리비전 정상"
};
const eventText = {job_started: "시작", approval_requested: "승인 요청"};
const regionKeys = {Digit1: "kr", Digit2: "sea", Digit3: "usw", Numpad1: "kr", Numpad2: "sea", Numpad3: "usw"};
const healthBadges = {healthy: "✓ 정상", progressing: "⟳ 진행 중", degraded: "✕ 저하", waiting: "⏸ 대기", unknown: "? 미확인"};
const sourceReasons = {rate_limited: "GitHub 호출 제한", upstream_error: "조회 실패", not_produced: "응답 없음"};
// Polling intervals (ms): a safety net while the change stream is connected, fast while a release or
// approval is in flight, slow when idle; errors back off exponentially up to the maximum.
const POLL = {stream: 60000, active: 5000, idle: 30000, retryStream: 60000, backoffMax: 120000};
const STALE_SECONDS = 60;
const baseTitle = document.title;

let selected = "kr", seq = 0, selectedRun = "", timer, soonTimer, writing = false, callback;
let data = null, receivedAt = 0, errors = 0, live = true, stream = null, streamOpen = false, streamOpened = false;
let streamRetryAt = 0, nextPollAt = 0, seenInbox = null, typedRegion = null;
const decided = new Map();
const signatures = new WeakMap();

function node(tag, value) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  return el;
}

function text(value) {
  return value == null ? "미확인" : value === true ? "예" : value === false ? "아니요" : String(value);
}

function dictionary(fields, dl = node("dl")) {
  dl.replaceChildren();
  for (const [label, value] of Object.entries(fields)) dl.append(node("dt", label), node("dd", text(value)));
  return dl;
}

function timestamp(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.valueOf()) ? date.toLocaleString("ko-KR") : "미확인";
}

function duration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}초`;
  if (s < 3600) return `${Math.floor(s / 60)}분 ${s % 60}초`;
  return `${Math.floor(s / 3600)}시간 ${Math.floor(s % 3600 / 60)}분`;
}

function setStatus(value) { $("message").textContent = value; }
function label(value) { return statuses[value] ?? value ?? "미확인"; }

// Keyed, incremental rendering: an entry whose data is unchanged keeps its DOM node (and with it open
// sections, scroll offsets and focus); changed entries are rebuilt by build(value, previousNode, key).
function keyed(container, entries, build) {
  const existing = new Map();
  for (const el of container.children) if (el.dataset.key !== undefined) existing.set(el.dataset.key, el);
  const next = entries.map(([key, value]) => {
    const signature = JSON.stringify(value);
    let el = existing.get(key);
    if (!el || signatures.get(el) !== signature) {
      el = build(value, el, key);
      el.dataset.key = key;
      signatures.set(el, signature);
    }
    return el;
  });
  next.forEach((el, index) => {
    if (container.children[index] !== el) container.insertBefore(el, container.children[index] ?? null);
  });
  while (container.children.length > next.length) container.lastElementChild.remove();
}

function health(job, region = {}) {
  if (["failure", "cancelled"].includes(job)) return "degraded";
  if (["waiting", "pending"].includes(job)) return "waiting";
  if (["queued", "in_progress"].includes(job)) return "progressing";
  if (region.unavailable) return "unknown";
  const serving = (region.revisions ?? []).filter(row => (region.traffic?.[row.name] ?? 0) > 0);
  if (serving.some(row => row.health != null && row.health !== "Healthy")) return "degraded";
  return job === "success" ? "healthy" : "unknown";
}

function station(el, job, region) {
  const state = health(job, region);
  el.dataset.status = job;
  el.querySelector(".badge").dataset.health = state;
  el.querySelector(".badge").textContent = healthBadges[state];
  el.querySelector(".status").textContent = label(job);
}

function stages(el, stage) {
  const box = el.querySelector(".stages");
  box.setAttribute("aria-label", stage == null ? "트래픽 단계 미확인" : `트래픽 단계 ${stage}%`);
  for (const step of box.children) step.toggleAttribute("data-reached", stage != null && Number(step.dataset.stage) <= stage);
}

function stepText(progress) {
  if (progress?.step) {
    const started = Date.parse(progress.started_at);
    const elapsed = Number.isNaN(started) ? progress.elapsed_seconds : (Date.now() - started) / 1000;
    return elapsed == null ? `단계: ${progress.step}` : `단계: ${progress.step} · ${duration(elapsed)}`;
  }
  return progress?.failed_step ? `실패 단계: ${progress.failed_step}` : "";
}

function revision({row, candidate, baseline}, previous) {
  const section = node("article");
  section.append(node("h3", row.name === candidate ? "후보 리비전" : row.name === baseline ? "이전 리비전" : "트래픽 리비전"),
    node("code", row.name), dictionary({
      "Azure 활성 / 헬스": `${text(row.active)} / ${text(row.health)}`,
      "실행 / 프로비전": `${text(row.running)} / ${text(row.provisioning)}`,
      "레플리카 (현재 / 최소 / 최대)": `${text(row.replica_count)} / ${text(row.scale?.minReplicas)} / ${text(row.scale?.maxReplicas)}`
    }));
  const details = node("details");
  details.open = Boolean(previous?.querySelector("details")?.open);
  details.append(node("summary", "주요 ENV · /verify 관찰값"));
  if (Object.keys(row.env ?? {}).length) details.append(node("h3", "Azure 리비전 설정"), dictionary(row.env));
  const s = row.sample;
  details.append(node("h3", "응답한 레플리카의 표본"));
  if (s) details.append(dictionary({"상태를 응답한 레플리카": s.replica, "준비 상태": s.ready,
    "진행 중 작업 수": s.inflight_count, "종료 유예 남은 시간": s.grace_remaining,
    "LONG_TASK_SECONDS": s.long_task_seconds, "LONG_TASK_ABORT_ON_DRAIN": s.abort_on_drain,
    "APP_VERSION": s.version, "버전을 응답한 레플리카": s.version_replica, "관찰 시각": timestamp(s.observed_at)}));
  else details.append(node("p", "이 리비전의 일치하는 응답을 관찰하지 못했습니다."));
  section.append(details);
  return section;
}

function summary(region) {
  const box = node("div");
  box.append(node("small", `Azure 관찰: ${timestamp(region.observed_at)}`), node("h3", "설정 트래픽"));
  if (region.candidate_weight != null) {
    const meter = node("meter"); meter.max = 100; meter.value = region.candidate_weight;
    meter.setAttribute("aria-label", `후보 트래픽 ${meter.value}%`);
    box.append(meter);
  }
  box.append(dictionary(Object.fromEntries(Object.entries(region.traffic ?? {}).map(([name, weight]) => [name, `${weight}%`]))),
    node("small", "설정 트래픽은 HTTP 표본 비율이나 배포 검증 결과가 아닙니다."));
  return box;
}

function readiness(ready) { return ready === true ? "healthy" : ready === false ? "degraded" : "unknown"; }

function combined(states) {
  if (!states.length) return "unknown";
  return states.includes("degraded") ? "degraded" : states.every(s => s === "healthy") ? "healthy" : "unknown";
}

function revisionHealth(row) {
  if (row.active === false || row.health == null) return "unknown";
  return row.health === "Healthy" ? "healthy" : "degraded";
}

// One tree node (li) with a health badge, name, detail and a child list; an existing node is updated in place
// so that unchanged children (keyed by revision, replica and container) keep their DOM.
function treeItem(previous, kind, state, title, detail) {
  const item = previous ?? node("li");
  if (!previous) {
    const badge = node("span"); badge.className = "badge";
    item.append(badge, node("strong"), node("small"), node("ul"));
  }
  const [badge, name, info] = item.children;
  item.dataset.node = kind;
  item.dataset.health = badge.dataset.health = state;
  badge.textContent = healthBadges[state];
  name.textContent = title;
  info.textContent = detail;
  return item;
}

function containerNode(c, previous) {
  return treeItem(previous, "container", readiness(c.ready), c.name ?? "컨테이너",
    `준비 ${text(c.ready)} · 재시작 ${text(c.restartCount)} · ${text(c.runningState)}`);
}

const observedText = {azure: "Azure 목록 · HTTP 응답 없음", http: "HTTP 응답만 · Azure 목록에 없음", both: "Azure 목록 · HTTP 응답"};

function replicaNode({replica, observed, sample}, previous) {
  const containers = replica.containers ?? [];
  const state = observed === "http" ? readiness(sample?.ready) : combined(containers.map(c => readiness(c.ready)));
  const item = treeItem(previous, "replica", state, replica.name,
    observedText[observed] + (sample ? ` · 응답 상태 ${text(sample.state)}` : ""));
  item.dataset.observed = observed;
  keyed(item.lastElementChild, containers.map((c, index) => [`${c.name ?? index}`, c]), containerNode);
  return item;
}

function revisionNode({row, traffic, role}, previous) {
  const item = treeItem(previous, "revision", revisionHealth(row), row.name,
    [role, `트래픽 ${traffic}%`, row.active === false ? "비활성" : row.active ? "활성" : "활성 미확인", `헬스 ${text(row.health)}`]
      .filter(Boolean).join(" · "));
  const listed = row.replicas ?? [];
  const sampled = row.sample?.replica;
  const entries = listed.map(r => [`replica:${r.name}`, {replica: r, observed: r.name === sampled ? "both" : "azure",
    sample: r.name === sampled ? row.sample : null}]);
  if (sampled && !listed.some(r => r.name === sampled)) {
    entries.push([`replica:${sampled}`, {replica: {name: sampled, containers: []}, observed: "http", sample: row.sample}]);
  }
  if (!entries.length) entries.push(["note", row.replicas ? "현재 레플리카가 없습니다." : "레플리카 목록 미관찰"]);
  keyed(item.lastElementChild, entries, (value, prev, key) => {
    if (key !== "note") return replicaNode(value, prev);
    const note = node("li", value); note.dataset.node = "note"; return note;
  });
  return item;
}

function tree(region, previous) {
  const box = previous ?? node("div");
  if (!previous) {
    box.id = "tree"; box.className = "tree";
    const root = node("ul"); root.setAttribute("aria-label", "앱, 리비전, 레플리카, 컨테이너");
    box.append(node("h3", "리소스 트리"), root);
  }
  const rows = region.revisions ?? [];
  const serving = rows.filter(row => (region.traffic?.[row.name] ?? 0) > 0).map(revisionHealth);
  keyed(box.querySelector("ul"), [["app", region]], (value, prev) => {
    const app = treeItem(prev, "app", combined(serving), value.app ?? "앱 미확인", `Container App · prod-${selected}`);
    keyed(app.lastElementChild, rows.map(row => [row.name, {row, traffic: value.traffic?.[row.name] ?? 0,
      role: row.name === value.candidate ? "후보" : row.name === value.baseline ? "이전" : ""}]), revisionNode);
    return app;
  });
  return box;
}

function selectRun(value) {
  selectedRun = value;
  navigate();
  refresh(true, true);
}

function selectRegion(key) {
  selected = key;
  document.querySelectorAll("[data-region]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.region === key)));
  navigate();
  refresh(true, true);
}

function historyEntry({entry, current}) {
  const item = node("li");
  if (current) item.setAttribute("aria-current", "true");
  const head = node("div"); head.className = "spread";
  head.append(node("strong", `#${entry.id} · ${label(entry.conclusion || entry.status)}`));
  if (!current) {
    const pick = node("button", "선택"); pick.type = "button"; pick.className = "select";
    pick.setAttribute("aria-label", `릴리스 #${entry.id} 선택`);
    pick.onclick = () => selectRun(String(entry.id));
    head.append(pick);
  }
  item.append(head,
    node("small", `시작 ${timestamp(entry.started_at)} · 소요 ${entry.duration_seconds == null ? "미확인" : duration(entry.duration_seconds)} · 프로필 ${entry.profile ?? "미확인"}`),
    node("small", `커밋 ${entry.commit?.slice(0, 7) ?? "미확인"} · 요청자 ${entry.requester ?? "미확인"}`));
  const actions = node("div"); actions.className = "actions";
  actions.append(...["rollback", "retire"].map(kind => commandButton(kind, entry.actions?.[kind], entry.id)));
  item.append(actions);
  return item;
}

function timelineEntry(event) {
  const item = node("li");
  item.dataset.kind = event.kind;
  const title = event.kind === "job_started" ? `${event.name} ${eventText.job_started}`
    : event.kind === "job_completed" ? `${event.name} ${label(event.status)}`
    : event.kind === "approval_requested" ? `${event.environment} ${eventText.approval_requested}`
    : [labels[event.kind] ?? event.kind, event.environment ?? (event.region ? `prod-${event.region}` : null), label(event.status)]
      .filter(Boolean).join(" · ");
  item.append(node("span", title), node("small", timestamp(event.at)));
  if (event.actor || event.actor_name) {
    item.append(node("small", [event.actor_name, event.actor ? `Entra 사용자 ${event.actor}` : null].filter(Boolean).join(" · ")));
  }
  return item;
}

function commandButton(kind, reason, runId, gate, explain = true) {
  const container = node("div"); container.className = "command";
  const button = node("button", labels[kind]);
  button.type = "button";
  button.dataset.blocked = reason ? "1" : "";
  button.disabled = Boolean(reason) || writing;
  button.onclick = () => confirm(kind, runId, gate);
  container.append(button);
  if (reason && explain) container.append(node("small", reasonText[reason] ?? reason));
  return container;
}

function gateKey(gate) { return `${gate.operation_id}:${gate.environment}`; }

function inboxItems() {
  const now = Date.now();
  for (const [key, until] of decided) if (until < now) decided.delete(key);
  return (data?.inbox ?? data?.pending ?? []).filter(gate => !decided.has(gateKey(gate)));
}

function inboxEntry(gate) {
  const item = node("li"); item.className = "request";
  const kind = gate.kind === "deploy" ? "배포" : labels[gate.kind] ?? gate.kind;
  item.append(node("h3", `${gate.environment} 승인 대기`),
    node("p", `${regionNames[gate.region] ?? gate.region} · ${kind} · GitHub 실행 #${gate.run_id}` +
      (gate.source_run_id ? ` · 원본 릴리스 #${gate.source_run_id}` : "")));
  if (gate.reason) {
    const why = node("p", reasonText[gate.reason] ?? gate.reason); why.className = "reason";
    item.append(why);
  }
  const actions = node("div"); actions.className = "actions";
  actions.append(...["approve", "reject"].map(kind => commandButton(kind, gate.reason, gate.run_id, gate, false)));
  item.append(actions);
  return item;
}

function renderInbox() {
  const items = inboxItems();
  keyed($("inbox-list"), items.map(gate => [gateKey(gate), gate]), inboxEntry);
  enableCommands();
  $("inbox-empty").hidden = items.length > 0;
  const mine = items.filter(gate => !gate.reason);
  $("inbox-title").textContent = items.length ? `승인 대기 ${items.length}건 · 내가 승인 가능 ${mine.length}건` : "승인 대기";
  document.title = mine.length ? `(${mine.length}) ${baseTitle}` : baseTitle;
  const keys = new Set(mine.map(gateKey));
  if (seenInbox && notificationsOn()) {
    for (const gate of mine) {
      if (seenInbox.has(gateKey(gate))) continue;
      new Notification("배포 승인 요청", {body: `${gate.environment} · GitHub 실행 #${gate.run_id}`, tag: gateKey(gate)});
    }
  }
  seenInbox = new Set([...(seenInbox ?? []), ...keys]);
}

function render(snapshot) {
  const run = snapshot.run;
  const picker = $("release");
  const options = [["", "현재 실행"], ...snapshot.runs.map(r => [String(r.id), `#${r.id} · ${label(r.conclusion || r.status)}`])];
  if (selectedRun && !options.some(([value]) => value === selectedRun)) options.push([selectedRun, `#${selectedRun}`]);
  const optionsSignature = JSON.stringify(options);
  if (signatures.get(picker) !== optionsSignature) {
    keyed(picker, options, (name, previous, value) => new Option(name, value));
    signatures.set(picker, optionsSignature);
  }
  picker.value = selectedRun;
  $("release-title").textContent = run ? `릴리스 #${run.id}` : "선택 가능한 릴리스 없음";
  $("meta").textContent = run ? `main · ${run.profile || "프로필 미확인"} · ${run.head_sha?.slice(0, 7) ?? "SHA 미확인"} · ${label(run.conclusion || run.status)}` : "";
  const link = $("github");
  link.hidden = !run?.url;
  if (run?.url) link.href = run.url;
  else link.removeAttribute("href");
  station($("build"), snapshot.build);
  for (const [key, r] of Object.entries(snapshot.regions)) {
    const el = document.querySelector(`[data-region="${key}"]`);
    station(el, r.job, r);
    el.querySelector(".weight").textContent = r.candidate_weight == null ? "Azure 트래픽 미확인" : `후보 트래픽 ${r.candidate_weight}%`;
    el.querySelector(".step").textContent = stepText(r.progress);
    stages(el, r.traffic_stage);
  }
  const region = snapshot.regions[selected];
  const entries = [["title", selected]];
  if (region.unavailable) entries.push(["unavailable", true]);
  else {
    entries.push(["summary", {observed_at: region.observed_at, candidate_weight: region.candidate_weight, traffic: region.traffic}]);
    entries.push(["tree", {app: region.app, traffic: region.traffic, candidate: region.candidate, baseline: region.baseline,
                           revisions: region.revisions}]);
    for (const row of region.revisions ?? []) entries.push([`revision:${row.name}`, {row, candidate: region.candidate, baseline: region.baseline}]);
    if (snapshot.restored) entries.push(["restored", true]);
    if (snapshot.retired) entries.push(["retired", true]);
  }
  keyed($("regional"), entries, (value, previous, key) => {
    if (key === "title") return node("h2", `${regionNames[value]} · prod-${value}`);
    if (key === "unavailable") return node("p", "Azure에서 이 지역을 조회할 수 없습니다.");
    if (key === "summary") return summary(value);
    if (key === "tree") return tree(value, previous);
    if (key === "restored") return node("p", "복구 근거와 이전 리비전 100% 트래픽을 확인했습니다.");
    if (key === "retired") return node("p", "종료 근거와 이전 리비전 비활성을 확인했습니다.");
    return revision(value, previous);
  });
  $("deploy").disabled = Boolean(snapshot.actions.deploy) || writing;
  $("deploy").title = reasonText[snapshot.actions.deploy] ?? snapshot.actions.deploy ?? "";
  keyed($("operations"), ["cancel", "rollback", "retire"].map(kind => [kind, {kind, reason: snapshot.actions[kind], run: run?.id}]),
    value => commandButton(value.kind, value.reason, value.run));
  keyed($("audit"), snapshot.audit.map((row, index) => [`${row.operation_id ?? index}`, row]), row => {
    const entry = node("li", `${labels[row.kind] ?? row.kind} · ${label(row.status)}`);
    entry.append(node("small", `${timestamp(row.created_at)} · 실행 ${row.run_id ?? row.source_run_id ?? "미확인"}`),
      node("small", row.actor_name ? `${row.actor_name} · Entra 사용자 ${row.actor}` : `Entra 사용자 ${row.actor}`));
    return entry;
  });
  const current = String(run?.id ?? "");
  keyed($("history"), (snapshot.history ?? []).map(entry => [String(entry.id), {entry, current: String(entry.id) === current}]),
    historyEntry);
  const seen = new Map();
  keyed($("timeline"), (snapshot.timeline ?? []).map(event => {
    const base = [event.at, event.kind, event.name, event.environment, event.actor].join("|");
    seen.set(base, (seen.get(base) ?? 0) + 1);
    return [`${base}|${seen.get(base)}`, event];
  }), timelineEntry);
  renderInbox();
  tick();
}

function sourceName(key) {
  if (key === "github") return "GitHub";
  if (key === "audit") return "운영 이력";
  if (key.startsWith("azure:")) return `Azure ${key.slice(6).toUpperCase()}`;
  if (key.startsWith("runtime:")) return `표본 ${key.slice(8)}`;
  return key;
}

// Ticks once a second: per-source "n seconds ago", stale warnings, current-step elapsed time and the poll countdown.
function tick() {
  if (!data) return;
  const passed = (Date.now() - receivedAt) / 1000;
  const sources = Object.entries(data.sources ?? {});
  keyed($("freshness"), sources.map(([key]) => [key, key]), key => {
    const item = node("li"); item.dataset.source = key; return item;
  });
  for (const [key, source] of sources) {
    const item = $("freshness").querySelector(`[data-key="${CSS.escape(key)}"]`);
    const age = source.age_seconds == null ? null : source.age_seconds + passed;
    const state = source.state === "fresh" && age > STALE_SECONDS ? "stale" : source.state;
    const parts = [sourceName(key)];
    if (state === "unavailable" || age == null) parts.push(`조회 불가${source.reason ? ` (${sourceReasons[source.reason] ?? source.reason})` : ""}`);
    else {
      parts.push(`${duration(age)} 전`);
      if (state === "stale") parts.push("오래됨");
      if (source.reason) parts.push(sourceReasons[source.reason] ?? source.reason);
    }
    if (source.retry_after_seconds) parts.push(`${duration(Math.max(0, source.retry_after_seconds - passed))} 후 재시도`);
    item.dataset.state = state;
    item.textContent = parts.join(" · ");
  }
  for (const [key, r] of Object.entries(data.regions)) {
    document.querySelector(`[data-region="${key}"] .step`).textContent = stepText(r.progress);
  }
  connection();
}

function active() {
  if (!data) return true;
  const busy = ["queued", "in_progress", "waiting", "pending"];
  return (data.run && data.run.status !== "completed") || inboxItems().length > 0 ||
    Object.values(data.regions).some(r => busy.includes(r.job));
}

function connection() {
  const el = $("connection");
  const wait = Math.max(0, Math.ceil((nextPollAt - Date.now()) / 1000));
  const [mode, message] = !live ? ["paused", "일시정지됨 · 자동 갱신하지 않습니다"]
    : document.hidden ? ["hidden", "탭이 숨겨져 갱신을 멈췄습니다"]
    : errors ? ["backoff", `갱신 오류 · ${wait}초 후 재시도`]
    : streamOpen ? ["stream", "실시간 연결됨"]
    : ["polling", `실시간 연결 없음 · ${wait}초 후 조회`];
  el.dataset.mode = mode;
  el.textContent = message;
}

function schedule() {
  clearTimeout(timer);
  nextPollAt = 0;
  if (live && !document.hidden) {
    let delay = errors ? Math.min(POLL.backoffMax, POLL.active * 2 ** (errors - 1))
      : streamOpen ? POLL.stream : active() ? POLL.active : POLL.idle;
    const retry = data?.sources?.github?.retry_after_seconds;
    if (retry) delay = Math.max(delay, retry * 1000);
    nextPollAt = Date.now() + delay;
    timer = setTimeout(() => refresh(), delay);
  }
  connection();
}

function openStream() {
  if (stream || !live || document.hidden || !window.EventSource || Date.now() < streamRetryAt) return;
  const source = stream = new EventSource("/api/changes");
  source.onopen = () => {
    streamOpen = true;
    // A reconnect may have missed hints; catch up once.
    if (streamOpened) soon();
    streamOpened = true;
    schedule();
  };
  source.addEventListener("change", soon);
  source.onerror = () => {
    streamOpen = false;
    if (source.readyState === EventSource.CLOSED && stream === source) {
      stream = null;
      streamRetryAt = Date.now() + POLL.retryStream;
    }
    schedule();
  };
}

function closeStream() {
  stream?.close();
  stream = null;
  streamOpen = false;
}

function soon() {
  clearTimeout(soonTimer);
  soonTimer = setTimeout(() => refresh(), 250);
}

// Reused command nodes keep whatever a previous lock set, so re-derive enabled state after each render.
function enableCommands() {
  document.querySelectorAll("#operations button, #inbox-list button, #history .command button").forEach(button => {
    button.disabled = Boolean(button.dataset.blocked) || writing;
  });
}

function disableActions() {
  document.querySelectorAll("#operations button, #inbox-list button, #history .command button, #deploy")
    .forEach(button => { button.disabled = true; });
}

async function refresh(clearMessage = true, selectionChanged = false) {
  clearTimeout(timer);
  clearTimeout(soonTimer);
  if (selectionChanged) {
    disableActions();
    $("regional").replaceChildren(node("p", "선택한 지역을 조회하는 중입니다."));
  }
  const id = ++seq;
  const query = new URLSearchParams({region: selected});
  if (selectedRun) query.set("run_id", selectedRun);
  try {
    const response = await fetch(`/api/dashboard?${query}`, {cache: "no-store"});
    if (!response.ok || response.redirected) throw new Error("unavailable");
    const snapshot = await response.json();
    if (id !== seq) return;
    data = snapshot;
    receivedAt = Date.now();
    errors = 0;
    render(snapshot);
    if (clearMessage) setStatus(!snapshot.github_available || !snapshot.audit_available
      ? "일부 상태를 확인할 수 없습니다. 오래된 정보로 변경을 요청하지 마세요."
      : `최근 조회 ${timestamp(snapshot.observed_at)}`);
    openStream();
  } catch {
    if (id !== seq) return;
    errors += 1;
    setStatus("갱신하지 못했습니다. 세션을 확인하세요. 기존 표시는 마지막 조회 결과이며 자동으로 다시 시도합니다.");
    disableActions();
  } finally {
    if (id === seq) schedule();
  }
}

function recoveryContext(kind, runId, region) {
  const r = data?.regions?.[region] ?? {};
  const serving = Object.entries(r.traffic ?? {}).filter(([, weight]) => weight > 0)
    .map(([name, weight]) => `${name} ${weight}%`).join(", ") || "미확인";
  return {
    "원본 릴리스": `#${runId}`,
    "지역": `${region.toUpperCase()} · ${regionNames[region]} · prod-${region}`,
    "현재 트래픽": serving,
    "변경 후": kind === "rollback" ? `${r.baseline ?? "이전 리비전"} 100%`
      : `${r.candidate ?? "후보 리비전"} 100% 유지 · ${r.baseline ?? "이전 리비전"} 비활성화`,
    "허용 근거": allowedBecause[kind]
  };
}

function typedOk() { return !typedRegion || $("typed").value.trim().toLowerCase() === typedRegion; }
function checkTyped() { $("confirm-apply").disabled = !typedOk(); }

function confirm(kind, runId, gate) {
  if (writing) return;
  const region = selected;
  const recovery = ["rollback", "retire"].includes(kind);
  $("confirm-title").textContent = labels[kind];
  $("confirm-detail").textContent = [runId ? `실행 #${runId}` : "", gate?.environment ?? (recovery ? regionNames[region] : ""), descriptions[kind]].filter(Boolean).join(" · ");
  $("profile-row").hidden = kind !== "deploy";
  $("profile").value = "demo";
  // Rollback and retire change production traffic: show the full context and require the typed region code.
  $("confirm-context").hidden = !recovery;
  if (recovery) dictionary(recoveryContext(kind, runId, region), $("confirm-context"));
  else $("confirm-context").replaceChildren();
  typedRegion = recovery ? region : null;
  $("typed-row").hidden = !recovery;
  $("typed").value = "";
  $("typed-code").textContent = region;
  checkTyped();
  callback = () => submit(kind, runId, region, gate);
  $("confirm").returnValue = "close";
  $("confirm").showModal();
  if (recovery) $("typed").focus();
}

async function submit(kind, runId, region, gate) {
  writing = true;
  disableActions();
  try {
    const body = gate ? {environment: gate.environment, approved: kind === "approve"} : {action: kind, request_id: crypto.randomUUID()};
    if (!gate) {
      if (kind === "deploy") body.profile = $("profile").value;
      else body.source_run_id = runId;
      if (["rollback", "retire"].includes(kind)) body.region = region;
    }
    const target = gate ? `/api/operations/${encodeURIComponent(gate.operation_id)}/review` : "/api/operations";
    const response = await fetch(target, {
      method: "POST",
      referrer: "/",
      referrerPolicy: "same-origin",
      headers: {"Content-Type": "application/json", "X-Dashboard-Write": "1"},
      body: JSON.stringify(body)
    });
    const result = await response.json().catch(() => ({}));
    // An accepted decision leaves the inbox at once, even while GitHub still reports the gate as waiting.
    if (gate && response.ok && result.status === "accepted") decided.set(gateKey(gate), Date.now() + 120000);
    if (result.run_id && kind === "deploy") {
      selectedRun = String(result.run_id);
      navigate();
    }
    setStatus(response.ok ? `${labels[kind]}: ${label(result.status)}. 운영 이력과 실제 상태를 확인하세요.`
      : (reasonText[result.detail] ?? result.detail ?? "요청을 처리하지 못했습니다."));
  } catch {
    setStatus("요청 결과를 확인할 수 없습니다. 다시 요청하지 말고 운영 이력을 확인하세요.");
  } finally {
    writing = false;
    if (data) renderInbox();
    refresh(false);
  }
}

function readLocation() {
  const query = new URLSearchParams(location.search);
  selected = Object.hasOwn(regionNames, query.get("region") ?? "") ? query.get("region") : "kr";
  selectedRun = /^[1-9][0-9]{0,18}$/.test(query.get("run") ?? "") ? query.get("run") : "";
  document.querySelectorAll("[data-region]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.region === selected)));
  if ([...$("release").options].some(o => o.value === selectedRun)) $("release").value = selectedRun;
}

// Selected release and region live in the URL (?run=&region=) so links can be shared and Back works.
function navigate() {
  const query = new URLSearchParams();
  if (selectedRun) query.set("run", selectedRun);
  query.set("region", selected);
  const url = `${location.pathname}?${query}`;
  if (url !== location.pathname + location.search) history.pushState(null, "", url);
}

function notificationsOn() {
  return window.Notification?.permission === "granted" && localStorage.getItem("dashboard.notify") === "on";
}

async function toggleNotifications() {
  if (notificationsOn()) localStorage.removeItem("dashboard.notify");
  else if (await Notification.requestPermission() === "granted") localStorage.setItem("dashboard.notify", "on");
  $("notify").setAttribute("aria-pressed", String(notificationsOn()));
}

function setLive(value) {
  live = value;
  $("live").setAttribute("aria-pressed", String(live));
  if (live) {
    streamRetryAt = 0;
    openStream();
    refresh();
  } else {
    clearTimeout(soonTimer);
    closeStream();
    schedule();
  }
}

$("release").onchange = () => selectRun($("release").value);
$("deploy").onclick = () => confirm("deploy", null, null);
$("refresh").onclick = () => refresh();
$("live").onclick = () => setLive(!live);
$("notify").hidden = !window.Notification;
$("notify").onclick = toggleNotifications;
$("notify").setAttribute("aria-pressed", String(notificationsOn()));
$("typed").addEventListener("input", checkTyped);
$("confirm").addEventListener("close", () => { if ($("confirm").returnValue === "apply" && typedOk()) callback(); });
for (const el of document.querySelectorAll("[data-region]")) el.onclick = () => selectRegion(el.dataset.region);
// Shortcuts use physical keys (event.code) so they also work with a Korean keyboard layout.
document.addEventListener("keydown", event => {
  if (event.defaultPrevented || event.repeat || event.ctrlKey || event.metaKey || event.altKey || $("confirm").open) return;
  if (event.target.closest?.("input, select, textarea, [contenteditable]")) return;
  if (regionKeys[event.code]) selectRegion(regionKeys[event.code]);
  else if (event.code === "KeyR") refresh();
  else if (event.code === "KeyL") setLive(!live);
  else return;
  event.preventDefault();
});
window.addEventListener("popstate", () => { readLocation(); refresh(true, true); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(soonTimer);
    closeStream();
    schedule();
  } else if (live) {
    openStream();
    refresh();
  }
});
setInterval(tick, 1000);
readLocation();
openStream();
refresh();
