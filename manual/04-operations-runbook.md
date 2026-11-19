# 4장. 운영 Runbook

명령은 PowerShell 7 기준입니다. 공통 변수:

```powershell
$subscription = '<구독 GUID>'
$repo = 'OWNER/REPOSITORY'
$acr = '<전역 고유 ACR 이름>'
$prefix = 'safeaca'
```

## 4.1 최초 구성 절차 (1회)

| 순서 | 작업 | 명령/위치 | 완료 확인 |
| --- | --- | --- | --- |
| 1 | 로컬 검증 | `python -m unittest discover -s tests -v` | 전체 통과 |
| 2 | Azure 인프라 생성 | `.\scripts\bootstrap.ps1 -SubscriptionId $subscription -AcrName $acr -Prefix $prefix` | `state\bootstrap-outputs.json` 생성 |
| 3 | GitHub Environment 7개 생성 | Settings → Environments | `build`, `prod-{kr,sea,usw}`, `prod-rollback-{kr,sea,usw}` |
| 4 | Environment 보호 | 각 환경 | `main` 브랜치 제한, `prod-*` required reviewer, self-review 금지 |
| 5 | OIDC claim 수집 | `gh workflow run oidc-inspect.yml --repo $repo --ref main` | 7개 artifact |
| 6 | claim 다운로드 | `gh run download <run> --repo $repo -D state\oidc-claims` | `claims-<env>.json` |
| 7 | identity 구성 | `configure-identity.ps1` × 7 ([3장 3.3절](03-scripts-reference.md)) | `build` 변수 4개, 지역 환경 변수 6개 |
| 8 | demo 릴리스로 E2E 검증 | 4.2절 | 3개 지역 `success.json` |

## 4.2 릴리스 실행

### 사전 점검표

- [ ] `main` 에 머지된 변경이 리뷰/CI 통과
- [ ] 진행 중인 release/restore run 없음 (`gh run list --workflow release.yml`)
- [ ] production 프로필: 각 지역에 충분한 organic 트래픽(5분당 candidate 100건 이상)
- [ ] DB migration/외부 side effect 가 있다면 별도 rollback 계획 준비
- [ ] 승인자와 on-call 대기

### 실행

```powershell
# 교육/검증용 (지역당 약 10~20분)
gh workflow run release.yml --repo $repo --ref main -f profile=demo

# 운영용 (지역당 3시간 이상, 전체 9시간 이상)
gh workflow run release.yml --repo $repo --ref main -f profile=production

gh run list --repo $repo --workflow release.yml --limit 3
gh run watch <RELEASE_RUN_ID> --repo $repo
```

> **RELEASE_RUN_ID를 반드시 기록하세요.** 복구 시 필요합니다.

### 승인자 체크 (prod-kr / prod-sea / prod-usw)

1. 직전 지역 job의 evidence artifact에서 `success.json` 확인
2. 직전 지역의 Azure Monitor 경보/대시보드 이상 없음
3. 업무 피크/동결 기간이 아님
4. 승인 → GitHub UI "Review deployments"

### 진행 중 모니터링

로그에서 다음 JSON 라인을 확인합니다.

```text
{"window": "...", "gate": {"candidate": {...}, "weight": 10, ...}}   ← window 통과
Waiting for telemetry for closed window ending ...                   ← 수집 지연 재시도
{"demo_load_interval": {...}}                                        ← demo 부하 통계
```

Azure 측 직접 확인(읽기 전용):

```powershell
az containerapp ingress traffic show -g "$prefix-kr" -n "$prefix-kr-app" -o table
az containerapp revision list -g "$prefix-kr" -n "$prefix-kr-app" `
  --query "[].{name:name,active:properties.active,health:properties.healthState,traffic:properties.trafficWeight}" -o table
```

## 4.3 결과 판정

| 상황 | 의미 | 후속 작업 |
| --- | --- | --- |
| 모든 job 성공 | 3개 지역 candidate 100% | 이전 revision은 0%로 활성 유지(포렌식용). 필요 시 별도 정리 |
| 지역 X job 실패, 로그에 `Restored exact baseline traffic` | X 자동 복구 완료, 이후 지역 미실행 | evidence 분석 → 수정 후 **새 release** |
| 지역 X 실패, `ROLLBACK NOT VERIFIED` | 자동 복구 실패 | **즉시 4.4 수동 복구** |
| 지역 X 실패, `mutation-intent` 없음 | preflight 단계 실패, Azure 변경 없음 | 원인(drift, stable 비정상, baseline SLO) 해결 |
| run 취소/runner 유실 | 복구 보장 없음 | run이 `completed` 된 후 4.4 수동 복구 |

evidence 다운로드:

```powershell
gh run download <RELEASE_RUN_ID> --repo $repo -p "evidence-*" -D evidence-review
Get-ChildItem evidence-review -Recurse -Filter *.json
```

## 4.4 수동 복구 (restore workflow)

### 언제 사용하나

- 자동 복구가 실패/미확인(`ROLLBACK NOT VERIFIED`)
- release run이 취소되었거나 runner가 사라짐
- 이미 성공한 릴리스를 사후에 되돌려야 함 (단, 더 최신 릴리스가 없을 때)

### 절차

```powershell
# 1. 원본 release run이 completed 인지 확인
gh run view <RELEASE_RUN_ID> --repo $repo --json status,conclusion,headBranch

# 2-a. 단일 지역
gh workflow run restore.yml --repo $repo --ref main `
  -f source_run_id=<RELEASE_RUN_ID> -f region=sea

# 2-b. 전체 (usw → sea → kr)
gh workflow run restore.yml --repo $repo --ref main `
  -f source_run_id=<RELEASE_RUN_ID> -f region=all

# 3. prod-rollback-* 승인 후 진행 확인
gh run watch <RESTORE_RUN_ID> --repo $repo
```

### 주의

- `source_run_id` 는 **원본 release run ID** 입니다(restore run ID 아님).
- 더 최신 릴리스가 배포된 뒤에는 이전 snapshot으로 되감을 수 없습니다(drift로 거부).
- 시작되지 않은 지역은 artifact가 없어 실패하며 아무것도 변경하지 않습니다(정상).
- 트래픽만 되돌립니다. DB/secret/DNS/외부 시스템 변경은 별도 절차가 필요합니다.
- `demo-retire.yml` 을 실행한 release는 이전 revision이 비활성이라 restore가
  `Stable unavailable` 로 거부됩니다. 먼저 `az containerapp revision activate` 로 이전
  revision을 살린 뒤 restore합니다([5.10](05-zero-downtime-demo.md#510-retire-이후-복구)).

## 4.5 장애 대응 의사결정

```text
배포 실패 알림
 │
 ├─ 로그에 "Restored exact baseline traffic" ? ── 예 → 서비스 정상. 원인 분석 후 새 release
 │
 ├─ "ROLLBACK NOT VERIFIED" 또는 취소/유실 ? ─── 예 → restore.yml (해당 지역 또는 all)
 │                                                     │
 │                                                     ├─ 성공 → 원인 분석
 │                                                     └─ drift 오류 → 4.6
 │
 └─ "drift"/"Unknown revision" 등 guard 오류 ? ── 예 → 4.6 (자동화 중단, 수동 조사)
```

## 4.6 Drift 오류 조사

guard 오류는 **누군가 파이프라인 밖에서 앱을 변경했거나, 다른 릴리스가 진행 중**임을
뜻합니다. 파이프라인은 의도적으로 덮어쓰지 않습니다.

1. 동시 실행 확인: `gh run list --repo $repo --workflow release.yml --status in_progress`
2. Azure Activity Log 에서 해당 Container App 의 최근 쓰기 작업과 주체 확인
3. 현재 트래픽과 revision 상태 확인(4.2절 명령)
4. 변경 주체와 협의하여 의도된 상태를 결정
5. 서비스 영향이 있으면 incident 절차에 따라 **알려진 정상 revision 이름을 명시해서**
   트래픽을 지정(예: `az containerapp ingress traffic set ... --revision-weight <name>=100`).
   `latest` 사용 금지
6. 상태가 안정되면 새 release run 으로 정상 경로 복귀

## 4.7 정기 운영 작업

| 주기 | 작업 |
| --- | --- |
| 릴리스마다 | RELEASE_RUN_ID, 승인자, evidence 위치 기록 |
| 월간 | 비활성 revision 수 확인(`maxInactiveRevisions: 20`), ACR 이미지 보존 정책 점검 |
| 분기 | 비운영 환경에서 실패 주입(`DEMO_FAILURE_PERCENT`, `DEMO_LATENCY_MS`) + restore 훈련 |
| 분기 | OIDC identity RBAC 검토, 불필요한 역할 제거, environment 보호 규칙 확인 |
| 변경 시 | 기본 브랜치/지역/환경명 변경 시 workflow, `check_source.py`, `inspect_oidc.py`, `configure-identity.*` 동시 수정 |

## 4.8 실패 주입 훈련 (비운영 전용)

별도 실습 앱 또는 로컬에서만 수행합니다.

```powershell
$env:DEMO_FAILURE_PERCENT = '100'   # /work 503
$env:DEMO_LATENCY_MS = '600'        # p95 SLO 초과 유도
python app\server.py
```

실습 Container App 에서는 candidate 이미지/환경 변수로 위 값을 주입한 뒤 demo
release 를 실행하여 10% 단계에서 gate 실패 → 자동 복구 → `automatic-rollback.json`
생성까지 확인합니다.

## 4.9 운영 전 최종 점검표

- [ ] 구독/지역별 서비스·할당량·Policy·비용 검토
- [ ] Environment 승인자, `main` 제한, self-review 금지
- [ ] 실제 protected job 에서 OIDC claim 수집 후 trust 구성
- [ ] 환경별 분리 identity + 최소 RBAC, client secret 없음
- [ ] 비운영에서 bootstrap → demo release → 실패 주입 → restore 전체 시험
- [ ] artifact 보존 90일, ACR 이미지/revision 보존
- [ ] DB/메시지/DNS/secret 등 트래픽 외 변경의 rollback 계획
- [ ] Azure Monitor alert + Action Group (runner 와 독립된 경보)
- [ ] `metrics.py` 임계값을 서비스 SLO 에 맞게 조정
