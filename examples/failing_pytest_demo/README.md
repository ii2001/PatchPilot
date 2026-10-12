# 통제된 pytest 실습 프로젝트

이 디렉터리의 일부 소스는 의도적으로 문법 오류 또는 import 오류를 포함한다.
호스트에서 이 코드를 실행하거나 PatchPilot 일반 테스트에 포함하지 않는다.
`tests/test_docker_integration.py`는 문자열로 읽어 DockerTestRunner에만 전달한다.

## 실제 Docker 시연

```sh
docker build -t patchpilot-sandbox:local docker
export PATCHPILOT_SANDBOX_IMAGE="$(docker image inspect patchpilot-sandbox:local --format '{{.Id}}')"
PATCHPILOT_RUN_DOCKER_TESTS=1 pytest -q -m docker
```

| 요구 시나리오 | 구현/검증 위치 | 검증 방식 |
| --- | --- | --- |
| AssertionError 수정 | assertion/src, tests | 실제 Docker pytest + scripted patch |
| SyntaxError 수정 | syntax/src, tests | 실제 Docker pytest + scripted patch |
| ImportError 수정 | import/src, tests | 실제 Docker pytest + scripted patch |
| 첫 실패 후 재시도 성공 | Docker retry 시나리오 | 실제 테스트 실패/성공 |
| 3회 수정 실패 | Docker max_failures | 실제 테스트 3회 실패 |
| 환경변수 누락 | environment + required_env | Docker harness의 명시적 manual action |
| 테스트 파일 수정 차단 | Docker protected + 정책 테스트 | 실제 재현 이후 apply 거부 |
| 재현되지 않는 실패 | Docker not_reproduced | 실제 원본 테스트 통과 후 종료 |
| LLM API 실패/Timeout | test_real_adapters.py | 모의 LangChain/API 오류, 키 없이 실행 |
| 중복 이벤트/PR | persistence_webhook, real_adapters | 서명된 모의 Webhook/REST 및 영속 검증 |

추가로 비특권·네트워크·read-only·secret 미전달, timeout/cleanup과
실제 Docker → SQLite 승인 대기 → 새 서비스 인스턴스 재개 → 모의 Draft PR 흐름을 검증한다.
LLM/GitHub가 Fake/Mock인 시연은 실제 외부 API 성공으로 보고하지 않는다.

## 실제 GitHub Actions 시연 준비

본인의 실습 저장소에 `assertion/` 또는 `syntax/` 또는 `import/`의 src/tests를 복사하고,
공통 `.github/workflows/ci.yml`을 그 저장소의 `.github/workflows/ci.yml`로 복사한다.
GitHub Actions 실패 Run을 만든 뒤 README의 자격 증명/allowlist/image를 설정하고 다음을 실행한다.

```sh
patchpilot repair --repo owner/controlled-demo --run-id FAILED_RUN_ID
patchpilot status --job-id JOB_ID
# JSON의 원인, Diff, 실제 테스트 결과와 위험을 검토한 후:
patchpilot approve --job-id JOB_ID
patchpilot status --job-id JOB_ID
```

이 작업의 외부 계정 실습은 자격 증명이 있을 때 사용자가 실행한다.
실제 테스트 재현, LLM 수정 결과, Draft PR과 remote CI는 각각 별도로 확인해야 한다.
