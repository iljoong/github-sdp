# GitHub Actions와 Azure Container Apps 안전 배포 예제

GitHub Actions, Azure Container Apps(ACA), Azure Container Registry(ACR)를 사용해
다중 지역에 애플리케이션을 **안전하게 순차 배포**하는 교육용 실행 예제입니다.

## 핵심 기능

- GitHub Actions OIDC를 통한 비밀 없는 Azure 인증
- 이미지 1회 빌드 및 `sha256` digest 고정 배포
- Korea Central → Germany West Central → Brazil South 순차 승격
- 지역별 GitHub Environment 승인과 최소 권한 배포 identity
- 10% → 50% → 100% 명시적 트래픽 분할 카나리
- 로그 기반 fail-closed health gate 및 지역 단위 자동 복구
- immutable baseline artifact를 사용하는 보호된 수동 복구 workflow

> 이 프로젝트는 실제 Azure CLI와 리소스를 사용합니다. 먼저 별도 비운영 구독에서
> 실행하고, ACA·ACR·Log Analytics·GitHub Actions 비용과 조직 정책을 확인하세요.

## 배포 흐름

```text
build/test → image push (digest 기록)
             ↓
       승인: prod-kr → KR 카나리
             ↓
       승인: prod-de → DE 카나리
             ↓
       승인: prod-br → BR 카나리
```

한 지역에서 실패하면 해당 지역만 baseline 트래픽으로 복구하며, 이미 건강한
선행 지역은 유지합니다.

## 빠른 로컬 확인

필수 도구: PowerShell 7, Python 3.13, Docker Linux engine, Azure CLI, Bicep CLI.

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app scripts tests
az bicep build --file infra\shared.bicep --stdout
az bicep build --file infra\region.bicep --stdout

python app\server.py
# 다른 PowerShell 창에서 실행
Invoke-RestMethod http://localhost:8080/healthz
Invoke-RestMethod http://localhost:8080/work
```

컨테이너도 로컬에서 검증할 수 있습니다.

```powershell
docker build --platform linux/amd64 -t safe-demo:local app
docker run --rm -p 8080:8080 safe-demo:local
```

## 시작하기

1. 이 디렉터리의 **내용과 `.github` 폴더를 포함**해 GitHub 저장소 루트에 둡니다.
2. Azure 리소스를 bootstrap하고 GitHub Environments/OIDC identity를 설정합니다.
3. `release.yml`을 `demo` 프로필로 실행해 전체 경로를 검증합니다.
4. 실제 SLI와 유기적 트래픽이 준비되면 `production` 프로필을 사용합니다.

명령, 권한, 환경 설정, health gate 기준 및 복구 runbook은
[INSTRUCTION.md](INSTRUCTION.md)를 참조하세요.

## 주요 구성

| 경로 | 설명 |
| --- | --- |
| [app/](app/) | `/healthz`, `/work` 엔드포인트를 제공하는 관측 가능한 데모 앱 |
| [.github/workflows/release.yml](.github/workflows/release.yml) | 이미지 빌드와 3개 지역 순차 배포 진입 workflow |
| [.github/workflows/restore.yml](.github/workflows/restore.yml) | baseline artifact 기반 보호된 복구 workflow |
| [infra/](infra/) | ACR, managed identity, ACA 환경/앱용 Bicep |
| [scripts/](scripts/) | bootstrap, OIDC 설정, snapshot, rollout, metrics 검증 도구 |
| [tests/](tests/) | 안전성 및 health gate 단위 테스트 |

## 주의 사항

- 기본 브랜치는 `main`입니다. 변경 시 진입 workflow와
  `scripts\check_source.py`의 검사를 함께 수정해야 합니다.
- `infra\region.bicep`와 `scripts\bootstrap.ps1`은 **최초 bootstrap 전용**입니다.
  라이브 앱에 재실행하면 안 됩니다.
- artifact에는 plain environment variable이 담길 수 있습니다. 비밀 값은
  `secretRef`와 Key Vault를 사용하고 artifact 접근 및 보존 기간을 관리하세요.
- 이 예제의 header 기반 telemetry 분류는 공개 서비스의 신뢰 경계가 아닙니다.
  운영 환경에서는 인증된 identity와 분산 추적을 사용하세요.

## 라이선스 및 참고

안전 배포 원칙은 [Azure Well-Architected Framework의 Safe Deployments](https://learn.microsoft.com/azure/well-architected/operational-excellence/safe-deployments)를
참고하세요.
