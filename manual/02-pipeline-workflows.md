# 2장. 파이프라인 Workflow 구현

## 2.1 Workflow 관계도

```text
release.yml (workflow_dispatch, profile)
 ├─ build        → build.yml  environment: build      (version_prefix r)
 ├─ korea        → region.yml environment: prod-kr
 ├─ southeastasia→ region.yml environment: prod-sea   (korea 성공 시)
 └─ westus       → region.yml environment: prod-usw   (sea 성공 시)

release-kr.yml (workflow_dispatch, profile=demo 기본) – 빠른 무중단 검증용
 ├─ build        → build.yml  environment: build      (version_prefix k)
 └─ korea        → region.yml environment: prod-kr

restore.yml (workflow_dispatch, source_run_id, region)
 ├─ westus        → restore-region.yml environment: prod-rollback-usw
 ├─ southeastasia → restore-region.yml environment: prod-rollback-sea (always)
 └─ korea         → restore-region.yml environment: prod-rollback-kr  (always)

oidc-inspect.yml (workflow_dispatch) – 7개 environment matrix, Azure 접근 없음

demo-retire.yml (workflow_dispatch, source_run_id, region) – 데모 전용
 └─ retire        environment: prod-<region>  (release 완료 후, 이전 revision 비활성화)
```

## 2.2 `release.yml` – 릴리스 진입점

| 항목 | 값 |
| --- | --- |
| 트리거 | `workflow_dispatch`, 입력 `profile` = `production`(기본) / `demo` |
| 동시성 | `safe-aca-production`, `cancel-in-progress: false` |
| 브랜치 제한 | `github.ref == refs/heads/main` 에서만 build 실행 |

**build job** → 재사용 workflow `build.yml` (environment `build`, 30분 제한).
`release.yml` 과 `release-kr.yml` 이 같은 build를 씁니다.

1. `run_attempt == 1` 확인 (재실행 거부)
2. `python -m unittest discover -s tests -v`, `python -m compileall`
3. `azure/login` (OIDC, SHA 고정 action)
4. `docker build --platform linux/amd64 --build-arg APP_VERSION=<version_prefix><run 번호>` → ACR push (`safe-demo:run-<run_id>`)
   (`APP_VERSION` 은 `/verify` 화면의 버전 라벨, [5장](05-zero-downtime-demo.md). `release.yml` = `r<N>`, `release-kr.yml` = `k<N>`;
   run 번호는 workflow마다 따로 세므로 접두어로 구분)
5. digest 조회 후 `<acr>.azurecr.io/safe-demo@sha256:...` 형태로 `release.json` 생성
6. `release-<run_id>` artifact 업로드 (90일 보존)

**지역 job**: `region.yml` 을 `region`/`environment`/`profile` 입력으로 호출합니다.
앞 지역 결과가 `success` 일 때만 다음 지역이 실행됩니다.

## 2.2.1 `release-kr.yml` – 빌드 + KR만 (빠른 무중단 검증)

| 항목 | 값 |
| --- | --- |
| 용도 | 데모/검증을 빠르게 반복. `release.yml` 과 같은 build·승인·canary·health gate·evidence, KR 이후 중단 |
| 트리거 | `workflow_dispatch`, `profile` = `demo`(기본) / `production` |
| 동시성 | `safe-aca-production` (release/restore/retire와 같은 lock) |
| 승인 | `prod-kr` 1회 |
| 버전 라벨 | `k<run 번호>` (예: `k1`, `k2`) |

```powershell
gh workflow run release-kr.yml --repo <repo> --ref main     # profile=demo
```

주의:

- SEA/USW는 바뀌지 않으므로 지역 간 버전이 달라질 수 있습니다. 운영 상태를 맞추려면 `release.yml` 을 실행합니다.
- `demo-retire.yml`, `restore.yml` 은 이 run ID를 `source_run_id` 로 받을 수 있습니다(`check_source.py` 허용 목록).
  baseline이 KR에만 있으므로 restore는 `region=kr` 로 실행합니다(`all` 이면 usw/sea는 snapshot 없음으로 안전 실패 후 kr 수행).

## 2.3 `region.yml` – 지역 1곳 배포

| 단계 | 내용 | 실패 시 영향 |
| --- | --- | --- |
| checkout / setup-python 3.13 | | |
| download `release-<run_id>` | 같은 run의 `release.json` 획득 | Azure 변경 없음 |
| azure/login | 지역 environment의 OIDC identity | Azure 변경 없음 |
| Verify CLI | `log-analytics` extension 설치 확인 | Azure 변경 없음 |
| **Capture stable state** | `python scripts/snapshot.py` (읽기 전용) | Azure 변경 없음 |
| **Persist baseline** | `baseline-<run_id>-<region>` artifact 업로드 | 업로드 실패 시 rollout 미실행 |
| **Progressive rollout** | `python scripts/rollout.py deploy --profile ... --snapshot state/baseline-<region>.json` | 내부에서 자동 복구 시도 |
| Best-effort cleanup | rollout 실패/취소 + `mutation-intent.json` 존재 시 `rollout.py restore` | 8분 제한 |
| Preserve evidence | `evidence-<run_id>-<region>-<attempt>` artifact 업로드 (`always()`) | |

job 제한 시간은 240분입니다(production 3단계 × 1시간 + 대기 여유).

필요한 environment variables (GitHub Environment `vars`):
`AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`RESOURCE_GROUP`, `CONTAINER_APP`, `LOG_ANALYTICS_WORKSPACE_ID`(workspace customer GUID).

## 2.4 `restore.yml` / `restore-region.yml` – 보호된 수동 복구

| 입력 | 설명 |
| --- | --- |
| `source_run_id` | **원본 release run ID** (restore run ID 아님) |
| `region` | `all`(usw → sea → kr), `kr`, `sea`, `usw` |

`restore-region.yml` 단계:

1. `python scripts/check_source.py` – 원본 run이 이 저장소의 `release.yml` 또는 `release-kr.yml`,
   `workflow_dispatch`, `main`, `completed` 인지 GitHub API로 검증하고 `sha` 출력
2. 원본 run의 `baseline-<source_run_id>-<region>` artifact 다운로드
3. snapshot의 `commit`/`run_id` 가 검증된 원본과 일치하는지 확인
4. `azure/login` – **rollback 전용** identity (`prod-rollback-<region>`)
5. `python scripts/rollout.py restore --snapshot restore-input/baseline-<region>.json`
6. `restore-evidence-*` artifact 업로드

`all` 모드에서는 앞 지역이 실패해도 `always()` 로 다음 지역을 시도합니다.
배포가 시작되지 않은 지역은 baseline artifact가 없어 다운로드 단계에서 안전하게 실패합니다.

## 2.5 `oidc-inspect.yml` – OIDC claim 수집

7개 environment(`build`, `prod-*`, `prod-rollback-*`)에서 실제 OIDC 토큰을 요청하여
허용된 claim metadata만 `oidc-claims-<run_id>-<env>` artifact(7일)로 저장합니다.
Azure 로그인은 하지 않습니다. 이 결과가 `configure-identity` 의 입력입니다.

## 2.5.1 `demo-retire.yml` – 데모 전용 이전 revision 비활성화

무중단 데모에서 이전 revision이 실제 SIGTERM을 받도록 하는 수동 workflow입니다
(운영 release는 즉시 복구를 위해 이전 revision을 활성 상태로 유지). 사용 시나리오는
[5장](05-zero-downtime-demo.md)을 참고하세요.

| 항목 | 값 |
| --- | --- |
| 입력 | `source_run_id`(완료된 release run), `region` = `kr` / `sea` / `usw` (한 지역만, `all` 없음) |
| 동시성 | `safe-aca-production`, `cancel-in-progress: false` (release/restore와 직렬) |
| environment | `prod-<region>` (배포 identity 재사용, 승인 필요) |
| 제한 | `main` 에서만, 15분 |

단계: `check_source.py` → 원본 run의 `baseline-<run>-<region>` 과
`evidence-<run>-<region>-1` 다운로드 → snapshot의 commit/run 바인딩 확인 → `azure/login` →
`python scripts/retire.py --snapshot ... --success .../success.json` →
`demo-retire-evidence-<run>-<region>` 업로드.

> retire 후 그 release에 대한 `restore.yml` 은 `Stable unavailable` 로 거부됩니다.
> 복구 절차는 [5.10](05-zero-downtime-demo.md#510-retire-이후-복구).

## 2.6 Artifact 일람

| Artifact | 생성 위치 | 내용 | 보존 |
| --- | --- | --- | --- |
| `release-<run>` | build | `release.json` (image digest, run, repo, commit) | 90일 |
| `baseline-<run>-<region>` | region | `baseline-<region>.json` – 복구의 유일한 근거 | 90일 |
| `evidence-<run>-<region>-<attempt>` | region | `evidence\<region>\*.json` | 90일 |
| `restore-evidence-<run>-<region>-<attempt>` | restore-region | `manual-rollback.json` 등 | 90일 |
| `demo-retire-evidence-<run>-<region>` | demo-retire | `demo-retire.json` | 90일 |
| `oidc-claims-<run>-<env>` | oidc-inspect | `claims-<env>.json` | 7일 |

## 2.7 Evidence 파일 일람 (`evidence\<region>\`)

| 파일 | 생성 시점 | 의미 |
| --- | --- | --- |
| `baseline-preflight.json` | 변경 전 preflight | stable 직접 확인, production은 organic baseline SLO |
| `mutation-intent.json` | candidate 생성 직전 | 이후 실패 시 cleanup이 복구를 수행해도 된다는 표식 |
| `gate-<weight>-<epoch>.json` | 각 window 평가 | KQL, 결과 행, `attempts`, `status`(waiting/passed/failed) |
| `demo-load-<weight>.json` | demo 각 단계 종료 | 합성 부하 통계(schema v2) |
| `success.json` | 100% 단계 통과 | 최종 revision, image |
| `failure.json` | 배포 실패 | 예외 종류와 메시지 |
| `automatic-rollback.json` | 자동 복구 성공 | 복원 트래픽, 검증 여부 |
| `manual-rollback.json` | restore workflow 성공 | 동일 |
| `demo-retire.json` | demo-retire 성공 | 비활성화한 이전 revision, candidate, 시각, `verified` |
