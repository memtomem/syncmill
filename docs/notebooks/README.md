# syncmill 실습 노트북

syncmill을 처음 쓰는 분이 셀을 하나씩 실행하면서 기본 흐름을 직접 확인하는
Jupyter 노트북 모음입니다. 읽기 위주의 설명은 [사용 안내서](../user-guide.ko.md)에
있고, 여기서는 같은 내용을 **직접 실행해 보는 것**에 집중합니다.

## 여는 방법

syncmill 소스 폴더에서 실행합니다.

```bash
uv sync
uv run --with jupyterlab jupyter lab docs/notebooks
```

Jupyter는 syncmill의 의존성이 아니므로 `--with`로 그때만 함께 띄웁니다.

## 순서

| 노트북 | 내용 | 시간 |
|---|---|---|
| [00. 소개](00-introduction.ipynb) | syncmill이 푸는 문제, 전체 구조, 용어, 안전 장치와 비용, 설치 확인 | 5분 |
| [01. 첫 실행](01-first-run.ipynb) | 샌드박스 → `init`/`doctor`/`run --dry-run` → `run` → `diff` → `apply` → 출력 읽는 법·실행 기록 → 공유 메모리 → 실패·대체 실행·`discard` → 실무 응용(품질 게이트, `continue`) → 내 프로젝트에 적용하기 | 15분 |
| [02. 전략 비교](02-strategies.ipynb) | `route`·`pipeline`·`compete`·`council`·`decompose`를 같은 과제로 비교, 호출 수와 결과 선택 규칙, 상황별 선택 가이드 | 10분 |
| [03. 공유 메모리](03-shared-memory.ipynb) | 기록의 저장 위치와 모양, 다음 작업에 붙는 방식, 종류 태그, 고장 시 동작, `serve`로 다른 도구에서 읽기 | 10분 |
| [04. 워크보드와 TUI](04-board-and-tui.ipynb) | 작업 등록·의존·팀 결정, 검증된 완료(`done`), 막힌 작업, `board stats`, TUI 소개 | 10분 |

처음이라면 00 → 01 순서로 보세요. 02~04는 01을 마친 뒤라면 필요한 것부터 봐도 됩니다.

## 비용과 안전

- 노트북은 `/tmp/syncmill-notebook.*` 아래에 일회용 샌드박스를 만들고 그 안에서만
  작업합니다. 가짜 에이전트로 실행하는 동안에는 여러분의 실제 프로젝트와 홈 디렉터리의
  syncmill·memtomem 상태를 건드리지 않습니다.
- 에이전트는 진짜 Claude/Codex/Kimi 대신 같은 출력 형식을 흉내 내는 **가짜 CLI**를
  씁니다. 로그인이 필요 없고 비용이 들지 않습니다.
- 실제 CLI를 부르는 셀은 01번의 12장 하나뿐이며, `RUN_REAL = True`로 직접 바꿔야만
  실행됩니다. 이 셀은 로그인 정보가 있는 진짜 홈 디렉터리와 환경을 사용하므로,
  memtomem이 여러분의 `~/.memtomem` 설정을 읽고 필요하면 갱신할 수 있습니다.

| 노트북 | 실제 provider 호출 |
|---|---|
| 00, 02, 03, 04 | 없음 |
| 01 | 없음 (12장을 켜면 1회) |

샌드박스 도우미의 동작은 [`_sandbox.py`](_sandbox.py)에서 볼 수 있습니다.
