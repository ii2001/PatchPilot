# MVP 검증 기록

검증일: 2026-10-12 (Asia/Seoul). 실제 외부 API 호출 성공과 로컬/모의 검증을 구분한다.

## 환경

- 개발: macOS arm64, Python 3.11.15, LangGraph 1.2.14, Pydantic 2.14.1, pytest 9.1.1.
- 연동 라이브러리: langchain-openai 1.7.0, openai 3.29.0, langgraph-checkpoint-sqlite 3.1.1.
- Docker Desktop Linux daemon: Docker 29.6.2; 신뢰하는 harness 이미지에서 pytest 8.4.2 사용.
- sandbox image ID: `sha256:2b62d25e1f1bc33f70817144b263563b6862167aa24bfd8a6c2cf3c86158eac8`.
  이 ID는 이번 개발 환경에서 빌드한 이미지이며 다른 환경에서는 직접 빌드·검토 후 ID를 설정한다.

## 수행한 검증

| 검증 | 결과 |
| --- | --- |
| 변경 전 Phase 1 회귀 | 기존 55개 모두 통과 |
| Editable 설치 | `pip install -e ".[dev,webhook]"` 성공 |
| 전체 pytest, Docker opt-in 포함 | **132 passed, 2 skipped** |
| 기본 pytest, opt-in 없음 | **121 passed, 13 skipped** |
| 실제 Docker 통합 | 11개: 오류 수정/재시도/최대 실패/수동 조치/정책/재현/격리/timeout/승인 재개 |
| 실제 Uvicorn + localhost HTTP | HMAC webhook POST → HTTP 202 → SQLite 작업 등록 성공 |
| 별도 CLI worker | 키가 없는 작업을 MANUAL_ACTION_REQUIRED로 기록하고 프로세스 종료 |
| 실제 LangChain/OpenAI SDK + 모의 HTTP | json_schema 요청, Diagnosis parsing, 토큰 기록, timeout 종료 확인 |
| 모의 GitHub REST | head_sha/blob 검증, failed Job/Step/log, 승인 gating, 원자적 Git tree/ref, Draft PR 멱등성, CI 상태 |
| SQLite 재시작 | interrupt/Command resume, 승인/거절, 중복 실행 방지, 승인 후 발행 실패 재시도 |
| Mock CLI | `patchpilot demo`, `python -m patchpilot demo`, 다섯 시나리오 및 실제 성공 집계 제외 |
| 배포 패키지 | sdist/wheel 빌드 및 wheel의 CLI smoke 확인 |
| 의존성/변경 검사 | pip check 및 git diff --check |

```sh
PATCHPILOT_RUN_DOCKER_TESTS=1 \
PATCHPILOT_SANDBOX_IMAGE="$(docker image inspect patchpilot-sandbox:local --format '{{.Id}}')" \
pytest -q
```

최종 테스트는 134개다. Docker opt-in을 포함한 전체 실행은 **132 passed, 2 skipped**이며, API 키가 없는 실제 서비스 smoke 2개만 Skip되었다.
일반 `pytest -q`에서는 Docker 11개와 live API 2개가 opt-in 없이 Skip된다.
Docker opt-in 검증에서도 live API 2개는 인증 정보 및 명시적 opt-in 부재로 Skip한다.

## 실제로 확인하지 못한 사항

`OPENAI_API_KEY`, `GITHUB_TOKEN`, 실습 저장소의 failed Run이 제공되지 않았다.
따라서 실제 OpenAI 서비스 응답, 실제 GitHub 실패 Run 읽기/쓰기, Draft PR 생성,
원격 CI 성공, 계정/조직의 Token 권한·SSO 및 공개 Webhook 배포는 확인하지 못했다.
모의 HTTP/PR 성공을 실제 계정의 End-to-End 성공으로 표현하지 않는다.
실제 시연은 examples와 README의 설정·명령을 사용하고 실행별 승인을 거쳐야 한다.

Docker 실습의 LLM과 GitHub는 Fake/Mock이며 pytest 실행만 실제다.
격리 옵션과 알려진 우회 탐지를 검증했지만 모든 악성 코드에 대한 안전성이나 수정의 의미적 정확성을 증명하지 않는다.
