# 1장. SDP 설계와 아키텍처

## 1.1 SDP(Safe Deployment Practices) 원칙과 구현 매핑

SDP는 "변경은 점진적으로, 관찰 가능하게, 언제든 되돌릴 수 있게" 배포한다는
원칙입니다. 이 파이프라인은 다음과 같이 각 원칙을 구현합니다.

| SDP 원칙 | 구현 방식 | 관련 파일 |
| --- | --- | --- |
| 한 번 빌드, 여러 번 배포 | 이미지를 build job에서 1회만 빌드하고 `sha256` digest로 고정 | `release.yml`, `common.image_valid` |
| 점진적 노출(Progressive exposure) | 지역 순차(kr → sea → usw) + 지역 내 카나리(10% → 50% → 100%) | `release.yml`, `rollout.deploy` |
| 사람의 승인 게이트 | 지역별 GitHub Environment required reviewer | `region.yml` `environment:` |
| 관찰 기반 승격(Bake + health gate) | 단계별 bake 시간 동안 Log Analytics SLI 평가 | `rollout.bake`, `metrics.gate` |
| Fail-closed | 텔레메트리 누락/조회 실패/모호성은 모두 실패 처리 | `metrics.metric`, `common.require` |
| 빠른 복구 | revision 삭제 없이 트래픽만 baseline으로 되돌림 | `rollout.restore` |
| 복구 가능성 사전 보장 | 변경 전에 baseline snapshot을 artifact로 영구 저장 | `snapshot.py`, `region.yml` |
| 폭발 반경 제한 | 실패 지역만 복구, 이미 건강한 선행 지역은 유지 | `release.yml` `needs/if` |
| 최소 권한 / 비밀 없는 인증 | 환경별 분리 OIDC identity, client secret 없음 | `configure-identity.*`, `inspect_oidc.py` |
| 감사 가능성(Evidence) | gate 쿼리·결과·실패·복구 기록을 artifact로 90일 보존 | `evidence\<region>\*.json` |

## 1.2 Azure 리소스 아키텍처

```text
 Subscription
 ├── <prefix>-shared  (koreacentral)
 │     ├── ACR (Basic, admin 비활성)                ← build identity: AcrPush, Reader
 │     └── User-assigned MI "<acr>-pull"           ← ACR scope AcrPull
 ├── <prefix>-kr      (koreacentral)
 │     ├── Log Analytics workspace "<app>-logs"    ← prod-kr identity: Log Analytics Reader
 │     ├── ACA managed environment "<app>-env" (Consumption)
 │     └── Container App "<prefix>-kr-app"         ← prod-kr identity: Container Apps Contributor
 ├── <prefix>-sea     (southeastasia)  … 동일 구성
 └── <prefix>-usw     (westus)         … 동일 구성
```

핵심 설정(`infra\region.bicep`):

- `activeRevisionsMode: Multiple` – stable과 candidate revision을 동시에 활성화하여 트래픽 분할
- 트래픽은 항상 **revision 이름 기반의 명시적 weight** (`latestRevision` 사용 금지)
- `minReplicas: 1` – stable이 0으로 scale-in되어 복구 대상이 사라지는 것을 방지
- `maxInactiveRevisions: 20` – 과거 revision을 포렌식/복구용으로 보존
- 단일 컨테이너 `web`, 0.25 CPU / 0.5Gi, probe는 `infra\health-probes.json` 공유

## 1.3 Revision / 트래픽 모델

| 용어 | 의미 | 예 |
| --- | --- | --- |
| stable (LKG) | 배포 시작 시 100% 트래픽을 받는 revision | `safeaca-kr-app--bootstrap` |
| candidate | 이번 release run이 생성하는 revision. 이름이 run ID에 묶임 | `safeaca-kr-app--r1234567890` |
| baseline | 배포 직전의 app 상태(트래픽, template, invariant hash 등) snapshot | `state\baseline-kr.json` |

candidate 이름이 `<app>--r<GITHUB_RUN_ID>` 로 고정되므로 **어떤 run이 어떤 revision을
만들었는지(소유권)** 를 항상 검증할 수 있습니다. 같은 이름의 revision이 이미 있으면
재실행으로 간주하고 중단합니다.

트래픽 전이:

```text
stable 100            (candidate 생성, 0%)
stable 90 / cand 10   → bake + gate
stable 50 / cand 50   → bake + gate
stable  0 / cand 100  → bake + gate (stable 기준값은 50% 단계에서 고정)
```

## 1.4 안전 장치(Guard) 설계

모든 쓰기 작업 전후로 `common.guarded_state()`가 다음을 검증합니다. 하나라도
어긋나면 **다른 사람/다른 릴리스의 변경(drift)** 으로 간주하고 덮어쓰지 않고 중단합니다.

1. snapshot의 schema, app ID, repository, region, run ID, candidate 이름, 이미지 digest 일치
2. app-scoped 설정(ingress 등, traffic 제외)·identity·environment·location의 hash(`invariant`) 일치
3. stable revision template hash 일치 및 stable이 Healthy
4. 트래픽에 stable/candidate 외 revision이 없음, 기대 weight와 일치
5. 현재 app template이 "배포 전 template" 또는 "기대 candidate template" 중 하나

추가 원칙:

- **재실행 금지**: `GITHUB_RUN_ATTEMPT != 1` 이면 즉시 중단. 실패 후에는 restore 또는 새 run.
- **동시성 직렬화**: release와 restore가 같은 concurrency group(`safe-aca-production`) 사용.
- **Mutation intent 기록**: 실제 Azure 변경 직전에 `mutation-intent.json` 기록.
  이 파일이 없으면 cleanup 단계는 Azure를 건드리지 않습니다.
- **비파괴 복구**: 복구 시 revision을 비활성화/삭제하지 않고 트래픽만 되돌립니다.

## 1.5 Health gate 설계

앱의 `/work` 요청마다 아래 JSON business log를 stdout에 출력하고, ACA가 이를
`ContainerAppConsoleLogs_CL` 테이블로 수집합니다.

```json
{"kind":"business","event_time":"...","request_id":"...","revision":"...",
 "release":"...","monitor_run":"<run_id>","source":"organic|demo|control|probe",
 "status":200,"business_ok":true,"duration_ms":15.2}
```

`source` 구분:

| source | 발생 주체 | gate 사용 |
| --- | --- | --- |
| `organic` | 실제 사용자 트래픽(헤더 없음) | production 프로필 판정 |
| `demo` | `demo_load.py` 합성 부하(공유 FQDN) | demo 프로필 판정 |
| `control` | `rollout.bake` 가 revision FQDN에 직접 보내는 5초 주기 요청 | 양 프로필에서 revision별 생존 확인 |
| `probe` | readiness/preflight/restore 확인 요청 | 판정 제외 |

프로필별 파라미터(`scripts\metrics.py` `PROFILES`):

| 항목 | production | demo |
| --- | --- | --- |
| 단계별 bake | 3600초 | 180초 |
| 평가 window | 300초 | 60초 |
| 수집 지연(lag) | 120초 | 120초 |
| candidate/stable 최소 표본 | 100 | 20 |
| revision별 최소 control 표본 | 30 | 5 |

판정 기준(모두 충족해야 통과):

- 오류율(5xx, 2xx 외, business 실패) ≤ 1%
- p95 ≤ 500ms
- candidate 오류율 ≤ stable 오류율 + 0.5%p
- candidate p95 ≤ stable p95 × 1.5 + 50ms
- 표본 부족/누락은 30초 간격 재조회, window 종료 후 lag + 300초까지만 대기 후 실패

> 이 수치는 예제입니다. 실서비스 SLO/오류 예산에 맞게 `metrics.py`를 조정하세요.
