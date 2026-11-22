# 배포 관제실 Azure 재구축 권한 체크리스트

대상: Azure 상용 클라우드에 배포 관제실(Console)과 GitHub 웹훅 수신기(Receiver)를 별도 Container App으로 설치하는 담당자. 설치 관리자, 런타임 관리 ID, GitHub Actions 배포 ID, SAS 갱신 담당자를 분리한다. 운영과 시험에는 별도 GitHub App, 웹훅 비밀, 토큰 저장소 및 관리 ID를 사용한다.

## 1. 최초 설치와 권한 할당

| 작업 | 필요한 권한과 범위 | 완료 후 |
| --- | --- | --- |
| Azure 리소스 설치 | 설치 담당자에 대상 리소스 그룹 `Contributor` 또는 동등한 배포 권한. 기존 공유 리소스는 별도 확인: ACA 환경 조회·join, UAMI 연결(`Managed Identity Operator` 또는 동등 권한), VNet 서브넷 및 Private Endpoint/Private DNS zone·link 작업 권한. 이미 설치돼 있으면 생성 권한은 불필요하다. 새 구독의 리소스 공급자 등록은 필요 시 구독 관리자에게 맡긴다. | 임시 설치 권한 회수 |
| Azure RBAC 구성 | 대상 범위의 `Microsoft.Authorization/roleAssignments/write`가 필요하다. `Role Based Access Control Administrator`는 기존 역할 할당에 사용할 수 있다. 삽입·서명 전용 사용자 지정 역할의 신규 정의에는 `Microsoft.Authorization/roleDefinitions/write`가 별도로 필요하므로 RBAC 관리자가 미리 정의하거나, 해당 범위에서 임시 `User Access Administrator` 등으로 처리한다. | 필수 역할 할당을 확인한 뒤 설치용 관리자 권한 회수 |
| ACR 이미지 게시 | 게시자만 일반 RBAC 레지스트리에 `AcrPush`, ABAC 레지스트리에 저장소 조건부 `Container Registry Repository Writer`. `az acr build`를 쓰면 레지스트리의 Quick Build 실행·상태 조회 권한(`Container Registry Tasks Contributor` 또는 동등 권한)도 확인한다. 런타임에는 게시 권한을 주지 않는다. | 일회성 게시 권한 회수 |
| Key Vault 초기화 | RBAC 기반 Vault의 비밀 작성자에 `Key Vault Secrets Officer`, RSA PEM 가져오기 담당자에 `Key Vault Crypto Officer` 또는 `keys/import/action` 및 필요한 조회 권한. 신규 객체 생성에 Vault 범위가 필요하면 임시로 사용한다. **이 구성의 RSA key import에 `Certificates Officer`는 필요하지 않다.** | 서명·비밀 해석 검증 후 임시 가져오기 권한 회수 |
| Entra 등록과 할당 | 테넌트 앱 등록 정책에 따른 권한과 앱 소유권. Enterprise Application의 할당 필수·앱 역할 할당은 해당 앱을 관리할 수 있는 소유자/관리자가, 관리자 동의가 필요한 경우에는 적격 Entra 관리자가 처리한다. Single-tenant 웹 앱에 Console의 `/.auth/login/aad/callback`, ID token 발급, `ReleaseOperator`/`ReleaseApprover`를 구성한다. | 관리자 역할을 사용자나 앱 런타임에 상시 부여하지 않는다. |
| GitHub 설정 | App 소유자 및 저장소 관리자: 대상 저장소에만 App 설치, `Actions: read/write`, `Deployments: read/write`, `Deployment protection rule`, `Workflow run`(`workflow_run`), `Workflow job`(`workflow_job`) 이벤트 구독, Receiver URL과 서명 비밀 등록, 환경별 App 규칙 및 브랜치 제한 확인. | 운영 사용자에게 App 소유권이나 저장소 관리자 권한을 일괄 부여하지 않는다. |

## 2. 상시 런타임 권한

| 주체 | 권한과 최소 범위 |
| --- | --- |
| Console/Receiver 관리 ID, 각자 | 일반 RBAC ACR의 `AcrPull` 또는 ABAC 레지스트리의 `Container Registry Repository Reader`를 해당 이미지 저장소 조건으로 할당. |
| Console 관리 ID | 모니터링할 각 ACA 리소스에 `Reader`; 로그인 비밀 및 SAS URL 비밀에 각각 `Key Vault Secrets User`; GitHub App 키 하나에 `Microsoft.KeyVault/vaults/keys/sign/action`만 가진 사용자 지정 역할; `dashboardops` 테이블에 `Storage Table Data Contributor`, `dashboardinbox` 테이블에 `Storage Table Data Reader`. Azure 대상 앱 쓰기 권한은 부여하지 않는다. |
| Receiver 관리 ID | 웹훅 비밀 하나에 `Key Vault Secrets User`; `dashboardinbox` 테이블에 `Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action`만 가진 역할. GitHub 서명, 운영 테이블, Azure 배포 권한은 부여하지 않는다. |
| GitHub Actions OIDC ID | 별도 ID에 대상 ACA별 `Container Apps Contributor` 또는 검증된 더 좁은 역할. 기존 워크플로가 Log Analytics 쿼리를 게이트에 사용하면 해당 워크스페이스에 필요한 조회 권한만 추가한다. 워크플로의 `id-token: write`와 실제 발급 `iss`/`aud`/`sub`에 일치하는 자격 증명을 구성한다. 저장소 및 환경 이름을 추정해 subject를 만들지 않는다. |
| 운영 사용자 | `ReleaseOperator`: 배포 요청·실행 취소·롤백·Retire 요청. `ReleaseApprover`: 다른 사용자 요청의 App 승인·거부. 두 역할이 있어도 자기 요청은 승인할 수 없다. |

## 3. SAS 갱신에 유지할 권한

- 발급 ID: Storage 계정 범위의 `Storage Blob Delegator` 또는 `generateUserDelegationKey/action`을 포함하는 기존 권한, 그리고 `dashboardtokens` **컨테이너 범위**의 `Storage Blob Data Contributor` 또는 동등한 `rwd` 데이터 권한. 활성 SAS의 발급 ID 데이터 권한은 함부로 회수하지 않는다.
- 갱신 담당자 또는 전용 Job: SAS 비밀에만 새 버전을 기록할 권한. 별도 적용 담당자에는 Console 리소스 변경 권한만 부여한다. SAS 원문은 로그·파일·채팅에 남기지 않는다.
- Storage에서 공개 접근·공유 키를 켜지 않는다. ACA VNet의 Blob/Table Private Endpoint 및 Private DNS를 구성한다. 사설 ACA 경로에서 교체 SAS로 임시 Blob PUT/GET/DELETE를 확인하고, Key Vault의 버전 URI를 적용한 후 새 로그인과 조회를 검증한다. 사용자 위임 SAS는 최대 7일이다.

## 4. 가동 확인과 회수

1. 새 브라우저의 Entra 로그인과 배포 목록, Receiver의 서명된 GitHub `ping` 응답 `204`, 권한 없는 요청 거부를 확인한다. `ping`만으로 승인 경로를 입증하지 않는다.
2. 격리 환경에서 **관제실 요청 → 서명된 보호 규칙 이벤트 → 다른 `ReleaseApprover`의 승인 → 배포**, 롤백과 별도 Retire를 검증한다. App-only 운영에서는 `Required reviewers`를 추가하지 않되 **각 환경에 올바른 App 규칙을 활성화**한다. 기존 브랜치 제한은 유지한다. CLI/GitHub에서 직접 시작해 관제실 요청 기록이 없는 실행은 관제실에서 승인할 수 없다.
3. 설치·키 가져오기에 부여한 임시 역할의 **할당 ID**를 기록하고 가동 검증 후 해당 ID만 회수한다. 런타임 관리 ID, 활성 SAS 발급자, 별도 승인된 상시 권한은 회수 대상이 아니다.

참고: [관제실 운영 가이드](07-deployment-dashboard.md) · [Azure Key Vault RBAC](https://learn.microsoft.com/azure/key-vault/general/rbac-guide) · [Azure ACR 역할](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-built-in-roles-directory-reference) · [ACA 토큰 저장소](https://learn.microsoft.com/azure/container-apps/token-store) · [GitHub App 보호 규칙](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/configure-custom-protection-rules).
