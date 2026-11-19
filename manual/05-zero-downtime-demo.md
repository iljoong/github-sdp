# 5장. 무중단 배포 데모 (readiness · SIGTERM drain · 긴 작업)

이 장은 데모 앱의 **무중단 배포 검증 기능**을 운영자/발표자 관점에서 설명합니다.
화면(`/verify`)에서 1초마다 버전을 확인하면서 canary 배포가 진행되어도 오류가 0인지,
그리고 **2분짜리 긴 작업이 진행 중인 이전 revision이 SIGTERM을 받아도 작업을 끝까지
처리한 뒤 종료하는지**를 보여 줍니다. 마지막 절(5.11)은 나중에 동작을 바꾸려는
개발자를 위한 "어디를 고치면 되는가" 지도입니다.

> 설계 근거: [docs/spec/zero-downtime-demo-app.md](../docs/spec/zero-downtime-demo-app.md)
> (결정 D1–D11, 요구 사항 ZDD-R00–R07)

> 실제 준비·실행·테스트 절차와 실습 중 만난 문제는 [6장](06-zero-downtime-demo-guide.md)을 참고하세요.

## 5.1 목적과 원리

| 보여 주는 것 | 구현 방법 |
| --- | --- |
| 배포 중 사용자 요청 오류 0 | readiness(`/readyz`)와 liveness(`/healthz`) 분리, ACA canary 10/50/100% |
| 종료 신호를 받은 replica가 새 요청을 받지 않음 | SIGTERM 수신 즉시 `/readyz` 가 503 → ACA가 load balancing에서 제외 |
| 진행 중인 긴 작업은 끝까지 처리 | drain 상태에서도 HTTP 서버는 계속 응답, 작업 종료 후에만 프로세스 종료 |
| 기본 30초가 아닌 충분한 종료 유예 | `template.terminationGracePeriodSeconds = 180` (`infra/revision-policy.json`) |

ACA는 **트래픽 0%로 바꾸는 것만으로는 SIGTERM을 보내지 않습니다.** revision을
비활성화(deactivate)하거나 scale-in 할 때만 보냅니다. 운영 파이프라인은 즉시 복구를 위해
이전 revision을 활성 상태로 유지하므로, 데모에서는 별도의 수동 workflow
`demo-retire.yml` 로 이전 revision을 비활성화해 실제 SIGTERM을 발생시킵니다.

## 5.2 상태 머신 (`app/server.py` 의 `Lifecycle`)

```text
          SIGTERM / SIGINT (두 번째 신호는 무시)
SERVING ─────────────────────────────▶ DRAINING ──┬─ 진행 중 작업 0개 + DRAIN_IDLE_SECONDS 경과 ─▶ STOPPED (exit 0, reason=idle)
 /readyz 200                            /readyz 503 │
 긴 작업 접수                            새 긴 작업 503 └─ deadline 도달 (GRACE - MARGIN 초) ─────────▶ STOPPED (exit 0, reason=deadline,
                                        진행 중 작업 계속                                                     abandoned=남은 작업 수)
```

- 신호 핸들러는 플래그와 시각만 기록합니다(signal-safe). 실제 종료 판단은 main thread의
  0.2초 루프(`should_exit`)가 합니다.
- deadline = drain 시작 + `GRACE_SECONDS` − `SHUTDOWN_MARGIN_SECONDS` (기본 180 − 10 = 170초).
  ACA가 SIGKILL을 보내기 전에 스스로 종료하기 위한 여유입니다.
- 상태 전환마다 `kind: "lifecycle"` 로그 한 줄(`serving` / `draining` / `stopped`)을, 긴 작업은
  `kind: "long_task"` 로그(`start` / `finish`)를 남깁니다. **`kind: "business"` 는 `/work` 만**
  남기므로 health gate(`metrics.py`)에 영향을 주지 않습니다.

## 5.3 엔드포인트

| 엔드포인트 | 용도 | SERVING | DRAINING |
| --- | --- | --- | --- |
| `GET /healthz` | liveness / startup probe | 200 | 200 |
| `GET /readyz` | readiness probe (상태 JSON 포함) | 200 | **503** |
| `GET /work` | 기존 business 계약 + health gate 로그 (변경 없음) | 200 | 200 |
| `GET /api/version` | 화면용 `{version, release, revision, replica, dnsSuffix, app, time}` | 200 | 200 |
| `GET /status` | `{state, ready, inflight[], drainStartedAt, graceSeconds, graceRemaining, longTaskSeconds, revision, replica}` | 200 | 200 |
| `POST /api/long-task` | `LONG_TASK_SECONDS` 동안 작업 후 `{id, revision, replica, durationMs, completed, drainObserved}` | 200 (replica당 동시 3개 초과 시 **429**) | 새 요청 **503**, 진행 중 작업은 계속 |
| `GET /verify` | 검증 화면 (`app/verify.html`) | 200 | 200 |

- `/api/*`, `/status` 는 CORS(`Access-Control-Allow-Origin: *`)와 `OPTIONS` preflight를 지원합니다.
  화면이 공유 FQDN에서 열린 뒤 revision 전용 FQDN을 직접 호출하기 때문입니다.
- `drainObserved: true` = 작업 도중 종료 신호를 받았지만 끝까지 처리했다는 증거입니다.

## 5.4 조정 값(tunable) 일람

| 값 | 기본 | 설정 위치 | 설명 |
| --- | --- | --- | --- |
| `APP_VERSION` | `dev` | **이미지** (`app/Dockerfile` `ARG`, `build.yml` 이 `release.yml` 은 `r<run 번호>`, `release-kr.yml` 은 `k<run 번호>`, bootstrap 이 `bootstrap`) | 화면의 큰 버전 글자. template env에 넣지 말 것(5.5) |
| `terminationGracePeriodSeconds` | 180 | `infra/revision-policy.json` | ACA가 SIGTERM 후 SIGKILL까지 기다리는 시간 |
| `GRACE_SECONDS` | 180 | candidate env (위 정책 값에서 자동 생성) | 앱이 deadline 계산과 화면 카운트다운에 사용 |
| `LONG_TASK_SECONDS` | 120 | 앱 env (기본값은 `server.py`) | 긴 작업 길이. 자동 상한 적용(5.5) |
| `SHUTDOWN_MARGIN_SECONDS` | 10 | 앱 env | SIGKILL 전에 스스로 종료하기 위한 여유 (`< GRACE_SECONDS`) |
| `DRAIN_IDLE_SECONDS` | 2 | 앱 env | 마지막 작업 종료 후 화면이 최종 상태를 볼 수 있게 기다리는 시간 |
| `LONG_TASK_MAX_CONCURRENT` | 3 | 앱 env | replica당 동시 긴 작업 수 (초과 시 429) |
| `POLL_INTERVAL_MS` | 1000 | `app/verify.html` 의 `CONFIG` | 화면 폴링 주기 |
| `REQUEST_TIMEOUT_MS` | 3000 | `CONFIG` | 이보다 느린 응답은 오류(빨강) |
| `STRIP_CELLS` | 120 | `CONFIG` | 스트립 칸 수(최근 N초) |
| `TRAFFIC_WINDOW_S` | 30 | `CONFIG` | 카드의 트래픽 비율 계산 구간 |
| `CARD_STATE_WINDOW_S` | 3 | `CONFIG` | 카드 상태 = 최근 N초 중 가장 나쁜 상태 |
| `DEFAULT_LONG_TASK_S` | 120 | `CONFIG` | `/status` 응답 전 진행 막대 기준 |
| `PALETTE`, `ERROR_COLOR` | 6색, 빨강 | `CONFIG` | 버전별 색(처음 본 순서), 오류 색 |

잘못된 env 값(숫자 아님, `GRACE_SECONDS` 범위 밖, `MARGIN ≥ GRACE`)은 앱 시작 시
`ValueError: <변수명> ...` 로 즉시 실패합니다 → revision이 Healthy가 되지 않아 배포가 안전하게 중단됩니다.

## 5.5 안전하게 값을 바꾸는 규칙

1. **`LONG_TASK_SECONDS` ≤ `GRACE_SECONDS` − `SHUTDOWN_MARGIN_SECONDS` − 5** 이어야 작업이
   SIGKILL 전에 끝납니다. 더 크게 주면 앱이 자동으로 이 값으로 줄입니다.
2. **긴 작업 ≤ 230초**: ACA ingress 요청 timeout이 240초이므로 앱이 230초로 자동 제한합니다.
3. **grace ≤ 600초**: ACA 최대값. 앱도 1..600 밖이면 시작을 거부합니다.
4. **grace는 `infra/revision-policy.json` 만 바꾸고 새 release를 실행**합니다.
   `snapshot.py` 가 이 값을 `candidate_termination_grace_seconds` 로 baseline에 고정하고,
   `common.expected_candidate` 가 candidate template과 `GRACE_SECONDS` env를 같이 맞춥니다.
   Portal/CLI로 직접 바꾸면 다음 release에서 "template drift" 로 거부됩니다.
   `infra/region.bicep` 도 같은 파일을 읽으므로 새 bootstrap에도 동일하게 적용됩니다.
5. **`APP_VERSION` 은 이미지에만** 넣습니다. 파이프라인은 stable revision의 env를 candidate로
   복사하므로, template env에 넣으면 v2가 v1의 라벨을 물려받습니다.
6. 화면 값(`CONFIG`)은 `app/verify.html` 만 고치면 되며 다음 이미지에 포함됩니다.
   즉시 시험하려면 로컬 파일을 브라우저로 열고 `?base=https://<앱 FQDN>` 을 붙입니다.

## 5.6 데모 준비 (1회, 결정 D9)

기존 환경의 v1은 drain 로직, `/status`, CORS, 180초 grace가 없는 이전 코드입니다.
따라서 **준비용 release를 1회** 실행해 새 코드를 v1으로 만들어 둡니다.

```powershell
gh workflow run release.yml --repo $repo --ref main -f profile=demo
# kr → sea → usw 승인. 완료 후 이 run의 revision(예: r41)이 데모의 v1 입니다.
```

이 release가 readiness를 `/readyz` 로 바꾸고 grace 180초를 적용합니다. 확인:

```powershell
az containerapp show -g <RG> -n <APP> --query "properties.template.{grace:terminationGracePeriodSeconds, probes:containers[0].probes[].{type:type,path:httpGet.path}}"
# grace: 180, Readiness: /readyz
```

플랫폼 사전 확인(ZDD-R00, 비운영 환경에서 1회): 비활성화 중인 revision의 FQDN이
draining replica로 계속 라우팅되는지, 열려 있는 요청이 비활성화 후에도 살아남는지
확인합니다. 결과에 따라 5.8의 "카드가 바로 STOPPED" 해석이 달라집니다.

## 5.7 발표 스크립트 (T0–T7)

| 시점 | 발표자 행동 | 화면에서 보이는 것 |
| --- | --- | --- |
| T0 | `https://<APP FQDN>/verify` 를 연다 | 큰 글자 v1(예: `r41`), 스트립이 v1 색으로 채워짐, v1 카드 `SERVING` |
| T1 | `gh workflow run release.yml -f profile=demo` 후 kr 승인 (빠르게: `gh workflow run release-kr.yml`, KR만) | 10% → 50% → 100% 동안 스트립이 두 색으로 섞이다가 v2 색으로 바뀜. **오류 0** |
| T2 | release run이 **전체 완료**될 때까지 기다림 (sea, usw 승인 포함) | v2 카드 등장. v1 카드: 트래픽 0%, `SERVING`, ready ✓ |
| T3 | v1 카드의 **"긴 작업 시작"** 클릭 | v1 카드에 진행 막대 `0s / 약 120s` |
| T4 | 즉시 `demo-retire.yml` 실행 후 `prod-kr` 승인 (아래 명령) | — |
| T5 | (deactivate → v1 replica에 SIGTERM) | v1 카드 `DRAINING`, ready ✗, grace 남음 180s부터 감소 |
| T6 | 약 120초 후 | 진행 막대 `완료 … drainObserved: 예`. 약 2초 뒤 v1 카드 `STOPPED` |
| T7 | 정리 | 스트립은 처음부터 끝까지 빨강 없음, 오류 카운터 0 |

```powershell
gh workflow run demo-retire.yml --repo $repo --ref main `
  -f source_run_id=<T1의 release run ID> -f region=kr
gh run watch <RETIRE_RUN_ID> --repo $repo
```

타이밍 규칙(결정 D8):

- retire는 **release run 전체가 완료된 뒤에만** 가능합니다. 같은 concurrency group
  (`safe-aca-production`)을 쓰고 `check_source.py` 가 completed run만 허용하기 때문입니다.
  `profile=demo` 를 쓰면 지역당 10~20분이라 대기가 짧습니다.
- T3(버튼)는 release 완료 후, T4 직전에 누릅니다. 120초면 승인 + deactivate에 충분합니다.
- retire 대상 지역은 그 run의 `evidence/<region>/success.json` 이 있어야 합니다.
- 화면은 그 지역의 앱 FQDN에서 열어야 합니다(지역마다 앱이 따로 있음).

## 5.8 화면 읽는 법

**상단 (`GET <base>/api/version`, 1초마다)**

| 요소 | 의미 |
| --- | --- |
| 큰 글자 | 방금 응답한 버전(`APP_VERSION`), 아래에 revision·replica·지연 시간 |
| 스트립 1칸 | 1초. 색 = 응답한 버전, **빨강** = 오류(비 200, 3초 초과, 네트워크 실패, JSON 아님), **회색** = 이전 요청이 아직 열려 있어 겹쳐 보내지 않음 |
| 성공 / 오류 / 연속 성공 | 누적 카운터. 데모의 핵심 증거는 "오류 0" |

**하단 카드 (revision마다, `GET <revision FQDN>/status` 1초마다)**

| 표시 | 의미 |
| --- | --- |
| `SERVING` (초록) | 정상 서비스 중 |
| `DRAINING` (주황) | SIGTERM 수신. 새 요청 제외, 진행 중 작업 처리 중. 최근 3초 중 한 번이라도 DRAINING이면 표시 |
| `STOPPED` (회색) | DRAINING을 본 뒤 응답 없음 = 정상 종료 |
| `UNREACHABLE` (빨강 배지) | DRAINING 없이 응답 없음 = 예기치 않은 중단 또는 네트워크 문제. **스트립은 빨강으로 만들지 않음** |
| 트래픽 비율 | 상단 폴링 중 최근 30초 동안 이 revision이 응답한 비율 |
| grace 남음 | DRAINING 중 SIGKILL까지 남은 초 |
| replica · 진행 중 작업 | `/status` 에 응답한 replica 기준 힌트. replica가 여러 개면 작업은 다른 replica에 있을 수 있음(결정 D10) |
| 긴 작업 진행 막대 | 브라우저 자체 타이머 기준. 결과: `완료 Ns · replica · drainObserved: 예/아니오` 또는 `실패 Ns · 사유` |

## 5.9 문제 해결

| 증상 | 원인 | 조치 |
| --- | --- | --- |
| 카드가 처음부터 `UNREACHABLE` | revision FQDN 호출 실패(CORS 없는 이전 코드, DNS 접미사 env 없음) | 5.6 준비 release가 끝났는지 확인. 브라우저 개발자 도구 Network 탭 확인 |
| T5에서 `DRAINING` 없이 바로 `STOPPED`/`UNREACHABLE` | 비활성화된 revision FQDN이 즉시 라우팅 중단(ZDD-R00 위험 a) | 증거는 긴 작업 응답의 `drainObserved: 예` 와 Log Analytics의 `kind: lifecycle` 로그로 대체 |
| 긴 작업 `실패 … network` (T5 직후) | 열린 요청이 deactivate에서 끊김(ZDD-R00 위험 b) | 데모 주장이 ACA에서 성립하지 않음. 설계 담당자에게 보고 |
| 긴 작업 `실패 … too many long tasks` (429) | replica당 동시 3개 초과 | 기다리거나 `LONG_TASK_MAX_CONCURRENT` 조정 |
| 긴 작업 `실패 … draining` (503) | 이미 DRAINING인 revision에 버튼을 누름 | 정상 동작. 새 revision 카드에서 실행 |
| 스트립에 회색 칸이 연속 | 응답이 1초보다 느림 | 지연 원인 확인. 3초 넘으면 빨강으로 바뀜 |
| `demo-retire.yml` 이 시작 안 하고 대기 | 다른 release/restore가 같은 concurrency group 사용 중 | 그 run 완료 대기 |
| retire 실패 `Traffic drift` | candidate가 100%가 아님(release 미완료 또는 이후 변경) | release 완료 확인, 최신 release run ID 사용 |
| retire 실패 `Stable unavailable` | 이전 revision이 이미 비활성화됨(두 번 실행) | 추가 조치 불필요 |
| retire 실패 `Region success evidence ...` | 그 지역이 성공하지 않았거나 다른 run의 evidence | `success.json` 이 있는 run/지역 지정 |
| evidence artifact 다운로드 실패 | release를 re-run해서 artifact 이름이 `-1` 이 아님 | 데모는 re-run 없는 release run을 사용 |

## 5.10 retire 이후 복구

retire 후에는 그 release의 이전 revision이 비활성이므로 `restore.yml` 이
`Stable unavailable` 로 **안전하게 거부**합니다(설계상 정상).

왜 거부하나: restore는 트래픽을 baseline의 이전 stable revision으로 100% 옮깁니다.
옮기기 전 `scripts/common.py` `guarded_state()` 가 그 revision이 `ready()` 인지,
즉 `active=true`, `Healthy`, `Provisioned`, `Running` 인지 확인합니다. retire는 바로 이
revision을 `deactivate` 했으므로 replica가 0개입니다. 확인 없이 트래픽을 보내면
모든 요청이 실패하므로, restore는 아무것도 바꾸지 않고 멈춥니다. 재활성화는 같은
revision 이름과 template을 다시 기동하는 것이라 baseline의 template hash 검사도 그대로
통과합니다.

되돌려야 한다면:

```powershell
# 1. 이전 revision 재활성화 (같은 template으로 새 replica 기동)
az containerapp revision activate -g <RG> -n <APP> --revision <APP>--r<이전 run>
az containerapp revision show -g <RG> -n <APP> --revision <APP>--r<이전 run> `
  --query "properties.{active:active,health:healthState,running:runningState}"
# active=true, Healthy, Running 확인

# 2. 평소처럼 보호된 복구
gh workflow run restore.yml --repo $repo --ref main -f source_run_id=<RELEASE_RUN_ID> -f region=kr
```

`evidence/<region>/demo-retire.json` (`demo-retire-evidence-<run>-<region>` artifact)에
비활성화한 revision과 시각이 남습니다. 이 workflow는 데모 전용이며 운영 revision 정리
정책([revision-cleanup.md](../docs/spec/revision-cleanup.md))을 대신하지 않습니다.

## 5.11 수정 지도 (나중에 고칠 때)

| 바꾸고 싶은 것 | 파일 / 위치 | 함께 확인할 것 |
| --- | --- | --- |
| 긴 작업 길이, 동시 개수, 종료 여유 | `app/server.py` `Settings.from_env` 기본값 또는 앱 env | 5.5 규칙, `tests/test_zero_downtime.py` `LifecycleTests` |
| 자동 상한(230초) | `server.py` `INGRESS_TIMEOUT_CAP_SECONDS` | ACA ingress timeout |
| drain 종료 조건 | `server.py` `Lifecycle.should_exit` | `LifecycleTests` |
| 신호 처리 | `server.py` `install_signal_handlers()` → `Lifecycle.request_drain()` (lock 없이 플래그만 설정, 실제 전환은 main loop의 `should_exit()`) | `test_signal_during_locked_section_does_not_deadlock`, `ProcessSignalTests` (POSIX) |
| 엔드포인트 응답 필드 | `server.py` `Handler.demo_get`, `Handler.long_task` | `EndpointTests`, `verify.html` 의 사용처 |
| CORS 허용 경로 | `server.py` `cors_path`, `CORS_HEADERS` | 화면이 revision FQDN을 호출하는지 |
| grace 값 | `infra/revision-policy.json` → 새 release | `scripts/snapshot.py`, `common.expected_candidate`, `infra/region.bicep` |
| probe 경로/타이밍 | `infra/health-probes.json` → 새 release | `tests/test_safety.py` probe 테스트 |
| 화면 주기·색·창 크기 | `app/verify.html` 맨 위 `CONFIG` | — |
| 카드 상태 규칙 | `verify.html` `pollStatus()` | 5.8 표 |
| 버전 라벨 형식 | `.github/workflows/build.yml` `--build-arg APP_VERSION=`, 호출 측 `version_prefix` | `app/Dockerfile` `ARG` |
| retire 안전 조건/대기 시간 | `scripts/retire.py` `retire(timeout=300, interval=5)` | `tests/test_safety.py` `test_retire_*` |
| retire workflow 입력/승인 환경 | `.github/workflows/demo-retire.yml` | `test_region_workflows_are_sequential_and_share_restore_lock` |

로컬 확인:

```powershell
python -m unittest discover -s tests -v
$env:LONG_TASK_SECONDS='20'; $env:GRACE_SECONDS='40'; python app/server.py
# 브라우저: http://localhost:8080/verify → "긴 작업 시작" → 서버 창에서 Ctrl+C
# 기대: 카드 DRAINING → 약 20초 뒤 "완료 … drainObserved: 예" → 카드 STOPPED, 서버 exit 0
```

Windows 콘솔의 `Ctrl+C`(SIGINT)는 SIGTERM과 똑같이 drain을 시작합니다. Linux/macOS에서는
`kill -TERM <pid>` 로 실제 SIGTERM을 보낼 수 있습니다
([INSTRUCTION.linux.md](../INSTRUCTION.linux.md) 2장).
