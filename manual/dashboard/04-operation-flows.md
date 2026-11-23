# 4. 작업 흐름 (요청 · 승인 · 변경 알림)

이 장은 관제실의 핵심 흐름을 시퀀스로 설명합니다. 공통 원칙은 다음과 같습니다.

- **Console은 요청과 결정만 GitHub에 전달**하고, Azure 변경은 GitHub Actions workflow가 OIDC로 수행합니다.
- **쓰기는 한 번에 하나**(`lock:write`)만 진행되며, 같은 결정은 `claim:` 행으로 한 번만 허용됩니다.
- 쓰기 직전에는 **캐시를 쓰지 않고** GitHub·artifact 증거·Azure를 다시 읽어 조건을 확인합니다.
- 웹훅은 **신호**일 뿐 권한 근거가 아닙니다. 승인 가능 여부는 서명된 보호 규칙 행 + GitHub job 상태 + 관제실 작업 기록을 함께 확인해 판단합니다.

## 4.1 배포 요청 → 지역 승인 (end-to-end)

```mermaid
sequenceDiagram
    autonumber
    actor OP as Operator
    actor AP as Approver (다른 사람)
    participant C as Console
    participant OPS as Table dashboardops
    participant GH as GitHub API / Actions
    participant R as Receiver
    participant INB as Table dashboardinbox
    participant AZ as 대상 ACA (kr)

    rect rgb(235, 245, 255)
    Note over OP,GH: ① 배포 요청
    OP->>C: POST /api/operations {action: deploy, profile, request_id}
    C->>C: 인증·Origin·ReleaseOperator 확인
    C->>OPS: 트랜잭션: op:id(reserved) + lock:write + recent:
    C->>GH: 진행 중인 보호 workflow 없는지 확인 (캐시 미사용)
    C->>GH: ② POST workflows/release.yml/dispatches (ref=main)
    GH-->>C: workflow_run_id
    C->>OPS: 트랜잭션: op:id(accepted, run_id) + run:id 색인 + lock 해제
    C-->>OP: operation_id, run_id
    end

    rect rgb(255, 248, 230)
    Note over GH,INB: ③④ 보호 규칙 요청 수신
    GH->>GH: build job 완료 → prod-kr job 대기
    GH->>R: POST /webhooks/github (deployment_protection_rule, HMAC 서명)
    R->>R: 서명·크기·installation·repo·main·workflow_dispatch·환경 검사
    R->>INB: insert run:env:delivery (environment, sha)
    R-->>GH: 204
    end

    rect rgb(235, 255, 240)
    Note over AP,AZ: ⑤⑥ 승인과 배포
    AP->>C: 화면 조회 (승인 대기 탭)
    C->>INB: 보호 규칙 행 읽기
    C->>GH: run · jobs 상태 (해당 지역 job이 waiting/pending?)
    C-->>AP: 승인 대기 표시
    AP->>C: POST /api/operations/{op}/review {environment: prod-kr, approved: true}
    C->>C: ReleaseApprover + 요청자와 다른 사용자 확인
    C->>GH: gate 재확인 (run attempt, sha 일치, job 대기)
    C->>OPS: claim 없음 확인 → op:new(reserved, claim) + lock:write
    C->>GH: POST runs/{run}/deployment_protection_rule (approved)
    GH-->>C: 204
    C->>OPS: op:new(accepted) + claim:review:run:prod-kr + lock 해제
    GH->>AZ: ⑦ prod-kr job: OIDC 로그인 → snapshot → 카나리 0→10→50→100% + health gate
    end

    Note over GH,AZ: prod-sea, prod-usw도 ③~⑦ 반복 (지역마다 별도 승인)
```

| 단계 | 실패 시 결과 | 운영 포인트 |
| --- | --- | --- |
| 3 | `lock:write`가 이미 있으면 409 | 미해결 쓰기가 남아 있는지 확인 |
| 4 | 다른 보호 workflow 진행 중이면 422, op는 `blocked` | 동시 배포 방지 |
| 5–6 | GitHub 4xx → `rejected`, 그 외 오류 → `confirmation_required`(잠금 유지) | **자동 재시도하지 않음** |
| 10–11 | 서명 실패·다른 저장소·허용되지 않은 환경 → 403, Inbox에 기록하지 않음 | GitHub App의 Recent Deliveries 확인. 자동 재전송 없음 → 필요 시 수동 Redeliver |
| 19 | 요청자 본인 → 403 | 다른 Approver 필요 |
| 20 | 더 이상 대기 아님 → 422 | 이미 결정·취소되었거나 job이 진행 중 |
| 21 | 같은 결정 존재 → 409 | 중복 클릭·다중 승인자 경합 차단 |

## 4.2 작업 상태 (op 행)

```mermaid
stateDiagram-v2
    [*] --> reserved: reserve (op + lock:write + recent)
    reserved --> blocked: 사전 확인 실패 (claim 존재, 조건 불충족)
    reserved --> accepted: GitHub 2xx
    reserved --> rejected: GitHub 400/401/403/404/409/422
    reserved --> confirmation_required: 기타 오류·타임아웃 (전송 여부 불명)
    blocked --> [*]: lock 해제
    accepted --> [*]: lock 해제 (+ claim, run 색인)
    rejected --> [*]: lock 해제
    confirmation_required --> confirmation_required: lock 유지 → 수동 확인 필요
```

- `accepted`는 "GitHub가 요청을 접수했다"는 뜻이지 **배포 성공이 아닙니다**. 결과는 GitHub 실행과 Azure 트래픽으로 확인합니다.
- `confirmation_required`나 `reserved`에 머무르면 새 쓰기가 모두 막힙니다. 정정 절차는 [7장 문제 해결](../07-deployment-dashboard.md#운영-중-문제-해결)을 따릅니다.

## 4.3 작업 종류별 매핑

| 작업 (UI) | 역할 | 호출 | 입력 | 게이트 환경 | 사전 확인 (쓰기 시점) |
| --- | --- | --- | --- | --- | --- |
| 새 배포 | Operator | `release.yml` dispatch | `profile` (demo/production) | `prod-kr` → `prod-sea` → `prod-usw` | 진행 중인 보호 workflow 없음 |
| 실행 취소 | Operator | `POST runs/{id}/cancel` | `source_run_id` | — | 원본이 main `release.yml`이고 미완료, `claim:cancel:<run>` 없음 |
| 롤백 요청 | Operator | `restore.yml` dispatch | `source_run_id`, `region` | `prod-rollback-<region>` | 원본 완료, baseline artifact 검증, Azure 현재 트래픽 대조 |
| 이전 리비전 종료 (Retire) | Operator | `demo-retire.yml` dispatch | `source_run_id`, `region` | `retire-<region>` (**GitHub에서 승인**) | 원본 완료, 증거·트래픽 대조, demo 프로필만 |
| 실패 지역부터 재개 | Operator | `release.yml` dispatch | `profile`, `resume_from_run_id`, `start_region` (**서버가 결정**) | 시작 지역부터 `prod-*` | 원본 실패/취소, 앞 지역 성공, 시작 지역 트래픽이 정상 리비전 하나에 100% |
| 승인 / 거부 | Approver | `POST runs/{id}/deployment_protection_rule` | `environment`, `state` | — | 요청자와 다른 사람, 대기 중, 같은 sha, 같은 attempt, claim 없음 |

## 4.4 실패 지역부터 재개

```mermaid
sequenceDiagram
    autonumber
    actor OP as Operator
    participant C as Console
    participant GH as GitHub
    participant ART as Artifact (baseline/evidence)
    participant AZ as 대상 ACA

    OP->>C: POST /api/operations {action: resume, source_run_id}
    C->>GH: 원본 run, 최근 attempt의 jobs (캐시 미사용)
    C->>ART: baseline 증거 (fresh)
    C->>AZ: 시작 후보 지역의 현재 트래픽·health (ARM)
    C->>C: 시작 지역 = KR→SEA→USW 중 처음 성공하지 않은 지역<br/>profile = 원본 운영 이력 값 (없으면 production)
    C->>GH: release.yml dispatch {profile, resume_from_run_id, start_region}
    GH-->>C: 새 run_id
    GH->>GH: promote job (build 환경): 원본 검증 + 같은 이미지 digest 재게시
    GH->>C: (보호 규칙 webhook → Receiver → Inbox) 시작 지역부터 승인 대기
    Note over GH,AZ: 이후 4.1의 ③~⑦과 동일. 재빌드 없음
```

## 4.5 GitHub Re-run 거부 흐름

GitHub UI에서 Re-run하면 같은 run ID의 attempt ≥ 2가 생깁니다. 배포 job은 attempt 1만 허용하므로 관제실은 이 게이트를 **거부만** 할 수 있게 합니다.

```mermaid
flowchart TD
    A["보호 규칙 요청 수신<br/>(같은 run, attempt N)"] --> B{"attempt > 1 ?"}
    B -- 아니요 --> N["일반 승인 대기"]
    B -- 예 --> C{"Inbox 행 수신 시각 ≥<br/>이번 attempt 시작 시각?"}
    C -- 아니요 --> X["표시 안 함<br/>(이전 attempt의 요청)"]
    C -- 예 --> D["'Re-run blocked — reject only'<br/>재실행 · 시도 N 배지"]
    D --> E{"Approver 결정"}
    E -- 승인 --> F["422 거부"]
    E -- 거부 --> G["claim:review:run:env:N 생성<br/>GitHub 해당 attempt 종료"]
    G --> H["남은 지역은 원본에서<br/>'실패 지역부터 재개' 또는 롤백"]
```

## 4.6 실시간 변경 알림 (`workflow_run` / `workflow_job`)

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant R as Receiver
    participant INB as Table dashboardinbox
    participant C as Console (ChangeFeed)
    participant BR as 브라우저

    BR->>C: GET /api/changes (SSE, 연결 수명 300초)
    GH->>R: workflow_run / workflow_job webhook (서명)
    R->>R: 서명·설치·저장소 검사<br/>main + 허용 workflow만 기록
    R->>INB: insert chg:UTC:delivery → chgid:delivery
    R-->>GH: 204 (허용 외 이벤트도 204, 기록 안 함)
    loop 2초 간격
        C->>INB: 최근 30초 겹침 구간의 chg: 행 읽기
        C->>C: 새 힌트 → 해당 run의 GitHub 읽기 캐시 무효화
        C-->>BR: event: change {run_id}
    end
    BR->>C: GET /api/dashboard (다시 조회)
```

- 허용 workflow: `release.yml`, `release-kr.yml`, `restore.yml`, `demo-retire.yml` (job 이벤트는 workflow 표시 이름으로 판단).
- 웹훅이 없어도 화면은 폴링(진행 중 약 5초, 유휴 30–60초)으로 갱신됩니다. 진행 중인데 15분 넘게 힌트가 없으면 **변경 알림** 칩이 오래됨으로 표시됩니다.
- 화면 조회용 GitHub 캐시: 진행 중 run 약 5초, 완료 run 약 5분(ETag 조건부 재조회). 쓰기 요청 직후 해당 run 캐시는 즉시 무효화됩니다.

## 4.7 증거(artifact) 조회

롤백·Retire·재개 가능 여부는 workflow가 남긴 artifact로 판단합니다.

```mermaid
sequenceDiagram
    autonumber
    participant C as Console
    participant GH as api.github.com
    participant DL as artifact 저장소<br/>(*.blob.core.windows.net /<br/>*.actions.githubusercontent.com)

    C->>GH: GET runs/{id}/artifacts (만료 여부 매번 확인)
    GH-->>C: baseline-run-region, restore-evidence-..., demo-retire-evidence-...
    C->>GH: GET artifacts/{id}/zip
    GH-->>C: 302 Location
    C->>C: 리다이렉트 호스트 허용 목록 검사
    C->>DL: zip 다운로드 (크기·항목 수 제한)
    DL-->>C: zip
    C->>C: 지정된 JSON 한 개만 파싱 → run·repo·region·commit·app ID·candidate 대조
```

- 화면 조회에서는 파싱 결과를 캐시하지만, **쓰기 시점에는 항상 새로 읽습니다**(`fresh=True`).
- artifact가 만료되면 롤백·재개 버튼이 사유와 함께 비활성화됩니다.

이전: [3. 인증 흐름](03-auth-flows.md) · 다음: [5. 배포와 운영](05-deployment-and-ops.md)
