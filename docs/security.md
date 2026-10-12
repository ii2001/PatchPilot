# 보안 정책과 지원 한계

## 신뢰 경계

GitHub 소스·로그와 LLM 응답은 신뢰하지 않는다. 운영자가 설정한 API 주소(고정 api.github.com),
계정 자격 증명, 허용 저장소 목록, reviewed immutable sandbox 이미지와 PatchPilot harness만 신뢰한다.
이미지 label은 출처 인증 수단이 아니며 관리자가 이미지 digest와 내용을 직접 검토해야 한다.
.env를 자동 실행하지 않고 환경변수만 읽는다. 로그 다운로드 redirect에는 GitHub 인증 헤더를 전달하지 않는다.

## Fail-closed 실행

Docker Linux/seccomp/cgroup memory/CPU/PID 지원, 이미지 pin/label, container inspect를 확인한다.
네트워크 none, read-only root, UID 65532, cap drop ALL, no-new-privileges와 제한된 tmpfs/리소스를 고정한다.
별도 임시 Docker 볼륨의 읽기 전용 소스만 마운트하고 host path/device/socket은 공유하지 않는다.
staging container는 static tar 복사에만 사용하며 한 번도 시작하지 않는다.
소스 tree의 symlink/submodule/binary/환경 secret 파일은 지원하지 않으며 호스트에서 추출하거나 실행하지 않는다.
Docker unavailable/unsafe/timeout/OOM/보고 모순/cleanup 실패는 정상 통과로 간주하지 않는다.

## Patch 검증

경로 순회·절대/Windows/모호 경로를 차단하고 tests/.github/.git 및 test_*/*_test.py,
conftest/pytest config/설치 bootstrap 파일을 보호한다. 기존 .py만 수정한다.
정확한 원본 내용, 중복 경로, 유효 Python AST, 기본 누적 3파일·100줄 제한을 확인한다.
새로운 pytest/unittest 조작, skip/exit/동적 실행과 assert 제거 같은 명시적인 우회를 거부한다.
최종 소스는 수정 이력·workspace digest·실제 test evidence와 일치해야 하고 baseline 테스트를 제거/추가 skip할 수 없다.
Publish 때도 원본 Git blob과 승인된 검증을 다시 확인한다. 인증 정보는 container와 graph state에 넣지 않는다.

## 승인과 운영

HMAC-SHA256을 원본 요청 body에 적용하고 payload size/허용 repo/Run/attempt/SHA를 검증한다.
Webhook에서는 긴 작업이나 승인·PR 생성이 일어나지 않는다. Worker는 trusted local CLI 승인만 사용한다.
공개 Webhook은 TLS reverse proxy 뒤에 두고 rate limit/연결 제한은 운영 환경에서 설정한다.
SQLite와 checkpoint에는 소스·로그·LLM 응답이 들어 있으므로 data directory와 backup을 안전하게 관리한다.
GitHub token은 selected repositories의 Actions read, Contents write, Pull requests write만 부여한다.
PatchPilot 개발 커밋·푸시 허용은 외부 repair의 자동 승인이 아니다.

## 보장하지 않는 사항

- Docker는 kernel exploit까지 막는 완전한 sandbox가 아니다. 임의 공개 저장소를 공용 호스트에서 실행하는 운영은 권장하지 않는다.
- 정적 AST 검사와 read-only 테스트 파일은 의미적/난독화된 모든 테스트 우회를 탐지할 수 없다.
  통제된 프로젝트를 우선 지원하며 악성 테스트/소스가 pytest를 런타임에 조작할 수 있다는 한계가 있다.
- 로컬 pytest 통과는 수정의 의미적 정확성이나 remote required checks/mergeability의 증명이 아니다.
- 저장소 requirements/config/plugin/setup 명령을 자동 실행하지 않는다. 특수 plugin/config가 필요한 프로젝트는 manual action이 필요하다.
- required env 설정은 누락 탐지용 이름 목록이다. secret 값 전달은 지원하지 않는다. 비밀값이 필요한 테스트는 manual action으로 처리한다.
- 하나의 호스트에서 SQLite/파일 락을 사용하는 MVP다. 다중 호스트 worker, 고가용성, App OAuth/Token refresh,
  webhook 배포·TLS·계정 설정, 자동 merge, 파일 생성/삭제와 binary repositories는 제공하지 않는다.
- Git 객체/branch/PR의 정상 replay는 멱등적이나, 요청 수신 후 응답 유실의 모든 원격 과금 부작용까지 exactly-once로 보장하지 않는다.
