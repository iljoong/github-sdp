# SDP 기반 배포 파이프라인 운영자 매뉴얼

이 매뉴얼은 이 저장소가 구현하는 **SDP(Safe Deployment Practices) 기반 다중 지역
배포 파이프라인**을 운영자가 이해하고, 구성하고, 실행하고, 장애 시 복구할 수 있도록
작성되었습니다. 특히 `scripts\` 폴더의 각 파일 용도와 사용 방법을 상세히 설명합니다.

> 대상 독자: 릴리스 담당자, 플랫폼/SRE 운영자, 승인자(reviewer), on-call 엔지니어

## 목차

| 장 | 문서 | 내용 |
| --- | --- | --- |
| 1 | [01-architecture.md](01-architecture.md) | SDP 원칙, 전체 아키텍처, 안전 장치 설계 |
| 2 | [02-pipeline-workflows.md](02-pipeline-workflows.md) | GitHub Actions workflow 구조와 단계별 동작 |
| 3 | [03-scripts-reference.md](03-scripts-reference.md) | `scripts\` 파일별 용도, 인자, 환경 변수, 사용 예, 오류 메시지 |
| 4 | [04-operations-runbook.md](04-operations-runbook.md) | 최초 구성, 릴리스, 모니터링, 복구, 장애 대응 runbook |
| 5 | [05-zero-downtime-demo.md](05-zero-downtime-demo.md) | 무중단 배포 데모: readiness/SIGTERM drain, `/verify` 화면, 긴 작업, demo retire, 수정 지도 |
| 6 | [06-zero-downtime-demo-guide.md](06-zero-downtime-demo-guide.md) | 무중단 배포 실습: 사전 점검, 데모 절차, retire 상세, 테스트 방법(화면·폴링·curl·KQL), 실습 교훈 |

## 한눈에 보는 파이프라인

```text
 [운영자] gh workflow run release.yml -f profile=demo|production
        │
        ▼
 ┌─────────────┐   unit test / compile / docker build (1회)
 │ build       │── ACR push → sha256 digest → release.json artifact
 └─────┬───────┘
       ▼ (승인: prod-kr)
 ┌─────────────┐  snapshot.py → baseline artifact 업로드 (변경 전)
 │ Korea (kr)  │  rollout.py deploy: candidate 0% → 10% → 50% → 100%
 └─────┬───────┘  각 단계 bake + Log Analytics health gate, 실패 시 자동 복구
       ▼ 성공 시에만 (승인: prod-sea)
 ┌─────────────┐
 │ SEA (sea)   │  동일 절차
 └─────┬───────┘
       ▼ 성공 시에만 (승인: prod-usw)
 ┌─────────────┐
 │ WestUS(usw) │  동일 절차
 └─────────────┘

 [장애 시] gh workflow run restore.yml -f source_run_id=<원본 run> -f region=all|kr|sea|usw
           → usw → sea → kr 역순으로 baseline 트래픽 복원
```

## 저장소 구성 요약

| 경로 | 역할 |
| --- | --- |
| `.github\workflows\release.yml` | 릴리스 진입점: build → kr → sea → usw 순차 배포 |
| `.github\workflows\release-kr.yml` | 빠른 무중단 검증: build → kr 만 (버전 `k<N>`) |
| `.github\workflows\build.yml` | 두 release가 공유하는 재사용 build (테스트, 이미지 push, digest 기록) |
| `.github\workflows\region.yml` | 지역 1곳의 snapshot / rollout / cleanup / evidence 업로드 (재사용 workflow) |
| `.github\workflows\restore.yml` | 수동 복구 진입점 (usw → sea → kr) |
| `.github\workflows\restore-region.yml` | 지역 1곳의 복구 (재사용 workflow) |
| `.github\workflows\oidc-inspect.yml` | Azure 접근 없이 환경별 OIDC claim만 수집 |
| `.github\workflows\demo-retire.yml` | 데모 전용: 완료된 release의 이전 revision 비활성화 (SIGTERM 시연) |
| `app\` | `/healthz`, `/readyz`, `/work`, `/api/version`, `/status`, `/api/long-task`, `/verify` 를 제공하는 데모 앱 |
| `infra\` | 공유 ACR / pull identity, 지역별 ACA 환경·앱·Log Analytics (Bicep), probe·grace 정책 |
| `scripts\` | 구성(bootstrap/identity), 배포(snapshot/rollout/metrics/demo_load), 검증(check_source/inspect_oidc) 도구 |
| `tests\` | 안전성/health gate/demo load 단위 테스트 |
| `docs\spec\` | 단계별 개선 명세 |

## 관련 문서

- [README.md](../README.md) – 프로젝트 개요
- [INSTRUCTION.md](../INSTRUCTION.md) – PowerShell 실행 가이드
- [INSTRUCTION.linux.md](../INSTRUCTION.linux.md) – Linux Bash 실행 가이드
- [docs/spec/](../docs/spec/README.md) – 개선 명세
