# syncmill 사용 안내서 (처음 시작하는 분들을 위해)

> 처음 syncmill을 써 보는 분을 위한 쉬운 안내서예요. 작업 중심 영어 가이드는
> [user-guide.md](user-guide.md)를, 전체 명령·옵션의 정확한 정본은
> `syncmill --help`와 `syncmill <명령> --help`를, 설계 배경은
> [CLAUDE.md](../CLAUDE.md)를 참고하세요.

## 목차

- [syncmill이 뭔가요?](#syncmill이-뭔가요)
- [설치하기](#설치하기)
- [`/tmp`에서 안전하게 시험하기](tmp-test-guide.ko.md)
- [실습 노트북 (가짜 에이전트로 비용 없이 따라 하기)](notebooks/README.md)
- [처음 실행해 보기](#처음-실행해-보기)
- [실제 에이전트로 안전하게 실행하기](#실제-에이전트로-안전하게-실행하기)
- [꼭 알아야 할 5가지](#꼭-알아야-할-5가지)
- [전략 고르기](#전략-고르기)
- [millflow 워크플로우](#millflow-워크플로우)
- [자주 쓰는 명령어](#자주-쓰는-명령어)
- [TUI 화면 안내](tui-guide.ko.md)
- [설정하기](#설정하기)
- [한 단계 더: 선택 기능](#한-단계-더-선택-기능)
- [워크보드로 팀처럼 일하기](#워크보드로-팀처럼-일하기)
- [자주 하는 작업](#자주-하는-작업)
- [문제가 생겼을 때](#문제가-생겼을-때)

---

## syncmill이 뭔가요?

**syncmill은 하나의 코딩 작업을 여러 AI 코딩 에이전트에게 동시에 시키는 도구예요.**
지원하는 에이전트는 Claude Code, Codex CLI, Kimi Code 세 가지입니다.

이렇게 생각하면 쉬워요. 어려운 일을 혼자 하는 대신, **여러 명의 전문가에게
맡기는 것**과 같아요. syncmill은 그 전문가들에게 일을 나눠 주거나, 같은 일을 두고
경쟁시키거나, 서로의 의견을 보고 다듬게 한 다음, **결과 하나**를 골라
돌려줍니다.

이 과정에서 두 가지를 챙겨 줘요.

- **공유 메모리** — 한 에이전트가 알아낸 내용을 다른 에이전트도 볼 수 있게 해 줘요.
  ([memtomem](https://pypi.org/project/memtomem/)이라는 장기 기억 저장소를 씁니다.)
- **격리된 작업 공간** — 일반 `run`은 각 에이전트를 격리된 임시 작업 폴더에서
  실행하고 결과를 artifact로 남겨요. 원본은 사용자가 `apply`할 때만 바뀝니다.
  대화형 `session start`는 원본에서 직접 실행되는 예외예요.

명령어 한 줄에 작업 내용(프롬프트)을 적어 주면, syncmill이 알아서 에이전트를
돌리고 결과를 정리해 줍니다.

---

## 설치하기

syncmill은 [uv](https://docs.astral.sh/uv/)와 Python 3.12 이상이 필요해요. 처음에는
소스 저장소를 내려받아 설치합니다.

```bash
git clone https://github.com/memtomem/syncmill.git
cd syncmill
uv sync
export SYNCMILL_SRC="$PWD"
```

`SYNCMILL_SRC`는 Syncmill 소스 위치예요. 이제 실제로 작업할 Git 저장소로 이동합니다.

```bash
cd /path/to/my-project
```

앞으로 초보자 흐름의 명령은 다음 형태를 사용합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill <명령>
```

선택형 mem0 백엔드도 쓰려면 `uv sync --project "$SYNCMILL_SRC" --extra mem0`으로 설치하세요.
로컬 LangGraph SQLite 백엔드는 `uv sync --project "$SYNCMILL_SRC" --extra langgraph`로 설치하세요.

### 미리 준비할 것

**1. 에이전트 CLI를 최소 하나는 설치하고 로그인하세요.** syncmill은 실제 프로그램을
불러서 실행하기 때문에, 아래 프로그램이 설치되어 있고 로그인되어 있어야 해요.

| 에이전트 이름 | 실행 파일 | 도구                                                    |
|--------------|----------|--------------------------------------------------------|
| `claude`     | `claude` | [Claude Code](https://claude.com/claude-code)          |
| `codex`      | `codex`  | Codex CLI                                              |
| `kimi-code`  | `kimi`   | [Kimi Code](https://moonshotai.github.io/kimi-code/)   |

세 개를 다 깔 필요는 없어요. 설치 안 된 에이전트는 그냥 건너뛰고 다음 에이전트가
실행됩니다. 하나만 있어도 시작할 수 있어요.

**2. 메모리용 임베딩 백엔드** *(선택 사항이지만 권장)*. 공유 메모리를 쓰려면 임베딩
백엔드가 필요한데, syncmill이 설치할 때 한국어를 지원하는 로컬 ONNX 백엔드를 함께
설치하므로 **별도 설정 없이도 바로 동작해요.** (로컬 ollama나 OpenAI 키도 쓸 수 있고,
활성 제공자는 `~/.memtomem/config.json`에서 설정해요.) 만약 메모리가 안 되더라도 syncmill은
멈추지 않아요 — 공유 메모리 없이 작업만 진행하고 노란색 안내 메시지를 보여 줍니다.

`syncmill init`으로 초기화한 프로젝트는 `[memory]`를 생성된 기본값 그대로 두면
메모리를 저장소 안 `<repo>/.syncmill/memory`에 둡니다. memtomem의 전역 DB와 메모리
디렉터리를 쓰는 것은 초기화하지 않은 체크아웃뿐입니다. 다른 위치를 쓰려면
`memory.db_path`와 `memory.memory_dir`를 함께 지정해야 합니다. DB 경로만 바꾸면
memtomem이 쓰는 Markdown 원본 파일 경로는 격리되지 않습니다.

대안으로 `SYNCMILL_MEMORY__BACKEND=mem0`를 선택할 수 있어요. 기본 설정은
`~/.syncmill/mem0` 아래의 로컬 Qdrant와 다국어 FastEmbed를 사용하고, 모든 쓰기에
`infer=False`를 강제해서 LLM을 호출하지 않아요. 처음 한 번은 임베딩 모델을 내려받으며,
mem0 익명 텔레메트리는 기본적으로 꺼져 있어요. 내장 Qdrant는 설정된 경로를 독점
잠그므로 기본 로컬 저장소는 한 번에 한 프로세스만 사용할 수 있어요. 여러 실행이나
`serve`, `status`를 동시에 사용하려면 `memory.mem0.config.vector_store.config.url`에
Qdrant 서버를 설정하세요. 이 경우에도 Mem0 기록은 `data_dir`에 로컬로 남아요. Mem0는
프로세스에서 처음 `mem0`를 import할 때 텔레메트리 설정을 확정하므로 실행 전에 설정하고,
값을 바꾸려면 프로세스를 다시 시작하세요. 다른 라이브러리가 Mem0를 먼저 import했다면
그때의 설정이 해당 프로세스에서 계속 적용됩니다.

세 번째 선택지는 LangGraph의 영구 `AsyncSqliteStore`예요.
`uv sync --project "$SYNCMILL_SRC" --extra langgraph`로 설치하고
`SYNCMILL_MEMORY__BACKEND=langgraph`를 설정하세요. 기본
파일은 `~/.syncmill/langgraph.db`이고, 로컬 다국어 FastEmbed로 검색하며 LLM은
호출하지 않아요. 경로는 `SYNCMILL_MEMORY__LANGGRAPH__DB_PATH`로 바꿀 수 있어요.

### 잘 설치됐는지 확인하기

```bash
uv run --project "$SYNCMILL_SRC" syncmill --version
uv run --project "$SYNCMILL_SRC" syncmill agents
```

---

## 처음 실행해 보기

처음부터 provider를 호출하지 마세요. 프로젝트 파일을 만들고 준비 상태와 실행 계획을
먼저 확인합니다.

실제 프로젝트를 건드리지 않고 Claude·Codex·Kimi Code를 모두 확인하려면 먼저
[`/tmp` 안전 테스트 가이드](tmp-test-guide.ko.md)를 따라가세요. Syncmill의
worktree·artifact·TUI 상태를 임시 폴더로 고정하고, 실제 호출 단계도 provider별로
하나씩 분리합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill init
uv run --project "$SYNCMILL_SRC" syncmill doctor
uv run --project "$SYNCMILL_SRC" syncmill run --dry-run \
  "README에 한 줄짜리 사용 예시를 추가해 줘"
```

`init`은 `syncmill.toml`, `.syncmill/project.json`, `.syncmill/.gitignore`를 만들고
자동으로 stage하지 않습니다. 따라서 바로 뒤의 `git status`가 dirty인 것은 정상이에요.
`project.json` 마커는 머신 로컬 식별자라서 `init`이 Git ignore 처리합니다. Git에
추적되고 있는 마커(또는 id가 평범한 경로 토큰이 아닌 마커)는 신뢰하지 않고, 경로
기반 id로 대체됩니다.

기본 `doctor`는 활성 provider 중 **하나만 설치·로그인되어 있어도 성공**합니다.
나머지는 warning으로 보여 줍니다. 모든 활성 provider가 준비돼야 하는 운영 환경에서는
`syncmill doctor --strict`를 사용하세요. `Status` 화면의 enabled는 설정값일 뿐이므로
실제 binary와 인증 확인은 항상 `doctor`가 기준입니다.

dry-run의 저장소·전략·agent·timeout이 의도한 값이면 Codex 하나로 첫 실제 실행을
시작합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill run \
  "README에 한 줄짜리 사용 예시를 추가하고 관련 검사를 실행해 줘" \
  --agent codex --timeout 120
```

작업 결과는 원본에 바로 복사되지 않습니다. `runs list`와 `diff`로 검토한 다음 정확한
run만 `apply`하세요.

```bash
uv run --project "$SYNCMILL_SRC" syncmill runs list
uv run --project "$SYNCMILL_SRC" syncmill diff <run-id>
uv run --project "$SYNCMILL_SRC" syncmill apply <run-id>
```

---

## 실제 에이전트로 안전하게 실행하기

테스트는 fake 에이전트로 빠르게 돌지만, `syncmill run`은 시뮬레이션이 아니에요.
현재 shell의 `PATH`에 있는 실제 에이전트 CLI를 실행하고, 각 CLI에 로그인된 계정을
사용합니다. 따라서 일반적인 에이전트 실행과 똑같이 네트워크, 사용량/요금, provider
quota, 그리고 해당 CLI의 비대화형 승인 정책이 적용될 수 있어요.

그래도 syncmill의 프로그램 실행은 대상 저장소를 격리합니다.

- syncmill은 현재 Git HEAD뿐 아니라 수정한 tracked 파일과 untracked 파일까지 개인
  제어 clone에 스냅샷한 뒤, 에이전트별 worktree를 만듭니다. ignored 파일은 제외합니다.
  Git 저장소가 아니거나 충돌/리베이스가 진행 중이면 빈 임시 폴더로 우회하지 않고 중단합니다.
- 결과에 `Files changed`가 나오더라도, 그 변경은 우승한 worktree의 diff 목록이에요.
  syncmill이 원래 작업 폴더에 자동으로 merge하거나 복사하지 않습니다. 성공한
  우승자의 코드는 런 아티팩트로 보존됩니다. 출력된 run id로 `syncmill diff <run-id>`를
  검토하고 `syncmill apply <run-id>`를 실행해야 원본 작업 폴더에 반영됩니다.
- 실행 worktree는 보통 정리하지만 `manifest.json`, `winner.patch`, 적용 영수증은
  `~/.syncmill/projects/<project-id>/runs/<run-id>/`에 남습니다. SIGKILL은 정리를
  건너뛸 수 있어요. 다음 정상 run이 소유 프로세스가 죽은 SyncMill 작업 공간을
  회수하지만, 복구 표시가 붙은 공간은 수동 점검을 위해 보존하고 PID 재사용 시에는
  나이 안전 한도까지 정리가 늦어질 수 있습니다. 기본값은 일주일이고
  `[workspace] stale_seconds`(또는 `SYNCMILL_WORKSPACE__STALE_SECONDS`, 최소 1시간)로
  조절합니다. 이 한도는 살아 있는 PID까지 무시하므로, 남은 작업 공간이 실제로 디스크를
  잡아먹을 때만 줄이세요. 너무 짧으면 실행 중인 run의 작업 공간을 지울 수 있습니다.
- 기존 `session start`는 예외예요. 원래 폴더에서 raw 대화형 에이전트를 띄우므로
  수정이 실제 로컬 파일에 바로 생깁니다. 코드 작업은 영속 격리 worktree를 쓰는
  `session open --workspace isolated`가 더 안전합니다.

### 첫 실제 실행 예시

처음에는 에이전트 하나만 강제로 지정하고 제한 시간을 짧게 잡으세요. 이렇게 하면
인증, subprocess 실행, worktree 생성, 결과 파싱, 메모리, 정리 흐름을 한 번에 확인하면서도
여러 에이전트를 동시에 쓰지 않습니다.

```bash
# 1. 전체 준비 상태 확인 (초기화는 앞의 "첫 실행"에서 이미 했으므로
#    다시 init을 실행하면 "already initialized"로 끝납니다)
uv run --project "$SYNCMILL_SRC" syncmill doctor

# 2. 사용할 CLI가 있는지, 직접 실행 가능한지 확인
command -v codex
codex --help

# 3. 실제 에이전트 하나만 실행
uv run --project "$SYNCMILL_SRC" syncmill run \
  "이 저장소를 살펴보고 위험이 낮은 개선점 하나를 요약해 줘. 파일은 수정하지 마." \
  --agent codex \
  --timeout 120
```

성공하면 출력은 대략 이런 모양이에요.

```text
[syncmill] Executing task 1a2b3c4d...
[syncmill] Agent: codex (forced, no fallback)

[codex] Completed in 42.1s

<에이전트 출력>
```

에이전트가 격리된 worktree에서 파일을 바꾸면 이런 줄도 나옵니다.

```text
[syncmill] Files changed: README.md, src/example.py
```

이 줄은 원래 checkout에 적용된 패치가 아니라, 우승한 worktree에서 생긴 변경 목록입니다.
성공 결과 아래에 표시된 run id로 diff를 검토하고 명시적으로 적용하세요.

```bash
uv run --project "$SYNCMILL_SRC" syncmill runs list
uv run --project "$SYNCMILL_SRC" syncmill runs show <run-id>    # 한 실행의 manifest
uv run --project "$SYNCMILL_SRC" syncmill diff <run-id>
uv run --project "$SYNCMILL_SRC" syncmill apply <run-id>
uv run --project "$SYNCMILL_SRC" syncmill discard <run-id>      # pending 실행을 의도적으로 폐기 표시
uv run --project "$SYNCMILL_SRC" syncmill runs prune --dry-run  # 오래된 terminal 실행 정리(pending은 항상 보호)
uv run --project "$SYNCMILL_SRC" syncmill recover [<run-id>]   # 크래시로 중단된 standalone apply를 조정(reconcile)
uv run --project "$SYNCMILL_SRC" syncmill recover --list        # 보존된 recovery 작업 공간 목록
uv run --project "$SYNCMILL_SRC" syncmill recover --discard <workspace>   # 회수하는 유일한 방법(이름은 --list에서)

# 같은 Claude/Codex/Kimi 세션에서 다음 요청을 이어가기
uv run --project "$SYNCMILL_SRC" syncmill continue <run-id> "이제 회귀 테스트도 추가해 줘"
```

`runs list`는 읽을 수 있는 run만 출력해요. 어떤 run 디렉터리의 manifest가 손상됐거나
거부되면 노란 stderr 안내로 그 이름과 이유를 알려 주고(stdout 행은 그대로 스크립트로
파싱 가능), `runs show <run-id>`로 확인할 수 있습니다. 자동으로 고치거나 지우지는 않아요.

`syncmill apply`(또는 분리된 `apply --detach` worker)가 apply 도중에 종료되면 런은
`applying` 상태로 남습니다. `syncmill recover`는 receipt·패치 digest·소스 fingerprint로
이를 분류해 조정합니다 — 완료된 apply는 마무리하고, 소스가 그대로면 `pending`으로
되돌리며, 판정이 애매하면 검토를 위해 `recovery_required`로 동결합니다. (저장소 apply
lock을 아직 쥐고 있는) 살아 있는 apply는 건드리지 않고, room apply는 `room recover`가
복구합니다. `apply`는 재시도 전에 자기 런에 대해 같은 조정을 수행하며, TUI Runs 탭에서는
`R` 키로 실행할 수 있습니다.

### 한 에이전트에서 다중 에이전트로 넓히기

하나가 잘 되는 걸 확인한 뒤 아래 순서로 넓혀 가는 편이 안전합니다.

```bash
# fallback: 성공하면 멈추므로 동시 전략보다 사용량을 아끼기 좋습니다.
uv run --project "$SYNCMILL_SRC" syncmill run "가장 작은 실패 테스트 하나를 고치고 설명해 줘" -s route

# 경쟁: 켜진 에이전트가 모두 실행되므로 병렬 사용량/quota를 예상하세요.
uv run --project "$SYNCMILL_SRC" syncmill run "CLI 오류 메시지 정리 방안을 제안해 줘" -s compete

# tmux와 실제 터미널이 있으면 동시 전략을 실시간으로 볼 수 있습니다.
uv run --project "$SYNCMILL_SRC" syncmill run "설정 검증 접근법 두 가지를 비교해 줘" -s compete --monitor
```

구현 작업을 맡길 때는 품질 게이트를 함께 쓰는 편이 좋습니다.

```toml
[gate]
commands = ["pytest -q"]
timeout_seconds = 300
```

그리고 실행합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "실패하는 테스트를 고쳐 줘" --agent codex --timeout 600
```

게이트는 에이전트의 worktree 안에서 실행됩니다. 통과했다는 것은 그 격리된 결과가
거기서 검증됐다는 뜻이고, 그래도 diff가 원래 checkout에 자동 적용되지는 않습니다.

### 실제 실행 문제 확인표

| 증상 | 확인할 것 |
|------|-----------|
| `CLI tool 'codex' not found in PATH` | syncmill을 실행하는 같은 shell에서 `command -v codex`를 확인하세요. |
| 에이전트가 인증 메시지와 함께 바로 실패 | 해당 CLI를 직접 열어서 로그인/인증을 끝내세요. |
| `uv run pytest`는 되는데 실제 에이전트 실행은 실패 | 테스트는 fake를 씁니다. 실제 CLI, 계정, 네트워크, quota를 확인하세요. |
| `Files changed`는 나오는데 내 폴더는 그대로 | 정상입니다. 출력된 run id로 `syncmill diff <run-id>`를 검토하고 `syncmill apply <run-id>`를 실행하세요. |
| 동시 실행이 비싸거나 너무 시끄러움 | 먼저 `--agent <id>`, 그다음 `route`, 마지막으로 `compete`/`council`/`decompose` 순서로 넓히세요. |

---

## 꼭 알아야 할 5가지

이 다섯 가지만 알면 syncmill을 잘 쓸 수 있어요.

1. **에이전트(agent)** — syncmill이 실행하는 AI 코딩 도구예요
   (`claude` / `codex` / `kimi-code`).

2. **우선순위(priority)** — 에이전트를 시도하는 기본 순서예요. 기본값은
   `claude` → `codex` → `kimi-code`이고, 바꿀 수 있어요.

3. **전략(strategy)** — 에이전트들을 *어떻게* 돌릴지 정하는 방식이에요. 모두 다섯
   가지가 있고, 기본값은 `route`입니다. (바로 다음 장에서 설명해요.)

4. **공유 메모리(shared memory)** — 에이전트들이 서로 직접 대화하지 않고도 협력할 수
   있게 해 주는 "공동 메모장"이에요. 작업 전에 읽어서 힌트를 주고, 작업 후에 결과를
   기록해 둬요. 기본 memtomem 백엔드는 성공 결과의 원본을 해당 에이전트의
   `agent-runtime:<id>`에 먼저 저장하고, 출처(`shared-from=<chunk-id>`)가 붙은 복사본을
   `shared`에 공유합니다. 다음 작업의 프롬프트에 붙일 기록은 `shared`에서 관련도 순으로
   찾습니다(개인 기록은 출처를 남기는 원본이고, 그 내용은 `shared`의 복사본으로 활용돼요).
   메모리는 **있으면 좋고, 없어도 작업은 진행돼요.**

5. **작업 공간(worktree)** — 각 에이전트가 일하는 임시 폴더예요
   (`~/.syncmill/workspaces/<run-id>/` 안에 만들어져요). **여러분의 실제 코드는
   안전합니다.** 결과 패치는 별도 런 저장소에 남고 `syncmill apply` 전에는 반영되지 않아요.

---

## 전략 고르기

`-s`(또는 `--strategy`)로 전략을 고를 수 있어요. 처음에는 기본값 `route`로 시작하고,
필요할 때 바꿔 보세요.

| 전략        | 한마디로                                          | 이럴 때 좋아요                                       |
|------------|--------------------------------------------------|---------------------------------------------------|
| `route`    | 순서대로 시도해서 **처음 성공한 것**을 씀 *(기본값)* | 그냥 누구든 빨리 해 주면 될 때. 가장 저렴해요.        |
| `pipeline` | 에이전트를 **순서대로 이어서** 작업하게 함          | "구현 → 테스트 → 문서화"처럼 단계가 나뉘는 작업.      |
| `compete`  | **모두 동시에** 돌린 뒤 설정한 선택 정책으로 우승자를 고름 | 같은 일을 여러 에이전트가 동시에 시도하고 명시적인 기준으로 하나를 고르고 싶을 때.  |
| `council`  | **제안 → 비평 → 종합** 으로 서로 검토하며 다듬음    | 서로의 아이디어를 검토하면 좋은 어려운 설계 문제.    |
| `decompose`| 작업을 **여러 조각으로 나눠** 동시에 하고 합침      | 독립적인 조각으로 깔끔히 나뉘는 큰 작업.            |

조금 더 풀어서 설명하면 이래요.

- **`route` (라우트)** — `claude`부터 시도하고, 실패하면 `codex`, 또 실패하면
  `kimi-code`. 빠르고 저렴해요(성공하면 바로 멈추니까요).
- **`pipeline` (파이프라인)** — 같은 작업 폴더에서 에이전트가 차례대로 작업해요. 뒤
  에이전트가 앞 에이전트의 결과를 이어받아요. 중간에 하나가 실패하면 거기서 멈추고
  어디서 멈췄는지 알려 줘요.
- **`compete` (경쟁)** — 모든 에이전트가 각자 폴더에서 **동시에** 같은 작업을 해요.
  기본 `verified` 정책은 게이트를 통과한 완료 후보를 고정 우선순위 순으로 골라요
  (빨리 끝낸 순서가 아니라요). `judge`는 검증된 낮은 우선순위 후보를 고를 수도 있어요.
- **`council` (협의)** — 모두가 먼저 안을 내고, 정해진 횟수만큼 서로의 안을 보며
  다듬은 뒤, 최종안을 만들어 그중 하나를 골라요.
- **`decompose` (분해)** — 우선순위가 가장 높은 에이전트가 작업을 작은 조각들로 나누고,
  그 조각들을 여러 에이전트가 나눠서 동시에 처리한 뒤, 결과를 하나로 합쳐요.

Pipeline 단계, Council 제안·비평, Decompose의 실제 실행 계획·하위 작업에는 각각
`pipeline-stage`, `council-proposal`, `council-critique`, `decompose-plan`,
`decompose-subtask` 종류 태그가 붙습니다. 따라서 다음 단계는 개인 기록과 `shared`를
통합 검색하면서 필요한 작업 산출물을 관련도 순으로 찾고, 고급 사용자는
`tag_filter`로 특정 종류만 조회할 수 있습니다. Decompose 계획은 원래 응답이 아니라
검증·정규화·개수 제한을 거쳐 **실제로 실행된 계획**만 공유됩니다.

> `compete` · `council` · `decompose`는 동시에 실행되기 때문에,
> [`--monitor`](#실시간으로-지켜보기---monitor)로 진행 상황을 실시간으로 볼 수 있어요.

`--selection verified`(기본값)는 완료 후보를 고정 우선순위 순으로 확인해 처음 게이트를
통과한 후보를 고릅니다. `priority`는 우선순위가 가장 높은 완료 후보 하나만 검사하므로
그 후보가 게이트에 실패하면 수렴도 실패합니다. `judge`는 모든 완료 후보를 검증한 뒤
에이전트가 순위를 정하고, 판정 실패 시 verified 우선순위로 돌아갑니다. 이 옵션은
`route`와 `pipeline`에는 영향을 주지 않습니다.

---

## millflow 워크플로우

하나의 목표를 여러 의존 작업으로 나눠 여러 에이전트가 맡아야 한다면 네이티브
워크플로우를 사용하세요. 계획과 실행은 반드시 두 단계로 나뉩니다.

```bash
# 1. 계획만 생성합니다. 아직 노드는 실행되지 않고 원본 소스도 바뀌지 않습니다.
uv run --project "$SYNCMILL_SRC" syncmill workflow plan \
  "인증 모듈을 개선하고 호출부를 마이그레이션한 뒤 회귀 테스트를 추가해 줘"

# 같은 planning-only 단축형입니다. 접두사는 소문자 `millflow:`와 정확히 일치해야 합니다.
uv run --project "$SYNCMILL_SRC" syncmill run \
  "millflow: 인증 모듈을 개선하고 호출부를 마이그레이션한 뒤 회귀 테스트를 추가해 줘"

# 2. 저장된 DAG와 제한을 검토합니다.
uv run --project "$SYNCMILL_SRC" syncmill workflow show <workflow-id>

# 3. 별도 명령으로 실행을 명시 승인합니다. POSIX의 foreground 실행도
# detached 작업과 같은 durable manager가 소유합니다.
uv run --project "$SYNCMILL_SRC" syncmill workflow run <workflow-id>

# 또는 분리 실행하고 다른 셸이나 TUI Runs 탭에서 감독합니다.
uv run --project "$SYNCMILL_SRC" syncmill workflow run <workflow-id> --detach
uv run --project "$SYNCMILL_SRC" syncmill workflow status <workflow-id> --watch
uv run --project "$SYNCMILL_SRC" syncmill workflow logs <workflow-id> --follow
```

기본 planner는 Claude이며 Codex도 선택할 수 있습니다. planner는 일회용 스냅샷에서
읽기 전용으로 동작하고, 응답은 엄격한 JSON DAG로 검증됩니다. 알 수 없는 필드,
순환·단절 노드, 사용할 수 없는 에이전트, 호출 수·깊이 제한을 넘는 계획은 거부됩니다.
에이전트가 만든 Python이나 셸 스크립트를 워크플로우 제어 코드로 실행하지 않습니다.

준비된 노드는 서로 다른 worktree에서 기본 최대 4개까지 병렬로 실행됩니다. 이전
노드에서는 텍스트 결과만 신뢰하지 않는 데이터로 표시해 전달합니다. 최종 노드는 원본
스냅샷의 새 worktree에서 전체 결과를 다시 구성하므로 중간 패치를 합치지 않습니다.
중간 패치는 진단용으로만 보존되고 `syncmill apply` 대상이 되지 않습니다. 최종 노드가 성공하면(품질 게이트를 설정했다면 그 게이트까지 통과하면) 표준 run
아티팩트 하나가 만들어지며, 평소처럼 `diff`로 검토한 뒤 명시적으로 `apply`합니다.
아티팩트는 항상 검토할 수 있고, 최종 노드가 실제로 무언가를 바꿨을 때만 적용할 수
있습니다. 바뀐 것이 없으면 `no_changes`로 기록됩니다. 계획 이후 원본 fingerprint가 바뀌면 실행을 거부합니다.

```bash
syncmill workflow list
syncmill workflow status <workflow-id> --watch
syncmill workflow logs <workflow-id> --node <node-id> --follow
syncmill workflow resume <workflow-id> [--detach] [--team <name>]
syncmill workflow cancel <workflow-id>           # 안전한 process-group 중단
syncmill workflow cancel <workflow-id> --force   # 10초 유예 뒤 강제 중단
syncmill workflow prune --dry-run
```

한 워크플로우는 한 lease owner만 실행합니다. 죽은 로컬 소유자는 즉시 회수할 수
있으며 살아 있는 로컬 소유자는 시간이 지나도 회수하지 않습니다. 원격 소유자는
하트비트 만료(기본 1800초)로 판단하고, 소유자·하트비트 증거가 없는 기존 claim은
명시적으로 취소해야 합니다. `status`나 `resume`이 복구 상태를 확인하며 별도 백그라운드
타이머는 없습니다. 재개해도 중단 시간을 포함한 최초 절대 deadline과 누적 호출·사용량을
유지합니다. 결과는 정확한 lease·시도별 불변 경로에 저장하고 두 파일의 기록과 hash 검증을
완료한 뒤 commit marker로 확정합니다.

새 manifest는 schema 3입니다. planner 포함 표시가 있는 schema 1 호출 횟수만 정확히
변환합니다. 표시 없는 schema 1과 기존 schema 2는 조회·취소할 수 있지만 실행하려면
다시 계획해야 합니다. 저장소를 열 때 실행 중인 기존 소유자의 기록을 변경하지 않습니다.
SyncMill 지원 플랫폼은 macOS와 Linux이며 detached job과 프로세스 그룹 제어는 POSIX를 사용합니다.

workflow planner/worker는 기본 OS 변수와 자기 provider 인증 변수만 받습니다. 추가
cloud/proxy 자격증명은 agent별 `workflow_env_allowlist`에 정확한 변수명을 등록해야 하며
wildcard는 거부됩니다. 이 정책은 ambient secret 노출을 줄이지만 완전한 filesystem/network
sandbox를 의미하지는 않습니다.

기본 안전 예산은 planning 포함 provider 호출 32회, DAG 깊이 6, 동시 실행 4개,
누적 60분 deadline, 2 MiB prompt, attempt당 32 MiB patch, 변경 파일 512개와 private
rolling log입니다. `[workflow]`, agent별 설정 또는 `SYNCMILL_...` 환경 변수로 조정합니다.

실행은 워크플로를 점유(claim)하고 작업하는 동안 그 점유를 갱신합니다. 소유 프로세스가
죽으면 `resume`이 그 점유를 인수할 수 있습니다. 같은 머신의 소유자는 프로세스 id로
확인하므로 죽었다면 즉시 회수할 수 있고, 다른 머신의 소유자는
`workflow.lease_timeout_seconds` 동안 갱신이 없을 때만 죽은 것으로 간주합니다.
살아 있는 소유자는 노드가 아무리 오래 걸려도 인수하지 않으므로, 끝난 것이 확실한
점유는 `workflow cancel`로 해제하세요.

Toolgraph와 `control_plan_enabled`를 함께 켜면 planning 시 본문 없는 control
projection도 워크플로우 옆에 동결합니다. 각 `run`/`resume`에는 새로운 `wfx-...`
실행 ID가 생기며, worktree를 만들기 전에 저장된 바이트 그대로 사전 검사합니다.
Toolgraph identity mapping이나 candidate-tool surface가 달라졌다면 새로 계획해야
합니다. `workflow show`에는 최근 preflight, 정지 사유, trace receipt가 표시되고,
증거는 `<workflow>/executions/<execution-id>/` 아래에 저장됩니다. resume에서 재사용한
결과는 서로 다른 실행의 span을 억지로 잇지 않고 현재 trace의 checkpoint-read span으로
표현합니다. SQLite checkpoint는 애플리케이션 수준 복구이며, 외부 부작용에 대한
인프라 수준 exactly-once 실행을 보장하지는 않습니다.

---

## 자주 쓰는 명령어

자주 쓰는 명령에는 `run`, `agents`, `status`, `serve`, `dashboard`, `tui`, 그리고
`workflow` / `session` / `board` / `team` 그룹이 있어요. 전체 명령의 정확한 정본은 Click 도움말입니다.
언제든 `uv run --project "$SYNCMILL_SRC" syncmill --help` 또는
`uv run --project "$SYNCMILL_SRC" syncmill <명령> --help`로 도움말을 볼 수 있고, `uv run --project "$SYNCMILL_SRC" syncmill --version`으로
버전을 확인할 수 있어요. (`team`은 [팀 프로필](#한-단계-더-선택-기능)에서, `board`와
`dashboard`는 [워크보드](#워크보드로-팀처럼-일하기)에서 자세히 설명해요.)

### `run` — 작업 실행하기

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "<할 일>" [옵션]
```

자주 쓰는 옵션은 이 정도예요.

| 옵션           | 짧게  | 설명                                                         |
|---------------|-------|-------------------------------------------------------------|
| `--agent`     | `-a`  | 에이전트 하나만 지정 (다음으로 안 넘어감).                     |
| `--exclude`   | `-x`  | 특정 에이전트 제외 (여러 번 쓸 수 있어요).                     |
| `--strategy`  | `-s`  | 전략 고르기 (`route`/`pipeline`/`compete`/`council`/`decompose`). |
| `--timeout`   | `-t`  | 에이전트별 제한 시간(초)을 바꿔요.                            |
| `--repo`      | `-r`  | 대상 저장소 폴더 (지정 안 하면 설정의 repo_root, 없으면 현재 폴더). |
| `--capability`| `-c`  | 능력 힌트 (여러 번 가능). 잘 맞는 에이전트를 앞으로 보내요.    |
| `--team`      |       | 이름 붙인 팀 프로필(`[teams.<이름>]`)로 실행해요.              |
| `--monitor`   |       | 동시 실행을 tmux 화면으로 실시간 보기.                        |
| `--emit-patch`|       | 보존된 우승자 패치를 지정한 파일에도 복사해요.                 |
| `--dry-run`   |       | 에이전트를 시작하지 않고 제한된 실행 계획만 검증·출력해요.     |
| `--selection` |       | 동시 전략 선택 정책: `verified`(기본), `priority`, `judge`.   |
| `--no-continue`|      | 이번 run의 provider-native 세션 이어받기를 끕니다.             |
| `--difficulty`|       | `low`/`medium`/`high` 모델 라우팅 또는 `auto` 관찰 분류.       |
| `--prompt-file`|      | 파일(또는 `-`=stdin)에서 프롬프트를 읽어요 — 멀티라인도 따옴표 없이. |

> 💡 작업 내용이 `-`(대시)로 시작하면 앞에 `--`를 붙이세요:
> `uv run --project "$SYNCMILL_SRC" syncmill run -- "--fix 부분을 고쳐 줘"`
> 여러 줄 프롬프트는 `--prompt-file task.md`(또는 stdin은 `--prompt-file -`)가
> 편해요. 위치 인자 프롬프트와는 동시에 쓸 수 없고, `room create`/`room round`도
> 같은 플래그를 받아요.

`apply --detach`, `room round --detach` 같은 분리 백그라운드 worker는 POSIX의
프로세스·파일 잠금 기능이 필요합니다. 비 POSIX 환경에서는 foreground 실행을 사용하세요.

### `agents` — 에이전트 목록 보기

```bash
uv run --project "$SYNCMILL_SRC" syncmill agents
```

우선순위 순서로 에이전트 목록과 켜짐/꺼짐 상태, 제한 시간, 최대 턴 수(max turns)를 보여 줘요.

### `strategies` — 오케스트레이션 전략 목록

```bash
uv run --project "$SYNCMILL_SRC" syncmill strategies
```

실행을 시작하지 않고 사용 가능한 전략을 출력합니다. `--strategy`가 받는 값입니다.

### `report list` / `report show` — 기록된 run envelope

```bash
uv run --project "$SYNCMILL_SRC" syncmill report list
uv run --project "$SYNCMILL_SRC" syncmill report show <run-id>
```

`[integration.report] runs_dir`에 대한 읽기 전용 뷰어입니다. envelope 기록이
비활성화된 상태에서도 동작하므로, 기록을 끈 뒤에도 이전 기록을 계속 읽을 수 있습니다.

### `status` — 메모리 상태 확인

```bash
uv run --project "$SYNCMILL_SRC" syncmill status
```

선택한 메모리 백엔드의 연결 상태와 안전하게 표시할 수 있는 저장소 정보를 보여 줘요.

### `serve` — 메모리를 다른 도구에 읽기 전용으로 열어 주기

syncmill이 쌓아 둔 공유 지식(그리고 [워크보드](#워크보드로-팀처럼-일하기)를 켰다면
보드 상태까지)을 **다른 MCP 클라이언트**(Claude Code/Desktop, Cursor 등)가 **읽을 수
있게** 열어 줘요. 도구는 여섯 개 — 공유 지식용 `shared_search` / `shared_read` /
`shared_stats`, 워크보드용 `board_list` / `board_get` / `board_stats` — 전부 읽기
전용이라 안전합니다(쓰기 도구는 없어요). 호출 중 backend가 실패하면
`shared memory is currently unavailable`(보드는 그에 해당하는 문구)로 답하고, 단순히
없는 chunk/항목은 그대로 `not found`로 답해 둘을 구분합니다.

```bash
# 기본(stdio) — 예: Claude Code에 등록하기
claude mcp add syncmill-shared -- uv run --project "$SYNCMILL_SRC" syncmill serve

# HTTP로 열기 (기본은 내 컴퓨터에서만, 포트 8000)
uv run --project "$SYNCMILL_SRC" syncmill serve --http
uv run --project "$SYNCMILL_SRC" syncmill serve --http --port 8765   # 포트 바꾸기 (--host로 바인드 주소도 변경)
```

> ⚠️ HTTP로 열면 **인증이 없어요.** 내 컴퓨터(loopback)에서만 쓰거나, 믿을 수 있는
> 네트워크/인증 프록시 뒤에서만 쓰세요.

### `session` — 대화형 세션 오래 유지하기

에이전트의 **대화형 모드**(직접 타이핑하는 그 화면)를 tmux 세션 안에서 띄워 줘요.
이렇게 하면 터미널을 닫거나 SSH가 끊겨도 **세션이 살아 있어서 이름으로 다시 붙을 수**
있어요. (tmux가 설치돼 있어야 하고, `start`/`attach`는 실제 터미널이 필요해요.)

```bash
uv run --project "$SYNCMILL_SRC" syncmill session start [에이전트]   # 시작(또는 재연결) 후 화면 붙기
uv run --project "$SYNCMILL_SRC" syncmill session list              # 살아 있는 세션 목록
uv run --project "$SYNCMILL_SRC" syncmill session attach 대상        # 다시 연결하기
uv run --project "$SYNCMILL_SRC" syncmill session stop 대상          # 하나 종료
uv run --project "$SYNCMILL_SRC" syncmill session stop --all        # 전부 종료
```

화면에서 빠져나올 때는 **`Ctrl-b` 누른 뒤 `d`** 를 누르세요. 에이전트는 계속 돌아가고,
`session attach`로 다시 붙으면 같은 세션이 그대로 있어요.

코드를 수정할 때는 격리 세션을 권장합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill session open codex --workspace isolated
uv run --project "$SYNCMILL_SRC" syncmill session isolated-list
uv run --project "$SYNCMILL_SRC" syncmill session attach-isolated <세션-id>
uv run --project "$SYNCMILL_SRC" syncmill session checkpoint <세션-id>
uv run --project "$SYNCMILL_SRC" syncmill session close <세션-id>
```

worktree는 터미널을 떼었다 다시 붙어도 유지됩니다. 붙기 전에는 tmux가 기록된
worktree에 있는지 확인합니다. `checkpoint`는 현재 diff를 `runs list`, `diff`, `apply`로
검토할 수 있는 표준 불변 실행 아티팩트로 내보내고, `close`는 격리 폴더를 정리합니다.
기존 원본 폴더 방식이 꼭 필요하면 `--workspace direct`를 명시하세요.

### `room` — 여러 CLI 후보를 비교하고 하나만 적용하기

Room은 서로 다른 provider CLI를 영속 격리 worktree에서 동시에 실행하고 검토 지점에서
멈춥니다. 한 라운드가 모든 멤버의 quota/요금을 사용할 수 있으므로 명시적으로 켜야 해요.

```bash
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room create "인증 리팩터링" -a claude -a codex
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room round <room-id> --detach
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room candidates <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room compare <room-id> [--round N | --all-rounds] [--json]
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room diff <후보-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room select <room-id> <후보-id> [--expected-revision N] [--expected-digest SHA256] [--rationale TEXT | --rationale-file PATH]
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room apply <room-id> --verify [--expected-revision N]
# worker가 죽고 lease가 만료된 뒤, 또는 apply가 중단된 뒤
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room recover <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room prune --dry-run
# 상태만 확인(변경 없음)
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room list
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room show <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room rounds <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room attach <room-id> [--round N]
# 아래는 조회가 아닙니다 — 방을 종료하고 멤버 작업 공간을 회수합니다
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room close <room-id>
```

`room compare <room-id>`는 한 라운드의 후보들을 기록된 증거 기준으로 나란히 보여 줍니다.
스스로 기록하는 것은 없습니다(선택·이벤트·상태 변경 없음). 다만 주변 명령에서 두 가지를
그대로 물려받습니다. 다른 `room` 명령과 똑같이 room store를 열고(필요하면 생성하거나
schema를 올림), 원본 확인은 `room apply`와 같은 git 지문 계산을 쓰므로 `git status`와
마찬가지로 git index의 파일 stat 캐시를 갱신할 수 있습니다. 파일 내용, stage된 변경, HEAD는
건드리지 않습니다. 후보마다 provider와 상태, gate 판정과 각 검사 결과(`passed` /
`failed`, 에이전트가 끝나지 않았으면 `not run`, 기록된 보고서가 없으면 `no gate report` —
보고서가 없다고 통과로 표시하지 않습니다), 소요 시간, 보고된 사용량(provider가 보내지
않았으면 0이 아니라 `not reported`), binary·large 표시가 붙은 patch 크기, 적용 가능 여부와
그 이유, 그리고 지금 보이는 적용 차단 사유(방 생성 이후 원본 변경, 읽을 수 없는 아티팩트,
선택할 수 없는 방 상태)를 보여 줍니다. 변경 파일은 모든 후보가 공통으로 바꾼 파일과 한
후보만 바꾼 파일로 나누고, 후보마다 `room diff` 명령을 함께 적습니다. 같은 라운드의 후보는
원본과 gate 설정이 같으므로 기본값은 최신 라운드이고, `--round N`으로 다른 라운드를,
`--all-rounds`로 라운드 간 gate 요약까지 볼 수 있습니다. 1라운드는 항상 방의 스냅샷에서
시작하지만 그 이후 라운드의 lineage는 기록되지 않으므로, 추측하지 않고 `not recorded`로
표시합니다. 손상된 후보는 조용히 빠지지 않고 `unreadable`로 남습니다. `--json`은 같은
read model(`schema_version: 1`)을 프롬프트·출력·patch·gate 출력 본문 없이 출력합니다.
예외는 아래에서 설명할 선택 사유(rationale)뿐이며, 이것은 사용자가 직접 쓴 글입니다.
차단 사유가 비어 있다고 보장되는 것은 아닙니다. `room select`와 `room apply`가 각각
아티팩트를 다시 확인합니다.

`room select`는 선택하기 전에 후보 아티팩트 전체를 읽고, patch나 output이 기록된 digest와 다르거나
manifest가 후보 기록과 맞지 않으면 선택을 거부합니다. 이 확인은 그 순간의 바이트에 대한
것이고, `room apply`가 다시 확인한 뒤 확인한 바이트만 적용합니다. 선택적 플래그 두 개로
검토한 내용에 선택을 묶을 수 있습니다. `--expected-revision N`은 방이 revision `N` 이후에
바뀌었으면 거부하고, `--expected-digest SHA256`은 후보의 patch digest가 정확히 그 값이
아니면 거부합니다. 두 값은 `room compare --json`에 있습니다(`room.revision`, 후보마다
`patch.sha256`). 플래그 없이 선택하면 지금 방에 있는 상태를 기준으로 선택하고, 기록에는
확인한 revision이 없다고 남습니다. `--rationale`(또는 `--rationale-file`)은 이 후보를 고른
이유를 최대 2000 UTF-8 바이트까지 기록합니다. 줄바꿈과 탭이 아닌 제어 문자는 지우지 않고
거부합니다(CRLF 줄바꿈 파일은 carriage return 때문에 거부됩니다). 선택 기록(후보, 두 digest, 확인한 revision과 선택 후 revision, 사유, CLI/TUI 구분,
시각)은 `room compare`와 TUI에 표시됩니다. 다시 선택하면 기록이 교체되고, 모든 선택은
`room show`의 이벤트 기록에 남습니다. 기록이 선택된 후보와 더 이상 맞지 않으면 compare는
`INCONSISTENT`로 표시하고, 다시 선택할 때까지 `room apply`가 거부합니다. 이 기록이 생기기
전에 선택한 방은 `not recorded`로 표시되며 이전처럼 적용됩니다. `room apply
--expected-revision N`도 같은 방식으로 apply를 묶습니다. 이 기록은 syncmill 자체 파일의
일관성 확인이지, 누군가 바이트를 검토했다는 증명이 아닙니다. 방 데이터베이스를 고쳐 쓸 수
있는 사람은 기록도 고쳐 쓸 수 있습니다.

성공뿐 아니라 실패·시간 초과·부분 patch·제한된 출력도 모두 보존합니다. 후보 선택만으로
코드가 반영되지는 않습니다. 적용할 때 원본 변경이나 후보 digest 불일치를 발견하면
중단하고, 검증을 통과하면 기존 표준 실행 아티팩트 적용 경로를 재사용합니다. 다음
라운드는 기본적으로 각 CLI의 worktree와 세션을 이어 가며(`continue-own`), `fresh` 또는
`branch-from --base-candidate <id>`도 선택할 수 있습니다. 첫 버전은 서로 다른 provider
최대 3개를 지원하며, 같은 CLI 여러 인스턴스는 아직 지원하지 않습니다.

각 멤버의 provider 네이티브 대화는 라운드를 넘어 유지됩니다(`claude --resume` /
`codex exec resume` / `kimi --session`). provider 쪽 프롬프트 캐시가 따뜻하게
유지되고, resume되는 멤버에게는 전체 체크포인트 대신 *델타* — 동료들의 새 출력만 —
전송됩니다. 세션 연속성은 lineage를 따릅니다: `continue-own`과 `branch-from`은 각
멤버의 대화를 이어 가고(branch-from은 트리만 교체), `fresh`는 콜드 스타트하며 저장된
세션 id도 지웁니다. 설정에서 세션 이어받기를 끄면(`[conversations] enabled=false`)
resume 자체가 없으므로 모든 멤버가 델타가 아니라 전체 체크포인트를 받습니다.
`room round --fresh-session`은 저장된 세션 id를 버리고 새 대화로 시작하며,
`room round --fork`(claude 멤버 전용)는 대화를 이어 가는 대신 fork합니다. 세션 id는
크래시와 `room recover`를 거쳐도 살아남습니다. provider가 토큰 사용량을 보고하면
`room candidates`와 TUI 후보 화면에 `cache 85% (34.2k/40.1k)`처럼 캐시 적중률이
표시됩니다.

라운드마다 새 프롬프트를 줄 수 있어요(`room round <id> "다음 지시"`, 멀티라인은
`--prompt-file`/`-`). 생략하면 방 생성 프롬프트를 재사용하는데 이제 **노란 경고**가
보이고, 실행된 프롬프트 첫 줄이 에코됩니다. `room rounds <id>`는 라운드별 프롬프트
목록을 보여 주고, TUI 방 상세에는 생성 프롬프트가, 후보 Details에는 그 라운드의
프롬프트가 표시돼요. TUI의 `x`는 이제 라운드 모달(직전 라운드 프롬프트로 미리 채워진
입력 + lineage + base 후보)을 엽니다. 라운드 프롬프트에는 `run`/`board`와 같은
베스트-에포트 공유 메모리 컨텍스트가 주입되고(메모리가 고장나도 라운드는 계속),
**적용된** 후보는 `room-result` 태그로 공유 메모리에 기록되어 이후 실행이 참조할 수
있어요.
분리 worker가 죽었다면 lease 만료 후 `room recover`를 실행하세요. 로컬 worker PID가
사라진 경우에만 복구 상태로 옮깁니다. apply 프로세스가 중단되어 Room이
`applying`에 남은 경우에도 같은 명령이 살아 있는 owner인지 확인한 뒤 지속 실행
아티팩트를 보존하며 명시적 복구 상태로 전환합니다. worktree와 후보는 삭제하지 않습니다.
`room prune`은 종료 상태에만 보존 개수·기간 설정을 적용합니다. 먼저 `--dry-run`으로
삭제 대상 id를 확인하세요.

### `board` — 작업을 보드에 쌓아 두고 하나씩 처리하기

여러 작업을 **워크보드**에 등록해 두고, syncmill이 하나씩 꺼내서 처리하게 하는
기능이에요. 기본적으로 꺼져 있어서 `SYNCMILL_BOARD__ENABLED=true`를 먼저 켜야 해요.
자세한 개념과 예시는 [워크보드로 팀처럼 일하기](#워크보드로-팀처럼-일하기)에 있어요.

```bash
uv run --project "$SYNCMILL_SRC" syncmill board add "<할 일>" --goal <목표>   # 작업 등록
uv run --project "$SYNCMILL_SRC" syncmill board import-review-candidates <후보.json>  # Tracegraph 검토 후보 가져오기
uv run --project "$SYNCMILL_SRC" syncmill board list                          # 목록 보기
uv run --project "$SYNCMILL_SRC" syncmill board run --loop                    # 처리 가능한 작업을 차례로 실행
uv run --project "$SYNCMILL_SRC" syncmill board show <id>                     # 하나를 자세히 (증거·이력 포함)
uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --sign-off   # 사람이 확인하고 완료 처리
uv run --project "$SYNCMILL_SRC" syncmill board decide "<결정 내용>"           # 팀 결정 기록 (매번 다시 논쟁하지 않기)
uv run --project "$SYNCMILL_SRC" syncmill board stats                         # 팀 건강 지표
uv run --project "$SYNCMILL_SRC" syncmill board compact [--older-than <초>] [--dry-run]  # 감사 로그 정리(수동 전용)
```

### `dashboard` — 보드를 실시간 화면으로 보기

워크보드를 **실시간 터미널 화면(TUI)** 으로 보여 줘요. 목록·상세·통계와
저장된 실행 보고서를 보여 주고, 키 하나로 사람 확인(검토→완료 승격, 막힘 해제 등)도
할 수 있어요.
보드가 켜져 있어야 하고, 실제 터미널에서 실행해야 해요.

```bash
uv run --project "$SYNCMILL_SRC" syncmill dashboard                 # 2초마다 갱신
uv run --project "$SYNCMILL_SRC" syncmill dashboard --refresh 0.5   # 더 자주 (최소 0.5초)
```

| 키  | 하는 일                                            |
|-----|---------------------------------------------------|
| `r` | 지금 바로 새로 고침                                 |
| `f` | 상태 필터 바꾸기                                    |
| `e` | 선택한 작업 수정 (`board update`와 같은 규칙)        |
| `u` | 막힌(blocked) 작업 풀기                             |
| `h` | 사람 확인 필요 표시 지우기                          |
| `s` | 통계 화면 (`board stats`)                           |
| `t` | 팀 프로필 패널                                      |
| `p` | 실행 보고서 패널(최신순, 패널 안에서 `r`로 새로 고침) |
| `q` | 끝내기                                              |

보고서 기록이 현재 꺼져 있어도 `[integration.report].runs_dir`에 기존 보고서가
있으면 볼 수 있어요. 선택한 상세 내용은 `report show`와 동일해요.

### `tui` — 보드 전체 작업 화면

`syncmill tui`는 **Board / Runs / Reports / Teams / Status / Sessions / Rooms** 일곱 탭을 제공하는
별도 화면이에요. `dashboard`와 달리 보드가 꺼져 있거나 손상돼도 제한 모드로 열리고,
Board 탭에는 해결 방법을 보여 주면서 나머지 탭은 계속 사용할 수 있어요.
탐색과 편집은 non-POSIX에서도 사용할 수 있지만, 분리 실행 `x`/`g`, 재연결, Runs의
`k`/`K` 제어는 POSIX `flock`, Unix socket, process-group signal이 필요합니다.
non-POSIX에서는 `syncmill board run`을 직접 사용하세요.

각 탭의 왼쪽·오른쪽·하단 정보, 모든 키, 빈 화면과 `LIMITED`/`DEGRADED`, 확인창과
화면 전환은 실제 120×40 화면을 포함한
**[TUI 화면 안내서](tui-guide.ko.md)**에서 하나씩 설명합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill tui                 # 기본 2초 갱신
uv run --project "$SYNCMILL_SRC" syncmill tui --refresh 0.5   # 최소 0.5초
uv run --project "$SYNCMILL_SRC" syncmill tui --tab rooms     # 특정 탭으로 바로 열기
```

`--tab auto`(기본값)는 **Rooms → Board → Runs → Status** 순서로, 기능이 켜져 있고
저장소가 실제로 열린 첫 탭에서 시작해요. 명시적으로 지정한 `--tab`은 항상 존중되며,
사용할 수 없는 탭이라면 해당 탭의 제한 모드 안내가 그대로 표시됩니다.

`1`~`7`로 탭을 바꾸면 그 탭의 목록에 포커스가 가고, 하단 액션 바가 현재 탭에서
실제로 동작하는 키만 보여 줘요(공통 키 `r`/`?`/`q`는 항상 표시). Board 탭에서는
항목 추가·후보 가져오기·수정·결정 관리와 선택 항목 실행(`x`), 목표별/전체 큐
실행(`g`)을 할 수 있어요. 시작 전 실제 에이전트의 비용·quota 확인을 반드시
거칩니다. 선택 실행은 dependency나 human-required 큐 조건을 우회할 수 있어서
확인창에서 다시 경고합니다.

Runs 탭은 `v`로 전환하는 두 하위 화면이 있어요: **Operations**(기존의 분리 실행
작업 목록)와 **Artifacts**(`syncmill runs list`가 보여 주는 durable run 아티팩트).
Artifacts에서 `d`는 요약↔diff 전환, `g`는 pending run을 durable 분리 워커로
적용(CLI `apply --detach`와 같은 확인·fencing), `k`는 pending run 폐기예요.
Sessions 탭에서 체크포인트(`c`)가 성공하면 이제 Runs → Artifacts로 이동해 새
run이 선택된 상태로 바로 검토할 수 있습니다. 읽을 수 없는 run 디렉터리는 숨기지
않고 상태 줄에 `DEGRADED: N unreadable`로 세며, 읽을 수 있는 run이 하나도 없으면
이름과 이유를 화면에 나열합니다.

Rooms의 `a`는 prompt와 member만 기록하며 provider를 호출하지 않습니다. 실제 비용은
`x`로 round를 시작할 때 member별로 발생합니다. 선택한 후보는 세 가지 화면으로
볼 수 있어요: `d` **Diff**(digest에
묶인 patch를 페이지 단위로 — 잘린 patch에서 `d`를 다시 누르면 더 불러오고, 바이너리
내용은 표시), `o` **Output**(상태·오류·게이트를 먼저, 그 뒤 제한된 provider 출력),
`m` **Details**(라운드·attempt·소요 시간·exit code·게이트 체크별 결과·파일·digest·
잘림 상태). 완료된 후보는 Diff로, 실패한 후보는 Output으로 열려요. `R`은
`room recover`로 **프로젝트 전체**의 죽은 room 워커/중단된 apply를 복구하고,
`k`는 선택한 room을
닫습니다(후보와 영수증은 보존).

실행은 Textual이 아니라 분리된 백그라운드 매니저가 맡아요. `q`는 화면만 닫고 실행은
계속합니다. 다시 `syncmill tui`를 열면 실행 상태와 로컬 rolling 로그에 재연결돼요.
Runs 탭의 `k`는 정리를 거치는 SIGINT 중단이고, 10초 뒤에도 끝나지 않을 때만 별도
확인 후 `K` 강제 종료를 쓸 수 있어요. 강제 종료는 보드 lease나 수정된 격리 worktree를
남길 수 있으므로 나중에 직접 확인해야 합니다.

기본 상태 폴더는 `~/.syncmill/tui`, 완료 작업 보존 수는 20개, 작업별 로그 한도는
10 MiB예요. `[tui] state_dir`, `keep_jobs`, `log_max_bytes` 또는 대응하는
`SYNCMILL_TUI__*` 환경 변수로 바꿀 수 있어요. 로그에는 에이전트 출력이 들어가므로
민감한 로컬 데이터로 취급하세요.

---

## 설정하기

설정은 세 군데에서 읽어 오고, **위에 있을수록 우선**이에요.

1. **환경 변수 `SYNCMILL_*`** (가장 우선)
2. **프로젝트의 `syncmill.toml` 파일** (`--repo`를 받는 명령은 대상 저장소의 것,
   그 외에는 실행 위치의 것)
3. **기본값** (가장 낮음)

즉 **환경 변수가 파일을 이기고, 파일이 기본값을 이겨요.** 그리고 `--repo` /
`--strategy` / `--timeout` 같은 명령줄 옵션은 이 셋보다 더 우선합니다(그때 한 번만).

> 💡 `--repo`는 명령에 따라 두 가지 뜻입니다. `run`에게는 **작업할 저장소**를
> 가리켜서, 그 프로젝트의 `syncmill.toml`을 읽고 `repo_root`를 넘긴 경로로
> 고정합니다(그래서 export된 `SYNCMILL_REPO_ROOT`보다 플래그가 우선). 프로젝트
> 명령(`diff`, `apply`, `runs *`, `room *`, `session open`, `doctor` 등)에게는
> **어느 프로젝트의 설정을 읽을지**를 가리키며, 그 설정의 `repo_root`는 평소대로
> 해석되어 **다른 체크아웃**을 가리킬 수도 있습니다 — 한 프로젝트가 다른 저장소를
> 향해 작업하게 만드는 구성이 이걸로 동작합니다. 둘 다 경로(기본값은 현재 폴더)의
> Git 루트 기준으로 해석하므로 하위 폴더에서 실행해도 됩니다. 대상을 받지 않는
> 명령(`agents`, `team list`, `report list`, `board *` 등)은 실행 위치의
> `syncmill.toml`만 읽고 상위로 올라가지 않습니다. 해당 파일의 문법이나
> project-state 경로가 잘못됐다면 현재 폴더 설정으로 조용히 대체하지 않고 오류를
> 보여 줍니다.
>
> 예외는 레거시 `session start --repo` 하나입니다. 여기서 경로는 대화형 에이전트의
> **작업 디렉터리**로만 쓰이고, 설정(`agent_priority` 포함)은 명령을 실행한 위치에서
> 읽습니다. 대상 프로젝트 설정을 그대로 쓰려면 `session open --workspace isolated`를
> 사용하세요.

### 환경 변수로 빠르게 바꾸기

자주 쓰는 것만 추려 봤어요.

| 변수                               | 기본값                       | 뜻                                            |
|-----------------------------------|------------------------------|-----------------------------------------------|
| `SYNCMILL_DEFAULT_STRATEGY`        | `route`                      | 기본 전략 고르기.                              |
| `SYNCMILL_AGENT_PRIORITY`          | `["claude","codex","kimi-code"]` | 에이전트 순서 (JSON 목록).                  |
| `SYNCMILL_REPO_ROOT`               | `.`                          | 기본 대상 폴더.                                |
| `SYNCMILL_MEMORY__BACKEND`         | `memtomem`                   | `memtomem`, `mem0`, 또는 `langgraph`.           |
| `SYNCMILL_MEMORY__DB_PATH`         | `~/.memtomem/memtomem.db`    | memtomem SQLite 파일.                            |
| `SYNCMILL_MEMORY__MEMORY_DIR`      | 미설정(memtomem 기본값)       | 격리 시 `DB_PATH`와 함께 지정할 Markdown 폴더.   |
| `SYNCMILL_MEMORY__MEM0__DATA_DIR`  | `~/.syncmill/mem0`           | 로컬 mem0 기록/내장 Qdrant 폴더.                 |
| `SYNCMILL_MEMORY__MEM0__TELEMETRY` | `false`                      | 프로세스의 첫 `mem0` import 때 사용할 값.         |
| `SYNCMILL_MEMORY__LANGGRAPH__DB_PATH` | `~/.syncmill/langgraph.db` | 영구 LangGraph SQLite 파일.                     |
| `SYNCMILL_CLAUDE__ENABLED`         | `true`                       | 에이전트 켜기/끄기.                            |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS` | `300`                        | 에이전트 제한 시간(초).                        |
| `SYNCMILL_CLAUDE__MAX_TURNS`       | `10`                         | 에이전트 최대 턴 수(claude만 적용).            |
| `SYNCMILL_INTEGRATION__TRACE__ENABLED` | `false`                  | body-free OTLP-JSON trace 파일 생성.             |
| `SYNCMILL_INTEGRATION__TRACE__EXPORTER` | `file`                   | `otlp-http`면 Phoenix에도 완료 span 전송.       |
| `SYNCMILL_INTEGRATION__TRACE__ENDPOINT` | 미설정                 | Phoenix endpoint; 없으면 `PHOENIX_COLLECTOR_ENDPOINT`, 그다음 로컬 기본값. |
| `SYNCMILL_INTEGRATION__TRACE__PROJECT_NAME` | 미설정             | Phoenix project; 없으면 `PHOENIX_PROJECT_NAME`, 그다음 `syncmill`. |

> 한 가지 팁: 하이픈이 든 `kimi-code`는 환경 변수에서 밑줄로 바뀌어요
> → `SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS`.

### Phoenix로 trace 스트리밍하기

선택 exporter를 설치하고 기존 trace 연계를 켭니다.

```bash
uv sync --project "$SYNCMILL_SRC" --extra phoenix
PHOENIX_API_KEY="..." \
SYNCMILL_INTEGRATION__TRACE__ENABLED=true \
SYNCMILL_INTEGRATION__TRACE__EXPORTER=otlp-http \
uv run --project "$SYNCMILL_SRC" syncmill run "실패하는 테스트 고쳐줘"
```

trace는 기본적으로 run envelope와 같은 트리인
`{SYNCMILL_INTEGRATION__REPORT__RUNS_DIR}/{run_id}/`에 쌓입니다. report 기록이
꺼져 있어도 각 run이 현재 run과 최신 SyncMill 소유 디렉터리를 합해
`SYNCMILL_INTEGRATION__REPORT__KEEP_LAST`개까지 best-effort로 보존합니다.
관련 없는 디렉터리는 건드리지 않고 한도에도 포함하지 않으므로 실제 전체 디렉터리
수는 더 많을 수 있습니다. 이는 소유한 run 디렉터리 "개수"만 제한할 뿐 용량을
제한하지 않고, 삭제에 실패하면 재시도 없이 경고만 남깁니다. trace가 크다면
`keep_last`를 낮추고 소유 디렉터리가 한도를 넘겨 남았다면 경고를 확인하세요.
`SYNCMILL_INTEGRATION__TRACE__DIR`로 직접 지정한 디렉터리는 사용자 소유이므로
SyncMill이 그 안을 절대 지우지 않습니다 — 직접 고른 디렉터리는 직접 정리하세요.

SyncMill은 Tracegraph용 `trace.otlp.json`을 계속 atomic write로 남기고,
완료된 body-free span을 background batch로 Phoenix에도 보냅니다. durable
spool은 없으므로 네트워크 오류와 queue drop은 경고 후 run envelope의
`trace.stream`에 남지만, 에이전트 실행과 로컬 trace는 계속 유지됩니다.
endpoint/project는 `syncmill.toml`에 넣어도 되지만, credential은 trace
속성에 들어가지 않도록 `PHOENIX_API_KEY`로만 제공하세요.

### memtomem과 Mem0 사이에서 이전하기

이전 중에는 메모리를 쓰는 `run`/`serve` 프로세스를 먼저 종료하세요. 계획 단계는
`shared`와 `agent-runtime:*`를 스냅샷으로 만들고, 대상 네임스페이스가 비어 있지 않으면
거부하며, 임시 `board-run:*` 데이터는 항상 제외합니다.

```bash
uv run --project "$SYNCMILL_SRC" syncmill memory migration plan --from memtomem --to mem0
# 설치된 memtomem, mem0, langgraph 사이의 모든 방향을 지원합니다.
uv run --project "$SYNCMILL_SRC" syncmill memory migration plan --from mem0 --to langgraph
uv run --project "$SYNCMILL_SRC" syncmill memory migration apply <migration-id>
uv run --project "$SYNCMILL_SRC" syncmill memory migration verify <migration-id>
uv run --project "$SYNCMILL_SRC" syncmill memory migration rollback <migration-id> --yes
```

`~/.syncmill/migrations/`는 **로컬 파일시스템**에 있어야 합니다. `apply`, `verify`,
`rollback`은 그 아래의 락 파일로 직렬화되는데, 네트워크 마운트에서 `flock`은 잘해야
에뮬레이션이고 나쁘면 조용히 로컬 전용입니다. 그러면 두 호스트가 모두 락을 쥐었다고
믿고, 둘 다 대상 비어 있음 검사를 통과하며, 각자 다른 대상 id를 씁니다. `state.json`을
나중에 쓴 쪽이 이기므로 다른 쪽이 넣은 레코드는 추적되지 않은 채 남습니다 — `verify`에
보이지 않고 `rollback`으로도 지울 수 없습니다.

정확한 네임스페이스만 고르려면 `plan`에 `--namespace`를 반복해서 지정하세요.
`~/.syncmill/migrations/` 아래에는 변경 불가능한 스냅샷과 재개 가능한 상태/영수증이
남습니다. 스냅샷에는 메모리 본문이 평문으로 들어 있고 자동 삭제되지 않으므로 폴더를
보호하고, 영수증이 더 필요 없을 때만 해당 아티팩트를 직접 삭제하세요. 이전은 원본을
지우거나 설정을 바꾸지 않으며, 롤백은 이전 뒤 변경된 대상 레코드를 삭제하지 않습니다.
계획은 해석된 대상 경로 또는 Qdrant endpoint·collection·history DB에도 아티팩트를
고정합니다. apply, verify, rollback 시 현재 대상이 다르면 저장소를 열거나 ledger를
바꾸기 전에 실패합니다. 이 binding이 없는 schema-v1 아티팩트는 현재 설정에 자동으로
연결하지 않습니다. 미적용 작업은 다시 plan하고, 이미 적용한 작업은 원래 대상 설정으로
수동 복구해야 합니다.
이미 적용된 v1 아티팩트는 보존하고 `state.json`의 target ID를 수동 복구 ledger로
사용하세요. Mem0 rollback은 Qdrant row를 지운 뒤 history DB에도 삭제 이벤트를 쓰므로
history DB 역시 의도적으로 destination binding에 포함됩니다.

### `syncmill.toml` 파일로 설정 고정하기

프로젝트마다 같은 설정을 쓰고 싶다면 파일로 적어 두면 편해요. 파일은 대상 Git
루트에 두세요. `--repo`를 받는 명령은 그 루트에서 파일을 찾으므로 하위 폴더에서
실행해도 적용되고, 대상을 받지 않는 명령은 **실행 위치**에서만 찾고 상위로
올라가지 않으니 루트에서 실행하세요.
[`syncmill.toml.example`](../syncmill.toml.example)을 복사해서 시작하세요.

```toml
default_strategy = "compete"
repo_root = "."

[memory]
backend = "memtomem"
db_path = "~/.memtomem/memtomem.db"
# memory_dir = "~/.syncmill/memtomem-memories"  # 비공개 db_path와 함께 지정

# mem0를 쓸 때(먼저 `uv sync --project "$SYNCMILL_SRC" --extra mem0`):
# backend = "mem0"
# [memory.mem0]
# data_dir = "~/.syncmill/mem0"
# telemetry = false

[claude]
timeout_seconds = 600

# 워크보드를 프로젝트에서 항상 켜 두고 싶다면 (기본은 꺼짐):
[board]
enabled = true
```

> 주의: 파일에 **맨 위(최상위) 항목 이름을 잘못 적으면** 오류로 알려 줘요(오타 보호).
> 그리고 환경 변수가 항상 파일을 이긴다는 점만 기억하세요.

---

## 한 단계 더: 선택 기능

아래 기능들은 **기본적으로 꺼져 있어요.** 필요할 때만 켜면 됩니다. (자세한 내용은
[영어 가이드](user-guide.md#optional-features)에 있어요.)

- **품질 게이트(quality gate)** — 에이전트가 일을 끝낸 *뒤* 테스트나 린트 같은 명령을
  돌려서, 통과해야만 결과를 받아들여요.
  ```toml
  [gate]
  commands = ["pytest -q", "ruff check ."]
  ```
- **서킷 브레이커(circuit breaker)** — 계속 실패하는 에이전트를 한동안 자동으로
  쉬게 해요(여러 번 실행에 걸쳐 기억해요).
  ```toml
  [breaker]
  enabled = true
  failure_threshold = 3
  ```
- **능력 매칭(capability matching)** — 어떤 에이전트가 무엇을 잘하는지 적어 두면,
  `-c` 힌트에 맞는 에이전트를 먼저 시도해요.
  ```toml
  [capabilities.agents]
  codex = ["python", "refactor"]
  ```
  ```bash
  uv run --project "$SYNCMILL_SRC" syncmill run "데이터 로더 최적화" -c python
  ```
- **팀 프로필(team profiles)** — 에이전트 구성(부분집합 + 순서)과 능력/전략을
  이름 붙여 저장해 두고, 실행할 때 골라 써요. `--strategy` 플래그가 프로필의
  전략보다 우선해요. `team list` / `team show <이름>`으로 확인할 수 있어요.
  ```toml
  [teams.python-team]
  agents = ["codex", "claude"]   # 순서 = 이 팀의 우선순위
  strategy = "compete"           # 생략하면 기본 전략을 따라요
  goals = ["backend"]            # 워크보드 goal — board run --goal backend 가 이 팀을 자동 적용
  ```
  ```bash
  uv run --project "$SYNCMILL_SRC" syncmill run "API 고쳐 줘" --team python-team
  ```
  한 goal은 최대 한 팀만 가질 수 있어요(두 팀이 같은 goal을 선언하면 설정 오류).
  goal과 워크보드가 어떻게 맞물리는지는
  [워크보드로 팀처럼 일하기](#워크보드로-팀처럼-일하기)를 보세요.

  프로필은 CLI에서 **저장·수정**할 수도 있어요 — `team save`가 `syncmill.toml`의
  `[teams.<이름>]` 테이블을 만들거나 통째로 바꿔 써요(다른 줄의 주석은 그대로
  보존돼요). `team use`는 최상위 `default_team` 키를 기록해서, `--team`을 안 주면
  `run`/`board run`이 그 팀을 자동으로 적용해요(우선순위: `--team` > goal 매핑 >
  `default_team`; `team use --clear`로 해제).
  ```bash
  uv run --project "$SYNCMILL_SRC" syncmill team save python-team -a codex -a claude -s compete -g backend
  uv run --project "$SYNCMILL_SRC" syncmill team use python-team
  ```
  대시보드에도 같은 기능의 **TEAM 패널**이 있어요 — `syncmill dashboard`에서
  `t`를 누르면 프로필 목록(기본 팀 표시 포함)이 열리고, `e`로 편집/새로 만들기,
  `u`로 기본 팀 지정/해제를 할 수 있어요. 저장 방식과 검증은 `team save`/`use`와
  똑같아요.

### 난이도 기반 모델 라우팅 (실험적)

작업 난이도에 따라 **provider 내부 모델**만 골라 쓰는 기능이에요 — 어떤
에이전트가 어떤 순서로 실행되는지, 전략·게이트·브레이커·승자 선택은 전혀
바뀌지 않아요. tier별 모델 이름(selector)은 여러분이 직접 정해요(syncmill은
모델 이름을 내장하지도, provider 카탈로그로 검증하지도 않아요). 설정한
selector는 `claude --model` / `codex -m` / `kimi -m` 플래그로 그대로 전달돼요.

```toml
[model_routing]
mode = "observe"          # off(기본) | observe (enforce는 아직 예약)

[model_routing.models.low]
claude = "<저렴한-claude-selector>"
codex = "<저렴한-codex-selector>"
kimi-code = "<저렴한-kimi-selector>"   # 실제 agent id 그대로

[model_routing.models.high]
claude = "<강력한-claude-selector>"
```

세 가지 사용 방법:

- **명시적 tier** — `syncmill run --difficulty low "오타 고쳐 줘"`는 `low`
  매핑을 바로 적용해요(`mode = "off"`여도요 — 플래그 자체가 명시적 요청이니까).
  해당 tier 매핑이 없는 에이전트는 provider CLI 기본 모델을 그대로 쓰고(배너에
  `N cli-default`로 표시), 적용 대상 에이전트 **전부** 매핑이 없으면 조용히
  no-op 하는 대신 실행 전에 실패해요.
- **observe** — `mode = "observe"`(또는 일회성 `--difficulty auto`)는
  결정적 휴리스틱(`prompt-complexity-v1`)으로 prompt를 분류해서 추천 tier를
  run plan에 **기록만** 해요(`--dry-run` JSON과 manifest의
  `plan.model_routing`에서 확인). 모델 플래그는 전달하지 않아요. 자동 적용을
  신뢰하기 전에 observe 데이터를 먼저 모으세요. 지금은 **직접 `syncmill run`
  (monitored 포함)과 `syncmill continue`에만** 적용돼요 — board run, TUI job,
  room은 `mode = "observe"`여도 아직 라우팅하지 않아요.
- **continuation** — `syncmill continue`는 **selector affinity**를 지켜요:
  부모 run이 실제로 쓴 selector 문자열(observe/과거 run이면 no-flag)을 그대로
  이어받아요 — 그 사이 설정이 바뀌었어도요. `continue`에는 `--difficulty`가
  없어요; 모델 정책을 바꾸려면 새 run을 시작하세요.

알아 둘 실패 동작: 형태는 유효하지만 실제로 없는 selector는 평범한 provider
실패예요(route가 다음 에이전트로 넘어가고, 브레이커가 켜져 있으면 실패로
기록돼요). 매핑을 고친 뒤 설정한 `cooldown_seconds`를 기다리면 다음 half-open
시도가 실행되고, 성공하면 브레이커가 닫히며 실패 횟수가 초기화됩니다. 즉시
재시도가 꼭 필요할 때만 브레이커를 잠시 끄세요. 롤백은 설정만
바꾸면 되지만(`mode = "off"` 또는 섹션 삭제), 명시적 `--difficulty` 플래그와
selector를 저장한 run의 continuation은 의도적으로 롤백 후에도 적용돼요.
전체 계약과 enforce 승격 조건은
[difficulty routing 계획](difficulty-model-routing-plan.md)에 있어요.

### 엄격한 Toolgraph 사전 검사 (preview)

Toolgraph 연계는 기본적으로 경고만 남겨요. 에이전트 실행 전에 선택한 profile을
실제로 강제하려면 report를 켜고 `mode = "strict"`를 명시하세요.

```toml
[integration.report]
enabled = true

[integration.toolgraph]
enabled = true
mode = "strict"
profile = "review"
control_plan_enabled = true
# control_plan_command = ["toolgraph", "control-preflight"]
candidate_tools = ["memtomem::mem_search", "syncmill::shared_read"]
unknown_policy = "block"
drift_policy = "warn"
unavailable_policy = "block"

[integration.toolgraph.agent_map]
codex = "codex"
"kimi-code" = "kimi"
```

`profile`은 Toolgraph의 거부 규칙을 고르고, `mode`는 SyncMill이 그 결과로 실행을
막을지 정해요. strict는 참여 가능한 모든 identity를 검사해요. 기본값은 unknown
identity/tool과 Toolgraph unavailable은 block, drift는 warn이에요. 세 범주는 각각
`block`/`warn`으로 바꿀 수 있지만, 깨졌거나 요청과 맞지 않는 artifact는 항상
block해요. 이 기능은 설정된 ambient MCP surface를 검사할 뿐 개별 tool 호출을
중재하지 않으며, `graph_generation`도 freshness timestamp는 아니에요.

`control_plan_enabled = true`를 켜면 identity별 subprocess 호출 대신 본문 없는
배치 하나를 사용해요. SyncMill은 선택한 strategy의 node, 코드/모델이 결정하는
edge, validator 존재 여부, timeout, 최대 호출 수와 병렬도, cycle 0이라는 상한만
선언합니다. prompt, gate command, output, patch, model selector, repository path는
보내지 않아요. Toolgraph는 모든 principal/tool 평가를 하나의 충돌 안전한
`graph_state`로 묶어 돌려주고, SyncMill은 정확한 plan digest와 전체 coverage를
검증한 뒤 run envelope 옆에 `control-plan.json`과 `control-preflight.json`을
저장해요. strict에서는 topology finding도 worktree나 agent가 시작되기 전에
차단합니다. 이는 정적 사전 검사이며 durable execution, 실제 실행 경로 일치 증명,
개별 tool call 중재는 아닙니다.

네이티브 MillFlow도 같은 계약을 사용합니다. 검증된 DAG를 planning 시점에 projection해
동결하고, 각 run/resume에서는 현재 설정으로 다시 만든 계획이 아니라 저장된 정확한
바이트를 검사합니다. 실행별 증거는 workflow store에 남고, 최종 applyable artifact는
동결된 plan digest를 참조합니다.

즉시 되돌리려면 `mode = "advisory"` 또는 `enabled = false`를 사용하세요.

---

## 워크보드로 팀처럼 일하기

`syncmill run`은 한 번 돌고 나면 잊어버려요. **워크보드(work-board)** 는 팀이 할 일을
**기록으로 남기면서** 일하게 해 줘요 — 할 일 하나하나가 목표(goal), 상태, 의존 관계,
검증 증거, 다음 사람을 위한 메모(handoff)를 갖고, 별도의 SQLite 데이터베이스
(`~/.syncmill/board.db`)에 저장돼요. 여러분이 보드에 작업을 등록해 두면
`board run`이 하나씩 꺼내서 평소의 오케스트레이션으로 처리하고, 사람이 결과를
검토해서 완료로 승격해요.

두 가지 원칙이 지켜져요.

- **에이전트는 보드를 직접 만지지 않아요.** syncmill이 작업 내용을 프롬프트로
  만들어 주고, 에이전트가 남긴 상태 보고만 읽어 와요.
- **상태는 보드에, 지식은 공유 메모리에** 있어요. 보드 작업이 끝나면 결과 요약을
  공유 메모리에도 기록해서 나중의 실행이 참고할 수 있게 해요. (다른 메모리 기록처럼
  이것도 최선-노력이에요 — 메모리가 안 되면 보드에는 결과가 남고, 요약이 공유
  메모리에 못 갔을 수 있다는 경고만 보여 줘요.) 실행 중에 에이전트들이 주고받는
  작업 메모는 공유 메모리가 아니라 실행별 임시 공간에 쌓여요. 보드 DB는 작업을
  클레임할 때 각 임시 네임스페이스의 소유권을 기록해요. 실행 결과를 기록한 뒤 또는
  클레임할 작업이 없을 때 오래된 정리 대상 최대 100개를 최선-노력으로 삭제하고,
  메모리가 성공을 확인한 항목만 tombstone 처리해요. 실패한 항목은 backoff 후 다시
  시도하며 정상 live claim은 보호합니다. GC 반복은 협력적 30초 예산을 적용해요.
  memtomem은 모든 인덱스 루트의 소유권이 확인된 legacy/native 일별 파일을 잠근 뒤
  파일을 먼저, DB 행을 나중에 삭제합니다. 소유권이 불명확하거나 소스 잠금 256개를
  넘거나 프로세스 파일 디스크립터 여유가 부족하면 거부하고, 미완료 삭제는 재시도해요. stale 복구가 단지 느린 run을 회수한
  경우 임시 네임스페이스는 lease 한 주기만큼 한 번 더 보호되지만, 그 유예가 지나면
  실행이 살아 있어도 정리될 수 있어요. 그때는 board 쓰기가 이미 fencing으로
  폐기되고 메모리는 보조 수단이며, 다음 쓰기가 네임스페이스를 다시 만듭니다. 실행
  종료 `finally`에서는 늦게 생긴 쓰기도 다음 청소 대상이 되도록 원장 행을 다시
  활성화해요(늦은 쓰기와 finalizer 사이의 강제 종료는 잔여 데이터를 남길 수 있어요).
  v0.1.3 이전 scratch는 namespace 구분 없이 shared 일별 파일에 섞였을 수 있어 자동
  정리가 불가능합니다. 수동으로 지우기 전 반드시 백업·검토하세요. 해당 파일을
  삭제하면 shared 지식도 함께 사라질 수 있습니다. [점검·복구 절차](improvement-roadmap-2026-09-08.ko.md)를 참고하세요.

워크보드는 **기본적으로 꺼져 있어요.** `SYNCMILL_BOARD__ENABLED=true`(또는
`syncmill.toml`에 `[board] enabled = true`)로 켜세요. 안 켜고 `board` 명령을 쓰면
이런 오류가 나요:

```text
Error: the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true (or [board] enabled=true in syncmill.toml).
```

### 작업의 상태

작업은 `planned → running → (blocked | review) → done` 순서로 움직여요.

| 상태       | 뜻                                                                                                         |
|-----------|-------------------------------------------------------------------------------------------------------------|
| `planned` | 대기 중. 의존하는 작업이 전부 `done`이고 "사람 필요" 표시가 없으면 자동으로 뽑혀요(오래된 것부터).              |
| `running` | `board run`이 집어간 상태. **원자적으로 클레임**돼서 두 실행이 같은 작업을 동시에 집을 수 없어요. 손으로는 못 바꿔요. 실행이 죽으면, 30분(설정 가능)이 지난 뒤 **다음 `board run`이** `planned`로 되돌려 줘요. |
| `blocked` | 실행이 실패했거나, 에이전트가 "진행 불가"라고 보고한 상태.                                                     |
| `review`  | 완료된 작업이나 가져온 검토 후보가 **사람 확인을 기다리는** 상태.                                             |
| `done`    | 끝났고 **검증까지 된** 상태.                                                                                  |

**`review`와 `done`의 구분이 이 기능의 핵심이에요.** `done`으로 바꾸려면 검증이
필요해요 — 실행 중 통과한 품질 게이트, `--evidence`로 붙인 증거, 또는 사람의
`--sign-off`. 그냥 `--state done`만 주면 거절돼요:

```text
Error: cannot mark item <id> done without verification — attach passing evidence (--evidence) or a human sign-off (--sign-off)
```

에이전트가 스스로 "다 했다"고 해도, 그 실행에서 실제 검증(예: 품질 게이트 통과)이
없었다면 `review`로만 기록돼요.

### Tracegraph 검토 후보 가져오기

Tracegraph가 내보낸 body-free 실패 패턴 보고서를 사람 검토용 보드 항목으로 가져올 수
있어요. 보고서에는 run id, 버전이 붙은 pattern id, qualified tool key, normalized
artifact digest만 들어 있어요.

```bash
uv run --project "$SYNCMILL_SRC" syncmill board import-review-candidates candidates.json
uv run --project "$SYNCMILL_SRC" syncmill board list --state review --goal tracegraph-review
uv run --project "$SYNCMILL_SRC" syncmill report show <run-id>  # 원래 보고서가 보존된 경우에만 선택적으로 확인
```

같은 후보는 결정적 id를 사용하므로 다시 가져와도 기존 항목을 다시 열지 않고 건너뛰어요.
항목은 `review` + `human-required`로 만들어져 `board run`이 자동 실행하지 않습니다.
Tracegraph digest는 검토가 필요한 이유를 나타내는 **미통과 증거**이며 완료 증거가
아닙니다. 사람 검토 후에는 기존 경로로 마무리하세요.

```bash
uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --sign-off \
  --clear-human-required --note "dismissed: false positive"
```

가져오기는 Toolgraph를 호출하거나 strict P3.1 판정을 다시 계산하지 않으며 unknown,
drift, unavailable 정책·manifest·agent priority·breaker·capability 설정도 바꾸지 않아요.

### 처음부터 끝까지 한 번 해 보기

```bash
export SYNCMILL_BOARD__ENABLED=true

# 작업 등록 (두 번째는 첫 번째가 끝나야 실행돼요)
uv run --project "$SYNCMILL_SRC" syncmill board add "API 스키마를 설계해 줘" --goal backend
uv run --project "$SYNCMILL_SRC" syncmill board add "엔드포인트를 구현해 줘" --goal backend --depends-on <첫번째-id>

# 팀 결정 기록 — 보드 실행의 프롬프트에 통째로 들어가요 (아래 '팀 결정 기록하기' 참고)
uv run --project "$SYNCMILL_SRC" syncmill board decide "REST만 쓰기, GraphQL 금지" --scope backend

# 보드 돌리기
uv run --project "$SYNCMILL_SRC" syncmill board run                 # 다음 작업 하나
uv run --project "$SYNCMILL_SRC" syncmill board run --loop          # 처리 가능한 작업을 전부 차례로
uv run --project "$SYNCMILL_SRC" syncmill board run --goal backend  # 이 goal만 (그 goal을 가진 팀이 자동 적용)

# 확인하고 완료 처리
uv run --project "$SYNCMILL_SRC" syncmill board list --state review
uv run --project "$SYNCMILL_SRC" syncmill board show <id>
uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --sign-off
```

잘 끝난 실행은 `review`에, 실패한 실행은 `blocked`에 놓여요. `board run`은
`run`과 같은 `-s`/`-a`/`--team` 옵션을 받아요.

### 팀 결정 기록하기 (`board decide`)

한 번 정한 것을 에이전트가 **매번 다시 논쟁하지 않게** 하는 기능이에요. 기록된
결정은 해당하는 보드 실행의 프롬프트에 **통째로 그대로** 들어가요 — 검색으로 골라
주는 게 아니에요. (단, 이 주입은 프롬프트를 풍부하게 하는 보조 기능이라, 결정을
읽지 못하면 실행을 막는 대신 결정 없이 진행해요.)

```bash
uv run --project "$SYNCMILL_SRC" syncmill board decide "pip 말고 uv만 쓰기"                    # 전체 적용
uv run --project "$SYNCMILL_SRC" syncmill board decide "REST만, GraphQL 금지" --scope backend  # 한 goal만
uv run --project "$SYNCMILL_SRC" syncmill board decisions                                     # 현재 유효한 결정들
uv run --project "$SYNCMILL_SRC" syncmill board decide "게이트웨이만 GraphQL 허용" --supersedes <id>  # 결정 바꾸기
```

`--supersedes`로 바꾸면 예전 결정은 **은퇴**해요(더 이상 주입되지 않지만 기록은
남아요).

### 팀 건강 지표 (`board stats`)

```text
items: 3
  by state:  done=1  planned=2
  reclaimed (stale-lease):  0
  redispatched (work redone):  0
  false-done (done, unverified):  0
  blocked > 3600s:  0
  human-required > 3600s:  0
  dropped handoffs > 3600s:  0
```

위에서부터: 죽은 실행에서 회수한 작업 수, 두 번 이상 집어가서 다시 한 작업 수,
검증 없이 `done`이 된 작업 수(정상 경로로는 불가능 — 무결성 경보), 그리고 1시간
(설정 가능) 넘게 방치된 blocked / 사람-필요 / 인계 메모가 있는 작업 수예요.

### 같이 쓰면 좋은 것들

- [`dashboard`](#dashboard--보드를-실시간-화면으로-보기) — 보드를 실시간 화면으로
  보면서 키 하나로 검토/승격까지.
- [`serve`](#serve--메모리를-다른-도구에-읽기-전용으로-열어-주기) — 다른 MCP
  클라이언트가 보드를 읽기 전용으로 조회 (`board_list`/`board_get`/`board_stats`).

---

## 자주 하는 작업

### 실시간으로 지켜보기 (`--monitor`)

`compete`·`council`·`decompose`처럼 동시에 도는 작업은, 각 에이전트의 화면을
tmux로 **실시간**으로 볼 수 있어요(에이전트마다 칸이 하나씩 생겨요).

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "캐시 계층을 설계해 줘" -s compete --monitor
```

`--monitor`는 동시 전략에서만 동작해요. 순서대로 도는 전략이거나, 실제 터미널이
아니거나, tmux가 없으면 안내만 보여 주고 평소처럼 실행돼요(꼭 필요한 건 아니에요).

> 참고: `claude`는 마지막에 결과를 한 번에 보여 줘서 칸이 끝에 채워지고,
> `codex`·`kimi-code`는 진행하면서 실시간으로 흘러가요.

### 나는 한 에이전트를 직접 쓰고, 나머지는 syncmill에게 맡기기

에이전트 하나는 **내가 직접 대화하며** 쓰고, 나머지는 syncmill이 돌리게 할 수 있어요.
이때 두 쪽은 **오직 공유 메모리**를 통해서만 만나요.

```bash
# 터미널 A: 내가 claude를 직접 대화형으로 사용 (세션이 계속 살아 있어요)
uv run --project "$SYNCMILL_SRC" syncmill session start claude

# 터미널 B: 나머지는 syncmill이 알아서
uv run --project "$SYNCMILL_SRC" syncmill run "이 기능을 만들어 줘" --exclude claude
```

일반 메모리를 공유하려면 **메모리를 쓸 때 `shared`를 꼭 지정**하세요.
(`session start`가 정확한 사용법을 화면에 안내해 줘요.)

- 기록하기: `mem_add(content=..., namespace="shared")`
- 읽어 오기: `mem_agent_search(query=..., include_shared=True)`

**두 쪽이 같은 메모리 저장소를 가리켜야 해요.** `init`한 프로젝트의 syncmill은 `[memory]`를
기본값으로 두면 저장소 안 `.syncmill/memory`에 기록하지만, 대화형 세션의 memtomem MCP는 따로
설정하지 않으면 `~/.memtomem`을 써요. 저장소가 다르면 서로의 기록이 보이지 않아요. 대화형
세션과 협업할 프로젝트라면 `syncmill.toml`에서 syncmill을 memtomem 기본 저장소로 맞추세요
(두 값을 **함께** 지정해야 해요).

```toml
[memory]
db_path = "~/.memtomem/memtomem.db"
memory_dir = "~/.memtomem/memories"
```

이렇게 하면 이 프로젝트의 syncmill 기록이 memtomem 전역 저장소에 함께 쌓여, 같은 저장소를 쓰는
다른 프로젝트와 기록을 공유하게 돼요.

후속 단계에서 읽을 메모에는 아래 산출물 종류 태그 중 하나를 붙여야 합니다.

| 단계 | 읽을 수 있는 태그 (하나만 일치해도 포함) |
|---|---|
| Pipeline 두 번째 단계부터 | `pipeline-stage` |
| Decompose 하위 작업 실행 | `decompose-plan`, `decompose-subtask` |
| Decompose 종합 | `decompose-subtask` |
| Council 첫 번째 비평 | `council-proposal` |
| Council 이후 비평과 종합 | `council-proposal`, `council-critique` |

태그가 없는 메모나 `task-result` 또는 `board-result:*` 태그만 붙은 메모는
route/compete, rooms, 각 전략의 첫 단계에서는 검색 대상이지만 위 후속 단계에서는
제외됩니다. 보드 실행이 전역 `shared`를 함께 검색할 때도 같은 필터가 적용됩니다.
태그가 맞아도 포함이 보장되지는 않습니다. 관련도 순위와 컨텍스트의 3,800바이트
예산이 적용되며, 행 수가 많을수록 각 행의 내용은 짧아집니다. 태그 필터를 지원하지
않는 백엔드는 일반 검색으로 대체하고 메모리 기능 저하 상태와 경고를 표시합니다.

내가 직접 한 수정은 내 실제 코드에 반영되고, syncmill 에이전트들은 격리된 폴더에서
일해요. 즉 **파일을 합치는 게 아니라 "지식"을 공유**하는 방식이에요.

### 할 일을 보드에 쌓아 두고 팀으로 돌리기

[워크보드](#워크보드로-팀처럼-일하기)와 팀 프로필을 함께 쓰면, 목표(goal)별 작업
큐를 정해진 팀이 알아서 처리하게 할 수 있어요.

```bash
export SYNCMILL_BOARD__ENABLED=true

# 한 번만 설정: backend goal을 가진 팀 + 팀 결정
uv run --project "$SYNCMILL_SRC" syncmill team save python-team -a codex -a claude -s compete -g backend
uv run --project "$SYNCMILL_SRC" syncmill board decide "REST만 쓰기, GraphQL 금지" --scope backend

# 작업 등록
uv run --project "$SYNCMILL_SRC" syncmill board add "API 스키마를 설계해 줘" --goal backend
uv run --project "$SYNCMILL_SRC" syncmill board add "엔드포인트를 구현해 줘" --goal backend --depends-on <첫번째-id>

# 터미널 A: 실시간으로 지켜보며 검토
uv run --project "$SYNCMILL_SRC" syncmill dashboard

# 터미널 B: backend 큐 비우기 — python-team이 자동으로 적용돼요
uv run --project "$SYNCMILL_SRC" syncmill board run --loop --goal backend
```

끝난 작업은 `review`에 놓여요. 확인했다면 대시보드에서 `e`를 누르거나
`board update <id> --state done --sign-off`로 완료 처리하세요.

---

## 문제가 생겼을 때

| 이런 메시지가 나오면…                                          | 이런 뜻이고, 이렇게 하세요                                                                 |
|-------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `memory unavailable — continuing without shared context`     | 선택한 백엔드를 못 열었어요. memtomem이면 `~/.memtomem/config.json`을, mem0이면 선택형 패키지 설치·첫 모델 다운로드·Qdrant/임베딩 차원을 확인하세요. 로컬 Qdrant 저장 폴더를 다른 프로세스가 사용 중이라는 오류가 나오면 그 프로세스를 종료하거나 Qdrant 서버 URL을 설정하세요. 작업은 계속됐고 `syncmill status`로 원인을 볼 수 있어요. |
| 에이전트가 건너뛰어지거나 "All agents failed:"                  | 프로그램이 없거나, 로그인이 안 됐거나, 꺼져 있을 수 있어요. `syncmill agents`로 확인하세요.   |
| `unknown strategy: …`                                        | 전략 이름이 틀렸어요. `route`/`pipeline`/`compete`/`council`/`decompose` 중에서 고르세요.   |
| `--monitor`가 실시간 화면 없이 그냥 실행됨                      | 동시 전략 + 실제 터미널 + tmux가 모두 있어야 해요. 안내 메시지가 무엇이 빠졌는지 알려 줘요.   |
| `session needs tmux …`                                       | `session` 기능은 tmux가 꼭 필요해요. tmux를 설치하고 실제 터미널에서 실행하세요.            |
| `the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true …` | `board` 명령과 `dashboard`는 워크보드를 먼저 켜야 해요. 환경 변수를 켜거나 `syncmill.toml`에 `[board] enabled = true`를 넣으세요. |
| `cannot mark item … done without verification — …`           | `done`은 "끝났고 검증까지 됨"이라는 뜻이라, `--state done`에는 `--evidence <증거>`나 `--sign-off`를 함께 줘야 해요. |
| `dashboard needs an interactive terminal (stdout is not a TTY).` | `dashboard`는 실제 터미널에서만 실행돼요(`session`처럼 대체 동작이 없어요).              |
| 작업 내용이 `-`로 시작해서 옵션으로 잘못 읽힘                    | 앞에 `--`를 붙이세요: `syncmill run -- "--고칠 내용"`                                       |
| `~/.syncmill/workspaces/<run-id>`가 남아 있음                  | live run, 의도적으로 보존한 recovery 경로, 또는 강제 종료 잔여물일 수 있어요. live 경로는 지우지 마세요. 소유자가 죽은 SyncMill 잔여물은 다음 정상 run이 정리하고 recovery 표시 경로는 수동 점검을 위해 남깁니다. |

---

더 자세한 작업 흐름은 [영어 사용 안내서](user-guide.md)에, 전체 옵션은
`syncmill --help`와 각 명령의 `--help`에, 설계 원리는
[CLAUDE.md](../CLAUDE.md)에 있어요. 즐겁게 사용하세요! 🚀
