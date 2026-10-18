# 안전 배포 예제 실행 가이드

이 문서는 이 저장소의 Azure Container Apps 안전 배포 예제를 처음 구성하고,
검증하고, 복구하는 절차를 설명합니다. 명령은 PowerShell 7 기준입니다.
Linux Bash 명령은 [INSTRUCTION.linux.md](INSTRUCTION.linux.md)를 참조하세요.

## 1. 동작 모델

### 배포 순서

1. `build` 환경에서 단위 테스트와 Python 컴파일을 실행합니다.
2. Linux/amd64 이미지를 한 번만 빌드하여 ACR에 push하고 immutable digest를
   `release-<run-id>` artifact에 기록합니다.
3. `prod-kr` 승인 후 한국에서 10% → 50% → 100%로 승격합니다.
4. 성공한 경우에만 `prod-sea`, 이어서 `prod-usw`에서 같은 과정을 반복합니다.

각 지역은 별도의 ACA 환경, 컨테이너 앱, Log Analytics workspace를 사용합니다.
세 지역은 하나의 Korea Central ACR에 있는 동일한 image digest를 pull합니다.

지역별 배포는 병렬 matrix가 아닙니다. Southeast Asia 실패 시 해당 지역만 자동 복구하고,
건강한 한국 배포는 되돌리지 않습니다. 전체 릴리스 복구는 별도 restore workflow가
West US → Southeast Asia → 한국 순서로 시도합니다.

### 안전 장치

- 배포와 복구 workflow는 동일한 `safe-aca-production` concurrency group을 사용합니다.
- 재실행(`run_attempt != 1`)은 변경 전에 중단합니다. 복구 후 새 workflow run을
  시작하세요.
- snapshot은 최초 트래픽 변경 전에 baseline을 artifact로 보관합니다. 업로드에
  실패하면 candidate 생성 및 트래픽 변경은 하지 않습니다.
- health gate, Log Analytics 조회, 표본 수, 로그 지연, 상태 확인 중 하나라도
  실패하면 fail-closed로 처리합니다.
- rollback은 다른 릴리스나 운영자 변경으로 판단되는 drift를 blind overwrite하지
  않고 중단합니다.

Template 비교는 Azure 응답의 일부 기본값 표현 차이만 정규화합니다:
scale의 null/생략된 `cooldownPeriod` 및 `pollingInterval`, 이 예제의
0.25 CPU/0.5Gi 컨테이너에서 자동 반환되는 `ephemeralStorage: 1Gi`입니다.
Azure가 health probe 배열 순서를 바꿔 반환할 수 있으므로 비교 시 `type`으로
정렬합니다. probe의 필드 값, 누락 및 중복은 그대로 비교하여 drift를 거부합니다.
저장된 snapshot과 공유 probe 정책의 순서는 변경하지 않습니다.
Candidate revision의 `revisionSuffix`가 null/생략된 경우에는 반환된 revision
이름이 해당 run의 candidate와 정확히 일치해야 합니다. 그 밖의 template 변경은
계속 drift로 처리하며, 저장된 stable template hash 검증은 유지합니다.

## 2. 사전 요구 사항

### 로컬 도구

- PowerShell 7
- Python 3.13
- Azure CLI 및 Bicep CLI
- Docker Desktop의 Linux container engine
- Git 및 GitHub CLI (`gh`)

### Azure 및 GitHub 권한

초기 설치자는 구독에서 Contributor와 Role Based Access Control Administrator
(또는 Owner), 그리고 Entra 애플리케이션/서비스 주체 생성 권한이 필요합니다.
실제 배포 identity에는 Owner, 구독 Contributor, client secret이 필요하지 않습니다.

GitHub-hosted Ubuntu 24.04 runner, Actions artifact v4, 그리고 required reviewer를
지원하는 GitHub Environment가 필요합니다. private 저장소의 승인 기능은 플랜별로
다를 수 있으므로 사전에 확인하세요.

### 사전 확인

- 선택한 구독에서 ACA Consumption profile, ACR, Log Analytics가 세 대상 지역에서
  사용 가능한지 확인합니다.
- 필요한 지역별 할당량과 Azure Policy 제한을 확인합니다.
- ACR은 Korea Central에 하나만 생성됩니다. 원격 image pull의 지연, 전송 비용,
  registry 장애 의존성 및 데이터 주권을 검토하세요.
- 대상 지역은 Korea Central, Southeast Asia(`southeastasia`), West US(`westus`)입니다.
  `Microsoft.App`(managedEnvironments/containerApps), `Microsoft.OperationalInsights`,
  `Microsoft.ContainerRegistry`, `Microsoft.ManagedIdentity`와 ACA Consumption
  workload profile이 세 지역에서 제공되는 것을 확인했지만, 구독별 지원과 할당량은
  다를 수 있으므로 다음으로 직접 확인하세요. West US는 Azure에서 "Other"(비권장)
  범주의 이전 지역이므로 가용성 영역 지원과 용량 제약을 별도로 검토하세요.
  ```text
  az containerapp env workload-profile list-supported -l southeastasia
  az containerapp env workload-profile list-supported -l westus
  az rest --method get --url "https://management.azure.com/subscriptions/<id>/providers/Microsoft.App/locations/<region>/usages?api-version=2024-03-01"
  ```
- 반드시 비운영 구독에서 통합 검증을 마친 후 운영 요구 사항에 맞게 확장하세요.

## 3. 로컬 검증

Azure 자격 증명이 없어도 다음 검증을 실행할 수 있습니다.

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app scripts tests
az bicep build --file infra\shared.bicep --stdout
az bicep build --file infra\region.bicep --stdout
```

앱을 직접 실행하고 별도 PowerShell 창에서 endpoint 계약을 확인합니다.

```powershell
python app\server.py
```

```powershell
Invoke-RestMethod http://localhost:8080/healthz
Invoke-RestMethod http://localhost:8080/work
```

`/healthz`는 준비 상태를, `/work`는 `result: 42` 비즈니스 계약과 JSON request
log를 제공합니다. 다음 변수는 **로컬 또는 별도 실습 앱에서만** 장애 실험에
사용하세요.

```powershell
$env:DEMO_FAILURE_PERCENT = '100' # /work가 503을 반환
$env:DEMO_LATENCY_MS = '600'      # /work 지연 주입(ms)
python app\server.py
```

컨테이너 이미지도 검증합니다.

```powershell
docker build --platform linux/amd64 -t safe-demo:local app
docker run --rm -p 8080:8080 safe-demo:local
```

## 4. 최초 Azure bootstrap

PowerShell 스크립트는 provider 등록, 공유 인프라, 이미지 build/push, RBAC 대기,
지역별 배포 및 결과 저장 단계에 타임스탬프가 있는 콘솔 진행 메시지를 출력합니다.
Azure 배포는 몇 분 걸릴 수 있으며, 완료 메시지는 해당 명령이 성공한 뒤 출력됩니다.

> `bootstrap.ps1` 및 `infra\region.bicep`는 최초 생성 전용입니다. 라이브 앱에
> 재실행하거나 복구/업그레이드 수단으로 사용하지 마세요.

Azure와 GitHub에 로그인한 후 변수 값을 실제 환경에 맞게 설정합니다.

```powershell
az login
gh auth login

$subscription = '실제-구독-GUID'
$repo = 'OWNER/REPOSITORY'
$acr = '전세계고유소문자숫자이름'
$prefix = 'safeaca'

az account set --subscription $subscription
az bicep version
docker info
```

초기 실행자에게 새 ACR에 push할 권한이 있어야 합니다. 일반 RBAC registry라면
필요에 따라 설치자 principal에 registry scope `AcrPush`를 부여합니다.

```powershell
.\scripts\bootstrap.ps1 `
  -SubscriptionId $subscription `
  -AcrName $acr `
  -Prefix $prefix
```

이 스크립트는 다음을 생성합니다.

| 리소스 | 내용 |
| --- | --- |
| `$prefix-shared` | Basic ACR 및 image pull 전용 user-assigned managed identity |
| `$prefix-kr` | Korea Central workspace, ACA 환경, 앱 |
| `$prefix-sea` | Southeast Asia workspace, ACA 환경, 앱 |
| `$prefix-usw` | West US workspace, ACA 환경, 앱 |
| `state\bootstrap-outputs.json` | 이후 OIDC 설정에 필요한 앱 및 workspace 식별자 |

`state\`는 Git 추적 대상이 아닙니다. 파일을 커밋하지 마세요.

## 5. GitHub Environment와 OIDC 구성

### 5.1 GitHub Environment 생성 및 보호

GitHub 저장소 **Settings → Environments**에서 다음 일곱 환경을 만듭니다.

```text
build
prod-kr, prod-sea, prod-usw
prod-rollback-kr, prod-rollback-sea, prod-rollback-usw
```

지역 코드는 Korea Central (`kr`, `koreacentral`), Southeast Asia
(`sea`, `southeastasia`), West US (`usw`, `westus`)입니다.
이전 `de`/`br` 환경 대신 새 환경을 생성하고 OIDC claim 검사를 다시 실행하세요.
기존 리소스, bootstrap 출력 및 claims 파일의 이름만 바꾸면 안 됩니다.

권장 설정:

- 모든 환경의 deployment branch를 `main`으로 제한
- `prod-*` 및 `prod-rollback-*`에 required reviewer 설정
- 가능한 경우 self-review와 관리자 우회 차단
- `build`도 `main`으로 제한

외부 PR의 비신뢰 코드가 실행되지 않도록 하고, workflow 및 artifact 수정 권한을
신뢰 경계로 관리하세요.

### 5.2 실제 OIDC claim 검사

먼저 현재 `main`의 파일을 push한 뒤, Azure 접근 권한 없이 실제 protected
environment claim을 수집합니다.

```powershell
gh workflow run oidc-inspect.yml --repo $repo --ref main
gh run list --repo $repo --workflow oidc-inspect.yml

$inspectRun = '위 목록에서 확인한 실제 검사 run ID'
gh run watch $inspectRun --repo $repo
gh run download $inspectRun --repo $repo -D state\oidc-claims

Get-Content `
  "state\oidc-claims\oidc-claims-$inspectRun-prod-kr\claims-prod-kr.json"
```

각 환경의 승인 후 claim metadata에서 `iss`, `aud`, `sub`, `repository`,
`repository_id`, `repository_owner_id`, `ref`, environment를 검토합니다. bearer
JWT 전체를 출력하거나 보관하지 마세요.

### 5.3 환경별 identity 생성

helper는 claim 파일의 실제 `sub`를 사용해 환경별 Entra application/service
principal, federated credential, 최소 Azure RBAC, GitHub Environment variables를
만듭니다. client secret은 만들지 않습니다.

```powershell
.\scripts\configure-identity.ps1 `
  -Repository $repo `
  -Environment build `
  -SubscriptionId $subscription `
  -AcrName $acr `
  -ClaimsFile "state\oidc-claims\oidc-claims-$inspectRun-build\claims-build.json"
```

지역 deployment 및 rollback identity를 구성합니다.

```powershell
$outputs = Get-Content state\bootstrap-outputs.json -Raw | ConvertFrom-Json

foreach ($region in @('kr', 'sea', 'usw')) {
  $r = $outputs.$region
  foreach ($environment in @("prod-$region", "prod-rollback-$region")) {
    .\scripts\configure-identity.ps1 `
      -Repository $repo `
      -Environment $environment `
      -SubscriptionId $subscription `
      -ResourceGroup $r.RESOURCE_GROUP `
      -ContainerApp $r.CONTAINER_APP `
      -WorkspaceResourceId $r.WORKSPACE_RESOURCE_ID `
      -WorkspaceCustomerId $r.LOG_ANALYTICS_WORKSPACE_ID `
      -ClaimsFile "state\oidc-claims\oidc-claims-$inspectRun-$environment\claims-$environment.json"
  }
}
```

환경별 권한은 다음과 같습니다.

| Environment | Azure 역할 |
| --- | --- |
| `build` | ACR scope `AcrPush`, `Reader` |
| `prod-*` | Container App scope `Container Apps Contributor`, resource group `Reader`, workspace `Log Analytics Reader` |
| `prod-rollback-*` | 해당 지역 복구에 필요한 위 regional 권한과 별도 client ID |
| ACA image pull identity | 공유 ACR scope `AcrPull` |

생성되는 GitHub Environment variables는 다음과 같습니다.

- 전체: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- `build`: `ACR_NAME`
- 지역 deploy/rollback: `RESOURCE_GROUP`, `CONTAINER_APP`,
  `LOG_ANALYTICS_WORKSPACE_ID`

`LOG_ANALYTICS_WORKSPACE_ID`는 Azure resource ID가 아니라 Log Analytics
workspace의 customer GUID입니다.

> `state\identity-<environment>.json`이 이미 있으면 helper는 중복 identity 생성을
> 막기 위해 중단합니다. 파일을 지우고 재실행하지 말고, 기록된 ID를 사용해 누락된
> RBAC 또는 변수를 복구하세요.

## 6. 배포 실행

### ACA health probe와 기존 앱 적용

[공식 health probe 문서](https://learn.microsoft.com/en-us/azure/container-apps/health-probes)에
따라 Startup, Readiness, Liveness를 모두 HTTP `GET /healthz` (port 8080)로 설정합니다.
`/healthz`는 지연/실패 주입이 있는 `/work`와 분리된 경량 endpoint이며 business
telemetry에 포함하지 않습니다. 현재 외부 의존성이 없는 예제이므로 세 종류가 같은
endpoint를 사용합니다. 의존성이 추가되면 readiness와 liveness의 의미를 분리하세요.

공유 정책은 [`infra/health-probes.json`](infra/health-probes.json)입니다.

| 종류 | Initial delay | Period | Timeout | Failure / success threshold |
| --- | --- | --- | --- | --- |
| Startup | 3초 | 15초 | 5초 | 10 / 1 |
| Readiness | 3초 | 5초 | 5초 | 3 / 1 |
| Liveness | 10초 | 10초 | 5초 | 3 / 1 |

기존 2초 timeout을 5초로 늘려 짧은 응답 지연에 여유를 줍니다. Startup은 주기 × 실패
횟수 약 150초의 기존 허용 범위를 유지합니다. 이는 정확한 wall-clock 기한이 아닙니다.
Readiness는 트래픽 수신 가능 여부, Liveness는 재시작 필요 여부를 판단하며,
probe 성공만으로 business/SLO gate를 통과하지 않습니다.

- 신규 bootstrap은 Bicep이 공유 정책을 읽습니다.
- 기존 앱은 **새 release workflow**로 적용합니다. snapshot에 `candidate_probes`를
  고정하고, stable snapshot을 복제한 전체 candidate template에만 정책을 적용합니다.
  `az containerapp update --yaml`의 template-only PATCH를 사용하며 traffic,
  app-scoped configuration, stable/LKG revision은 변경하지 않습니다. 후보는 0%로
  생성한 뒤 readiness/business 확인과 기존 10/50/100% gate를 통과해야 합니다.
- 예전 snapshot에 `candidate_probes`가 없으면 기존 stable-derived probe를 그대로
  사용하므로 과거 artifact의 restore/ownership 의미가 바뀌지 않습니다.
- **라이브 앱에 bootstrap Bicep을 재적용하거나 현재 stable revision을 수정하지 마세요.**

`ProbeFailed ... timeout in 2 seconds`만으로 원인을 확정할 수는 없습니다.
새 정책은 새 candidate에만 적용되므로 기존 stable의 로그에는 2초 timeout이 남을 수
있습니다. 계속 실패하면 revision/replica별 CPU·메모리, restart, startup 시간과 probe
설정을 조사하세요. 이 변경은 CPU 부족이나 app hang 자체를 해결하지 않습니다.
현재 활성 revision의 설정은 다음 read-only 명령으로 확인할 수 있습니다.

```powershell
az containerapp revision show --resource-group <resource-group> --name <app-name> --revision <revision-name> --query properties.template.containers[0].probes -o json
```

### Demo 프로필

전체 파이프라인 연결을 빠르게 확인하는 교육용 프로필입니다. `/work`에 대략
10 req/s의 합성 부하를 발생시키고 단계마다 180초 관찰과 120초 로그 수집 지연을
기다립니다. 실제 사용자 SLI를 검증하지 않습니다. 로그 표본이 늦게 도착하면
아래의 제한된 telemetry 재시도 시간만큼 더 걸릴 수 있습니다.

합성 부하는 모니터링 루프와 독립된 scheduler에서 shared app endpoint의 `/work`로
초당 10건을 목표로 전송합니다. Azure CLI 조회, control probe 및 telemetry 재시도
중에도 계속 실행되며 실제 90/10 등의 라우팅을 통과합니다. HTTP worker는 최대
16개, 실행/대기 요청은 합계 최대 32개로 제한합니다. 일시적인 큐 포화에는 제출을
중지하고 여유가 생기면 초당 10건의 pacing으로 재개하며 밀린 요청을 몰아서 보내지
않습니다. 연속 포화가 30초에 도달하거나 요청이 실패하면 명시적 실패로 처리합니다.
Production 경로에는 이 scheduler나 demo evidence를 생성하지 않습니다.

Demo transport에는 **curl 8.4.0 이상**이 필요하며 mutation 전 baseline preflight에서
버전을 확인합니다. 별도 curl 프로세스의 전체 DNS/connect/TLS/body 작업 제한은 10초,
프로세스 watchdog은 12초이며 kill 후 회수에는 추가 최대 1초를 허용합니다.
응답 크기는 64 KiB로 제한합니다. 기존 readiness/control probe transport는 변경하지 않습니다.
정상 종료, 오류, SIGINT/SIGTERM 시 제출을 중단하고 시작 전 요청을 취소하며 실행 중
요청을 최대 20초 drain합니다. 해결되지 않은 요청은 evidence에 남기며 thread 종료를
주장하지 않습니다. SIGKILL/host loss는 보호된 수동 restore workflow를 사용하세요.

단계별 `evidence/<region>/demo-load-<weight>.json`의 schema version 2에는 region,
profile, 구현 commit, generator 상태, 제출/성공/실패/시작 전 취소/미해결 수,
queue delay 및 end-to-end duration의 고정 크기 histogram, 포화 횟수/시간과 shutdown
결과를 기록합니다. `completed`는 기존과 같이 **성공한 요청 수**이며 `achieved_rps`는
drain을 포함한 전체 경과 시간의 성공 RPS입니다. 60초 단위 완결 interval과 마지막
부분 interval을 구분하고 target/actual RPS, queue pressure를 콘솔에도 출력합니다.
최초 controller/load 오류를 primary로 보존하고 후속 요청/drain/evidence 오류는
secondary로 기록합니다. 파일 저장 오류는 콘솔에 표시하며 승격을 허용하지 않습니다.
토큰, credential header, 응답 본문은 evidence에 기록하지 않습니다.
실제 속도와 candidate 배분은 네트워크/라우팅에 따라 달라지므로 표본 기준을
보장하거나 완화하지 않습니다. production 프로필에서는 합성 부하를 생성하지 않습니다.

```powershell
gh workflow run release.yml --repo $repo --ref main -f profile=demo
gh run list --repo $repo --workflow release.yml
gh run watch '실제-RELEASE-RUN-ID' --repo $repo
```

### Production 프로필

실서비스의 유기적 트래픽과 SLI가 준비된 경우에만 사용합니다.

```powershell
gh workflow run release.yml --repo $repo --ref main -f profile=production
```

각 10%, 50%, 100% 단계는 최소 한 시간 관찰합니다. 각 지역은 약 3시간 이상,
전체는 9시간 이상이 걸릴 수 있습니다. 일/주 단위 또는 업무 피크 전체의 bake는
이 예제가 제공하지 않으므로 durable orchestrator와 연속 SLI 평가를 별도로
설계해야 합니다.

## 7. Health gate 기준

workflow는 `ContainerAppConsoleLogs_CL`에서 정확한 revision 및 닫힌 UTC
window의 JSON business log를 조회하고, request ID/revision별 중복을 제거합니다.
판정 쿼리와 집계 결과는 artifact evidence에 저장됩니다.

| 기준 | Production | Demo |
| --- | --- | --- |
| 후보 organic `/work` 표본 | 100건 이상 | 현재 run의 demo `/work` 20건 이상 |
| stable organic 표본 | 10%/50% 단계에서 100건 이상 | 해당 없음 |
| live control 요청 | 5분당 revision별 30건 이상 | 1분당 revision별 5건 이상 |
| 오류 또는 business 실패 | stable/candidate 합집합 1% 이하 | 동일 |
| p95 지연 | 각 revision 500ms 이하 | 동일 |
| 후보 오류율 | stable 기준 +0.5 percentage point 이하 | 동일 |
| 후보 p95 | stable p95 × 1.5 + 50ms 이하 | 동일 |

100% 단계에서는 직전 50% 단계의 검증된 stable 기준값을 고정하고 stable control
요청을 계속 검사합니다.

배포 중 각 닫힌 window는 종료 후 기본 120초 수집 지연을 기다린 뒤 평가합니다.
telemetry 누락 또는 표본 부족만 있으면 동일한 시작/종료 시각과 run/revision 조건으로
30초 간격 재조회하며, 종료 후 총 420초(기본 120초 + 추가 300초)까지만 기다립니다.
window를 이동하거나 표본 기준을 낮추지 않습니다. 기다리는 동안 control probe,
candidate health 및 drift 검사를 계속하며 각 조회의 시각과 집계 표본을 해당
gate evidence의 `attempts`에 저장합니다.

추가 대기 한도에도 자료가 부족하면 실패하고 현재 지역의 복구를 시도합니다.
관측된 절대 SLO/상대 회귀 위반은 다른 stream의 표본이 부족해도 즉시 실패하며,
`NaN`, 중복 집계, query/권한 실패도 재시도하지 않습니다.
production의 배포 전 organic baseline preflight는 기존처럼 단일 조회로
fail-closed 처리합니다. 이 재시도는 배포 중 10%/50%/100% gate에 적용됩니다.

이 수치는 예제 값이며 실제 서비스의 SLO, 오류 예산, 최소 표본, 수집 지연,
피크 시간대에 맞게 재설계해야 합니다. `/healthz`의 200 응답만으로 승격하지
않습니다.

## 8. 실패, 취소 및 수동 복구

일반 예외나 health gate 실패 시 현재 지역만 snapshot의 원본 트래픽으로 즉시
복구를 시도합니다. candidate와 stable revision은 보존됩니다.

GitHub workflow 취소는 rollback 완료를 보장하지 않습니다. 취소된 release run이
`completed` 상태가 된 것을 확인한 뒤, 새 runner와 별도 rollback OIDC identity를
사용하는 restore workflow를 실행하세요.

### 단일 지역 복구

```powershell
gh workflow run restore.yml `
  --repo $repo `
  --ref main `
  -f source_run_id=ORIGINAL_RELEASE_RUN_ID `
  -f region=sea
```

### 전체 릴리스 복구

```powershell
gh workflow run restore.yml `
  --repo $repo `
  --ref main `
  -f source_run_id=ORIGINAL_RELEASE_RUN_ID `
  -f region=all
```

restore는 보호된 rollback environment의 승인을 요구하고, 원본 `release.yml` run의
repository, commit, 완료 상태 및 immutable baseline artifact를 다시 검증합니다.
`all`은 West US, Southeast Asia, 한국 순으로 실행하며 앞 지역의 실패에도 다음 지역을
시도합니다. 시작되지 않은 지역에 baseline artifact가 없으면 안전하게 실패하며
변경하지 않습니다.

더 최신 릴리스가 이미 완료된 상태에서는 오래된 snapshot으로 되감지 않습니다.
drift 오류를 해결하기 위해 임의의 `latest` revision에 100% 트래픽을 주지 말고,
변경 기록과 보관된 evidence를 조사한 뒤 incident 절차에 따라 처리하세요.

## 9. 운영 전 점검표

- [ ] Azure 서비스/SKU/할당량/Policy 및 비용을 대상 지역별로 검토했다.
- [ ] GitHub Environment의 승인자, `main` branch 제한, self-review 방지 설정을 확인했다.
- [ ] OIDC claim을 실제 protected job에서 수집하고 trust를 구성했다.
- [ ] 환경마다 별도 OIDC identity와 최소 RBAC를 부여했다.
- [ ] 비운영 구독에서 bootstrap, OIDC, approval, 로그 수집, 카나리, 취소 및 restore를 시험했다.
- [ ] artifact retention 90일 이상, ACR image/revision 보존, 접근 제어를 설정했다.
- [ ] DB migration, 메시지, DNS, secret/Key Vault, 외부 side effect의 별도 rollback 계획을 마련했다.
- [ ] 인증/인가, 외부 가용성, dependency 오류, queue lag, saturation, 핵심 business KPI를 gate에 추가했다.
- [ ] Azure Monitor alert와 Action Group 등 runner와 독립적인 on-call 경보를 구성했다.

## 10. 한계와 운영 확장

revision traffic rollback은 데이터 변경, DB schema migration, secret 값, identity/RBAC,
DNS/custom domain, 외부 시스템 side effect를 되돌리지 않습니다. expand/contract
migration, 하위 호환성, secret version pinning, feature flag/kill switch 및 데이터
복구 계획이 필요합니다.

revision FQDN은 일반 ingress traffic 분배를 우회할 수 있습니다. 공개 데모 앱에
비밀이나 운영 데이터를 넣지 마세요. 운영에서는 애플리케이션 계층 인증, internal
ingress/VNet과 private runner 또는 검증된 공통 접근 제어를 main host와 revision
host에 모두 적용하세요.

GitHub Actions의 자동 cleanup은 runner 유실, 강제 종료, 네트워크/Azure API 장애
등에서 보장되지 않습니다. Azure Monitor 기반의 독립적인 관측 및 incident runbook은
필수입니다.

## 참고 자료

- [Azure Well-Architected: Safe deployments](https://learn.microsoft.com/azure/well-architected/operational-excellence/safe-deployments)
- [Azure Container Apps revision CLI](https://learn.microsoft.com/cli/azure/containerapp/revision)
- [Azure Container Apps traffic CLI](https://learn.microsoft.com/cli/azure/containerapp/ingress/traffic)
- [GitHub Actions OIDC for Azure](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure)
- [GitHub Environments](https://docs.github.com/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment)
