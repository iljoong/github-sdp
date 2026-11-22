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
  "A different Entra approver is required": "요청자가 아닌 다른 운영자가 승인해야 합니다.",
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
  "The candidate is not verified healthy": "후보 리비전의 헬스를 확인할 수 없습니다."
};
let selected = "kr", seq = 0, selectedRun = "", timer, writing = false, callback;

function node(tag, value) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  return el;
}

function text(value) {
  return value == null ? "미확인" : value === true ? "예" : value === false ? "아니요" : String(value);
}

function dictionary(fields) {
  const dl = node("dl");
  for (const [label, value] of Object.entries(fields)) dl.append(node("dt", label), node("dd", text(value)));
  return dl;
}

function timestamp(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.valueOf()) ? date.toLocaleString("ko-KR") : "미확인";
}

function setStatus(value) { $("message").textContent = value; }
function label(value) { return statuses[value] ?? value ?? "미확인"; }

function station(el, job) {
  el.dataset.status = job;
  el.querySelector("span").textContent = label(job);
}

function revision(row, data) {
  const section = node("article");
  section.append(node("h3", row.name === data.candidate ? "후보 리비전" : row.name === data.baseline ? "이전 리비전" : "트래픽 리비전"),
    node("code", row.name), dictionary({
      "Azure 활성 / 헬스": `${text(row.active)} / ${text(row.health)}`,
      "실행 / 프로비전": `${text(row.running)} / ${text(row.provisioning)}`,
      "레플리카 (현재 / 최소 / 최대)": `${text(row.replica_count)} / ${text(row.scale?.minReplicas)} / ${text(row.scale?.maxReplicas)}`
    }));
  if (row.replicas?.length) {
    const table = node("table");
    const header = table.createTHead().insertRow();
    for (const entry of ["Azure 레플리카", "컨테이너 준비 상태", "HTTP 관찰"]) header.append(node("th", entry));
    const body = table.createTBody();
    for (const replica of row.replicas) {
      const tr = body.insertRow();
      tr.append(node("td", replica.name), node("td", replica.containers.map(c => `${c.name}: ${text(c.ready)} (재시작 ${text(c.restartCount)})`).join(", ") || "미확인"),
        node("td", row.sample?.replica === replica.name ? text(row.sample.state) : "미관찰"));
    }
    const container = node("div"); container.className = "scroll"; container.append(table); section.append(container);
  } else {
    section.append(node("p", row.replicas ? "현재 레플리카가 없습니다." : "레플리카 목록 미관찰"));
  }
  const details = node("details");
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

function commandButton(kind, reason, runId, gate) {
  const container = node("div"); container.className = "command";
  const button = node("button", labels[kind]);
  button.type = "button";
  button.disabled = Boolean(reason) || writing;
  button.onclick = () => confirm(kind, runId, gate);
  container.append(button);
  if (reason) container.append(node("small", reasonText[reason] ?? reason));
  return container;
}

function render(data) {
  const run = data.run;
  const now = $("release").value;
  $("release").replaceChildren(new Option("현재 실행", ""), ...data.runs.map(r => new Option(`#${r.id} · ${label(r.conclusion || r.status)}`, r.id)));
  if (now && ![...$("release").options].some(o => o.value === now)) $("release").add(new Option(`#${now}`, now));
  $("release").value = now;
  $("release-title").textContent = run ? `릴리스 #${run.id}` : "선택 가능한 릴리스 없음";
  $("meta").textContent = run ? `main · ${run.profile || "프로필 미확인"} · ${run.head_sha?.slice(0, 7) ?? "SHA 미확인"} · ${label(run.conclusion || run.status)}` : "";
  const link = $("github");
  link.hidden = !run?.url;
  if (run?.url) link.href = run.url;
  else link.removeAttribute("href");
  station($("build"), data.build);
  for (const [key, r] of Object.entries(data.regions)) {
    const el = document.querySelector(`[data-region="${key}"]`);
    station(el, r.job);
    el.querySelector("small").textContent = r.candidate_weight == null ? "Azure 트래픽 미확인" : `후보 트래픽 ${r.candidate_weight}%`;
  }
  const region = data.regions[selected];
  const view = $("regional");
  const expanded = [...view.querySelectorAll("article details")].flatMap(el => el.open ? [el.parentElement.querySelector("code").textContent] : []);
  view.replaceChildren(node("h2", `${regionNames[selected]} · prod-${selected}`));
  if (region.unavailable) view.append(node("p", "Azure에서 이 지역을 조회할 수 없습니다."));
  else {
    view.append(node("small", `Azure 관찰: ${timestamp(region.observed_at)}`),
      node("h3", "설정 트래픽"));
    if (region.candidate_weight != null) {
      const meter = node("meter"); meter.max = 100; meter.value = region.candidate_weight;
      meter.setAttribute("aria-label", `후보 트래픽 ${meter.value}%`);
      view.append(meter);
    }
    view.append(dictionary(Object.fromEntries(Object.entries(region.traffic ?? {}).map(([name, weight]) => [name, `${weight}%`]))),
      node("small", "설정 트래픽은 HTTP 표본 비율이나 배포 검증 결과가 아닙니다."));
    view.append(...(region.revisions ?? []).map(row => {
      const el = revision(row, region);
      el.querySelector("details").open = expanded.includes(row.name);
      return el;
    }));
    if (data.restored) view.append(node("p", "복구 근거와 이전 리비전 100% 트래픽을 확인했습니다."));
    if (data.retired) view.append(node("p", "종료 근거와 이전 리비전 비활성을 확인했습니다."));
  }
  $("deploy").disabled = Boolean(data.actions.deploy) || writing;
  $("deploy").title = reasonText[data.actions.deploy] ?? data.actions.deploy ?? "";
  $("pending").replaceChildren(...data.pending.filter(g => g.region === selected).map(gate => {
    const box = node("div"); box.className = "request";
    box.append(node("h3", `${gate.environment} 승인 대기`), node("p", `GitHub 실행 #${gate.run_id}`),
      ...["approve", "reject"].map(kind => commandButton(kind, gate.reason, gate.run_id, gate)));
    return box;
  }));
  $("operations").replaceChildren(...["cancel", "rollback", "retire"].map(kind =>
    commandButton(kind, data.actions[kind], run?.id)));
  $("audit").replaceChildren(...data.audit.map(row => {
    const entry = node("li", `${labels[row.kind] ?? row.kind} · ${label(row.status)}`);
    entry.append(node("small", `${timestamp(row.created_at)} · 실행 ${row.run_id ?? row.source_run_id ?? "미확인"}`),
      node("small", row.actor_name ? `${row.actor_name} · Entra 사용자 ${row.actor}` : `Entra 사용자 ${row.actor}`));
    return entry;
  }));
}

function disableActions() {
  document.querySelectorAll("#operations button, #pending button, #deploy").forEach(button => { button.disabled = true; });
}

async function refresh(clearMessage = true, selectionChanged = false) {
  clearTimeout(timer);
  if (selectionChanged) {
    disableActions();
    $("pending").replaceChildren();
    $("regional").replaceChildren(node("p", "선택한 지역을 조회하는 중입니다."));
  }
  const id = ++seq;
  const query = new URLSearchParams({region: selected});
  if (selectedRun) query.set("run_id", selectedRun);
  try {
    const response = await fetch(`/api/dashboard?${query}`, {cache: "no-store"});
    if (!response.ok || response.redirected) throw new Error("unavailable");
    const data = await response.json();
    if (id !== seq) return;
    render(data);
    if (clearMessage) setStatus(!data.github_available || !data.audit_available
      ? "일부 상태를 확인할 수 없습니다. 오래된 정보로 변경을 요청하지 마세요."
      : `최근 조회 ${timestamp(data.observed_at)}`);
  } catch {
    if (id !== seq) return;
    setStatus("갱신하지 못했습니다. 세션을 확인하세요. 기존 표시는 마지막 조회 결과입니다.");
    disableActions();
  } finally {
    if (id === seq) timer = setTimeout(refresh, 20000);
  }
}

function confirm(kind, runId, gate) {
  if (writing) return;
  const region = selected;
  $("confirm-title").textContent = labels[kind];
  $("confirm-detail").textContent = [runId ? `실행 #${runId}` : "", gate?.environment ?? (["rollback", "retire"].includes(kind) ? regionNames[region] : ""), descriptions[kind]].filter(Boolean).join(" · ");
  $("profile-row").hidden = kind !== "deploy";
  $("profile").value = "demo";
  callback = () => submit(kind, runId, region, gate);
  $("confirm").returnValue = "close";
  $("confirm").showModal();
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
    if (result.run_id && kind === "deploy") {
      selectedRun = String(result.run_id);
      $("release").add(new Option(`#${result.run_id}`, result.run_id));
      $("release").value = selectedRun;
    }
    setStatus(response.ok ? `${labels[kind]}: ${label(result.status)}. 운영 이력과 실제 상태를 확인하세요.`
      : (reasonText[result.detail] ?? result.detail ?? "요청을 처리하지 못했습니다."));
  } catch {
    setStatus("요청 결과를 확인할 수 없습니다. 다시 요청하지 말고 운영 이력을 확인하세요.");
  } finally {
    writing = false;
    refresh(false);
  }
}

$("release").onchange = () => { selectedRun = $("release").value; refresh(true, true); };
$("deploy").onclick = () => confirm("deploy", null, null);
$("confirm").addEventListener("close", () => { if ($("confirm").returnValue === "apply") callback(); });
for (const el of document.querySelectorAll("[data-region]")) {
  el.onclick = () => {
    selected = el.dataset.region;
    document.querySelectorAll("[data-region]").forEach(b => b.setAttribute("aria-pressed", String(b === el)));
    refresh(true, true);
  };
}
refresh();
