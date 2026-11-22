"""Generates the throwaway prototype pages dashboard/static/prototype/index-{A,B,C}.html."""
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "worktrees" / "preview" / "dashboard" / "static" / "prototype"

NAMES = {"A": "Pipeline", "B": "Tabs", "C": "Inspector"}

HEAD = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Deployment Dashboard</title>
  <link rel="stylesheet" href="/assets/dashboard.css">
  <link rel="stylesheet" href="/assets/prototype.css">
  <script defer src="/assets/dashboard.js"></script>
  <script defer src="/assets/prototype.js"></script>
</head>
<body class="proto" data-variant="{v}" data-variant-name="{name}">
  <header class="topbar">
    <div class="brand"><span class="logo" data-icon="logo" aria-hidden="true"></span><strong>Deployment Dashboard</strong></div>
    <div class="tools" role="group" aria-label="갱신 제어">
      <span id="connection" data-mode="starting">연결 중</span>
      <button type="button" class="icon" id="live" aria-pressed="true" aria-label="실시간 갱신" title="실시간 갱신 (L)" data-icon="live"></button>
      <button type="button" class="icon" id="refresh" aria-label="새로고침" title="새로고침 (R)" data-icon="refresh"></button>
      <button type="button" class="icon" id="notify" aria-pressed="false" aria-label="승인 요청 알림" title="승인 요청 알림" data-icon="bell"></button>
      <details class="who">
        <summary aria-label="계정"><span class="who-alias">{{{{account_alias}}}}</span><span class="who-role">{{{{account_role}}}}</span></summary>
        <div class="who-menu">
          <strong id="account-name">{{{{account_name}}}}</strong>
          <small>{{{{account_role}}}}</small>
          <a id="switch-account" href="{{{{switch_account}}}}">계정 전환</a>
          <a href="/.auth/logout" class="signout"><span data-icon="signout" aria-hidden="true"></span>Sign out</a>
        </div>
      </details>
    </div>
  </header>
  <div class="shell">
    <aside class="sidebar" aria-label="릴리스 목록">
      <button id="deploy" class="primary new" disabled><span data-icon="plus" aria-hidden="true"></span>새 배포</button>
      <label class="sr" for="release">릴리스</label><select id="release" class="sr"><option value="">현재 실행</option></select>
      <h2 class="side-title">Releases</h2>
      <ol id="releases" class="releases"></ol>
    </aside>
    <main class="content">
"""

RELEASE_HEAD = """      <div class="release-head">
        <div>
          <h1 id="release-title">릴리스</h1>
          <p id="meta"></p>
        </div>
        <a id="github" class="icon-link" hidden title="GitHub 실행 보기" aria-label="GitHub 실행 보기"><span data-icon="github" aria-hidden="true"></span></a>
      </div>
      <p id="message" role="status">배포 정보를 불러오는 중입니다.</p>
"""

FRESHNESS = """      <ul id="freshness" class="freshness" aria-label="출처별 관찰 시각"></ul>
"""


def station(region, name):
    return (f'<button type="button" class="station" data-region="{region}" aria-pressed="{str(region == "kr").lower()}">'
            f'<strong>{region.upper()} / {name}</strong><span class="badge" data-health="unknown">? 미확인</span>'
            '<span class="status">미확인</span><small class="weight"></small><small class="step"></small>'
            '<span class="stages" aria-label="트래픽 단계 미확인"><span data-stage="0">0</span><span data-stage="10">10</span>'
            '<span data-stage="50">50</span><span data-stage="100">100%</span></span></button>')


RAIL = ('      <nav class="rail" aria-label="배포 단계">\n'
        '        <div class="station" id="build"><strong>Build</strong><span class="badge" data-health="unknown">? 미확인</span><span class="status">미확인</span></div>\n'
        f'        {station("kr", "한국 중부")}\n        {station("sea", "동남아시아")}\n        {station("usw", "미국 서부")}\n'
        '      </nav>\n')

INBOX = """      <section id="inbox" class="panel inbox" aria-labelledby="inbox-title">
        <h2 id="inbox-title">승인 대기</h2>
        <p id="inbox-empty">모든 지역에 대기 중인 승인 요청이 없습니다.</p>
        <ol id="inbox-list"></ol>
        <p id="inbox-truncated" hidden>진행 중인 실행이 많아 일부 실행의 승인 요청만 표시합니다.</p>
      </section>
"""

REGIONAL = '      <section id="regional" class="panel" aria-label="지역 상세"></section>\n'
OPS = """      <section class="panel ops">
        <h2>지역 작업</h2>
        <div id="operations"></div>
        <h2>최근 운영 이력</h2>
        <ol id="audit"></ol>
      </section>
"""
TIMELINE = """      <section class="panel" aria-labelledby="timeline-title">
        <h2 id="timeline-title">이벤트 타임라인</h2>
        <ol id="timeline"></ol>
      </section>
"""
HISTORY = """      <details class="panel history-box">
        <summary><h2 id="history-title">릴리스 이력 · 롤백/종료</h2></summary>
        <ol id="history"></ol>
      </details>
"""

TAIL = """    </main>
  </div>
  <small id="shortcuts" class="sr">단축키: 1·2·3 지역 · R 새로고침 · L 실시간/일시정지</small>
  <dialog id="confirm">
    <form method="dialog">
      <h2 id="confirm-title"></h2>
      <p id="confirm-detail"></p>
      <dl id="confirm-context" hidden></dl>
      <p id="profile-row" hidden><label for="profile">프로필</label>
        <select id="profile"><option value="demo">demo</option><option value="production">production</option></select>
      </p>
      <p id="typed-row" hidden><label for="typed">확인하려면 지역 코드 <strong id="typed-code"></strong>를 입력하세요</label>
        <input id="typed" autocomplete="off" spellcheck="false" autocapitalize="off">
      </p>
      <footer class="actions"><button value="close" autofocus>닫기</button><button id="confirm-apply" class="primary" value="apply">확인</button></footer>
    </form>
  </dialog>
  <!-- PROTOTYPE switcher (throwaway) -->
  <nav class="proto-switch" aria-label="Prototype variant">
    <button type="button" id="proto-prev" aria-label="이전 variant">&larr;</button>
    <span id="proto-label"></span>
    <button type="button" id="proto-next" aria-label="다음 variant">&rarr;</button>
  </nav>
</body>
</html>
"""


def tab(key, label, selected=False):
    return (f'<button type="button" role="tab" data-tab="{key}" aria-selected="{str(selected).lower()}">{label}'
            + ('<span class="count" id="inbox-count"></span>' if key == "inbox" else "") + "</button>")


BODIES = {
    # A: Argo-style app view - pipeline rail on top, detail grid below, timeline full width.
    "A": RELEASE_HEAD + FRESHNESS + RAIL + INBOX
         + '      <div class="grid-2">\n' + REGIONAL + OPS + "      </div>\n" + TIMELINE + HISTORY,
    # B: compact step bar + tabbed content; only one concern on screen at a time.
    "B": RELEASE_HEAD + RAIL
         + '      <div class="tabs" role="tablist">' + tab("overview", "개요", True) + tab("inbox", "승인 대기")
         + tab("timeline", "타임라인") + tab("audit", "운영 이력") + tab("history", "릴리스 이력") + "</div>\n"
         + '      <div data-panel="overview">\n' + REGIONAL + "      </div>\n"
         + '      <div data-panel="inbox" hidden>\n' + INBOX + "      </div>\n"
         + '      <div data-panel="timeline" hidden>\n' + TIMELINE + "      </div>\n"
         + '      <div data-panel="audit" hidden>\n' + OPS + FRESHNESS + "      </div>\n"
         + '      <div data-panel="history" hidden>\n'
         + '      <section class="panel"><h2 id="history-title">릴리스 이력 · 롤백/종료</h2><ol id="history"></ol></section>\n'
         + "      </div>\n",
    # C: three panes - vertical pipeline graph + timeline in the centre, region inspector on the right.
    "C": INBOX.replace('class="panel inbox"', 'class="panel inbox banner"') + RELEASE_HEAD
         + '      <div class="three">\n        <div class="center">\n' + RAIL + TIMELINE + HISTORY + "        </div>\n"
         + '        <aside class="inspector" aria-label="선택 지역">\n' + REGIONAL + OPS + FRESHNESS + "        </aside>\n"
         + "      </div>\n",
}

for v, body in BODIES.items():
    (OUT / f"index-{v}.html").write_text(HEAD.format(v=v, name=NAMES[v]) + body + TAIL, encoding="utf-8")
    print("wrote", OUT / f"index-{v}.html")
