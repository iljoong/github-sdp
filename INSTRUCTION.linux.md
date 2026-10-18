# 안전 배포 예제 실행 가이드 (Linux Bash)

이 문서는 Azure Container Apps 안전 배포 예제의 Linux 전용 명령을 설명합니다.
배포 동작, health gate 기준, 복구 제약 및 운영상 주의 사항은
[PowerShell 가이드](INSTRUCTION.md)와 동일합니다.

배포 순서는 Korea Central (`kr`, `koreacentral`) → Southeast Asia
(`sea`, `southeastasia`) → West US (`usw`, `westus`)입니다.

## 1. 사전 요구 사항

- Bash 4+
- Python 3.13
- Bicep을 포함한 Azure CLI
- Linux container engine이 있는 Docker
- Git, GitHub CLI (`gh`), `jq`

최초 Azure 설치자는 구독에서 Contributor와 Role Based Access Control
Administrator(또는 Owner), 그리고 Entra 애플리케이션 및 서비스 주체 생성 권한이
필요합니다. provisioning script 실행 전에 Azure와 GitHub CLI에 로그인하세요.

최초 통합 테스트에는 비운영 구독을 사용하세요. 아래 명령은 실제 Azure 리소스를
생성하며 비용이 발생할 수 있습니다.

## 2. 로컬 검증

저장소 루트에서 다음 명령을 실행합니다.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app scripts tests
az bicep build --file infra/shared.bicep --stdout
az bicep build --file infra/region.bicep --stdout
```

앱을 시작하고 다른 터미널에서 endpoint를 확인합니다.

```bash
python3 app/server.py
```

```bash
curl --fail http://localhost:8080/healthz
curl --fail http://localhost:8080/work
```

로컬 장애 주입을 시험하려면 서버 실행 전에 같은 셸에서 환경 변수를 설정합니다.

```bash
export DEMO_FAILURE_PERCENT=100 # /work returns 503
export DEMO_LATENCY_MS=600      # Inject /work latency in milliseconds
python3 app/server.py
```

컨테이너를 로컬에서 빌드하고 실행합니다.

```bash
docker build --platform linux/amd64 -t safe-demo:local app
docker run --rm -p 8080:8080 safe-demo:local
```

## 3. 최초 Azure bootstrap

> Bootstrap은 최초 생성 전용입니다. 라이브 앱에 재실행하거나 릴리스, 복구,
> 업그레이드 수단으로 사용하지 마세요.

로그인하고 배포 변수를 설정합니다.

```bash
az login
gh auth login

subscription='YOUR-SUBSCRIPTION-GUID'
repo='OWNER/REPOSITORY'
acr='globally-unique-lowercase-name'
prefix='safeaca'

az account set --subscription "$subscription"
az bicep version
docker info
```

Bootstrap requires a reachable Docker daemon for ACR login and image build/push.
Verify both the client and engine before running it:

```bash
docker version
docker info
```

If either command cannot connect to the daemon, start the Docker service (for
example, `sudo systemctl start docker` on a system using systemd) or enable
Docker Desktop's integration with your Linux environment. The bootstrap script
checks the daemon before making Azure changes.

On WSL, make sure `az` is the Linux build, not the Windows one:

```bash
command -v az   # must not start with /mnt/c/
```

If it points to `/mnt/c/...`, `az acr login` fails with `DOCKER_COMMAND_ERROR`
because the Windows CLI cannot reach the Linux Docker engine. Install Azure CLI
inside the Linux distribution
([instructions](https://learn.microsoft.com/cli/azure/install-azure-cli-linux)),
run `hash -r`, and run `az login` again, since the Linux CLI keeps its own login
state.

설치자는 새 ACR에 push할 수 있어야 합니다. RBAC registry를 사용하는 경우
필요하다면 registry scope에서 설치자에게 `AcrPush`를 부여합니다.

스크립트는 사전 확인, provider 등록, 이미지 build/push, 60초 RBAC 대기,
지역별 배포 및 결과 저장 단계에 타임스탬프가 있는 진행 메시지를 stderr로 출력합니다.
Azure 배포는 몇 분 걸릴 수 있으며, 완료 메시지는 해당 명령이 성공한 뒤 출력됩니다.

```bash
./scripts/bootstrap.sh \
  --subscription-id "$subscription" \
  --acr-name "$acr" \
  --prefix "$prefix"
```

스크립트는 공유 ACR 및 pull identity를 만든 다음 세 지역의 resource group,
workspace, ACA environment 및 앱을 생성합니다. identity 설정에 필요한 출력은
`state/bootstrap-outputs.json`에 기록합니다. `state/` 아래 파일을 커밋하지 마세요.

## 4. GitHub Environment 및 OIDC

저장소 설정에서 다음 일곱 GitHub Environment를 만들고 보호하세요.

```text
build
prod-kr, prod-sea, prod-usw
prod-rollback-kr, prod-rollback-sea, prod-rollback-usw
```

배포 branch를 `main`으로 제한하고 production 및 rollback environment에 required
reviewer를 설정하세요. 가능한 경우 self-review와 관리자 우회를 차단하세요.
신뢰할 수 없는 pull request 코드가 권한 있는 workflow에서 실행되지 않도록 하세요.

workflow 파일을 `main`에 push한 다음 보호된 environment job에서 실제 claim을
검사합니다. bearer JWT 전체를 출력하거나 보관하지 마세요.

```bash
gh workflow run oidc-inspect.yml --repo "$repo" --ref main
gh run list --repo "$repo" --workflow oidc-inspect.yml
inspect_run='RUN-ID-FROM-THE-LIST'
gh run watch "$inspect_run" --repo "$repo"
gh run download "$inspect_run" --repo "$repo" -D state/oidc-claims
```

각 environment 승인 후 claims metadata의 `iss`, `aud`, `sub`, repository ID,
branch 및 environment를 검토합니다.

### build identity 구성

helper는 검사한 claim `sub`를 사용하고, client secret 없이 federated credential을
만들며, 최소 Azure role을 할당하고 GitHub Environment variable을 설정합니다.

```bash
./scripts/configure-identity.sh \
  --repository "$repo" \
  --environment build \
  --subscription-id "$subscription" \
  --acr-name "$acr" \
  --claims-file "state/oidc-claims/oidc-claims-${inspect_run}-build/claims-build.json"
```

### 지역별 배포 및 rollback identity 구성

다음 반복문은 지역마다 배포 및 rollback용 identity를 별도로 구성합니다.

`state/bootstrap-outputs.json`의 지역 키가 `kr`, `sea`, `usw`인지 확인하세요.
기존 `de`/`br` 키나 claims 파일의 이름만 바꾸지 마세요. 새 지역의 실제 리소스와
새 environment에서 검사한 claims 파일이 필요합니다.
이미 `prod-kr` 및 `prod-rollback-kr` 구성이 성공했다면 아래 반복문의 첫 줄을
`for region in sea usw; do`로 바꾸어 나머지 지역만 실행하세요.

```bash
for region in kr sea usw; do
  resource_group=$(jq -er --arg region "$region" '.[$region].RESOURCE_GROUP' state/bootstrap-outputs.json)
  container_app=$(jq -er --arg region "$region" '.[$region].CONTAINER_APP' state/bootstrap-outputs.json)
  workspace_resource_id=$(jq -er --arg region "$region" '.[$region].WORKSPACE_RESOURCE_ID' state/bootstrap-outputs.json)
  workspace_customer_id=$(jq -er --arg region "$region" '.[$region].LOG_ANALYTICS_WORKSPACE_ID' state/bootstrap-outputs.json)

  for environment in "prod-$region" "prod-rollback-$region"; do
    ./scripts/configure-identity.sh \
      --repository "$repo" \
      --environment "$environment" \
      --subscription-id "$subscription" \
      --resource-group "$resource_group" \
      --container-app "$container_app" \
      --workspace-resource-id "$workspace_resource_id" \
      --workspace-customer-id "$workspace_customer_id" \
      --claims-file "state/oidc-claims/oidc-claims-${inspect_run}-${environment}/claims-${environment}.json"
  done
done
```

`state/identity-<environment>.json`이 이미 존재하면 중복 identity 생성을 막기 위해
helper가 중단합니다. 누락된 role assignment나 variable을 복구할 때는 기록된 ID를
사용하세요. 기록을 삭제한 뒤 무작정 재실행하지 마세요.

## 5. 릴리스 및 복구 workflow

### ACA health probes

[공식 문서](https://learn.microsoft.com/en-us/azure/container-apps/health-probes)에
따라 Startup, Readiness, Liveness를 HTTP `GET /healthz` (port 8080)로 명시합니다.
공유 정책은 [`infra/health-probes.json`](infra/health-probes.json)입니다. `/healthz`는
`/work`의 지연/실패 주입이나 business telemetry와 분리됩니다. 현재 외부 의존성이
없는 예제에서는 같은 endpoint를 사용하며, 의존성이 추가되면 readiness와 liveness를
분리해야 합니다.

| 종류 | Initial delay | Period | Timeout | Failure / success threshold |
| --- | --- | --- | --- | --- |
| Startup | 3초 | 15초 | 5초 | 10 / 1 |
| Readiness | 3초 | 5초 | 5초 | 3 / 1 |
| Liveness | 10초 | 10초 | 5초 | 3 / 1 |

2초 timeout을 5초로 늘립니다. Startup의 주기 × 실패 횟수는 기존처럼 약 150초이며
정확한 wall-clock 기한은 아닙니다. Readiness는 트래픽 수신 가능 여부를,
Liveness는 재시작 필요 여부를 판단합니다. 기존 business/SLO gate는 유지합니다.

신규 bootstrap은 Bicep이 같은 JSON 정책을 읽습니다. **기존 앱은 새 release
workflow로 적용**하세요. snapshot에 `candidate_probes`를 고정하고 stable snapshot을
복제한 candidate template에만 적용합니다. `az containerapp update --yaml`은
template-only PATCH를 보내므로 traffic/app-scoped 설정과 stable/LKG revision을
수정하지 않습니다. 후보는 0%에서 readiness/business 확인 후 기존 10/50/100% gate로
승격합니다. 예전 snapshot에 정책 필드가 없으면 원래 probe를 유지하여 과거 복구
artifact와의 호환성을 보존합니다.

**라이브 앱에 bootstrap Bicep을 다시 배포하지 마세요.** 기존 stable의 probe 설정은
바뀌지 않으므로 그 revision에는 2초 timeout 로그가 계속 나타날 수 있습니다.
로그 한 줄로 원인을 확정할 수 없으며 이 변경은 CPU 부족이나 hang을 해결하지
않습니다. 계속 실패하면 해당 revision/replica의 CPU·메모리, restart 및 startup 시간을
조사하세요. 실제 revision 설정을 read-only로 확인할 수 있습니다.

```bash
az containerapp revision show --resource-group <resource-group> --name <app-name> \
  --revision <revision-name> --query 'properties.template.containers[0].probes' -o json
```

### Demo load와 복구

Demo 부하는 shared app endpoint에 초당 10건을 목표로 독립적으로 전송하므로
Azure 조회 및 telemetry 대기 중에도 계속 실행됩니다. worker 최대 16개와
실행/대기 요청 합계 최대 32개로 제한합니다. 포화 시 제출을 멈추고 여유가 생기면
10 RPS pacing으로 재개하며 밀린 요청을 burst로 보내지 않습니다. 연속 포화가 30초에
도달하거나 요청이 실패하면 명시적으로 실패합니다. 회복 시 연속 타이머만 초기화하며
누적 포화 시간과 반복 횟수는 유지합니다.

Demo에는 **curl >= 8.4.0**이 필요하며 mutation 전 baseline preflight에서 확인합니다.
curl 프로세스의 DNS/connect/TLS/body 전체 제한은 10초, 별도 process watchdog은
12초이며 kill 후 회수에는 추가 최대 1초를 허용합니다. 응답은 64 KiB로 제한합니다.
기존 readiness/control probe는 변경하지 않습니다. 정상 종료, 오류, SIGINT/SIGTERM
시 새 제출을 멈추고 시작 전 요청을 취소하며 실행 중 요청은 최대 20초 drain합니다.
미해결 요청을 명시적으로 기록하며 thread가 강제 종료되었다고 주장하지 않습니다.
SIGKILL/host loss에는 보호된 수동 restore workflow가 필요합니다.

`evidence/<region>/demo-load-<weight>.json` schema version 2는 region/profile/구현
commit, generator 상태, 제출/성공/실패/시작 전 취소/미해결 수, queue delay와
end-to-end duration의 고정 크기 histogram, 포화 시간, shutdown 결과를 포함합니다.
`completed`는 기존처럼 성공 수이며 `achieved_rps`는 drain을 포함한 전체 경과 시간의
성공 RPS입니다. 완결된 60초 interval 및 마지막 부분 interval의 처리율을 구분하고
목표/실제 RPS와 queue pressure를 콘솔에도 출력합니다. 최초 controller/load 오류는
primary, 이후 요청/drain/evidence 오류는 secondary로 남깁니다. 파일 저장 실패도
콘솔에 표시하며 승격을 허용하지 않습니다. 토큰/credential header/응답 본문은 기록하지
않습니다.
표본/SLO 기준은 유지하며 production에는 합성 부하를 생성하지 않습니다.

배포 중 gate는 기본 120초 로그 수집 대기 후 telemetry 누락/표본 부족에 한해
같은 닫힌 window를 30초 간격으로 추가 최대 300초 재조회합니다.
표본 기준은 그대로 유지하며 health/drift 검사를 계속합니다. 관측된 SLO 위반이나
query 오류는 즉시 실패합니다. 재시도 기록은 gate evidence의 `attempts`에 남습니다.
production의 배포 전 baseline preflight에는 이 재시도가 적용되지 않습니다.

전체 배포 경로를 검증하려면 demo profile을 실행합니다.

```bash
gh workflow run release.yml --repo "$repo" --ref main -f profile=demo
gh run list --repo "$repo" --workflow release.yml
gh run watch 'RELEASE-RUN-ID'
```

실제 유기적 트래픽과 service-level indicator가 준비된 경우에만 production
profile을 사용합니다.

```bash
gh workflow run release.yml --repo "$repo" --ref main -f profile=production
```

실패 처리, 보호된 복구, health gate 기준, 운영 전 점검표 및 운영상 한계는
[PowerShell 가이드](INSTRUCTION.md)의 7~10절을 따르세요.

단일 지역 복구:

```bash
gh workflow run restore.yml \
  --repo "$repo" \
  --ref main \
  -f source_run_id=ORIGINAL_RELEASE_RUN_ID \
  -f region=sea
```

전체 릴리스 복구 (West US, Southeast Asia, 한국 순):

```bash
gh workflow run restore.yml \
  --repo "$repo" \
  --ref main \
  -f source_run_id=ORIGINAL_RELEASE_RUN_ID \
  -f region=all
```

restore는 보호된 rollback environment의 승인을 요구하며, traffic을 변경하기 전에
원본 release와 immutable baseline artifact를 검증합니다.
