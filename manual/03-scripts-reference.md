# 3장. `scripts\` 상세 레퍼런스

## 3.1 파일 분류

| 분류 | 파일 | 실행 주체 | Azure 변경 | 요약 |
| --- | --- | --- | --- | --- |
| 최초 구성 | `bootstrap.ps1` / `bootstrap.sh` | 운영자(로컬) | **예** (리소스 생성) | ACR, 3개 지역 ACA/Log Analytics 생성, 최초 이미지 배포 |
| 최초 구성 | `configure-identity.ps1` / `configure-identity.sh` | 운영자(로컬) | **예** (UAMI/RBAC) | 환경별 OIDC managed identity, RBAC, GitHub 변수 생성 |
| 구성 검증 | `inspect_oidc.py` | `oidc-inspect.yml` | 아니오 | OIDC claim metadata 수집 |
| 배포 | `snapshot.py` | `region.yml` | 아니오 (읽기 전용) | 배포 직전 baseline 캡처 |
| 배포/복구 | `rollout.py` | `region.yml`, `restore-region.yml` | **예** (revision, 트래픽) | 카나리 배포·bake·자동/수동 복구 |
| 배포 | `metrics.py` | `rollout.py` 가 import | 아니오 | Log Analytics 조회와 health gate 판정 |
| 배포 | `demo_load.py` | `rollout.py` 가 import (demo) | 아니오 | demo 프로필 합성 부하 생성 |
| 공통 | `common.py` | 위 Python 스크립트가 import | 일부 함수 | Azure CLI 래퍼, guard, 정규화, OIDC 재로그인 |
| 복구 | `check_source.py` | `restore-region.yml`, `demo-retire.yml` | 아니오 | 복구 대상 원본 run 검증 |
| 데모 | `retire.py` | `demo-retire.yml` | **예** (revision 비활성화) | 완료된 release의 이전 revision을 비활성화해 SIGTERM 발생 |

> `.ps1` 과 `.sh` 는 기능이 동일한 쌍입니다. Windows/PowerShell 7 은 `.ps1`,
> Linux/macOS Bash 는 `.sh` 를 사용합니다. Bash 버전은 `jq` 가 추가로 필요합니다.

의존 관계:

```text
rollout.py ──► common.py
    ├────────► metrics.py ──► common.py
    └────────► demo_load.py ─► common.py
snapshot.py ─► common.py
inspect_oidc.py ─► common.py
check_source.py ─► common.py
retire.py ─► common.py, rollout.request
```

Python 스크립트는 표준 라이브러리만 사용합니다(외부 pip 패키지 없음). 대신 `az` CLI
(및 demo 프로필의 경우 `curl >= 8.4.0`)가 PATH에 있어야 합니다.

---

## 3.2 `bootstrap.ps1` / `bootstrap.sh` – 최초 Azure 인프라 구성

### 용도

빈 구독에 이 예제가 사용하는 **모든 Azure 리소스를 최초 1회** 생성하고, 첫 번째
stable revision(`<app>--bootstrap`)을 배포합니다.

> ⚠️ **최초 생성 전용**입니다. 라이브 앱에 재실행하거나 복구·업그레이드 수단으로
> 쓰면 안 됩니다. 지역 리소스 그룹에 이미 Container App이 있으면 스크립트가 중단합니다.

### 사전 조건

- PowerShell 7 (또는 Bash), Azure CLI + Bicep, Docker(Linux engine), Python 3.13, (`.sh` 는 `jq`)
- `az login` 완료, 구독에 Contributor + RBAC Administrator(또는 Owner)
- ACR에 push 가능한 권한(AcrPush), registry admin 계정은 사용하지 않음

### 매개변수

| PowerShell | Bash | 필수 | 검증 규칙 | 설명 |
| --- | --- | --- | --- | --- |
| `-SubscriptionId` | `--subscription-id` | 예 | | 대상 구독 GUID |
| `-AcrName` | `--acr-name` | 예 | `^[a-z0-9]{5,50}$` | 전역 고유 ACR 이름 |
| `-Prefix` | `--prefix` | 아니오 | `^[a-z][a-z0-9]{2,10}$`, 기본 `safeaca` | 리소스 그룹/앱 이름 접두사 |

### 처리 순서

1. 구독 선택, resource provider 등록(`Microsoft.App`, `OperationalInsights`, `ContainerRegistry`, `ManagedIdentity`)
2. `<prefix>-shared` 리소스 그룹 생성 → `infra\shared.bicep` 배포(ACR, pull MI, AcrPull)
3. `<prefix>-kr|sea|usw` 리소스 그룹에 Container App이 이미 있는지 검사 → 있으면 중단
4. `az acr login` → `docker build --platform linux/amd64 app` → push (`safe-demo:bootstrap-<epoch>`)
5. digest 조회 → `<server>/safe-demo@sha256:...` 확인
6. AcrPull RBAC 전파를 위해 60초 대기
7. 지역별로 리소스 그룹 생성 → `infra\region.bicep` 배포
8. 결과를 `state\bootstrap-outputs.json` 에 저장

### 사용 예

```powershell
.\scripts\bootstrap.ps1 -SubscriptionId $subscription -AcrName $acr -Prefix safeaca
```

```bash
scripts/bootstrap.sh --subscription-id "$subscription" --acr-name "$acr" --prefix safeaca
```

### 출력: `state\bootstrap-outputs.json`

```json
{
  "kr": {
    "RESOURCE_GROUP": "safeaca-kr",
    "CONTAINER_APP": "safeaca-kr-app",
    "LOG_ANALYTICS_WORKSPACE_ID": "<workspace customer GUID>",
    "WORKSPACE_RESOURCE_ID": "/subscriptions/.../workspaces/safeaca-kr-app-logs",
    "APP_RESOURCE_ID": "/subscriptions/.../containerApps/safeaca-kr-app",
    "FQDN": "safeaca-kr-app.<hash>.koreacentral.azurecontainerapps.io"
  },
  "sea": { ... }, "usw": { ... }
}
```

`state\` 는 `.gitignore` 대상입니다. 커밋하지 마세요.

### 주요 오류

| 메시지 | 원인 / 조치 |
| --- | --- |
| `<group> already contains apps. Bootstrap is not a release/recovery command.` | 이미 구성된 환경. 재실행하지 말고 release/restore workflow 사용 |
| `ACR login failed` | 설치자에게 AcrPush 권한 부여, Docker 실행 여부 확인 |
| `Immutable digest not resolved` | push 실패 또는 ACR 조회 권한 부족 |
| 지역 배포 중 이미지 pull 실패 | AcrPull 전파 지연. 60초 대기는 보장이 아님 — 해당 리소스 그룹 정리 후 재시도 |

---

## 3.3 `configure-identity.ps1` / `configure-identity.sh` – 환경별 OIDC identity 구성

### 용도

GitHub Environment 하나당 **전용 user-assigned managed identity(UAMI)** 를 전용 RG
(`safeaca-identity`)에 만들고, 실제 OIDC claim의 `sub` 로 federated credential을 등록하고,
최소 RBAC 와 GitHub Environment variables 를 설정합니다. managed identity는 **client
secret을 가질 수 없고**, Entra 앱 등록 권한이 필요 없으며, 테넌트의 workload identity
Conditional Access 정책 대상이 아닙니다(그 대신 GitHub environment 보호가 1차 통제입니다).
배포 workflow에서는 호출되지 않는 프로비저닝 도구입니다. workflow는 변경 없이
`AZURE_CLIENT_ID` 만 UAMI의 client ID로 바뀝니다.

### 사전 조건

- `oidc-inspect.yml` 실행으로 얻은 `claims-<environment>.json`
- GitHub에 해당 Environment가 미리 생성되어 있을 것 (`gh variable set --env` 대상)
- identity 전용 RG: `az group create -n safeaca-identity -l koreacentral`.
  이 RG에 쓰기 권한이 있으면 federated credential을 추가해 배포 identity를 가로챌 수
  있으므로 접근을 최소화합니다.
- `az`, `gh` 로그인 (Bash는 `jq` 추가). Azure 권한: identity RG `Contributor`(또는
  `Managed Identity Contributor`) **와** ACR·지역 RG에 역할 할당 권한(`Owner`, `User Access
  Administrator` 또는 `Role Based Access Control Administrator`). 구독 `Contributor` 만으로는
  역할 할당 단계에서 `AuthorizationFailed` 로 멈춥니다.

### 매개변수

| PowerShell | Bash | 필수 | 설명 |
| --- | --- | --- | --- |
| `-Repository` | `--repository` | 예 | `OWNER/REPO` |
| `-Environment` | `--environment` | 예 | `build`, `prod-kr`, `prod-sea`, `prod-usw`, `prod-rollback-kr`, `prod-rollback-sea`, `prod-rollback-usw` |
| `-SubscriptionId` | `--subscription-id` | 예 | 구독 GUID |
| `-ClaimsFile` | `--claims-file` | 예 | `inspect_oidc.py` 가 만든 claims JSON |
| `-AcrName` | `--acr-name` | `build` 일 때 | ACR 이름 |
| `-ResourceGroup` | `--resource-group` | 지역 환경 | `bootstrap-outputs.json` 의 `RESOURCE_GROUP` |
| `-ContainerApp` | `--container-app` | 지역 환경 | `CONTAINER_APP` |
| `-WorkspaceResourceId` | `--workspace-resource-id` | 지역 환경 | `WORKSPACE_RESOURCE_ID` |
| `-WorkspaceCustomerId` | `--workspace-customer-id` | 지역 환경 | `LOG_ANALYTICS_WORKSPACE_ID` |
| `-IdentityResourceGroup` | `--identity-resource-group` | 아니요 | UAMI를 둘 기존 RG, 기본 `safeaca-identity` |

### 검증 단계 (하나라도 실패하면 아무것도 만들지 않음)

1. claim의 `iss` = `https://token.actions.githubusercontent.com`, `aud` = `api://AzureADTokenExchange`
2. `repository` 가 대소문자까지 일치, `ref` = `refs/heads/main`, `inspected_environment` 가 요청 환경과 일치
3. `environment` claim 또는 `sub` 에 `:environment:<env>` 포함
4. `gh api repos/<repo>` 로 현재 `repository_id`, `owner_id` 가 claim과 일치 (저장소 이름 변경/이전 대응)
5. `state\identity-<env>.json` 이 이미 존재하면 중단 (중복 principal 방지)

### 생성 결과

| 대상 | 생성/설정 내용 |
| --- | --- |
| Azure (identity RG) | UAMI `safe-aca-<owner>-<repo>-<env>`, federated credential `github-<env>` |
| RBAC (`build`) | ACR scope `AcrPush`, `Reader` |
| RBAC (지역) | Container App scope `Container Apps Contributor`, RG `Reader`, workspace `Log Analytics Reader` |
| GitHub vars (공통) | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` |
| GitHub vars (`build`) | `ACR_NAME` |
| GitHub vars (지역) | `RESOURCE_GROUP`, `CONTAINER_APP`, `LOG_ANALYTICS_WORKSPACE_ID` |
| 로컬 파일 | `state\identity-<env>.json`(identity resource/client/principal ID), `state\oidc-<env>.json`(federated credential 정의) |

### 기존 Entra 앱(service principal) 구성에서 전환

1. `main` 에서 `oidc-inspect.yml` 을 실행해 claims를 새로 받습니다.
2. 위 사용 예대로 7개 환경을 다시 실행합니다. 마지막 단계에서 `AZURE_CLIENT_ID` 가 UAMI로
   바뀌므로, 그 전까지는 기존 앱으로 계속 배포됩니다.
3. KR demo release로 로그인을 확인한 뒤, 기존 앱의 역할 할당을 제거하고 Entra 관리자에게
   앱 등록 삭제를 요청합니다.

### 사용 예

```powershell
# build
.\scripts\configure-identity.ps1 -Repository $repo -Environment build `
  -SubscriptionId $subscription -AcrName $acr `
  -ClaimsFile "state\oidc-claims\oidc-claims-$inspectRun-build\claims-build.json"

# 지역 deploy + rollback (6회)
$outputs = Get-Content state\bootstrap-outputs.json -Raw | ConvertFrom-Json
foreach ($region in 'kr','sea','usw') {
  $r = $outputs.$region
  foreach ($env in "prod-$region","prod-rollback-$region") {
    .\scripts\configure-identity.ps1 -Repository $repo -Environment $env `
      -SubscriptionId $subscription -ResourceGroup $r.RESOURCE_GROUP `
      -ContainerApp $r.CONTAINER_APP -WorkspaceResourceId $r.WORKSPACE_RESOURCE_ID `
      -WorkspaceCustomerId $r.LOG_ANALYTICS_WORKSPACE_ID `
      -ClaimsFile "state\oidc-claims\oidc-claims-$inspectRun-$env\claims-$env.json"
  }
}
```

```bash
scripts/configure-identity.sh --repository "$repo" --environment prod-kr \
  --subscription-id "$subscription" --resource-group safeaca-kr \
  --container-app safeaca-kr-app --workspace-resource-id "$ws_id" \
  --workspace-customer-id "$ws_guid" --claims-file "state/oidc-claims/.../claims-prod-kr.json"
```

### 주요 오류와 조치

| 메시지 | 조치 |
| --- | --- |
| `Claims do not match the expected issuer/audience/repository/main/environment` | `main` 에서 `oidc-inspect.yml` 재실행, 올바른 환경의 claims 파일 지정 |
| `No environment trust context in inspected claims` | 저장소의 OIDC custom subject template 검토 |
| `Claims IDs do not match current GitHub repository/owner` | 저장소 이름 변경/이전 후 claim 재수집 |
| `Identity record exists: state\identity-<env>.json` | **파일을 지우지 말 것.** 기록된 ID로 누락된 RBAC/변수만 수동 보완 |
| `AuthorizationFailed ... roleAssignments/write` | 역할 할당 권한이 있는 운영자가 실행. 이미 만들어진 UAMI·federated credential은 재실행 시 그대로 재사용되므로, 이 경우에만 `state\identity-<env>.json` 을 지우고 다시 실행 |
| `Failed setting <NAME>; create/protect GitHub environment first` | GitHub Settings → Environments 에서 환경 생성 |

---

## 3.4 `inspect_oidc.py` – OIDC claim metadata 수집

### 용도

`oidc-inspect.yml` 에서 각 protected environment의 **실제** OIDC 토큰을 받아,
허용 목록의 claim만 추출해 `state\claims-<env>.json` 으로 저장합니다. 토큰(JWT) 원문은
출력하거나 저장하지 않으며, 서명 검증은 Entra 로그인 시 수행되므로 여기서는 하지 않습니다.

### 환경 변수

| 변수 | 제공자 | 설명 |
| --- | --- | --- |
| `ACTIONS_ID_TOKEN_REQUEST_URL`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN` | GitHub (`id-token: write`) | OIDC 토큰 요청 엔드포인트 |
| `GITHUB_REPOSITORY` | GitHub | 저장소 검증 |
| `INSPECT_ENVIRONMENT` | workflow matrix | 7개 허용 환경 중 하나 |

### 추출 claim

`iss, aud, sub, repository, repository_id, repository_owner, repository_owner_id,
environment, ref, run_id, workflow_ref, job_workflow_ref, job_workflow_sha` + `inspected_environment`

### 사용 방법

로컬 실행용이 아닙니다. 다음으로 실행하고 결과를 다운로드합니다.

```powershell
gh workflow run oidc-inspect.yml --repo $repo --ref main
gh run download <inspectRun> --repo $repo -D state\oidc-claims
```

---

## 3.5 `snapshot.py` – 배포 전 baseline 캡처 (읽기 전용)

### 용도

지역 배포 직전 현재 Container App 상태를 읽어 `state\baseline-<region>.json` 으로 저장합니다.
이 파일은 **자동/수동 복구의 유일한 근거**이며, workflow는 이 파일을 artifact로 업로드한
뒤에만 `rollout.py` 를 실행합니다. Azure를 변경하지 않습니다.

### 입력

- 작업 디렉터리의 `release.json` (build job artifact)
- `infra\health-probes.json` (candidate에 적용할 probe 정책: Startup/Liveness `/healthz`, Readiness `/readyz`)
- `infra\revision-policy.json` (candidate의 `terminationGracePeriodSeconds`, 기본 30)
- 환경 변수: `GITHUB_RUN_ATTEMPT`, `GITHUB_RUN_ID`, `GITHUB_REPOSITORY`, `GITHUB_SHA`,
  `REGION`, `AZURE_SUBSCRIPTION_ID`, `RESOURCE_GROUP`, `CONTAINER_APP`

### 사전 검증 (fail-closed)

| 검사 | 실패 메시지 |
| --- | --- |
| 첫 시도 run | `Deploy reruns are forbidden...` |
| baseline 파일 미존재 | `Never overwrite an existing baseline` |
| `release.json` repo/run/commit 일치 | `Wrong release repository/run/commit` |
| 이미지가 ACR digest 형식 | `Digest required` |
| `activeRevisionsMode == Multiple` | `Multiple mode required` |
| 외부 HTTP ingress | `Example expects public HTTP ingress` |
| 정확히 1개 revision이 100% | `Require exactly one explicit stable revision at 100%` |
| stable Healthy/Running/Provisioned | `Stable must be active, provisioned, running and Healthy` |
| 단일 `web` 컨테이너, digest 이미지, `minReplicas >= 1` | 각 메시지 |
| candidate 이름 미존재 | `Candidate already exists; fail closed` |

### 출력 스키마 (`schema: 1`)

| 키 | 설명 |
| --- | --- |
| `run_id`, `repository`, `commit`, `region`, `app_id`, `captured_at` | 소유권/맥락 |
| `image` | 배포할 digest 이미지 |
| `stable`, `candidate` | revision 이름 (`<app>--r<run_id>`) |
| `traffic` | 배포 전 트래픽 (복구 목표) |
| `invariant` | app-scoped 설정 hash (drift 탐지) |
| `head_template`, `stable_template`, `stable_template_hash` | template 원본과 hash |
| `candidate_probes` | 적용할 probe 정책 |
| `candidate_termination_grace_seconds` | candidate의 종료 유예(초). 이 키가 없는 이전 snapshot은 stable 값을 그대로 유지 |

### 로컬 실행 (조사 목적)

GitHub 변수를 흉내 내야 하므로 일반적으로 workflow에서만 실행합니다. 불가피하게
로컬에서 상태를 확인하려면 별도 폴더에서 필요한 환경 변수와 `release.json` 을
준비한 후 `python scripts\snapshot.py` 를 실행합니다(읽기 전용).

---

## 3.6 `rollout.py` – 점진 배포와 복구 (핵심)

### 사용법

```text
python scripts/rollout.py {deploy|restore} --snapshot <baseline.json> [--profile production|demo|quick]
```

| 인자 | 설명 |
| --- | --- |
| `deploy` | candidate 생성 → 프로필의 `stages` 순서로 승격(10/50/100, quick은 50/100), 실패 시 자동 복구 |
| `restore` | snapshot의 트래픽으로 복원 (evidence: `manual-rollback.json`) |
| `--snapshot` | `snapshot.py` 결과 파일 (필수) |
| `--profile` | `production`(기본) / `demo` / `quick`. `restore` 에서는 무시 |

### 필요 환경 변수

`AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `RESOURCE_GROUP`,
`CONTAINER_APP`, `LOG_ANALYTICS_WORKSPACE_ID`, `REGION`, `GITHUB_REPOSITORY`,
`GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, (restore) `SOURCE_RUN_ID`,
(장시간 실행) `ACTIONS_ID_TOKEN_REQUEST_URL`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN`

### `deploy` 흐름

```text
1. run_attempt == 1, snapshot.run_id == GITHUB_RUN_ID, verify_context
2. guarded_state(트래픽 == snapshot.traffic)        ← drift면 복구 없이 중단
3. candidate 미존재 확인
4. baseline_preflight (읽기 전용)
     - synthetic 프로필(demo/quick): curl >= 8.4.0 확인
     - stable FQDN 으로 /healthz, /work 직접 확인
     - production: 직전 window의 organic SLO 확인
5. mutation-intent.json 기록                         ← 이후부터 실패 시 자동 복구
6. az containerapp update --yaml (template만 PATCH, stable=100 유지)
7. wait_ready: 최대 600초, candidate Healthy + /healthz, /work 확인
8. for weight in PROFILES[profile]["stages"]:   # (10, 50, 100) / quick: (50, 100)
     - guard + candidate Healthy 확인
     - set_traffic(stable=100-w, candidate=w), 최대 60초 라우팅 반영 확인
     - bake(): 아래 참조
9. success.json 기록
예외/신호 발생 시: failure.json → restore("automatic-rollback") → 예외 재발생
```

### `bake()` 루프 (단계별)

| 주기 | 동작 |
| --- | --- |
| 5초 | stable/candidate revision FQDN에 `control` 요청(`/healthz`, `/work`) |
| 60초 | `guarded_state` + candidate Healthy 재확인 |
| window 종료 + lag 후 | `metrics.query` → `metrics.gate` 평가, `gate-*.json` 저장 |
| 텔레메트리 부족 | 30초 후 재조회, 마감 초과 시 실패 |
| demo/quick | `demo_load.Generator` 가 공유 FQDN에 ~10 rps 부하, 오류 즉시 전파 |

### `restore` 흐름

1. `verify_context`, `snapshot.run_id == SOURCE_RUN_ID(없으면 GITHUB_RUN_ID)`
2. `guarded_state` (기대 트래픽 미지정 – stable/candidate 외 revision이 있으면 거부)
3. candidate가 있으면 소유권(template) 검증, 없으면 현재 트래픽이 stable 100% 여야 함
4. stable FQDN 으로 `/healthz`, `/work` 확인
5. `set_traffic(snapshot.traffic)`
6. 최대 120초 동안 ARM 상태 + 공유 FQDN `/work` 5회가 stable에 도달하는지 확인
7. `<evidence_name>.json` 기록. **candidate revision은 비활성화/삭제하지 않음**

### 신호 처리

- SIGINT/SIGTERM 수신 시 예외로 변환 → 자동 복구 시도(best effort)
- 복구 진행 중에는 추가 신호 무시
- SIGKILL, runner 유실, Azure 장애 시에는 복구가 보장되지 않음 → `restore.yml` 사용
- 복구 실패 시 로그: `ROLLBACK NOT VERIFIED: ... Run manual restore with original run ID.`

### 로컬/수동 실행 예 (비상 시, 권장하지 않음)

정상 경로는 `restore.yml` 입니다. workflow를 쓸 수 없는 비상 상황에서 rollback 권한이
있는 identity로 로그인한 운영자가 원본 baseline artifact로 직접 복구할 수 있습니다.

```powershell
gh run download <SOURCE_RUN_ID> --repo $repo -n "baseline-<SOURCE_RUN_ID>-kr" -D restore-input
$env:REGION='kr'; $env:SOURCE_RUN_ID='<SOURCE_RUN_ID>'; $env:GITHUB_RUN_ID='<SOURCE_RUN_ID>'
$env:GITHUB_REPOSITORY=$repo; $env:AZURE_SUBSCRIPTION_ID=$subscription
$env:RESOURCE_GROUP='safeaca-kr'; $env:CONTAINER_APP='safeaca-kr-app'
python scripts\rollout.py restore --snapshot restore-input\baseline-kr.json
```

> 로컬 실행은 30분 이상 지속되면 `refresh_login` 이 GitHub OIDC 엔드포인트를 요구하여
> 실패합니다. 복구는 보통 수 분 내에 끝나므로 영향이 적지만, 감사 추적을 위해
> 가능하면 workflow를 사용하세요.

---

## 3.7 `metrics.py` – Health gate

### 용도

Log Analytics에서 닫힌 시간 window의 business log를 revision/source별로 집계하고
SLO 및 상대 회귀를 판정합니다. `rollout.py` 가 import하여 사용합니다.

### 주요 구성 요소

| 이름 | 설명 |
| --- | --- |
| `PROFILES` | 프로필별 `bake`, `window`, `lag`, `samples`, `controls`, `stages`(canary 단계), `synthetic`(합성 부하로 판정 여부) (1장 표 참조) |
| `TELEMETRY_RETRY_SECONDS = 30` | 표본 부족 시 재조회 간격 |
| `TELEMETRY_RETRY_TIMEOUT = 300` | lag 이후 추가 대기 한도 |
| `IncompleteTelemetry` | "아직 수집 중일 수 있음" 을 나타내는 재시도 가능 예외 |
| `query(snapshot, start, end, workspace)` | KQL 실행, `(rows, kql)` 반환 |
| `metric(rows, revision, source, minimum)` | 단일 stream 검증: 절대 SLO(오류 ≤1%, p95 ≤500ms) |
| `gate(rows, snapshot, profile, weight, stable_reference)` | 필요한 stream 전체 평가 + 상대 회귀 |

### KQL 요점

- `ContainerAppConsoleLogs_CL`, 대상 앱 + stable/candidate revision만
- `kind == 'business'`, `event_time` 이 `[start, end)` 인 이벤트만
- `organic` 은 모두, `demo`/`control` 은 **현재 run ID** 것만 포함
- `RequestId, Revision` 기준 중복 제거 후 `Samples, Errors, BusinessFailures, P95` 집계

### 평가 규칙

- 필요한 stream: stable control, candidate control, candidate 판정 source,
  (10/50%) stable 판정 source. 100% 에서는 50% 단계의 stable 값을 기준으로 사용
- **SLO 위반은 다른 stream 표본이 부족해도 즉시 실패** (재시도 대상 아님)
- 동일 revision/source 중복 행, NaN, 범위 밖 값은 즉시 실패

### 튜닝 방법

임계값을 바꾸려면 `PROFILES` 및 `metric()` / `gate()` 의 상수(0.01, 500, 0.005,
1.5, 50)를 수정하고 `tests\test_safety.py` 를 갱신·실행하세요.

---

## 3.8 `demo_load.py` – Demo 합성 부하

### 용도

`demo` 프로필에서만 사용됩니다. 실제 사용자 트래픽이 없는 데모 환경에서 공유 ingress
FQDN의 `/work` 에 약 10 req/s 부하를 보내 카나리 분할 경로를 통과하는 표본을 만듭니다.
**production 프로필에서는 절대 사용되지 않습니다.**

### 주요 상수

| 상수 | 값 | 의미 |
| --- | --- | --- |
| `WORKERS` | 16 | 동시 HTTP worker |
| `OUTSTANDING` | 32 | 실행+대기 요청 상한 |
| `SATURATION_SECONDS` | 30 | 연속 포화 허용 시간(초과 시 `LoadCapacityError`) |
| `DRAIN_SECONDS` | 20 | 종료 시 진행 중 요청 대기 |
| `REQUEST_SECONDS` / `PROCESS_SECONDS` | 10 / 12 | curl 요청 제한 / 프로세스 watchdog |

### 동작

- 요청은 `curl` 하위 프로세스로 전송(헤더 `X-Safe-Source: demo`, `X-Safe-Run: <run_id>`)
- 응답 64 KiB 제한, `ok == true` 및 `result == 42` 아니면 실패
- 실패 분류: `network_timeout`, `http`, `response_parsing`, `business_contract`, `internal`
- 첫 요청 실패 시 즉시 중단 → bake 실패 → 자동 복구
- 결과: `evidence\<region>\demo-load-<weight>.json` (schema v2, 60초 interval 통계, histogram)

### 요구 사항

`curl >= 8.4.0` (GitHub ubuntu-24.04 runner는 충족). `baseline_preflight` 가 확인합니다.

---

## 3.9 `common.py` – 공통 라이브러리

| 함수 | 용도 |
| --- | --- |
| `az(*args)` | `az ... --only-show-errors -o json` 실행(180초 제한), 30분마다 `refresh_login` |
| `refresh_login()` | GitHub OIDC 토큰을 새로 받아 `az login --federated-token`. 장시간 bake 중 토큰 만료 대응, JWT는 출력하지 않음 |
| `save` / `load` | JSON 파일 저장/읽기 (UTF-8) |
| `digest(value)` | 정렬된 JSON의 SHA-256 |
| `require(cond, msg)` | 실패 시 `RuntimeError(msg)` – fail-closed의 기본 수단 |
| `image_valid(image)` | `<acr>.azurecr.io/<repo>@sha256:<64hex>` 형식 검사 |
| `app_id()` / `app()` | Container App 리소스 ID / ARM REST 조회 (`api-version=2024-03-01`) |
| `revision(name)` | revision 상세 조회 |
| `invariant(value)` | traffic 제외 configuration, identity, environment, location 의 hash |
| `traffic(value)` | 명시적 revision weight dict. `latestRevision`, label, 합계≠100 거부 |
| `equal_traffic(a, b)` | 0% 항목을 무시하고 비교 |
| `set_traffic(weights)` | `az containerapp ingress traffic set --revision-weight` |
| `ready(value)` | active + Healthy + Provisioned + Running |
| `verify_context(snapshot)` | snapshot이 현재 app/repo/region/run과 일치하는지 |
| `normalized_template(t)` | Azure 반환값 차이(scale null, grace null, env/probe 순서, ephemeralStorage) 정규화 |
| `expected_candidate(snapshot)` | stable template + digest + `RELEASE_ID` + probe + grace(`GRACE_SECONDS` env 포함)로 기대 candidate template 생성 |
| `guarded_state(snapshot, expected)` | 1.4절의 guard 전체 수행, `(app, weights)` 반환 |
| `candidate_owned(snapshot)` | candidate template이 기대값과 같은지(소유권) 검증 |

---

## 3.10 `check_source.py` – 복구 대상 run 검증

### 용도

`restore-region.yml` 에서 입력된 `source_run_id` 가 신뢰할 수 있는 원본 release인지
GitHub REST API로 확인하고, 그 run의 커밋 SHA를 `GITHUB_OUTPUT` 에 `sha=` 로 기록합니다.

### 환경 변수

`SOURCE_RUN_ID`(숫자), `GITHUB_REPOSITORY`, `GH_TOKEN`, `GITHUB_OUTPUT`

### 검증 항목

| 조건 | 실패 메시지 |
| --- | --- |
| run ID 숫자 | `Source run ID must be numeric` |
| 같은 저장소 | `Wrong source repository` |
| `.github/workflows/release.yml` + `workflow_dispatch` | `Source must be a release.yml dispatch` |
| `main` 브랜치 + `completed` | `Source must be completed main run` |

> 진행 중인 release run은 복구 대상이 될 수 없습니다. 취소 후 run이 `completed` 가 된
> 것을 확인한 다음 restore를 실행하세요.

---

## 3.10.1 `retire.py` – 데모 전용 이전 revision 비활성화

### 용도

무중단 데모([5장](05-zero-downtime-demo.md))에서 **완료된 release**의 이전 stable revision을
`az containerapp revision deactivate` 로 비활성화해 실제 SIGTERM을 발생시킵니다.
`demo-retire.yml` 에서만 실행합니다. 운영 정리 용도가 아닙니다.

### 사용법

```bash
python scripts/retire.py --snapshot retire-input/baseline-kr.json \
                         --success retire-input/evidence/kr/success.json
```

환경 변수: `SOURCE_RUN_ID`, `REGION`, `GITHUB_REPOSITORY`, `AZURE_SUBSCRIPTION_ID`,
`RESOURCE_GROUP`, `CONTAINER_APP`

### 처리 순서 (`retire(snapshot, success, timeout=300, interval=5)`)

| 단계 | 내용 | 실패 메시지 |
| --- | --- | --- |
| 1 | `verify_context` – snapshot이 이 app/repo/region/run 소유 | 각 메시지 |
| 2 | `SOURCE_RUN_ID == snapshot.run_id`, `success.revision == candidate` | `Source run mismatch`, `Region success evidence does not match ...` |
| 3 | `guarded_state(snapshot, {candidate: 100})` – drift 없음, stable 0% | `Traffic drift ...`, `Stable unavailable ...` 등 |
| 4 | `ready(candidate_owned(snapshot))` | `Candidate not ready; refusing to retire ...` |
| 5 | `az containerapp revision deactivate --revision <stable>` | Azure CLI 오류 |
| 6 | stable `active == false` 까지 `interval` 초마다 확인. 매번 candidate ready + 공유 FQDN `/work` 가 candidate로 응답하는지 검증 | `Candidate became unready during retire`, `Stable still active after 300s ...` |
| 7 | `evidence/<REGION>/demo-retire.json` 저장 | |

- 단계 1–4 중 하나라도 실패하면 **Azure를 변경하지 않습니다.**
- 같은 run에 두 번 실행하면 3단계에서 `Stable unavailable` 로 거부됩니다(이미 비활성).
- 실행 후 그 release의 `restore` 는 거부됩니다. 복구는
  [5.10](05-zero-downtime-demo.md#510-retire-이후-복구) 참고.

---

## 3.11 공통 오류 메시지 → 의미 → 조치

| 메시지 | 의미 | 조치 |
| --- | --- | --- |
| `Deploy reruns forbidden` | "Re-run jobs" 사용 | 새 `release.yml` run 시작 |
| `App-scoped drift; manual investigation required` | ingress/identity 등 앱 설정이 snapshot 이후 변경 | 변경 이력(Activity Log) 조사, 임의 덮어쓰기 금지 |
| `Stable template changed` | stable revision이 다른 방식으로 변경 | 조사 후 새 release |
| `Stable unavailable; cannot safely route to it` | stable 비정상 | stable 복구 먼저 (incident) |
| `Unknown revision in traffic` | 다른 릴리스/운영자가 트래픽 변경 | 소유자 확인, blind overwrite 금지 |
| `Traffic drift; refusing blind overwrite` | 기대 weight와 다름 | 동시 작업 여부 확인 |
| `Candidate exists: this run is not safely replayable` | 같은 run의 candidate 존재 | restore 후 새 run |
| `Candidate template drift; do not mutate` | candidate가 외부에서 변경 | 조사 |
| `Candidate readiness timed out` / `Candidate provisioning failed` | 새 이미지 기동 실패 | revision 로그/probe 확인 (자동 복구됨) |
| `Error/business-failure SLO exceeded` / `p95 SLO exceeded` | 절대 SLO 위반 | 원인 분석 (자동 복구됨) |
| `Candidate error regression` / `Candidate latency regression` | stable 대비 회귀 | 원인 분석 (자동 복구됨) |
| `Insufficient samples` / `Missing telemetry` (최종 실패) | 트래픽 부족 또는 로그 수집 지연 | production은 organic 트래픽 확인, demo는 부하 증거 확인 |
| `Fresh GitHub OIDC endpoint unavailable` | `id-token: write` 없음 또는 로컬 실행 | workflow 권한 확인 |
| `ROLLBACK NOT VERIFIED` | 자동 복구 실패 | 즉시 `restore.yml` 실행 |
