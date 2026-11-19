# 6장. 무중단 배포 실습 가이드 (사전 점검 · 데모 · 테스트)

5장이 데모 앱의 **설계와 수정 지도**라면, 이 장은 실제로 데모를 준비하고 실행하고
검증하는 **실습 절차**입니다. 2026-11 KR/SEA/USW 환경에서 실제 수행한 내용과 그 과정에서
만난 문제를 바탕으로 정리했습니다.

> 예시 값은 실습 당시 값입니다. 자신의 환경에서는 `<…>` 를 바꿔 쓰세요.
>
> | 항목 | 실습 당시 값 |
> | --- | --- |
> | 저장소 | `iljoong/github-sdp` |
> | KR 앱 / RG | `safeaca-kr-app` / `safeaca-kr` |
> | KR 앱 FQDN | `safeaca-kr-app.greencoast-9d197771.koreacentral.azurecontainerapps.io` |
> | v1 release (r13) | run `44452501783` → revision `safeaca-kr-app--r44452501783` |
> | v2 release (r14) | run `44460515139` → revision `safeaca-kr-app--r44460515139` |

## 6.1 무중단 배포 핵심 정리

| 주제 | 이 저장소의 구현 | 왜 필요한가 |
| --- | --- | --- |
| liveness vs readiness | `/healthz`(프로세스 살아 있음) / `/readyz`(새 요청 받을 수 있음) 분리 | drain 중에는 ready=✗ 로 새 트래픽만 빼고, 살아 있으므로 재시작되지 않음 |
| SIGTERM 처리 | 신호 핸들러는 플래그만 세움 → 메인 루프가 `DRAINING` 전환 → 진행 중 작업 종료 후 exit 0 | 신호 핸들러에서 lock을 잡으면 교착(deadlock) 위험. 실제로 이 버그를 테스트로 잡고 수정함 |
| 종료 유예 | `terminationGracePeriodSeconds = 180` (`infra/revision-policy.json`) | 기본 30초로는 2분 작업이 SIGKILL 됨 |
| 앱 측 상한 | 긴 작업 ≤ `min(LONG_TASK_SECONDS, GRACE − MARGIN − 5, 230)` | ACA ingress 요청 timeout 240초, SIGKILL 전 여유 |
| 점진적 전환 | canary 0 → 10 → 50 → 100% (quick 프로필: 0 → 50 → 100%), 단계별 bake + Log Analytics health gate | 문제를 일부 트래픽에서 먼저 발견, 실패 시 자동 복구 |
| 이전 revision 유지 | 운영 release는 이전 revision을 **활성 상태로 남김** | 즉시 restore 가능. 그래서 데모에서는 `demo-retire.yml` 로 따로 SIGTERM을 발생시킴 |

**용어 구분 (실습 중 혼동했던 부분)**

| 상태 | 의미 | SIGTERM? | 파이프라인 영향 |
| --- | --- | --- | --- |
| 트래픽 0% | 라우팅만 제외, replica는 계속 실행 | 아니오 | 없음 |
| scale-to-zero (`minReplicas: 0`) | 요청이 없으면 replica 0개 (`ScaledToZero`) | 축소 시 예 | `snapshot.py` 가 거부 (`minReplicas >= 1`, `runningState` Running 필요) |
| deactivate (`active=false`) | revision 자체 비활성화, replica 종료 | **예** | `restore.yml` 이 거부 (5.10, 6.6) |

## 6.2 사전 점검 체크리스트

데모 전에 아래를 모두 확인합니다. 하나라도 어긋나면 release가 snapshot 단계에서
**안전하게 실패**합니다(변경 없음).

### (1) 트래픽을 받는 revision이 min 1 이상이고 Running

```powershell
foreach ($r in 'kr','sea','usw') {
  "== $r"
  az containerapp revision list -g safeaca-$r -n safeaca-$r-app `
    --query "[?properties.active].{name:name, traffic:properties.trafficWeight, min:properties.template.scale.minReplicas, state:properties.runningState}" -o table
}
```

- 트래픽 100% revision의 `min` ≥ 1, `state` 가 `Running` 또는 `RunningAtMaxScale` 이어야 합니다.
- 앱 template만 min 1로 고쳐도 **트래픽이 예전 min 0 revision에 남아 있으면** 실패합니다.
  같은 이미지의 min 1 revision이 있다면 트래픽을 옮깁니다.

```powershell
az containerapp ingress traffic set -g safeaca-kr -n safeaca-kr-app --revision-weight <min1-revision>=100
curl.exe -s -o NUL -w "%{http_code}`n" https://<APP FQDN>/healthz   # 200 확인
```

### (2) 앱이 drain 지원 코드이고 grace/probe 가 적용됨

```powershell
az containerapp show -g safeaca-kr -n safeaca-kr-app --query "{grace:properties.template.terminationGracePeriodSeconds, probes:properties.template.containers[0].probes[].{t:type,p:httpGet.path}}" -o json
# grace: 180, Readiness: /readyz, Liveness/Startup: /healthz
curl.exe -s https://<APP FQDN>/api/version   # version, revision, dnsSuffix 필드가 있어야 함
```

아니라면 5.6의 **준비용 release** 를 1회 실행합니다(그 결과가 데모의 v1).

### (3) OIDC 로그인 (UAMI) 과 GitHub 보호 설정

| 확인 | 명령 / 위치 | 기대값 |
| --- | --- | --- |
| 환경 변수 | `gh variable list --env prod-kr --repo <repo>` | `AZURE_CLIENT_ID` = `safeaca-identity` RG의 UAMI client ID |
| branch 제한 | Settings → Environments | 7개 환경 모두 `main` 만 |
| 승인자 | Settings → Environments → `prod-*` | Required reviewers 지정 |
| main 보호 | Settings → Branches | PR 필수, force-push/삭제 금지 |

> 실습 중 Entra 앱(service principal) 로그인이 Conditional Access로 차단(`AADSTS53003`)되어
> user-assigned managed identity로 전환했습니다. 절차는 3.3절 참고.

## 6.3 데모 절차 (KR, 약 25~35분)

| 단계 | 행동 | 확인 |
| --- | --- | --- |
| 0 | 브라우저로 `https://<APP FQDN>/verify` 열기. (선택) 터미널에서 6.4-B 폴링 실행 | 큰 글자 v1(`r13`), 카드 1개 `SERVING` |
| 1 | `gh workflow run release.yml --repo <repo> --ref main -f profile=demo` | build 성공 후 `prod-kr` 승인 대기 |
| 2 | Actions → run → **Review deployments** → `prod-kr` 승인 | 10% → 50% → 100% 동안 스트립 두 색 혼합 → v2 색. **오류 0** |
| 3 | `prod-sea`, `prod-usw` 도 승인하고 **run 전체 완료**까지 대기 | v2 카드 등장, v1 카드 트래픽 0%·`SERVING` |
| 4 | v1 카드 **"긴 작업 시작"** 클릭 (또는 6.4-C curl로 v1 revision FQDN 호출) | 진행 막대 `0s / 약 120s` |
| 5 | 바로 retire 실행 후 `prod-kr` 승인 (아래) | — |
| 6 | deactivate → v1 replica SIGTERM | v1 카드 `DRAINING`, ready ✗, grace 180s부터 감소 |
| 7 | 약 120초 후 | `완료 … drainObserved: 예`, 약 2초 뒤 v1 카드 `STOPPED` |
| 8 | 결과 정리 | 스트립에 빨강 없음, 오류 카운터 0 |

### retire 명령 상세

```powershell
$release = <1단계 release run ID>          # 예: 44460515139
gh run view $release --repo <repo> --json status,conclusion   # completed / success 확인
gh workflow run demo-retire.yml --repo <repo> --ref main -f source_run_id=$release -f region=kr
Start-Sleep 5
$rid = gh run list --repo <repo> --workflow demo-retire.yml -L 1 --json databaseId -q '.[0].databaseId'
gh run watch $rid --repo <repo>             # 승인은 GitHub UI에서
```

| 인자 | 의미 |
| --- | --- |
| `--ref main` | workflow 와 `prod-kr` 환경 모두 `main` 만 허용 |
| `source_run_id` | v2를 배포한 release run. 그 run의 `baseline-<run>-kr` artifact에 기록된 **배포 직전 stable revision(v1)** 이 retire 대상 |
| `region` | 한 지역만. `/verify` 를 연 지역과 같아야 함 |

workflow 내부: `check_source.py`(완료된 main release인지) → baseline·`success.json` 다운로드 →
출처(commit, run) 일치 확인 → `prod-kr` 승인 → UAMI 로그인 → `retire.py`(drift 확인 →
v1 deactivate → v2 응답 확인) → `demo-retire-evidence-<run>-kr` 업로드(90일).

**타이밍 규칙**

- release run **전체**(sea, usw 포함)가 끝난 뒤에만 실행합니다. 같은 concurrency group
  (`safe-aca-production`)이라 일찍 실행하면 대기하고, `check_source.py` 는 completed run만 허용합니다.
- 긴 작업 버튼은 retire **직전**에 누릅니다. 이미 DRAINING인 revision은 새 작업을 503으로 거절합니다.
- 같은 release로 두 번 실행하면 `Stable unavailable` 로 실패합니다(이미 비활성, 추가 조치 불필요).

### 6.3.1 Quick 모드 데모 (KR만, 약 10~15분)

`release-kr.yml` 은 build 후 KR만 배포하고, 기본 프로필 `quick` 으로 canary를
**stable:candidate = 100:0 → 50:50 → 0:100** 두 단계, 단계별 60초(30초 window × 2)로 진행합니다.
sea/usw 승인을 기다릴 필요가 없어 KR rollout이 끝나면 곧바로 retire할 수 있습니다.

| 비교 | `release.yml` + demo | `release-kr.yml` + quick |
| --- | --- | --- |
| 지역 | kr → sea → usw | kr |
| 승인 | `prod-kr`, `prod-sea`, `prod-usw` | `prod-kr` |
| canary (candidate %) | 10 → 50 → 100 | 50 → 100 |
| 단계별 bake | 180초 | 60초 |
| 버전 라벨 | `r<N>` | `k<N>` |
| retire 가능 시점 | 3개 지역 완료 후 (약 45~60분) | KR 완료 후 (약 10분) |

> quick은 교육용입니다. window가 짧고 단계가 적어 회귀 탐지력이 낮습니다. 운영 판단에는
> `production` 을 쓰세요. 실행 후 SEA/USW는 이전 버전으로 남습니다(지역 간 버전 불일치).
> retire 후 이전 버전으로 되돌릴 때는 6.6을 따르되 `region=kr` 로 실행합니다.

절차: ① `/verify` 열기 → ② release-kr 실행 → ③ `prod-kr` 승인 → ④ 화면에서 50:50 혼합 → v2 확인,
run 완료 대기 → ⑤ v1 긴 작업 시작 → ⑥ retire 실행·승인 → ⑦ `DRAINING` → `drainObserved: 예` → `STOPPED`.

**PowerShell**

```powershell
$repo = 'iljoong/github-sdp'

# 1) release-kr 실행 (profile 생략 시 quick)
gh workflow run release-kr.yml --repo $repo --ref main
Start-Sleep 5
$release = gh run list --repo $repo --workflow release-kr.yml -L 1 --json databaseId -q '.[0].databaseId'
"release run: $release"

# 2) prod-kr 승인 (UI의 Review deployments 대신 API 사용 가능, 승인자 본인 계정이어야 함)
$envId = gh api "repos/$repo/environments/prod-kr" -q .id
do { Start-Sleep 15; $pending = gh api "repos/$repo/actions/runs/$release/pending_deployments" -q 'length' } while ($pending -eq '0')
gh api -X POST "repos/$repo/actions/runs/$release/pending_deployments" `
  -F "environment_ids[]=$envId" -f state=approved -f comment='quick demo' | Out-Null

# 3) 완료 대기 (success 확인)
gh run watch $release --repo $repo --exit-status
gh run view $release --repo $repo --json status,conclusion

# 4) v1 긴 작업 시작: 화면 버튼 또는 별도 터미널에서 curl (v1 revision FQDN)
#    curl.exe -sS -X POST -m 240 -w "`nHTTP %{http_code}`n" https://<v1 revision FQDN>/api/long-task

# 5) retire 실행 + 승인 + 대기
gh workflow run demo-retire.yml --repo $repo --ref main -f source_run_id=$release -f region=kr
Start-Sleep 5
$retire = gh run list --repo $repo --workflow demo-retire.yml -L 1 --json databaseId -q '.[0].databaseId'
do { Start-Sleep 10; $pending = gh api "repos/$repo/actions/runs/$retire/pending_deployments" -q 'length' } while ($pending -eq '0')
gh api -X POST "repos/$repo/actions/runs/$retire/pending_deployments" `
  -F "environment_ids[]=$envId" -f state=approved -f comment='quick demo retire' | Out-Null
gh run watch $retire --repo $repo --exit-status
```

**Bash**

```bash
repo=iljoong/github-sdp

# 1) release-kr 실행 (profile 생략 시 quick)
gh workflow run release-kr.yml --repo "$repo" --ref main
sleep 5
release=$(gh run list --repo "$repo" --workflow release-kr.yml -L 1 --json databaseId -q '.[0].databaseId')
echo "release run: $release"

# 2) prod-kr 승인
env_id=$(gh api "repos/$repo/environments/prod-kr" -q .id)
until [ "$(gh api "repos/$repo/actions/runs/$release/pending_deployments" -q 'length')" != 0 ]; do sleep 15; done
gh api -X POST "repos/$repo/actions/runs/$release/pending_deployments" \
  -F "environment_ids[]=$env_id" -f state=approved -f comment='quick demo' >/dev/null

# 3) 완료 대기
gh run watch "$release" --repo "$repo" --exit-status
gh run view "$release" --repo "$repo" --json status,conclusion

# 4) v1 긴 작업 시작: 화면 버튼 또는 별도 터미널에서
#    curl -sS -X POST -m 240 -w '\nHTTP %{http_code}\n' https://<v1 revision FQDN>/api/long-task

# 5) retire 실행 + 승인 + 대기
gh workflow run demo-retire.yml --repo "$repo" --ref main -f source_run_id="$release" -f region=kr
sleep 5
retire=$(gh run list --repo "$repo" --workflow demo-retire.yml -L 1 --json databaseId -q '.[0].databaseId')
until [ "$(gh api "repos/$repo/actions/runs/$retire/pending_deployments" -q 'length')" != 0 ]; do sleep 10; done
gh api -X POST "repos/$repo/actions/runs/$retire/pending_deployments" \
  -F "environment_ids[]=$env_id" -f state=approved -f comment='quick demo retire' >/dev/null
gh run watch "$retire" --repo "$repo" --exit-status
```

`/verify` 대신 터미널로 보려면 6.4-B 폴링을 같이 실행합니다. quick에서는 50:50 구간이 약 2분이라
버전이 `r<N>`/`k<N>` 으로 섞이는 시간이 짧습니다.

다른 프로필로 KR만 배포하려면 `-f profile=demo`(10/50/100, 180초) 또는 `-f profile=production` 을 붙입니다.

## 6.4 테스트 방법

### A. 검증 화면 (`/verify`)

화면 요소의 의미는 5.8절 표를 참고합니다. 판정 기준만 요약하면:

| 판정 | 조건 |
| --- | --- |
| 무중단 배포 성공 | 상단 오류 카운터 0, 스트립에 빨강 칸 없음 |
| SIGTERM 안전 처리 성공 | 긴 작업 `완료 … drainObserved: 예`, 이후 카드 `STOPPED` |
| 확인 필요 | `DRAINING` 없이 바로 `UNREACHABLE`, 또는 긴 작업 `실패 … network` (5.9절) |

### B. 터미널 폴링 (화면 없이 오류 수 측정)

```powershell
$u = 'https://<APP FQDN>/api/version'; $ok = 0; $err = 0
while ($true) {
  $t = Get-Date
  try {
    $r = Invoke-RestMethod $u -TimeoutSec 3
    $ok++; "{0:HH:mm:ss} {1,-5} {2} ok={3} err={4}" -f $t, $r.version, ($r.revision -split '--')[-1], $ok, $err
  } catch {
    $err++; "{0:HH:mm:ss} ERROR {1} ok={2} err={3}" -f $t, $_.Exception.Message, $ok, $err
  }
  $wait = 1000 - ((Get-Date) - $t).TotalMilliseconds
  if ($wait -gt 0) { Start-Sleep -Milliseconds $wait }
}
```

Bash:

```bash
u="https://<APP FQDN>/api/version"; ok=0; err=0
while true; do
  if body=$(curl -fsS -m 3 "$u"); then ok=$((ok+1)); echo "$(date +%T) $(jq -r .version <<<"$body") ok=$ok err=$err"
  else err=$((err+1)); echo "$(date +%T) ERROR ok=$ok err=$err"; fi
  sleep 1
done
```

release 동안 `err` 가 0으로 유지되고 버전이 v1 → (혼합) → v2 로 바뀌면 성공입니다.

> 루트 `/` 는 라우트가 없어 **404** 입니다. 헬스 확인은 `/healthz`, 버전은 `/api/version` 을 쓰세요.

### C. 긴 작업 직접 호출 (curl)

요청 body는 필요 없습니다. 작업이 기본 120초이므로 timeout(`-m`)을 240으로 둡니다.
Windows에서는 `curl.exe` 를 명시합니다(PowerShell의 `curl` 별칭 회피).

```powershell
# 공유 FQDN: 트래픽 비율에 따라 revision 결정
curl.exe -sS -X POST -m 240 -w "`nHTTP %{http_code}  %{time_total}s`n" https://<APP FQDN>/api/long-task

# 특정 revision 지정 (화면 버튼과 동일) - retire 대상 v1을 겨냥할 때 사용
curl.exe -sS -X POST -m 240 -w "`nHTTP %{http_code}  %{time_total}s`n" https://<APP>--<revision 접미사>.<DNS 접미사>/api/long-task

# revision FQDN 조회
az containerapp revision list -g <RG> -n <APP> --query "[?properties.active].{name:name,fqdn:properties.fqdn,traffic:properties.trafficWeight}" -o table

# 진행 상태 (state, inflight, graceRemaining)
curl.exe -s https://<revision FQDN>/status
```

| 상황 | HTTP | 응답 |
| --- | --- | --- |
| 정상 완료 | 200 | `{"completed":true,"drainObserved":false,"durationMs":120004,…}` |
| 작업 중 SIGTERM, 끝까지 처리 | 200 | `"drainObserved":true` ← **핵심 증거** |
| DRAINING 중인 revision에 새 요청 | 503 | `{"error":"draining"}` |
| replica당 동시 3개 초과 | 429 | `{"error":"too many long tasks"}` |

데모 변형: 터미널 1에서 v1 revision FQDN으로 위 curl 실행 → 터미널 2에서 retire 실행 →
약 120초 후 터미널 1에 `HTTP 200` + `"drainObserved":true`.

### D. Azure CLI로 revision 상태 확인

```powershell
az containerapp revision list -g safeaca-kr -n safeaca-kr-app `
  --query "[].{name:name, active:properties.active, traffic:properties.trafficWeight, state:properties.runningState, replicas:properties.replicas}" -o table
```

retire 후 v1이 `active=False`, v2가 트래픽 100% 이면 정상입니다.

### E. Log Analytics (사후 증거)

앱은 상태 전환마다 `kind: lifecycle`(`serving`/`draining`/`stopped`), 긴 작업마다
`kind: long_task`(`start`/`finish`, `drainObserved`) 로그를 남깁니다.

```powershell
az config set extension.use_dynamic_install=yes_without_prompt   # 최초 1회: log-analytics 확장 자동 설치
$ws = az monitor log-analytics workspace show -g safeaca-kr -n safeaca-kr-app-logs --query customerId -o tsv
$q = @'
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(2h)
| extend d = parse_json(Log_s)
| where tostring(d.kind) in ('lifecycle', 'long_task')
| project TimeGenerated, RevisionName_s, logKind = tostring(d.kind), logEvent = tostring(d.event), drainObserved = tostring(d.drainObserved)
| order by TimeGenerated asc
'@ -replace "`r?`n", ' '
az monitor log-analytics query -w $ws --analytics-query $q -o table
```

> Windows 주의: 여러 줄 쿼리를 그대로 넘기면 첫 줄만 전달되므로 `-replace` 로 한 줄로 만듭니다.
> 열 이름에 `kind` 처럼 KQL 예약어를 쓰면 `SYN0002` 구문 오류가 납니다(`logKind` 사용).
> Portal에서는 Log Analytics 작업 영역 → **로그** 에 같은 쿼리를 붙여 넣으면 됩니다.

기대 순서(v1): `long_task start` → `lifecycle draining` → `long_task finish (drainObserved=true)` → `lifecycle stopped`.
로그 수집은 수 분 지연될 수 있습니다. 실습 실제 결과(UTC, v1 = `r44452501783`):

```text
05:06:49.8  long_task  start
05:06:54.3  lifecycle  draining        ← retire(deactivate)로 SIGTERM 수신
05:08:49.5  long_task  finish   drainObserved=true   ← drain 중에도 120초 작업 완료
05:08:52.7  lifecycle  stopped         ← 작업 종료 후 약 3초 만에 정상 종료 (grace 180초 이내)
```

### F. 단위 테스트 (코드 수정 시)

```powershell
python -m unittest discover -s tests       # drain 상태 머신, SIGTERM 교착 방지, 설정 검증 포함
python -m compileall -q app scripts tests
```

## 6.5 데모 후 정리

- 다음 데모는 v2가 새 v1이 됩니다. 별도 정리 없이 6.3을 반복하면 됩니다.
- retire로 비활성화된 revision은 `maxInactiveRevisions: 20` 범위 안에서 남아 있습니다.
- 검증용으로 부여했던 임시 권한(예: 운영자 VM의 RBAC Administrator)은 회수합니다.

## 6.6 retire 이후 이전 버전으로 되돌리기

retire 후에는 `restore.yml` 이 **거부**됩니다. 이전 revision이 비활성(replica 0)이라
`guarded_state()` 의 `ready()` 검사(active, Healthy, Provisioned, Running)를 통과하지 못하기
때문입니다. 먼저 재활성화한 뒤 복구합니다(자세한 이유는 5.10절).

```powershell
az containerapp revision activate -g <RG> -n <APP> --revision <APP>--r<이전 run>
az containerapp revision show -g <RG> -n <APP> --revision <APP>--r<이전 run> `
  --query "{active:properties.active, health:properties.healthState, state:properties.runningState}"
# active=true, Healthy, Running 확인 후
gh workflow run restore.yml --repo <repo> --ref main -f source_run_id=<release run> -f region=kr
```

## 6.7 실습 중 만난 문제와 교훈

| 증상 | 원인 | 조치 / 교훈 |
| --- | --- | --- |
| 모든 지역 트래픽이 `ScaledToZero` revision | 일시적으로 `minReplicas: 0` 으로 바꾼 revision이 100% 트래픽 보유 | 같은 이미지의 min 1 revision으로 트래픽 이동. template만 고쳐서는 부족 (6.2-1) |
| release KR `azure/login` 실패 `AADSTS53003` | 테넌트 Conditional Access가 workload identity(Entra 앱 SP) 차단 | user-assigned managed identity + federated credential로 전환. 대신 GitHub environment 보호(branch·승인자)를 1차 통제로 |
| UAMI 구성 중 `AuthorizationFailed` | 실행자가 구독 Contributor만 보유(역할 할당 불가) | 대상 scope에 조건부 RBAC Administrator(4개 역할·ServicePrincipal만) 임시 부여 후 실행, 완료 후 회수 |
| SEA가 snapshot에서 실패할 뻔함 | KR만 정리하고 SEA/USW는 min 0 revision에 트래픽 | release는 지역 선택이 없으므로 **3개 지역 모두** 사전 점검 |
| smoke test 404 | `/` 라우트 없음 | `/healthz`, `/api/version` 사용 |
| retire가 시작하지 않고 대기 | release가 sea/usw 진행 중 (같은 concurrency group) | release 전체 완료 후 실행 |
| retire 후 `restore.yml` 거부 | 이전 revision 비활성 | `revision activate` 후 restore (6.6) |
| KQL 조회가 전체 열 반환 / `SYN0002` | Windows에서 여러 줄 인자 잘림, `kind` 예약어 | 한 줄로 변환, 열 이름 변경 (6.4-E) |

## 6.8 실습 검증 기록

| 항목 | 결과 |
| --- | --- |
| UAMI 전환 후 release `44452501783` (demo) | build → kr → sea → usw 모두 success, 3개 지역 `r44452501783` 100% |
| KR v1 상태 | version `r13`, grace 180, Readiness `/readyz`, Liveness/Startup `/healthz`, `/verify` 200 |
| v2 release `44460515139` (r14) + KR retire | 완료 후 KR 활성 revision은 `r44460515139` 1개 (v1 비활성) |
| SIGTERM 중 긴 작업 (Log Analytics) | v1 `draining` 후 진행 중 작업 `finish drainObserved=true`, 이어서 `stopped` (6.4-E) |
| 단위 테스트 | 105 OK (1 skipped) |
