GitHub Actions + Azure Container Apps 안전 배포 실행 예제
========================================================

0. 범위와 중요한 전제

이 디렉터리 전체를 별도 GitHub 저장소 루트에 복사한다. .github(숨김 폴더)를
반드시 포함한다. 부모 디렉터리 그대로 저장소 하위에 넣으면 workflow가 검색되지
않는다. 기본 브랜치는 main이다. 다른 브랜치명은 두 진입 workflow와
scripts/check_source.py의 main 검사를 함께 수정해야 한다.

교육용이지만 Azure CLI를 실제 호출하는 실행 예제이다. 가짜 deploy 명령이나
성공을 가정하는 health gate가 아니다. Azure 자격 증명 없는 로컬 검증만으로
Azure 실제 성공을 주장하지 않는다. 먼저 별도 비운영 구독에서 검증한다.
실서비스의 데이터/지역 규제, 인증, 장애 모델, 부하, SLO에 맞춘 확장이 필요하다.
Azure Container Apps, ACR, Log Analytics, GitHub Actions 실행 비용이 발생한다.

지역 순서:
  Korea Central(koreacentral) -> Southeast Asia(southeastasia)
  -> West US(westus)
서비스/SKU/Consumption profile의 구독별 지역 지원, 할당량, 조직 Azure Policy,
Log Analytics 사용 가능 여부를 운영자가 사전 확인한다. 이름 목록에 있다고
배포 용량/권한이 보장되는 것은 아니다. 세 지역은 각자 다른 ACA 환경/앱/
Log Analytics workspace를 사용한다. ACR은 Korea Central에 하나이다.
동일한 registry/repository@sha256 digest를 세 지역 모두 사용한다. Southeast Asia/West US
원격 image pull의 지연, 전송비, registry 장애 의존성과 데이터 주권을 검토한다.
필요하면 Premium 지리 복제/지역 registry를 설계하되 digest 동일성을 검증한다.

실행 흐름:
  build 환경 OIDC -> unittest -> linux/amd64 image 한 번 build/push
  -> release-RUN_ID artifact에 digest/repo/commit/run 기록
  -> prod-kr 승인 -> 한국 10/50/100% -> prod-sea 승인 -> Southeast Asia 10/50/100%
  -> prod-usw 승인 -> West US 10/50/100%.
각 지역은 보호된 reusable workflow이다. matrix 병렬 배포가 아니다.
다음 지역은 이전 지역 job의 success에만 실행된다.

Southeast Asia 실패 시 해당 지역만 자동 복구하고 건강한 한국은 그대로 둔다. West US 실패 시
West US만 자동 복구한다. 전 지역 원자성이나 분산 트랜잭션을 보장하지 않는다.
전체 릴리스 복구는 별도 restore workflow의 all로 West US->Southeast Asia->한국 순서로
시도한다. 일부 실패해도 나머지 지역을 시도한다.

1. 준비물과 로컬 확인

  Azure 구독과 Entra tenant, GitHub 저장소 main 브랜치.
  GitHub 호스팅 Ubuntu 24.04 runner 및 Actions artifact v4 사용 가능.
  GitHub environment required reviewers를 지원하는 저장소/요금제.
    특히 private repo의 승인 기능은 플랜에 따라 다르다. 사용할 수 없는
    환경에서 approval이 있다고 가정하지 않는다.
  로컬 PowerShell 7, Python 3.13, Azure CLI, Bicep CLI, Docker Linux engine,
  Git, GitHub CLI(gh). Docker Desktop 라이선스 조건도 확인한다.
  CLI 2.91.0/Bicep 0.43.8로 로컬 Bicep 컴파일을 검증했다.
  hosted runner의 Azure CLI/extension 버전은 운영 전에 검증하고 고정한다.
  region workflow가 log-analytics extension이 없을 때 명시적으로 설치한다.
  Container Apps CLI는 core 명령을 사용한다.
  초기 설치자: 해당 범위 Contributor + Role Based Access Control Administrator
    또는 Owner. Entra app registration/SP 생성 권한(Application Developer/
    Application Administrator 또는 조직 정책에 따른 동등 권한).
  배포 실행 identity에는 Owner/구독 Contributor/클라이언트 비밀이 필요 없다.

이 디렉터리에서:
  python -m unittest discover -s tests -v
  python -m compileall -q app scripts tests
  az bicep build --file infra\shared.bicep --stdout
  az bicep build --file infra\region.bicep --stdout
  python app\server.py
다른 터미널:
  Invoke-RestMethod http://localhost:8080/healthz
  Invoke-RestMethod http://localhost:8080/work
  docker build --platform linux/amd64 -t safe-demo:local app
  docker run --rm -p 8080:8080 safe-demo:local
Python 앱은 표준 라이브러리만 사용한다. /work는 42를 반환하는 비즈니스
계약과 JSON request log를 제공한다. /healthz는 준비 상태 확인일 뿐 SLI가 아니다.
DEMO_FAILURE_PERCENT=100이면 비즈니스 요청 503, DEMO_LATENCY_MS로 지연을
주입할 수 있다. 이런 실험은 로컬/별도 실습 앱에서만 한다.

2. 최초 Azure bootstrap (배포/복구용으로 재실행 금지)

  az login
  gh auth login
  $subscription = '실제-구독-GUID'
  $repo = 'OWNER/REPOSITORY'
  $acr = '전세계고유소문자숫자이름'
  $prefix = 'safeaca'
  az account set --subscription $subscription
  az bicep version
  docker info

본인에게 최초 ACR push 권한이 필요하다. 새 registry 생성 후 Owner라도
조직 RBAC/ABAC 정책 때문에 push가 안 될 수 있다. 이 경우 registry scope의
AcrPush를 설치자 principal에 부여한다. 이 템플릿은 일반 RBAC registry이다.
ABAC repository permission mode로 바꾼 경우 AcrPush/AcrPull을 그대로 쓸 수
없으므로 역할과 workflow를 별도로 수정해야 한다.

  .\scripts\bootstrap.ps1 -SubscriptionId $subscription -AcrName $acr -Prefix $prefix

생성:
  PREFIX-shared: ACR Basic, image pull 전용 user-assigned managed identity,
                 그 identity의 registry scope AcrPull.
  PREFIX-kr/sea/usw: workspace, ACA Consumption 환경, 앱, startup/readiness/
                  liveness probes, minReplicas=1, maxReplicas=3.
  초기 bootstrap image도 digest로 고정한다.
  Multiple revisions + 명시적 APP--bootstrap=100. latestRevision=true 없음.
  state\bootstrap-outputs.json에 app resource ID, group, workspace resource ID,
  workspace customer GUID, FQDN을 저장한다. state는 gitignore 대상이다.
  60초의 최초 RBAC 전파 대기는 보장이 아니다. pull 실패 시 RBAC 전파/네트워크/
  공급자 등록을 조사한다. 기존 앱 발견 시 스크립트는 중단하므로 부분 bootstrap
  실패 복구는 Azure 상태를 확인한 후 아직 생성되지 않은 지역에 한해서 다음
  수동 명령을 사용한다(이미 존재하는 앱에는 실행하지 않는다):

  az deployment group show -g "$prefix-shared" -n shared
  az acr repository show -n $acr --image 'safe-demo:실제-bootstrap-tag'
  # 위 결과의 loginServer, pullIdentityId, digest를 아래에 입력
  az group create -n "$prefix-usw" -l westus
  az deployment group create -g "$prefix-usw" -n bootstrap --template-file infra\region.bicep --parameters location=westus "appName=$prefix-usw-app" image=REGISTRY.azurecr.io/safe-demo@sha256:DIGEST acrServer=REGISTRY.azurecr.io pullIdentityId=실제-identity-resource-ID

infra\region.bicep는 반드시 bootstrap 전용이다. 라이브 앱에 재배포하면
template/traffic을 bootstrap으로 덮어쓰므로 복구/업그레이드 수단이 아니다.
실제 인프라 관리에서는 application template/traffic 소유권을 pipeline과
분리하고 IaC drift 정책/잠금을 설계한다. 이 예제는 bootstrap을 배포하지 않는다.

3. GitHub 보호 환경과 OIDC 설정

GitHub Settings -> Environments에 다음 7개를 미리 만든다:
  build
  prod-kr, prod-sea, prod-usw
  prod-rollback-kr, prod-rollback-sea, prod-rollback-usw

prod-*와 rollback 환경에 required reviewers를 지정하고 승인자 본인의 배포
승인을 막는다(Prevent self-review). main만 deployment branch로 허용하고
관리자 우회도 조직 정책에 맞게 차단한다. build도 main만 허용한다.
복구는 긴급 승인자가 신속히 승인하도록 별도 reviewer/on-call 절차를 둔다.
배포 환경의 승인 대기 중에는 트래픽이 바뀌지 않는다.
main branch 보호, CODEOWNERS로 .github/workflows, scripts, infra 변경 검토,
외부 PR의 비신뢰 코드 실행 차단, Actions 사용 제한도 필수이다.
Artifact와 workflow 수정 권한이 있는 저장소 관리자를 신뢰 경계로 간주한다.

각 환경은 서로 다른 Entra app/SP identity를 갖는다. 아래 helper가
클라이언트 비밀 없이 federated credential과 최소 범위 역할을 생성하고
gh variable set --env로 환경 변수를 설정한다. gh는 저장소 관리 권한이 필요하다.
먼저 파일을 main에 push하고 Azure 로그인 없는 OIDC 검사 workflow를 실행한다:

  gh workflow run oidc-inspect.yml --repo $repo --ref main
  gh run list --repo $repo --workflow oidc-inspect.yml
  $inspectRun = '위에서-확인한-실제-검사-run-ID'
  gh run watch $inspectRun --repo $repo
  # 7개 환경의 승인 필요. JWT 자체는 출력/보관하지 않고 claim metadata만 저장.
  gh run download $inspectRun --repo $repo -D state\oidc-claims
  Get-Content "state\oidc-claims\oidc-claims-$inspectRun-prod-kr\claims-prod-kr.json"

이 검사만 matrix이며 실제 배포는 여전히 지역별 순차 실행이다. 각 claim의
iss/aud/sub/repository/repository_id/repository_owner_id/ref/환경을 검토한다.
파일은 신뢰한 main workflow의 해당 run에서 내려받아야 한다. helper는 로컬 JWT
서명을 검증하는 도구가 아니며, 최종 서명/issuer/audience/subject 검증은 Entra가
한다. metadata 파일도 임의 편집하지 않는다. 실제 운영 job과 다른 sub가 나오는
조직 custom template(job_workflow_ref 포함 등)을 사용한다면 이 보조 검사만으로
설정하지 말고 실제 build/region/restore job의 로그인 직전 동일 allowlist 검사로
정확한 sub를 수집하고 trust를 맞춘다. token 전체를 로그에 출력하면 안 된다.

반복 실행 시 중복 identity를 막기 위해 state\identity-ENV.json 존재를 검사한다.
부분 실패 시 이 파일의 appObjectId/clientId/principalId로 기존 identity의
누락 RBAC/변수를 복구한다. 파일을 지우고 무작정 재실행하지 않는다.

  .\scripts\configure-identity.ps1 -Repository $repo -Environment build -SubscriptionId $subscription -AcrName $acr -ClaimsFile "state\oidc-claims\oidc-claims-$inspectRun-build\claims-build.json"
  $outputs = Get-Content state\bootstrap-outputs.json -Raw | ConvertFrom-Json
  foreach ($region in @('kr','sea','usw')) {
    $r = $outputs.$region
    foreach ($environment in @("prod-$region", "prod-rollback-$region")) {
      .\scripts\configure-identity.ps1 -Repository $repo -Environment $environment -SubscriptionId $subscription -ResourceGroup $r.RESOURCE_GROUP -ContainerApp $r.CONTAINER_APP -WorkspaceResourceId $r.WORKSPACE_RESOURCE_ID -WorkspaceCustomerId $r.LOG_ANALYTICS_WORKSPACE_ID -ClaimsFile "state\oidc-claims\oidc-claims-$inspectRun-$environment\claims-$environment.json"
    }
  }

역할:
  build: registry scope AcrPush(push/manifest 조회) + Reader(az acr show/login 조회).
  prod-REGION / prod-rollback-REGION:
    해당 앱 resource scope Container Apps Contributor.
    해당 지역 resource group scope Reader(환경/리소스 조회).
    해당 workspace resource scope Log Analytics Reader.
  앱의 image pull UAMI: 공유 ACR scope AcrPull.
  배포 identity는 역할 부여 권한, Entra 관리 권한, registry push 권한이 없다.
  rollback identity는 별도 client ID이나 같은 지역 복구에 필요한 앱 권한을 갖는다.
  Reader/Contributor의 광범위 read 접근이 민감하면 조직 custom role로 줄인다.

GitHub environment variables(대소문자 그대로):
  모두: AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
  build: ACR_NAME
  각 지역 deployment/rollback: RESOURCE_GROUP, CONTAINER_APP,
                               LOG_ANALYTICS_WORKSPACE_ID
  LOG_ANALYTICS_WORKSPACE_ID는 Azure resource ID가 아니라 customer GUID이다.
  비밀 값/ACR admin password/AZURE_CREDENTIALS secret은 사용하지 않는다.
  gh variable list --repo $repo --env prod-kr 로 확인한다.

  OIDC federated credential의 claim:
  issuer:    https://token.actions.githubusercontent.com
  audience:  api://AzureADTokenExchange
    subject:   검사한 해당 job의 실제 sub를 한 글자도 바꾸지 않고 사용.

  2026-07-15 이후 생성된 저장소 및 그 날짜 이후 이름 변경/이전된 저장소는
  GitHub.com에서 owner/repository ID가 포함된 immutable 기본 sub를 사용한다.
  기존 저장소는 opt-in하지 않았다면 legacy 형식을 유지한다. GHES에는 해당
  immutable rollout이 적용되지 않는다. 현재 날짜가 2026년 10월이므로 새 저장소에
  legacy 문자열을 하드코딩해서는 안 된다.

  기본 환경 subject의 형식 예(실제 token 검사를 대신하지 않음):
    immutable:
      repo:OWNER@OWNER_ID/REPOSITORY@REPOSITORY_ID:environment:prod-kr
      repo:OWNER@OWNER_ID/REPOSITORY@REPOSITORY_ID:environment:prod-rollback-sea
    legacy(그 형식을 유지하는 기존 저장소만):
      repo:OWNER/REPOSITORY:environment:prod-kr
  build/Southeast Asia/West US/복구는 각각 정확한 environment 이름으로 바뀐다. ID를
  추측하거나 repo.created_at만 보고 형식을 결정하지 않는다. 조직/저장소 custom
  subject template, immutable opt-in, rename/transfer에 따라 다를 수 있다.
  configure-identity.ps1은 ClaimsFile의 실제 sub를 사용하며 현재 GitHub API의
  repository/owner ID와 일치하는지도 확인한다. 이름/ID/환경/trust 설정을 바꾸면
  다시 검사하고 기존 Entra federated credential을 명시적으로 갱신해야 한다.
  immutable은 ID를 포함해 namespace 재사용을 방지하지만 예시처럼 이름도 포함될
  수 있으므로 rename 후 문자열이 그대로라고 가정하지 않는다.
  environment가 있는 기본 job이므로 repo:...:ref:refs/heads/main claim이 아니다.
main 제한은 GitHub environment deployment branch protection과 workflow 검사가
담당한다. reusable workflow 경로 자체를 subject로 넣지 않는다.
권한은 contents:read, OIDC job의 id-token:write, artifact 복구의 actions:read.
공식 GitHub Azure OIDC 문서에서 확인한 azure/login commit
8c334a195cbb38e46038007b304988d888bf676a로 최초 임시 token을 얻는다.
최초 OIDC assertion을 3시간 재사용할
수 없으므로 common.py는 30분마다 job의 GitHub OIDC endpoint에서 새 assertion을
받아 동일 identity로 az login --federated-token을 다시 실행한다. assertion은
stdout/로그에 전혀 출력하지 않고(mask 명령에도 넣지 않음), artifact에도 저장하지
않는다. OIDC token은 메모리에서만 취급하며 저장된 Azure credential 파일을 직접
읽지 않는다. az login의 stdout/stderr는 캡처해 버리고 실패 시 exit code만
보고한다. timeout 예외에도 token이 든 command line이 출력되지 않게 처리한다.
이 재인증/Entra token lifetime 정책과
GitHub endpoint 접근을 비운영 구독에서 검증한다. 재인증 실패는 승격하지 않고 rollback을 시도하며,
복구 identity의 새 OIDC 로그인은 별도 restore workflow에서 얻는다.

4. 실행과 진행 상태

파일을 저장소 루트 main에 push한 뒤:
  gh workflow run release.yml --repo $repo --ref main -f profile=demo
  gh run list --repo $repo --workflow release.yml
  gh run watch 실제-RUN_ID --repo $repo

demo는 코드 동작 확인 전용이다. 합성 부하를 /work에 대략 10 req/s로 발생시키고,
각 단계 180초 관찰 + 마지막 로그 수집 지연 120초를 기다린다.
지역당 약 15분 이상이며 지역 간 지연/CLI 지연으로 더 길어진다.
실사용자 검증이나 운영 권장 bake가 절대 아니다.

실서비스 organic 부하/SLI가 준비된 후:
  gh workflow run release.yml --repo $repo --ref main -f profile=production

production: 각 지역 10%, 50%, 100%마다 최소 1시간씩 관찰한다.
5분의 닫힌 비중복 window를 모든 단계에 사용하고 매 window 종료 후 120초
ingestion 지연을 기다린다. 전체 1시간 window를 마지막까지 평가한다.
지역당 약 3시간 6분 + 준비/CLI 시간, 세 지역 총 9시간 이상이다.
regional timeout 240분, build 30분으로 각 job은 hosted 6시간 제한 이내이다.
6시간 초과의 한 job, sleep 24시간, 승인 시간으로 bake 대체는 하지 않는다.
일/주 단위, 지역별 업무 피크, 주말/월말 검증은 이 샘플의 1시간 설정으로
충족되지 않는다. 지역 업무 피크 전체/24~48시간 bake는 외부 durable
orchestrator(예: Durable Functions) + 연속 SLI 평가 + 재개 상태 저장과
보호된 promotion workflow를 별도 설계해야 한다. 예제에 그 기능은 없다.
전체 workflow/GitHub 보관 기간/승인 대기 제한과 runner 과금도 검토한다.

snapshot 단계는 READ ONLY이다:
  명시적 stable=100%, Multiple, stable Healthy/Running/Provisioned/active,
  단일 web 컨테이너, digest image, minReplicas>=1을 확인한다.
  기존 latestRevision=true(0%라도), revision label, 2개 이상 양수 트래픽이면 중단.
  baseline-RUN_ID-REGION artifact를 90일 보존으로 먼저 업로드한다.
  업로드가 실패하면 candidate 생성이나 트래픽 변경은 실행되지 않는다.
  snapshot은 원본 traffic, stable template/hash, 현재 app template, app-scoped
  설정 fingerprint, repo/commit/run/지역/resource ID를 포함한다.
  plain env에 민감정보를 넣으면 artifact에 노출될 수 있다. 비밀은 secretRef와
  Key Vault를 사용하고 artifact 접근 권한/retention을 제한한다.

durable baseline 업로드 뒤에도 candidate 생성/traffic write 전에 read-only
business preflight를 반드시 통과해야 한다:
  guarded baseline을 다시 검증하고 stable revision FQDN의 /healthz와 /work 계약을
  probe source로 확인한다(organic 표본으로 세지 않음).
  production은 end=현재 UTC-120초, start=end-300초인 가장 최근 닫힌 organic
  window를 조회해 stable >=100건, 오류/비즈니스 실패 <=1%, p95<=500ms를
  요구한다. 이미 건강하지 않거나 표본/telemetry가 없으면 새 배포를 시작하지 않는다.
  demo는 직접 business probe만 확인한다. organic baseline SLO를 검증하는
  production preflight를 대체하지 못하는 교육용 제한이다.
  판정/원시 집계/쿼리는 baseline-preflight.json evidence로 기록한다.
  실패하면 mutation-intent를 쓰지 않고 candidate copy/traffic write도 하지 않는다.
  always cleanup 역시 mutation-intent가 없는 preflight 실패에는 traffic을
  재설정하지 않는다. 기존 baseline traffic을 그대로 둔다.

새 candidate 이름: APP--rRUN_ID. 기존 stable을 --from-revision으로 복사하고
image digest와 RELEASE_ID만 바꾼다. 복사 전 이미 traffic이 명시적 stable=100
이므로 새 revision이 latest=100을 상속할 여지가 없다. 생성 직후에도
stable=100/candidate=0 상태를 검증한다. latest/label 대상 traffic 명령은 없다.
준비 완료 후 candidate revision FQDN의 /healthz와 /work를 직접 검사하고,
명시적 이름으로 stable=90/candidate=10, 50/50, 0/100으로 바꾼다.
각 단계/관찰 중 app-scoped 설정, stable template, candidate 전체 template,
활성/준비 상태, traffic drift를 검사한다. 이전 stable은 비활성화/삭제하지 않는다.
다음 릴리스는 직전 candidate를 새 stable로 삼는다.

주의: revision FQDN은 일반 ingress traffic 분배를 우회한다. 이 예제는 label을
추가하지 않지만 공개 revision FQDN 자체가 직접 호출 가능하다. label을 쓰더라도
제거만으로 revision URL 접근이 차단되는 것은 아니다. 공개 데모 앱에 비밀/운영
데이터를 넣지 않는다. 운영은 앱 계층 인증, 내부 ingress/VNet + private runner,
또는 검증된 공통 접근 제어를 사용하고 main/revision host 양쪽에 정책을 적용한다.
단순히 label을 지우거나 DNS를 숨기는 것을 보안으로 간주하지 않는다.

5. Health gate의 의미와 fail-closed

ContainerAppConsoleLogs_CL의 Log_s JSON을 쿼리한다. 앱/정확한 revision 이름,
앱 이벤트 UTC timestamp의 닫힌 window, 로그 수집 시간 범위, request_id를
사용하고 request_id/revision별 중복을 제거한다. 제어/데모 로그는 현재
monitor_run=RUN_ID에 한정한다. 일반 사용자 로그는 요청에 배포 run을 강제로
붙이지 않으므로 현재 run의 window와 정확한 revision으로 범위를 한정한다.
후보 revision 이름 자체도 run 전용이다. 다른 릴리스의 window/전체 workspace
평균으로 후보 오류를 숨기지 않는다. 쿼리/원시 집계/판정은 evidence에 저장된다.

모든 평가 window:
  production 후보 organic /work >=100건, 10/50%의 stable organic >=100건.
  demo는 현재 run의 demo /work >=20건(organic을 대신하지 않음).
  두 revision의 live control business 요청을 계속 실행:
    production >=30건/5분, demo >=5건/1분.
  후보와 stable의 HTTP 5xx/비즈니스 결과 실패 합집합 <=1%.
  각각 p95 latency <=500ms.
  후보 error rate <=stable reference +0.5 percentage point.
  후보 p95 <=stable reference*1.5 +50ms.
  direct /healthz, /work contract, revision identity도 계속 검사.
  100% 단계에는 stable organic 유입이 없으므로 직전 50% 단계 마지막 window의
  검증된 business reference를 고정하고 stable live control을 계속 검사한다.
  reference 없이 100% gate를 통과하지 않는다.

위 숫자는 예시일 뿐 업종/SLO/통계적 검정의 권장치가 아니다. 낮은 부하에서
통과시키려고 samples=0으로 바꾸지 않는다. 실제 SLO, 최소 표본, 관측 지연,
오류 예산, 시간대/피크 부하를 서비스 운영자가 결정해야 한다.
누락 테이블/권한/쿼리 실패/표본 부족/NaN/로그 지연은 모두 실패로 처리한다.
2분 이후 도착하는 로그는 해당 판정에 없을 수 있다. 허용 가능한 수집 지연을
측정하고 lag를 조정해야 한다. 지연 허용을 늘려도 자동 성공 처리하지 않는다.
첫 닫힌 window가 준비되기 전에도 readiness/contract/control 실패는 중단한다.
전체 duration 평균이나 healthz=200만으로 승격하지 않는다.

이 앱의 telemetry는 최소 데모 모델이다. header X-Safe-Source/X-Safe-Run은
인증되지 않아 외부 사용자가 위조할 수 있다. 운영에서는 synthetic 분류/추적을
서버의 신뢰된 인증 identity 및 tracing으로 바꾼다. 로그가 앱 도달 후에만 생성되어
ingress/network timeout, client 오류, 부분 로그 유실은 이 로그만으로 완전히
포착하지 못한다. 실제 서비스에서는 ingress/외부 사용자 가용성, dependency/
DB errors, queue lag, saturation, CPU/memory/restarts와 핵심 business KPI를
추가하고 동일 run/window/revision 상관관계로 함께 fail-closed gate해야 한다.
특히 성공 요청만 log하는 실제 앱으로 교체하면 안 된다.
rollback 후 검증은 exact traffic/준비 상태/다중 business probe를 확인하지만
장시간 실제 사용자 SLI 복구를 보증하지 않는다. 장애 대응자가 별도 관찰한다.

6. 오류/취소/재시도/수동 복구 runbook

일반 예외/metric 실패: 현재 지역만 즉시 원본 snapshot traffic으로 되돌린다.
candidate 생성 실패도 원본 보존 상태이면 복구할 수 있다.
복구 전 app/traffic/template 소유권 drift 검사와 old revision /work 검사,
복구 후 exact traffic 및 main host에서 old revision 응답 5회 확인을 한다.
Azure 반영 지연은 최대 120초 재검사한다. 후보와 stable 모두 active로 남긴다.
다른 릴리스/운영자 변경이 보이면 blind overwrite하지 않고 수동 조사로 중단한다.
stable이 Healthy하지 않거나 비즈니스 계약이 깨지면 잘못된 old로 강제 복귀하지
않는다. 앱 복구 또는 다른 검증된 revision 선택은 별도 incident 절차이다.

워크플로 취소:
  Python SIGINT/SIGTERM에서 best-effort rollback.
  별도 always cleanup step도 baseline 업로드 성공 시 복구를 재시도한다.
  그러나 GitHub 취소의 짧은 유예/SIGKILL/runner 유실/네트워크 단절/Azure API
  장애/job timeout에서는 cleanup 실행이나 완료를 절대 보장할 수 없다.
  취소되었다고 트래픽이 원복되었다고 간주하지 않는다.
  취소 후 원본 run이 completed 상태가 된 뒤 아래 보호된 복구를 실행한다.
  복구는 새 runner/새 rollback OIDC identity로 로그인한다.

Runner와 독립된 Alert Manager / out-of-band 복구(운영 필수):
  이 샘플의 Actions gate/always step만으로 on-call 경보를 대신할 수 없다.
  Azure Monitor scheduled-query/metric alerts와 Action Group을 GitHub runner와
  독립적으로 구성하여 각 지역의 오류/latency/비즈니스 KPI, telemetry 부재,
  앱/revision 실패, ingress 실패를 paging 시스템/Alertmanager에 연결한다.
  GitHub workflow/job completed/cancelled webhook 및 별도 주기적 run 상태 검사로
  runner 상실/timeout을 감지하고, 배포 진행 중에는 실제 Azure traffic과
  기대 단계도 독립적으로 대조한다. Actions 프로세스가 보내는 성공 신호만
  신뢰하거나 건강한 /healthz만으로 경보를 해제하면 안 된다.
  경보에 repository, 원본 run ID, region, stable/candidate 이름, snapshot artifact
  이름을 포함하고 incident 담당자가 위 취소 확인 후 아래 restore를 실행한다.
  GitHub 호스팅 runner 전체 장애에 대비해 별도 관리 runner/관리 워크스테이션에서
  새 승인된 rollback identity로 로그인하여 원본 snapshot을 검증하고 guarded
  restore를 실행할 수 있어야 한다. 원본 baseline을 추측해서 새로 만들지 않는다.
  GitHub 자체 장애까지 대비하려면 mutation 전에 baseline/evidence를 독립된
  불변 Blob 저장소에도 복제하고 검증해야 하며, 별도 Azure recovery orchestrator/
  identity/접근 승인 및 break-glass 감사 절차가 필요하다. 예제는 GitHub artifact만
  필수 저장하므로 이 독립 저장/경보/orchestrator는 구현되어 있지 않다.
  실제 운영 투입 전 위 경보 및 runner 강제 종료 후 복구 훈련을 별도로 완료한다.

원본 release run ID를 찾는다(복구 workflow run ID가 아님):
  gh run list --repo $repo --workflow release.yml
  gh run view ORIGINAL_RUN_ID --repo $repo
  gh run download ORIGINAL_RUN_ID --repo $repo -n baseline-ORIGINAL_RUN_ID-kr -D state\inspect-kr
  Get-Content state\inspect-kr\baseline-kr.json

단일 지역:
  gh workflow run restore.yml --repo $repo --ref main -f source_run_id=ORIGINAL_RUN_ID -f region=sea
전체 릴리스:
  gh workflow run restore.yml --repo $repo --ref main -f source_run_id=ORIGINAL_RUN_ID -f region=all

rollback 환경 승인 -> 원본 release.yml/main/completed/repo/commit 확인
-> 원본 run의 immutable baseline artifact 다운로드 -> 현재 Azure drift 확인
-> 해당 stable의 원본 traffic 복구. 소스 workflow는 본 저장소의 workflow_dispatch
release.yml이어야 하며, artifact filename/run/context를 재검증한다.
all은 usw,sea,kr를 순차 시도하며 앞 지역 실패에도 다음을 시도한다.
원본 릴리스에서 시작하지 않은 지역은 baseline artifact가 없으므로 그 복구 job은
안전하게 실패(변경 없음)한다. 실패가 모두 "미시작 지역"인지 실제 복구 실패인지
로그를 구분한다. 이런 경우 실제 변경한 지역만 골라 복구하는 것이 명확하다.

release.yml의 Re-run jobs/failed jobs는 금지한다. run_attempt!=1이면 변경 전에
중단하고 기존 baseline을 덮어쓰지 않는다. 애매한 candidate를 멱등하게 재사용하지
않고 fail-closed한다. 재시도는 원본 ID로 복구/현재 상태 검증 후 NEW dispatch
(새 run ID)이다. restore 자체는 현재 상태가 원본 또는 해당 run candidate 범위일
때 반복 가능하다. 더 최신 릴리스가 이미 끝났다면 오래된 snapshot 복구는
template/traffic drift 검사로 거부한다. 순서가 다른 릴리스를 강제로 되감지 않는다.
baseline이 만료/삭제되면 이 자동 복구 경로는 사용할 수 없다. 추측해서
latest/임의 revision 100%를 주지 말고 변경 기록/보관된 독립 증거를 조사한다.

두 진입 workflow는 동일 global concurrency key safe-aca-production 및
cancel-in-progress:false를 사용한다. 배포/수동 복구는 동시에 실행되지 않는다.
실행 중 release가 멈추지 않으면 복구는 대기한다. 급하면 release를 취소하고
completed 확인 후 restore를 요청한다. GitHub concurrency는 저장소 범위이며,
다른 저장소/수동 CLI/IaC를 막는 Azure 분산 잠금이 아니다. 동시 write를
RBAC/변경 창으로 금지한다. read-check/write 사이의 경쟁을 완전히 원자적으로
막는 시스템도 아니다. pending 항목은 GitHub concurrency 동작상 후속 실행에
의해 대체될 수 있고 FIFO 큐가 아니다. 여러 복구/배포 요청을 연속 제출하지
말고 incident 담당자가 하나씩 진행한다.

7. 보관/보안/비복구 가능 변경과 운영 전 체크

baseline/release/gate/restore evidence artifact는 90일 요청 보관.
저장소/조직의 허용 retention이 90일 이상인지 먼저 확인한다. 더 긴 규정은
불변 Blob 보관, 서명/attestation, 접근통제 및 감사로그를 별도 추가한다.
후보 이미지/기존 stable image digest와 revision을 보관 기간 동안 삭제하지 않는다.
ACR 자동 purge/retention과 수동 revision cleanup에서 복구 대상을 보호한다.
활성 old revision + minReplicas=1은 지속 비용이 든다. 릴리스마다 쌓이므로
보관 정책에 따른 별도 승인 cleanup이 필요하다. 이 예제는 자동 삭제하지 않는다.
maxInactiveRevisions는 ACTIVE revision 누적을 정리해 주는 설정이 아니다.

revision traffic rollback은 DB migration, 데이터 write, schema/메시지 변경,
공유 secrets 값/Key Vault 최신 버전, identity/RBAC, DNS/custom domains,
app-scoped ingress/auth/scale 주변 설정, 외부 시스템 side effect를 되돌리지 못한다.
fingerprint는 GET으로 보이는 설정 drift를 감지하는 것이지 숨겨진 secret 값/
외부 상태 변경을 감지하거나 롤백하는 기능이 아니다. expand/contract migration,
하위 호환성, secret version pinning, feature flag/kill switch, 데이터 복구 절차가
별도로 필요하다. drift가 발견되면 자동 트래픽 복구도 거부할 수 있다.

azure/login은 공식 GitHub Azure OIDC 문서에서 확인한 full commit SHA로 고정했다.
나머지 Actions는 검증되지 않은 SHA를 꾸며 넣지 않고 공개 major tags(v4/v5)를
사용한다. tag는 mutable이다. 운영 투입 전 사용 action의 release/공급망을
검토하고 검증한 full commit SHA에 고정, Dependabot 등 업데이트 절차를 적용한다.
Docker base python:3.13-slim도 build 시점 mutable이다. 최종 배포 image digest는
한 번 고정되지만 재현 가능한 build를 위해 검증된 base digest, SBOM, image
서명/취약점 검사 및 provenance를 추가해야 한다.

자격 증명 없이 로컬에서 확인한 범위:
  Python unittest, HTTP contract/log, safety/metric/rollback 유닛 검증,
  Python 컴파일, workflow YAML 파싱, Bicep 컴파일, PowerShell 파싱.
Azure 실제 프로비저닝/이미지 빌드·push/GitHub OIDC/승인/Log Analytics 수집/
실제 split/cancellation 복구는 비운영 구독에서 반드시 통합 시험한다.

공식 참조(명령/API 형식 확인):
  https://learn.microsoft.com/en-us/azure/well-architected/operational-excellence/safe-deployments
  https://learn.microsoft.com/en-us/devops/operate/safe-deployment-practices
  https://learn.microsoft.com/cli/azure/containerapp/revision
  https://learn.microsoft.com/cli/azure/containerapp/ingress/traffic
  https://learn.microsoft.com/cli/azure/monitor/log-analytics
  https://learn.microsoft.com/azure/container-apps/log-monitoring
  https://learn.microsoft.com/azure/templates/microsoft.app/2024-03-01/containerapps
  https://learn.microsoft.com/azure/developer/github/connect-from-azure-openid-connect
  https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure
  https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims
  https://docs.github.com/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment
  https://docs.github.com/actions/using-jobs/using-concurrency
  https://docs.github.com/actions/reference/limits
