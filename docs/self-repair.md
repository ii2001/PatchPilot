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

## 2026-10-12 실제 검증 기록

- 정상 main의 실제 GitHub CI 및 전용 Docker 회귀 테스트 통과를 먼저 확인했다.
- 실패 실습 브랜치: `test/self-repair-policy-20261012`.
- 실패 Commit: `b64e8c7dbd1922e526c1aefd5c208e395880b269`.
- 실제 실패 Run: [38213983578](https://github.com/ii2001/PatchPilot/actions/runs/38213983578).
- 의도적으로 변경한 `policy.py`의 비교 연산자 한 줄을 기존 테스트가 감지했다.
  Docker 대상 4 failed, 전체 22 failed / 104 passed / 13 skipped를 확인했다.
- 첫 Agent 실행은 모델의 부분 문자열 제안을 전체 파일 Patch로 취급하여 원본 불일치로 거부됐다.
  파일과 PR은 변경하지 않았다.
- 실행 어댑터가 유일하게 일치하는 부분 문자열을 원본 전체에 안전하게 적용하도록 보완하고,
  원래의 원본 비교·경로·AST·수정량·실제 테스트 검증은 유지했다.
- 동일 Run attempt 2에서 실제 OpenAI `gpt-6-luna`가 원인 분류·진단·수정에 성공했다.
- 수정 파일 1개, 시도 1회, LLM 호출 3회. 입력 286385 tokens, 출력 481 tokens.
- 실제 Docker 대상 29 passed, 전체 126 passed / 13 skipped. 테스트 파일과 CI 설정은 수정하지 않았다.
- Job ID: `63c05a5518da58958285dbdaa340f106`. 검증 후 AWAITING_APPROVAL까지 체크포인트로 저장했다.

```diff
-        if change.path not in files or files[change.path] == change.before:
+        if change.path not in files or files[change.path] != change.before:
```

이 기록의 성공은 실제 API 및 Docker 실행 결과다. Mock 데모 결과와 구분한다.

사용자 승인 후 발행 시 `POST /git/trees`에서 HTTP 403과 `contents=write` 요구가 반환됐다.
이 때 상태는 PUBLISH_FAILED이며 로컬 검증과 승인 체크포인트는 유지된다.
토큰의 선택 저장소와 Contents 쓰기 권한을 설정한 뒤 동일 Job의 approve로 발행만 재시도할 수 있다.
