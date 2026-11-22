# 7장. 배포 관제실 운영 가이드

관제실은 별도 Azure Container App입니다. 운영자는 **Build → KR → SEA → USW**를 따라 실행, 지역 승인, Azure 트래픽·리비전·레플리카를 봅니다. `/verify`와 같은 JSON endpoint를 읽되 HTTP에 응답한 표본을 Azure 인벤토리와 구분합니다. 주요 ENV는 허용 목록만 표시합니다. 승인 대기와 Azure 트래픽 100%는 서로 다른 정보입니다.

관제실은 기존 `release.yml`, `restore.yml`, `demo-retire.yml`에 요청합니다. Azure 쓰기는 기존 워크플로의 OIDC 및 안전 스크립트가 담당합니다. 재실행, 진행 중 원본의 롤백, 전체 지역 롤백, 운영 프로필의 retire와 비활성 리비전 재활성화는 관제실에서 지원하지 않습니다.

## 사전 준비

새 Azure 환경의 역할·할당 범위·회수 절차는 [Azure 재구축 권한 체크리스트](08-deployment-dashboard-permissions.md)를 참고합니다.

1. **비운영 환경부터 구성합니다.** GitHub App custom deployment protection rule은 public preview입니다. 비공개 저장소의 플랜 조건과 조직 정책을 확인합니다. 운영에 적용하기 전 기존 required reviewer를 대체하는 정책 변경을 승인받습니다.
2. **Entra:** single-tenant 웹 앱을 등록하고 `https://<console-fqdn>/.auth/login/aad/callback`을 redirect URI로 설정합니다. ID token 발급을 활성화하고, Entra Enterprise Application에서 **Assignment required**를 켭니다. 사용자에만 할당할 수 있는 `ReleaseOperator`/`ReleaseApprover` 역할을 정의하고 관리자에게 지정합니다. 역할 없이 로그인한 사용자는 앱이 거부합니다. Entra 로그인 secret을 Key Vault에 저장합니다. 앱은 Easy Auth token store가 전달하는 Entra ID token의 서명·issuer·audience·수명과 플랫폼 사용자 정보를 대조합니다. 세션 만료 시 다시 로그인해야 할 수 있습니다.
3. **GitHub App:** 권한을 **Actions: read/write**, **Deployments: read/write**로 지정하고 대상 저장소 하나에만 설치합니다. repository ID, installation ID, App **client ID**를 기록합니다. GitHub App에서 발급한 RSA private key를 Key Vault에 서명 가능한 key로 import합니다. 앱에는 버전이 포함된 Key Vault key URI만 전달합니다. Webhook secret은 서명 키와 별개이며, 같은 고엔트로피 값(32바이트 이상)을 GitHub App과 receiver의 Key Vault secret으로 구성합니다. 키/secret 원문을 이미지·파라미터 파일·로그에 남기지 않습니다.
4. **Azure:** 두 개의 전용 user-assigned MI와 ACA Environment, ACR을 준비합니다. 두 MI에 이미지 pull 권한을 부여합니다(ABAC를 사용하는 ACR이면 repository 읽기 권한을 사용). 콘솔 MI에는 등록된 세 대상 ACA의 `Reader`, Entra 로그인 secret에 대한 Key Vault secret 읽기, GitHub App key에 대한 `Microsoft.KeyVault/vaults/keys/sign/action` DataAction만 부여합니다. Receiver MI에는 webhook secret 읽기만 부여하고 GitHub App 서명 권한을 주지 않습니다. 템플릿은 별도의 Table Storage와 각 테이블의 권한을 생성합니다. 수신기는 inbox insert만, 콘솔은 inbox 읽기와 작업 테이블 사용이 가능합니다. 원격 Key Vault에서 RBAC 전파를 확인한 후 ACA를 구성합니다.
5. **이미지 및 IaC:** 저장소 루트에서 `docker build --file dashboard\Dockerfile --tag <registry>/deployment-dashboard:<tag> dashboard`로 이미지를 만듭니다. 승인된 ACR에 게시해 digest를 얻은 뒤, `infra\dashboard.bicep`의 `image`에 digest 참조를 전달합니다. 두 ACA는 같은 digest를 사용하지만 실행 모듈과 역할이 다릅니다. 템플릿 파라미터 `targets`에는 `kr`/`sea`/`usw` 각각의 전체 Container App 리소스 ID를 전달합니다. 외부 URL은 템플릿의 기본 ACA FQDN을 사용합니다(사용자 지정 hostname과 Azure 정부/중국 클라우드는 범위 밖입니다). 이 템플릿은 VNet·Private Endpoint·DNS 또는 Storage의 공용 네트워크 차단을 구성하지 않습니다. 운영에 사용하기 전에 이 항목을 별도로 구성하고 사설 경로를 검증합니다.
6. **Callback:** 템플릿 출력 `githubCallback`을 GitHub App webhook URL로 등록하고 요청의 `deployment_protection_rule` 이벤트를 받도록 구성합니다. receiver는 서명과 설치·저장소·환경·실행 ID를 검사합니다. `consoleUrl`에는 운영자가 로그인하고, callback URL에는 Easy Auth를 적용하지 않습니다. 서명 실패는 대기 요청을 만들지 않습니다.
   - 같은 App webhook에서 **Workflow runs**(`workflow_run`)와 **Workflow jobs**(`workflow_job`) 이벤트도 구독합니다. 추가 권한은 필요 없습니다(Actions: read에 포함). receiver는 같은 서명·크기·설치·저장소 검사를 적용하고, `main` 브랜치의 허용된 workflow(`release.yml`, `release-kr.yml`, `restore.yml`, `demo-retire.yml`; job 이벤트는 해당 workflow 이름)만 inbox에 delivery당 한 번 변경 힌트(`chg:` 행)로 기록합니다. 다른 브랜치·workflow 이벤트는 저장하지 않고 수락만 하며, 위조·다른 저장소 이벤트는 거부합니다.
   - 변경 힌트는 콘솔의 GitHub 읽기 캐시를 비우고 열린 브라우저에 `/api/changes` 스트림으로 "run X 변경"만 알려 즉시 새로고침하게 하는 용도입니다. 승인·배포·취소·롤백·retire 판단에는 쓰이지 않으며, 쓰기 시점에는 항상 GitHub와 증거를 다시 읽습니다. 두 이벤트를 구독하지 않아도 콘솔은 주기적 새로고침으로 동작하지만 화면 반영이 느려집니다.

**Easy Auth 토큰 저장소:** ACA는 SAS URL이 있는 private Blob 컨테이너를 요구합니다. 신규 배포에서는 대상 Storage와 private `dashboardtokens` 컨테이너를 먼저 준비한 뒤, 공유 키를 켜지 않고 HTTPS 전용 사용자 위임 SAS(`rwd`, 최대 7일)를 발급합니다. URL은 Key Vault 비밀로 저장하고 URI만 `tokenStoreSasSecretUri`에 전달합니다. 콘솔 ID에만 해당 비밀 읽기를 부여합니다. 만료 전에 갱신하고 새 세션 로그인을 확인합니다. 갱신 절차를 검증하기 전 운영 인증에 사용하지 않습니다.

신규 설치는 두 단계로 진행합니다.

1. 먼저 `infra\dashboard.bicep`의 `storage`, `blobService`, `tokenStore`와 같은 이름·리소스 그룹·지역·설정으로 Storage 계정과 Blob 컨테이너를 별도 프로비저닝합니다. 사설 Blob/Table 경로와 발급자 RBAC를 구성한 뒤, 해당 컨테이너에 SAS를 발급하고 콘솔 ID가 읽을 수 있는 Key Vault 비밀의 버전 URI를 준비합니다.
2. 같은 Storage 계정 이름과 비밀 URI를 전달해 전체 템플릿을 **incremental** 모드로 배포합니다. 기존 컨테이너를 재사용합니다. Storage 계정 설정은 템플릿이 다시 적용하므로, 사설 전용 설치에서는 배포 전에 템플릿의 `storage.properties.publicNetworkAccess`를 `Disabled`로 맞추고 배포 후에도 이를 확인합니다.

이미지·IaC 검증:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r dashboard\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s dashboard\tests -v
python -m unittest discover -s tests -v
az bicep build --file infra\dashboard.bicep --stdout
```

기존 `release.yml`이 앱을 빌드할 때는 기존 `tests\`만 실행합니다. 관제실 테스트는 새 의존성을 설치한 환경에서 별도로 실행합니다.

## 비운영 검증과 전환

- 서로 다른 Entra 관리자 두 명을 사용합니다. 운영자에게 `ReleaseOperator`, 승인자에게 `ReleaseApprover`를 부여합니다. 두 역할을 모두 가진 경우에도 본인의 요청을 승인할 수 없는지 확인합니다. 계정 연결 없이 로그인/요청/승인하고, 미할당 사용자/서비스 주체/무권한 사용자의 접근이 거부되는지 확인합니다.
- 검증용 GitHub 환경의 `prod-kr`, `prod-sea`, `prod-usw`, `prod-rollback-*`에서 이 App의 custom protection rule을 사용합니다. branch 제한은 유지하고, 기존 required reviewer가 남아 있으면 별도로 만족해야 합니다. `build` 환경에 사람 gate가 있는지 확인합니다. 기존 환경이 custom rule을 받도록 수정하기 **전에** 되돌릴 보호 설정을 기록합니다.
- demo run을 관제실에서 시작하고 정확한 run ID가 돌아오는지 확인합니다. KR → SEA → USW 순서로 별도 관리자가 승인하고, webhook delivery 및 GitHub job 상태가 맞는지 확인합니다. 거부·취소 후에는 자동 복구를 추정하지 말고 artifact/Azure 트래픽을 대조합니다. 완료한 run에서 한 지역 롤백과 별도 demo run의 retire를 각각 검증합니다. retire 후 이전 revision 재활성화가 별도 절차라는 점을 확인합니다.
- GitHub의 자동 redelivery를 가정하지 않습니다. 서명된 webhook 실패 시 GitHub App의 최근 배달에서 상태를 점검하고 필요하면 그 delivery를 redeliver합니다. 정책을 끄거나 수동으로 통과시키기 전에 이상 원인을 조사합니다.
- 화면의 GitHub 조회는 콘솔 인스턴스 내에서 공유됩니다. 진행 중인 run(및 그 job·승인 대기)과 run 목록은 약 5초, 완료된 run은 약 5분간 재사용하며(ETag 조건부 재조회 유지), 콘솔에서 배포·취소·승인/거절을 요청하면 해당 run과 run 목록의 캐시를 즉시 무효화합니다. 승인·취소·배포를 실제로 요청할 때는 캐시를 사용하지 않고 조건을 다시 확인합니다.
- 운영 정책은 GitHub `Required reviewers`를 관제실의 Entra `ReleaseApprover`와 GitHub App 보호 규칙으로 대체합니다. 브랜치 제한은 그대로 유지하고, 각 환경의 App 규칙 활성화를 다음 배포 전에 확인합니다. 전환 후 CLI에서 직접 시작한 release/restore/retire, `release-kr.yml`은 관제실에서 승인할 수 없습니다. 전환 전에 관제실 장애 시 사용할 별도 승인 경로를 마련하고 비운영 환경에서 검증합니다.

## 운영 중 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| 환경은 대기 중인데 승인할 수 없음 | 콘솔에서 요청한 동일 run, App 설치, webhook delivery/서명, 해당 환경의 규칙과 GitHub job 상태, Entra 승인자의 역할 및 요청자 분리 |
| 작업 결과가 `확인 필요` 또는 `reserved`에 머묾 | 진행 중인 쓰기가 끝나기를 기다립니다. `reserved`는 GitHub 요청이 전달됐지만 저장에 실패한 경우에도 남을 수 있습니다. 운영 지원 담당자가 작업 종류에 맞춰 GitHub run/결정 이력의 워크플로·브랜치·입력·원본 실행·요청 시각을 대조하고 Azure 트래픽을 확인합니다. 해당 App의 승인/거부 결과를 job 진행 여부만으로 추정하지 않습니다. 정확한 결과를 입증하지 못하면 쓰기 잠금을 유지하고 재시도하지 않습니다. 입증되면 `dashboardops`에서 `op:<operation_id>` 정정(접수된 dispatch는 확인한 `run_id` 포함), 접수된 dispatch의 `run:<run_id>` 색인 행 생성(`operation_id=<operation_id>`), 접수 확인된 승인·거부·취소의 `claim:<op의 claim 값>` 생성(`owner=<operation_id>`), 같은 소유자의 `lock:write` 해제를 한 파티션 트랜잭션으로 처리합니다. 작업 행과 잠금은 조회 당시 ETag를 사용합니다. `recent:<역순 시각>:<operation_id>`와 `run:<run_id>`는 `operation_id`만 담는 조회 색인이며 상태는 항상 `op:` 행을 따릅니다. `meta:index-v1`은 기존 행 색인이 끝났다는 표시이므로 삭제하지 않습니다. |
| 실행은 완료됐지만 복구/종료 검증이 표시되지 않음 | 해당 시도의 artifact 유무·만료, 원본 run과 stable revision, 현재 실제 트래픽/active 값을 대조합니다. UI의 `GitHub 요청 접수`는 작업의 성공이 아닙니다. |
| 관제실 중단 또는 보호 규칙 동작 이상 | 기존 안전장치를 삭제하지 말고 변경 요청을 중단합니다. 비상 복구는 승인된 운영 정책과 기존 runbook을 따릅니다. 전환을 되돌리려면 기록한 환경 보호 정책을 복구한 다음 custom rule을 해제합니다. |

Key Vault 서명 키를 교체할 때는 GitHub에서 새 키를 발급해 Key Vault에 import하고 versioned URI를 전환한 뒤, 새 installation token 발급을 확인하고 이전 키를 폐기합니다. Entra/webhook secret도 각각 별도로 회전합니다. 운영 감사 보관 기간은 조직 정책을 적용하고, 진행 중 잠금/작업과 필요한 증거를 보존합니다.
