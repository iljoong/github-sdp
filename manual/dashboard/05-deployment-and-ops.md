# 5. 배포와 운영

이 장은 관제실 자체의 설치·이미지 갱신·비밀 회전·장애 진단을 다룹니다. 명령어 수준의 절차는
[7장 사전 준비](../07-deployment-dashboard.md#사전-준비)와 [8장 권한 체크리스트](../08-deployment-dashboard-permissions.md)를 따릅니다.

## 5.1 최초 설치 흐름

Easy Auth token store가 **SAS URL이 있는 기존 Blob 컨테이너**를 요구하므로, 설치는 두 단계로 나뉩니다.

```mermaid
flowchart TD
    subgraph P0["0. 사전 준비"]
        A1["Entra 앱 등록<br/>앱 역할 · Assignment required"]
        A2["GitHub App 생성<br/>권한 · 단일 저장소 설치"]
        A3["UAMI ×2, ACA 환경, ACR 준비"]
        A4["Key Vault: Entra secret,<br/>webhook secret, App RSA key import"]
        A5["수동 RBAC: AcrPull, 대상 ACA Reader,<br/>KV secret/key 권한"]
    end
    subgraph P1["1단계: Storage 선행 구성"]
        B1["Storage 계정 + dashboardtokens 컨테이너<br/>(Bicep과 같은 이름·설정)"]
        B2["사설 Blob/Table 경로 + DNS"]
        B3["사용자 위임 SAS 발급 (rwd, HTTPS, ≤7일)"]
        B4["SAS URL → Key Vault secret (버전 URI)"]
    end
    subgraph P2["2단계: 전체 템플릿"]
        C1["이미지 빌드 → ACR push → digest"]
        C2["infra/dashboard.bicep 배포 (incremental)<br/>Table·역할·ACA·Easy Auth 생성"]
        C3["출력 consoleUrl → Entra redirect URI"]
        C4["출력 githubCallback → GitHub App webhook URL<br/>이벤트 3종 구독"]
    end
    subgraph P3["3. 가동 검증"]
        D1["Receiver: 서명된 ping → 204"]
        D2["Console: 새 브라우저 로그인 · 목록 표시<br/>역할 없음/미할당 거부"]
        D3["비운영 환경에서 요청 → 승인 → 배포,<br/>롤백 · Retire · 재개"]
        D4["임시 설치 권한 회수 (할당 ID 기준)"]
    end
    A1 & A2 & A3 & A4 & A5 --> B1 --> B2 --> B3 --> B4 --> C1 --> C2 --> C3 --> C4 --> D1 --> D2 --> D3 --> D4
```

| 주의 | 이유 |
| --- | --- |
| Key Vault RBAC 전파를 기다린 뒤 ACA를 배포 | ACA secret 참조가 권한 부족으로 실패하면 리비전이 기동하지 않음 |
| 사설 전용 설치면 재배포 전에 템플릿의 `publicNetworkAccess`를 `Disabled`로 맞춤 | 템플릿이 Storage 설정을 다시 적용 |
| `ping` 204만으로 승인 경로를 입증하지 않음 | 보호 규칙 이벤트는 실제 Environment 규칙이 있어야 발생 |
| GitHub `Required reviewers` 대체는 비운영 검증 후 | 기존 보호 설정을 기록해 두고 되돌릴 수 있게 함 |

## 5.2 이미지 갱신 (`dashboard-deploy.yml`)

인프라·비밀·Easy Auth는 바꾸지 않고 **두 ACA의 이미지 digest만** 교체합니다.

```mermaid
sequenceDiagram
    autonumber
    actor M as 배포 담당자
    participant GA as GitHub Actions<br/>(environment: dashboard)
    participant EN as Entra (OIDC)
    participant ACR as ACR
    participant REC as Receiver ACA
    participant CON as Console ACA

    M->>GA: workflow_dispatch (main 브랜치만)
    GA->>GA: dashboard 테스트 + compileall
    GA->>EN: OIDC 로그인 (federated credential: environment:dashboard)
    GA->>ACR: docker build 1회 → push deployment-dashboard:run-RUNID
    GA->>ACR: digest 조회 (sha256:64hex 검증)
    GA->>REC: az containerapp update --image repo@digest
    GA->>CON: az containerapp update --image repo@digest
    loop 최대 30회 × 10초 (앱마다)
        GA->>REC: latest == latestReady, image 일치, Healthy?
        GA->>CON: latest == latestReady, image 일치, Healthy?
    end
    GA->>REC: curl https://receiver/healthz
    GA-->>M: Step summary: 배포 digest, Console URL
```

- 순서는 **Receiver → Console**입니다. 같은 digest를 사용하므로 두 앱의 코드 버전이 어긋나지 않습니다.
- `concurrency: deployment-dashboard`로 동시 실행을 막습니다. 실패해도 이전 리비전 자동 복구는 없으므로, 필요하면 이전 digest로 다시 `az containerapp update`합니다.
- Single revision 모드이므로 새 리비전이 Ready가 되면 트래픽이 전환됩니다. SSE 연결은 끊기고 브라우저가 자동 재연결합니다.

## 5.3 비밀·키 회전

```mermaid
flowchart LR
    subgraph SAS["Token store SAS (≤7일, 정기)"]
        S1["새 사용자 위임 SAS 발급"] --> S2["ACA 사설 경로에서<br/>임시 Blob PUT/GET/DELETE 확인"] --> S3["KV secret 새 버전"] --> S4["ACA secret 버전 URI 적용"] --> S5["새 로그인 · 조회 확인"]
    end
    subgraph KEY["GitHub App RSA key"]
        K1["GitHub에서 새 키 발급"] --> K2["KV에 새 버전 import"] --> K3["DASHBOARD_SIGNING_KEY를<br/>새 버전 URI로 교체"] --> K4["설치 토큰 발급 확인"] --> K5["GitHub에서 이전 키 폐기"]
    end
    subgraph WH["Webhook secret"]
        W1["새 값 생성 (≥32바이트)"] --> W2["KV secret 새 버전 + Receiver 재시작"] --> W3["GitHub App secret 교체"] --> W4["Recent Deliveries에서 204 확인<br/>(교체 사이 실패분은 Redeliver)"]
    end
    subgraph EC["Entra client secret"]
        E1["새 secret 추가"] --> E2["KV secret 새 버전 → ACA 반영"] --> E3["새 로그인 확인"] --> E4["이전 secret 삭제"]
    end
```

| 대상 | 주기·트리거 | 실패 시 증상 |
| --- | --- | --- |
| Token store SAS | 최대 7일 → 만료 전 | 로그인 후 토큰 저장 실패, `/.auth/refresh` 실패, 로그인 반복 |
| GitHub App RSA key | 조직 정책 / 유출 의심 | 모든 GitHub 호출 503, 화면 GitHub 칩 오류 |
| Webhook secret | 조직 정책 / 유출 의심 | Receiver 403, 승인 대기 미표시 |
| Entra client secret | 만료일 전 | 로그인 실패 (Easy Auth 오류) |

SAS 원문, 키, secret 값은 로그·파일·채팅에 남기지 않습니다.

## 5.4 상태 확인 지점

| 대상 | 확인 | 정상 |
| --- | --- | --- |
| Console 리비전 | `az containerapp revision list` | Healthy, Running |
| Receiver | `GET https://<receiver>/healthz` | `{"ok": true}` |
| Receiver 수신 | GitHub App → Advanced → Recent Deliveries | 204 |
| Easy Auth | 로그인 후 `/.auth/me`, `/.auth/refresh` | 200, ID token 만료 시각 갱신 |
| 화면 칩 | GitHub · 운영 이력 · Azure 지역 · 레플리카 표본 · 변경 알림 | "n초 전", 강조 없음 |
| 쓰기 잠금 | `dashboardops`의 `lock:write` 행 | 평소 없음 |

## 장애 진단

```mermaid
flowchart TD
    S["증상"] --> Q1{"콘솔에 접속이 안 됨?"}
    Q1 -- "AADSTS50105" --> F1["Enterprise App 사용자 할당"]
    Q1 -- "'접근이 제한되었습니다' 403" --> F2["ReleaseOperator/Approver 역할 할당"]
    Q1 -- "로그인 반복 / Easy Auth 오류" --> F3["Token store SAS 만료 · Entra client secret 만료<br/>· redirect URI · 사설 Blob 경로"]
    Q1 -- "리비전 Unhealthy" --> F4["환경 변수 형식(config.py) · KV 참조 권한<br/>· AcrPull · 이미지 digest"]
    Q1 -- "접속은 됨" --> Q2{"승인 대기가 안 보임?"}
    Q2 -- 예 --> Q2a{"Recent Deliveries에<br/>deployment_protection_rule?"}
    Q2a -- 없음 --> F5["App 이벤트 구독 · Environment에 App 규칙 활성화<br/>· main 브랜치 · workflow_dispatch"]
    Q2a -- "403" --> F6["webhook secret 불일치 · installation/repo ID<br/>· 허용 외 환경"]
    Q2a -- "204" --> F7["관제실에서 시작한 run인지 (CLI 실행은 승인 불가)<br/>· job이 waiting/pending인지 · Inbox 읽기 권한"]
    Q2 -- 아니요 --> Q3{"쓰기 요청 실패?"}
    Q3 -- "409" --> F8["다른 결정 존재 또는 lock:write 미해결<br/>→ op 행 상태 확인"]
    Q3 -- "422" --> F9["조건 불충족: 진행 중 workflow · 증거 만료<br/>· 트래픽 상태 · Re-run 게이트"]
    Q3 -- "403" --> F10["역할 · 자기 승인 · Origin(접속 URL)"]
    Q3 -- "503" --> F11["GitHub/Azure 조회 실패: KV sign 권한 · App 키<br/>· ARM Reader · egress 경로<br/>※ 재시도 전 GitHub에서 결과 확인"]
    Q3 -- 아니요 --> Q4{"화면 갱신이 느림 /<br/>변경 알림 칩 오래됨?"}
    Q4 -- 예 --> F12["App 이벤트 Workflow runs · Workflow jobs 구독<br/>· Receiver 수신 상태 (폴링은 계속 동작)"]
    Q4 -- 아니요 --> Q5{"op가 confirmation_required<br/>또는 reserved에 머묾?"}
    Q5 -- 예 --> F13["GitHub run/결정 이력으로 결과 입증 후<br/>op · run · claim · lock을 한 트랜잭션으로 정정<br/>(7장 문제 해결 표 참고)"]
```

| 증상 | 1차 확인 | 근거 |
| --- | --- | --- |
| 레플리카 표본이 비어 있음 | 대상 앱이 internal ingress이거나 Console egress 차단 | `observations.py` (리비전 FQDN 조회) |
| Azure 지역 칩 오류 | Console UAMI의 대상 ACA `Reader`, `management.azure.com` egress | `observations.py` |
| 롤백 버튼 비활성 "baseline unavailable" | artifact 만료, 원본 run 불일치 | `evidence.py` |
| 승인에 "Re-run blocked — reject only" | 누군가 GitHub에서 Re-run 함 → 거부 후 재개 사용 | [4.5](04-operation-flows.md#45-github-re-run-거부-흐름) |

관제실 장애·보호 규칙 이상 시에는 기존 안전장치를 삭제하지 말고 변경 요청을 멈춘 뒤, 미리 승인된 별도 승인 경로와 기존 runbook([4장 운영 runbook](../04-operations-runbook.md))을 따릅니다.

이전: [4. 작업 흐름](04-operation-flows.md) · 처음: [README](README.md)
