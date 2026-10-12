# 같은 저장소에서 실제 Repair 검증

PatchPilot 자체 저장소도 수리 대상으로 사용할 수 있다. 정상 `main`에서 별도 `test/` 브랜치를 만들고
기존 테스트가 잡아내는 작은 소스 버그를 넣어 CI를 실패시킨다. 수리 PR의 base는 그 실패 브랜치이며
의도적으로 만든 버그를 `main`으로 병합하지 않는다.

## 자체 프로젝트 전용 Sandbox

일반 sandbox는 pytest만 포함한다. 자체 테스트에는 LangGraph·Pydantic·OpenAI SDK·FastAPI도 필요하므로
검토한 고정 의존성의 별도 이미지를 사용한다. 대상 저장소의 setup/requirements는 실행하지 않는다.

```sh
docker build -f docker/Dockerfile.patchpilot -t patchpilot-self-sandbox:local docker
export PATCHPILOT_SANDBOX_IMAGE="$(docker image inspect patchpilot-self-sandbox:local --format '{{.Id}}')"
export PATCHPILOT_ALLOWED_REPOS=ii2001/PatchPilot
```

이 이미지의 고정 pytest 설정은 자체 회귀 테스트를 실행하며, 의도적인 SyntaxError/ImportError를
포함한 `examples/` 교육용 fixture를 수집하지 않는다. 저장소의 pyproject `testpaths=tests`와 같은 테스트 범위다.
API 키·토큰 및 Docker socket은 컨테이너에 전달하지 않으며 opt-in API/Docker 테스트는 Skip된다.

## 검증 흐름

1. `.github/workflows/ci.yml`의 정상 CI를 먼저 확인한다.
2. `main`에서 `test/` 브랜치를 만들고 기존 소스에 작은 버그를 주입한다. 테스트와 CI 설정은 바꾸지 않는다.
3. 그 브랜치를 Push하고 Actions의 실패 Run ID를 얻는다.
4. 키와 토큰을 설정한 로컬 환경에서 아래를 실행한다.

```sh
patchpilot repair --repo ii2001/PatchPilot --run-id FAILED_RUN_ID
patchpilot status --job-id JOB_ID
```

원본 Commit/로그 수집 → 실제 Docker 재현 → OpenAI 분류/진단/수정 → 실제 회귀 테스트 통과 →
검증된 Diff와 `AWAITING_APPROVAL` 상태까지 확인한다. 검토 후에만 다음을 실행한다.

```sh
patchpilot approve --job-id JOB_ID
patchpilot status --job-id JOB_ID
```

Draft PR의 base가 `test/` 브랜치인지 확인하고 실제 GitHub CI 결과를 확인한다.
발행 테스트를 위해 정상 `main`을 강제로 되돌리거나 실패 코드를 Merge하지 않는다.
Token에는 이 저장소의 Actions 읽기, Contents 쓰기, Pull requests 쓰기 권한이 필요하다.
개발자가 CI 설정을 처음 추가할 때는 별도 Workflows 쓰기 권한이 필요할 수 있다.
Agent는 수리 Patch에서 CI 설정을 수정하지 않는다.
