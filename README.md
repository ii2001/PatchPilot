# PatchPilot

GitHub Actions의 실패한 Python/pytest 테스트를 분석하고, Docker에서 재현·수정·검증한 뒤
**실행별 사용자 승인 후 Draft PR을 제안**하는 LangGraph Agent MVP다. 자동 Merge는 하지 않는다.
기존 Phase 1 Mock 데모와 테스트도 유지한다.

## 빠른 시작

Python 3.11+, GitHub 저장소 읽기/쓰기 권한, OpenAI 계정, 안전 설정을 지원하는 Linux Docker 데몬이 필요하다.
macOS에서는 Docker Desktop의 Linux VM을 사용할 수 있다. Mock 데모에는 인증 정보나 Docker가 필요 없다.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,webhook]"
pytest -q
patchpilot demo
python -m patchpilot demo --scenario retry_success
```

CLI는 JSON을 출력하며 `summary` 또는 Mock의 `report`에 사람이 읽을 수 있는 설명이 있다.
Mock 결과는 항상 `simulated: true`이며 실제 검증 성공으로 집계하지 않는다.
Mock 시나리오는 성공, 2회째 성공, 3회 실패, 환경 오류, 재현 실패의 다섯 가지다.

## 환경변수 및 인증

[.env.example](.env.example)을 복사해 로컬 `.env`를 작성한다. 코드가 dotenv 파일을 자동으로 실행하거나 읽지는 않는다.
셸이나 운영 환경에서 변수를 설정한다. 실제 인증 값을 터미널 출력·로그·Git에 남기지 않는다.

```sh
cp .env.example .env
# .env를 직접 편집한 다음, 신뢰하는 본인의 설정 파일만 로드한다.
set -a
. ./.env
set +a
```

필수: `GITHUB_TOKEN`, `OPENAI_API_KEY`, `PATCHPILOT_OPENAI_MODEL`, `PATCHPILOT_ALLOWED_REPOS`, `PATCHPILOT_SANDBOX_IMAGE`.
모델은 본인의 계정에서 사용 가능한 Structured Output 지원 모델로 설정한다. Temperature/Timeout도 환경변수로 관리한다.
현재 `.env.example`은 테스트용 OpenAI 프로필 `gpt-6-luna`, `PATCHPILOT_REASONING_EFFORT=none`을 사용한다.
오류 분류·원인 진단·Patch 생성은 모두 동일한 OpenAI 모델을 호출한다.
추론을 높이려면 모델이 지원하는 `low`, `medium`, `high` 등으로 설정한다. 추론이 활성화되면 Temperature는 요청에서 제외한다.
추론을 지원하지 않는 모델로 변경하면 `PATCHPILOT_REASONING_EFFORT`를 비워둔다.
이 프로필은 실행 흐름을 저렴하게 확인하기 위한 설정이며 실제 코드 수정 품질은 대상 프로젝트로 평가해야 한다.
[GPT-6 Luna 모델 안내](https://developers.openai.com/api/docs/models/gpt-6-luna),
[추론·Temperature 호환성](https://developers.openai.com/api/docs/guides/latest-model)을 참고한다.
GitHub fine-grained Token은 **허용 대상 저장소만** 선택하고 Actions: read, Contents: read/write,
Pull requests: read/write를 부여한다. Metadata 읽기는 기본 권한이다. Workflow 쓰기나 관리자 권한은 요구하지 않는다.
조직의 별도 승인·SSO 정책은 사용자가 설정해야 한다. 토큰은 GitHub API 클라이언트에서만 사용하며
컨테이너, 그래프 상태, 체크포인트 및 LLM 프롬프트 설정에 전달하지 않는다.
소스코드와 실패 로그는 OpenAI에 전달되므로 허용 저장소의 데이터 전송 정책을 먼저 확인한다.

## Docker Sandbox

저장소의 `Dockerfile`, setup script 또는 requirements를 실행해 이미지를 만들지 않는다.
아래 빌드는 PatchPilot의 신뢰하는 harness와 pytest만 설치한다. 이미지 빌드 단계는 네트워크를 사용하고,
대상 저장소 테스트 컨테이너는 네트워크를 차단한다.

```sh
docker build -t patchpilot-sandbox:local docker
export PATCHPILOT_SANDBOX_IMAGE="$(docker image inspect patchpilot-sandbox:local --format '{{.Id}}')"
```

운영에서는 검토한 immutable 이미지 ID 또는 `registry/image@sha256:...`를 설정한다. 가변 tag는 거부한다.
추가 의존성은 관리자가 신뢰하는 별도 이미지에 미리 설치하고 harness와 `patchpilot.sandbox.version=1` label을 유지한다.
컨테이너 시작 시 pip install, 네트워크 설치 또는 저장소 제공 shell 명령을 실행하지 않는다.

- UID/GID 65532, read-only rootfs, 모든 capability 제거, no-new-privileges, 기본 seccomp.
- network none, CPU 1, memory/swap 512 MiB, PID 64, file descriptor 256, timeout 기본 120초.
- 소스는 호스트 bind mount 없이 임시 Docker-managed 볼륨에 복사하고 read-only로 마운트한다.
- `/tmp`만 크기가 제한된 noexec/nosuid/nodev tmpfs로 제공한다. Docker socket과 호스트 비밀값은 전달하지 않는다.
- 설정과 실제 inspect 결과를 확인한 뒤 실행하며, 완료/오류/timeout 후 컨테이너와 볼륨을 정리한다.
  정리 실패는 성공으로 숨기지 않고 수동 정리 대상 ID를 보고한다.

Docker 자체는 모든 악성 코드를 막는 완전한 보안 경계가 아니다. 운영에서는 전용 격리 호스트/VM과
최소 권한 서비스 계정을 사용해야 한다. 세부 한계는 [보안 문서](docs/security.md)를 참고한다.

## CLI 실행과 승인

```sh
patchpilot repair --repo owner/repository --run-id 123456789
patchpilot status --job-id JOB_ID
patchpilot approve --job-id JOB_ID
patchpilot reject --job-id JOB_ID
# 모든 명령은 python -m patchpilot ... 방식도 지원한다.
```

`repair`는 입력 Run이 완료된 실패인지 확인하고, 실패 Job/Step과 로그, 정확한 `head_sha` 및 Git blob을 수집한다.
원본 SHA의 소스를 Docker에서 재현한 후 LLM 분류/진단/수정과 테스트를 최대 3회 수행한다.
매 시도마다 대상 테스트와 전체 테스트를 각각 실행한다. `PATCHPILOT_TEST_TARGETS`를 지정하지 않으면
대상 단계도 전체 테스트를 실행한다. Log의 임의 shell 명령을 실행하지 않는다.

검증 후 `AWAITING_APPROVAL`과 `job_id`/`thread_id`, 원인·파일·Diff·시도 횟수·실행 명령·결과·위험이 출력된다.
`approve`는 저장된 검증 결과를 재검사한 뒤 GitHub Git Database API로 commit/ref를 만들고 Draft PR을 생성한다.
이는 호스트에서 대상 저장소를 checkout하거나 git push하는 방식이 아니다.
원본 브랜치가 움직였으면 발행을 거부한다. 같은 Run/SHA의 브랜치는 재실행 attempt가 달라도 덮어쓰지 않고,
동일 변경의 재실행은 기존 브랜치·PR을 재사용한다. PR 본문에는 원인·수정·로컬 검증·원본 Run 링크가 포함된다.
승인 거절은 PR 생성 없이 종료한다. 동일한 승인 명령을 반복해도 두 번째 PR을 만들지 않는다.

`PUBLISH_FAILED` 상태에서는 동일한 `approve`를 다시 실행해 이미 승인한 발행만 재시도할 수 있다.
테스트나 LLM을 다시 실행하지 않으며 동일한 검증 결과를 재검사한다.
재현/환경/수정 실패를 해결한 후 다시 작업하려면 GitHub Actions를 재실행해 새 `run_attempt`를 사용한다.
작업 CLI는 실행 오류와 실패/수동 조치/발행 실패에 종료 코드 1, 잘못된 인자에 2를 반환한다.
worker는 작업별 상태를 기록하고 큐 처리를 계속하며 `--once`는 큐 처리를 완료하면 0을 반환한다.
승인 대기·거절·재현 실패·CI pending은 정상 처리 결과로 0이다. 성공 여부는 JSON `status`를 확인한다.

## Webhook와 별도 Worker

Webhook은 선택 기능이며 CLI만으로도 전체 흐름을 사용할 수 있다.

```sh
pip install -e ".[webhook]"
# PATCHPILOT_WEBHOOK_SECRET과 PATCHPILOT_ALLOWED_REPOS 설정 후:
uvicorn patchpilot.webhook:create_app --factory --host 127.0.0.1 --port 8000
# 별도 프로세스/서비스에서 같은 환경 및 DATA_DIR로 실행:
patchpilot worker
patchpilot worker --once
```

GitHub 저장소 Settings → Webhooks에서 HTTPS endpoint `/webhooks/github`, Content type `application/json`,
같은 Secret, Workflow runs 이벤트를 설정한다. 공개 연결에는 TLS reverse proxy를 사용한다.
Receiver는 HMAC SHA-256 서명을 원본 body로 검증하고 완료된 failure 이벤트만 영속 큐에 등록한다.
허용 저장소 밖의 이벤트, fork Run, `patchpilot/` 브랜치 및 중복 delivery/Run attempt를 처리하지 않는다.
요청 처리에서 Agent를 실행하지 않는다. worker는 파일 락으로 작업을 독점하고 재시작 시 체크포인트에서 재개한다.
Webhook에는 승인 endpoint가 없다. 신뢰하는 운영자가 로컬 CLI로 결과를 검토하고 승인한다.

## 상태·평가 지표

| 상태 | 의미 |
| --- | --- |
| `NOT_REPRODUCED` | 원본 테스트가 통과하여 실패를 재현하지 못함 |
| `MANUAL_ACTION_REQUIRED` | 환경/지원 범위/실행 설정 문제 |
| `REPAIR_FAILED` | 정책 위반·LLM 오류·3회 수정 실패 |
| `LOCAL_VERIFIED` | 실제 Docker의 대상 및 전체 pytest와 패치 정책 검증 완료 |
| `AWAITING_APPROVAL` | 로컬 검증 후 승인 interrupt에서 대기 |
| `REJECTED` | 사용자 거절, 발행하지 않음 |
| `PUBLISH_FAILED` | 승인 후 발행 실패, 검증된 변경 발행만 재시도 가능 |
| `CI_PENDING` | Draft PR 생성, 원격 CI 대기 |
| `CI_VERIFIED` | 정확한 발행 SHA에서 원본 workflow 및 관측된 workflow들이 성공 |
| `CI_FAILED` | 발행 SHA의 원격 workflow 실패 |

`status`는 CI 상태를 새로 조회한다. 이는 branch protection의 모든 required check 통과나 Merge 허가를 의미하지 않는다.
보고에는 총 경과 시간(승인 대기 포함), 실제 노드 실행 시간, LLM 요청/캐시 횟수·토큰,
수정 시도·변경 파일 수, 로컬 테스트 통과 여부, 검증 수준, 원격 CI 상태, PR URL을 포함한다.
LLM 반환 데이터와 사용량은 영속 상태에 기록하고 인증 정보는 저장하지 않는다.
SQLite 데이터 디렉터리는 0700, DB/lock 파일은 0600으로 제한한다. 백업 시 원본 소스와 로그가 포함됨에 유의한다.

## 지원 범위와 테스트

MVP는 최대 200개 파일/1 MB의 UTF-8 text 저장소, 기존 Python 파일 수정, Python 3.11+/pytest를 지원한다.
Binary, symlink, submodule, fork Run, 파일 추가·삭제, 임의 dependency 설치는 fail-closed로 거부한다.
테스트·CI·Git·pytest 설정 변경, 기본 3개 파일/100줄 초과, 경로 순회, 중복/불일치 Patch와
명시적 skip/exit/pytest 조작/동적 실행/assert 제거를 차단한다. 정적 검사는 모든 의미적 우회를 증명할 수 없다.
pytest config는 신뢰하는 고정 설정을 사용하고 외부 plugin autoload를 차단한다.
특수 config/plugin이 필요한 프로젝트는 현재 지원 범위 밖일 수 있다.

```sh
pytest -q
PATCHPILOT_RUN_DOCKER_TESTS=1 pytest -q -m docker
# 자격 증명과 대상 failed Run을 설정한 뒤에만 실제 API smoke 실행:
PATCHPILOT_RUN_LIVE_TESTS=1 pytest -q -m live
python -m build
pip check
```

Docker/API 테스트는 명시적 opt-in이 없으면 안전하게 Skip한다.
실제 API smoke는 GitHub 읽기와 LLM 호출만 수행하며 승인 없는 PR 발행을 하지 않는다.
[실습 시나리오](examples/failing_pytest_demo/README.md), [검증 기록](docs/validation.md),
[아키텍처](docs/architecture.md), [개발 지침](AGENTS.md)을 참고한다.

다른 저장소를 만들지 않고 검증하려면 [동일 저장소 실제 수리 가이드](docs/self-repair.md)를 사용한다.
정상 `main`과 분리된 테스트 브랜치에서 실패를 만들고, 자체 의존성을 설치한 전용 sandbox로 재현한다.
