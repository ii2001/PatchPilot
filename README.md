# PatchPilot

GitHub Actions에서 실패한 Python/pytest 테스트를 분석하고 수정하는 AI CI Repair Agent.
Phase 1은 **실제 GitHub/LLM API 없이 LangGraph 흐름을 검증하는 Mock 구현**이다.
데모의 성공은 실제 코드 수정의 정확성이나 외부 저장소 테스트 통과를 의미하지 않는다.

## 설치 및 실행

Python 3.11 이상이 필요하다. 설치 과정에서 PyPI 패키지를 내려받지만 데모 실행에는 API나 인증이 필요하지 않다.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m patchpilot demo
python -m patchpilot demo --scenario retry_success
```

`.env.example`은 안내용이다. Phase 1은 환경변수 파일을 읽지 않으며 API Key를 요구하지 않는다.
CLI는 각 시나리오에 대해 JSON 한 줄을 출력한다. 모든 결과에 `simulated: true`와 `[MOCK ONLY]` 보고가 포함된다.
CLI 종료 코드 0은 **데모 실행 완료**를 의미하며 repair 성공 여부는 JSON의 `status`로 확인한다.
잘못된 명령이나 시나리오는 argparse 종료 코드 2를 반환한다.

## 시나리오

| 이름 | 최종 상태 | 수정 시도 |
| --- | --- | --- |
| `success` | `success` | 1 |
| `retry_success` | `success` | 2 |
| `max_failures` | `failed` | 3 |
| `environment_error` | `manual_action_required` | 0 |
| `not_reproduced` | `not_reproduced` | 0 |

`python -m patchpilot demo`는 기본으로 위의 다섯 시나리오를 모두 실행한다.
재현 단계가 통과하면 실패를 재현하지 못한 것으로 보고 종료한다.
재현 또는 수정 후 실행에서 환경 오류가 발생하면 수동 조치 상태로 종료한다.
수정 후 일반 테스트 실패만 최대 3회까지 재진단하며, 정책 위반과 어댑터 오류는 즉시 실패 보고한다.

## 구성과 안전 경계

- `state.py`: 실패 정보, 진단 이력, 파일 스냅샷, 수정 이력, 시도 횟수, Mock 테스트 결과, 최종 상태를 담는 Pydantic 모델.
- `adapters.py`: `GitHubClient`, `LLMClient`, `TestRunner` Protocol과 Fake 구현.
- `nodes.py`, `graph.py`: 9개 노드와 결정론적 조건분기. LLM은 진단과 Patch 제안만 담당한다.
- `policy.py`: 경로 검사 및 원자적 메모리 수정. 기존 파일의 정확한 이전 내용이 맞을 때만 수정한다.
- `scenarios.py`, `__main__.py`: 독립적인 Fake 시나리오와 CLI.
- `tests/`: 실제 로컬 pytest로 실행하는 PatchPilot 자체 검증 코드.

FakeGitHubClient는 예시 파일 문자열만 제공한다. FakeLLMClient는 정해진 수정 제안을 반환하고,
FakeTestRunner는 파일 내용을 평가하지 않는 **사전 설정 결과**만 반환한다.
외부 저장소를 clone하거나 호스트에서 실행하지 않는다. 파일 수정은 메모리에서만 수행한다.
`tests`, `.github`, `.git` 디렉터리는 위치와 대소문자에 관계없이 수정 금지이며,
절대 경로, 경로 순회, Windows 경로, 중복 경로와 무효 수정도 거부한다.
이 정책은 Agent가 제안하는 Patch에 적용되며, 개발자가 프로젝트의 테스트를 작성하는 것을 금지하지 않는다.

## 검증 및 다음 Phase

Phase 1 구현 시 Python 3.11.15, LangGraph 1.2.14, Pydantic 2.14.1, pytest 9.1.1 환경에서 확인했다.
`pip install -e ".[dev]"` 성공, `pytest -q` **55 passed**, `python -m patchpilot demo` 다섯 시나리오 실행 성공,
`pip check` 의존성 충돌 없음, `git diff --check` 통과를 확인했다.

`pytest -q`는 그래프 실행 순서, 성공·재시도·최대 실패·환경 오류·재현 실패 분기,
수정 중 환경 오류, 어댑터 예외, 보호 파일과 경로 순회 차단, 원자적 수정,
수정 이력과 최종 파일 비교, CLI 출력을 검증한다. 이 결과는 데모의 Mock 결과와 별개다.

다음 Phase에서는 Protocol을 구현하는 어댑터를 `build_graph(github, llm, runner)`에 주입할 수 있다.
현재 `TestResult.simulated`는 `True`만 허용하므로 실제 실행을 추가할 때 결과 출처 모델과 테스트를 함께 확장해야 한다.
실제 runner는 격리된 실행 환경을 먼저 갖춰야 한다. 파일 시스템 어댑터에는 symlink 검증과 안전한 쓰기가 별도로 필요하다.

미구현: GitHub Actions 로그 수집/API 인증, 실제 LLM 호출, 외부 저장소 clone,
격리된 pytest 실행, unified diff 적용, 파일 추가·삭제, GitHub Push/PR 생성,
체크포인트·재개, 네트워크 재시도. API 키는 하드코딩하지 않으며 **Phase 1에서 실제 Push/PR은 금지**한다.

상세 흐름은 [아키텍처 문서](docs/architecture.md)를 참고한다.
개발 지침은 [AGENTS.md](AGENTS.md)에 있다.
