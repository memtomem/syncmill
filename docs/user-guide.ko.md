# syncmill 사용 안내서 (처음 시작하는 분들을 위해)

> 처음 syncmill을 써 보는 분을 위한 쉬운 안내서예요. 모든 옵션을 빠짐없이 정리한
> 영어 레퍼런스는 [user-guide.md](user-guide.md)를, 설계 배경은
> [CLAUDE.md](../CLAUDE.md)를 참고하세요.

## 목차

- [syncmill이 뭔가요?](#syncmill이-뭔가요)
- [설치하기](#설치하기)
- [처음 실행해 보기](#처음-실행해-보기)
- [실제 에이전트로 안전하게 실행하기](#실제-에이전트로-안전하게-실행하기)
- [꼭 알아야 할 5가지](#꼭-알아야-할-5가지)
- [전략 고르기](#전략-고르기)
- [자주 쓰는 명령어](#자주-쓰는-명령어)
- [설정하기](#설정하기)
- [한 단계 더: 선택 기능](#한-단계-더-선택-기능)
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
- **격리된 작업 공간** — 각 에이전트는 격리된 임시 작업 폴더(git worktree)에서
  일해요. 그래서 **여러분이 실제로 작업 중인 코드는 절대 건드리지 않아요.**

명령어 한 줄에 작업 내용(프롬프트)을 적어 주면, syncmill이 알아서 에이전트를
돌리고 결과를 정리해 줍니다.

---

## 설치하기

syncmill은 [uv](https://docs.astral.sh/uv/)와 Python 3.12 이상이 필요해요.

```bash
uv sync          # 필요한 라이브러리를 설치하고 .venv를 만들어요
```

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

### 잘 설치됐는지 확인하기

```bash
uv run syncmill agents     # 어떤 에이전트가 켜져 있는지 보여 줘요
uv run syncmill status     # 메모리(memtomem)가 연결됐는지 확인해요
```

---

## 처음 실행해 보기

가장 기본적인 명령은 이거예요.

```bash
uv run syncmill run "main.py에 타입 힌트를 추가해 줘"
```

이게 전부예요! 기본값은 **`route`(라우트)** 전략이라, 우선순위 순서대로
(`claude` → `codex` → `kimi-code`) 에이전트를 시도하고 **처음으로 성공한 결과**를
돌려줘요. 첫 번째 에이전트가 실패하거나 설치되어 있지 않으면 다음 에이전트가
이어받습니다.

몇 가지 변형도 해 볼까요.

```bash
# 에이전트 하나만 딱 지정 (다른 에이전트로 넘어가지 않음)
uv run syncmill run "인증 모듈을 리팩터링해 줘" -a claude

# 다른 폴더(저장소)를 대상으로, 시간(초)을 더 넉넉히 주기
uv run syncmill run "실패하는 테스트를 고쳐 줘" -r /경로/저장소 -t 600

# 모든 에이전트를 동시에 붙여서 승자(우선순위가 가장 높은 성공 결과)를 선택
uv run syncmill run "캐시 계층을 설계해 줘" -s compete
```

작업이 끝나면 결과와 함께 어떤 파일이 바뀌었는지 보여 줘요.

---

## 실제 에이전트로 안전하게 실행하기

테스트는 fake 에이전트로 빠르게 돌지만, `syncmill run`은 시뮬레이션이 아니에요.
현재 shell의 `PATH`에 있는 실제 에이전트 CLI를 실행하고, 각 CLI에 로그인된 계정을
사용합니다. 따라서 일반적인 에이전트 실행과 똑같이 네트워크, 사용량/요금, provider
quota, 그리고 해당 CLI의 비대화형 승인 정책이 적용될 수 있어요.

그래도 syncmill의 프로그램 실행은 대상 저장소를 격리합니다.

- 에이전트는 `../.syncmill-worktrees/` 아래의 별도 git worktree에서 작업해요. git
  저장소가 아니면 임시 폴더를 씁니다.
- 결과에 `Files changed`가 나오더라도, 그 변경은 우승한 worktree의 diff 목록이에요.
  syncmill이 원래 작업 폴더에 자동으로 merge하거나 복사하지 않습니다. 우승자의 코드를
  가져오려면 `--emit-patch <파일>`을 붙이세요. worktree가 정리되기 전에 전체 diff(새
  파일 포함)를 그 파일로 내보내 주고, 검토 후 `git apply <파일>`로 직접 적용하면 됩니다.
- 실행이 끝나면 worktree와 `syncmill/<agent>/<task>` 브랜치를 정리합니다. 비정상 종료로
  빈 worktree가 남으면 다음 `syncmill run`에서 치워요.
- `session`은 예외예요. `syncmill session start ...`는 원래 폴더에서 raw 대화형
  에이전트를 띄우므로, 거기서 생긴 수정은 실제 로컬 수정입니다.

### 첫 실제 실행 예시

처음에는 에이전트 하나만 강제로 지정하고 제한 시간을 짧게 잡으세요. 이렇게 하면
인증, subprocess 실행, worktree 생성, 결과 파싱, 메모리, 정리 흐름을 한 번에 확인하면서도
여러 에이전트를 동시에 쓰지 않습니다.

```bash
# 1. syncmill이 보는 현재 설정 확인
uv run syncmill agents
uv run syncmill status

# 2. 사용할 CLI가 있는지, 직접 실행 가능한지 확인
command -v codex
codex --help

# 3. 실제 에이전트 하나만 실행
uv run syncmill run \
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
worktree가 정리되기 전에 이 diff를 실제 패치 파일로 내보내려면 `--emit-patch`를 붙이세요.

```bash
uv run syncmill run "src/example.py에 타입 힌트 추가" --emit-patch winner.patch
git apply --stat winner.patch   # 먼저 검토
git apply winner.patch          # 확인 후 적용
```

### 한 에이전트에서 다중 에이전트로 넓히기

하나가 잘 되는 걸 확인한 뒤 아래 순서로 넓혀 가는 편이 안전합니다.

```bash
# fallback: 성공하면 멈추므로 동시 전략보다 사용량을 아끼기 좋습니다.
uv run syncmill run "가장 작은 실패 테스트 하나를 고치고 설명해 줘" -s route

# 경쟁: 켜진 에이전트가 모두 실행되므로 병렬 사용량/quota를 예상하세요.
uv run syncmill run "CLI 오류 메시지 정리 방안을 제안해 줘" -s compete

# tmux와 실제 터미널이 있으면 동시 전략을 실시간으로 볼 수 있습니다.
uv run syncmill run "설정 검증 접근법 두 가지를 비교해 줘" -s compete --monitor
```

구현 작업을 맡길 때는 품질 게이트를 함께 쓰는 편이 좋습니다.

```toml
[gate]
commands = ["pytest -q"]
timeout_seconds = 300
```

그리고 실행합니다.

```bash
uv run syncmill run "실패하는 테스트를 고쳐 줘" --agent codex --timeout 600
```

게이트는 에이전트의 worktree 안에서 실행됩니다. 통과했다는 것은 그 격리된 결과가
거기서 검증됐다는 뜻이고, 그래도 diff가 원래 checkout에 자동 적용되지는 않습니다.

### 실제 실행 문제 확인표

| 증상 | 확인할 것 |
|------|-----------|
| `CLI tool 'codex' not found in PATH` | syncmill을 실행하는 같은 shell에서 `command -v codex`를 확인하세요. |
| 에이전트가 인증 메시지와 함께 바로 실패 | 해당 CLI를 직접 열어서 로그인/인증을 끝내세요. |
| `uv run pytest`는 되는데 실제 에이전트 실행은 실패 | 테스트는 fake를 씁니다. 실제 CLI, 계정, 네트워크, quota를 확인하세요. |
| `Files changed`는 나오는데 내 폴더는 그대로 | 정상입니다. 변경은 격리 worktree에 있었고 정리됐습니다. `--emit-patch <파일>`로 다시 실행해 diff를 내보낸 뒤 `git apply <파일>`로 직접 적용하세요. |
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
   기록해 둬요. 메모리는 **있으면 좋고, 없어도 작업은 진행돼요.**

5. **작업 공간(worktree)** — 각 에이전트가 일하는 임시 폴더예요
   (`../.syncmill-worktrees/` 안에 만들어져요). **여러분의 실제 코드는 안전합니다.**
   작업이 끝나면 syncmill이 알아서 정리해요.

---

## 전략 고르기

`-s`(또는 `--strategy`)로 전략을 고를 수 있어요. 처음에는 기본값 `route`로 시작하고,
필요할 때 바꿔 보세요.

| 전략        | 한마디로                                          | 이럴 때 좋아요                                       |
|------------|--------------------------------------------------|---------------------------------------------------|
| `route`    | 순서대로 시도해서 **처음 성공한 것**을 씀 *(기본값)* | 그냥 누구든 빨리 해 주면 될 때. 가장 저렴해요.        |
| `pipeline` | 에이전트를 **순서대로 이어서** 작업하게 함          | "구현 → 테스트 → 문서화"처럼 단계가 나뉘는 작업.      |
| `compete`  | **모두 동시에** 돌려서 우선순위가 가장 높은 성공 결과를 고름 | 같은 일을 여러 에이전트가 동시에 시도하고, 성공한 결과 중 우선순위가 가장 높은 것을 고르고 싶을 때.  |
| `council`  | **제안 → 비평 → 종합** 으로 서로 검토하며 다듬음    | 서로의 아이디어를 검토하면 좋은 어려운 설계 문제.    |
| `decompose`| 작업을 **여러 조각으로 나눠** 동시에 하고 합침      | 독립적인 조각으로 깔끔히 나뉘는 큰 작업.            |

조금 더 풀어서 설명하면 이래요.

- **`route` (라우트)** — `claude`부터 시도하고, 실패하면 `codex`, 또 실패하면
  `kimi-code`. 빠르고 저렴해요(성공하면 바로 멈추니까요).
- **`pipeline` (파이프라인)** — 같은 작업 폴더에서 에이전트가 차례대로 작업해요. 뒤
  에이전트가 앞 에이전트의 결과를 이어받아요. 중간에 하나가 실패하면 거기서 멈추고
  어디서 멈췄는지 알려 줘요.
- **`compete` (경쟁)** — 모든 에이전트가 각자 폴더에서 **동시에** 같은 작업을 해요.
  우승자는 우선순위가 가장 높으면서 성공한 에이전트예요(빨리 끝낸 순서가 아니라요).
- **`council` (협의)** — 모두가 먼저 안을 내고, 정해진 횟수만큼 서로의 안을 보며
  다듬은 뒤, 최종안을 만들어 그중 하나를 골라요.
- **`decompose` (분해)** — 우선순위가 가장 높은 에이전트가 작업을 작은 조각들로 나누고,
  그 조각들을 여러 에이전트가 나눠서 동시에 처리한 뒤, 결과를 하나로 합쳐요.

> `compete` · `council` · `decompose`는 동시에 실행되기 때문에,
> [`--monitor`](#실시간으로-지켜보기---monitor)로 진행 상황을 실시간으로 볼 수 있어요.

---

## 자주 쓰는 명령어

명령은 `run`, `agents`, `status`, `serve`, 그리고 `session` 그룹이 있어요. 언제든
`uv run syncmill --help` 또는 `uv run syncmill <명령> --help`로 도움말을 볼 수 있고,
`uv run syncmill --version`으로 버전을 확인할 수 있어요.

### `run` — 작업 실행하기

```bash
uv run syncmill run "<할 일>" [옵션]
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
| `--monitor`   |       | 동시 실행을 tmux 화면으로 실시간 보기.                        |

> 💡 작업 내용이 `-`(대시)로 시작하면 앞에 `--`를 붙이세요:
> `uv run syncmill run -- "--fix 부분을 고쳐 줘"`

### `agents` — 에이전트 목록 보기

```bash
uv run syncmill agents
```

우선순위 순서로 에이전트 목록과 켜짐/꺼짐 상태, 제한 시간, 최대 턴 수(max turns)를 보여 줘요.

### `status` — 메모리 상태 확인

```bash
uv run syncmill status
```

메모리(memtomem)가 연결됐는지와 데이터베이스 위치를 보여 줘요.

### `serve` — 메모리를 다른 도구에 읽기 전용으로 열어 주기

syncmill이 쌓아 둔 공유 지식을 **다른 MCP 클라이언트**(Claude Code/Desktop, Cursor
등)가 **읽을 수 있게** 열어 줘요. 읽기 전용이라 안전합니다.

```bash
# 기본(stdio) — 예: Claude Code에 등록하기
claude mcp add syncmill-shared -- uv run syncmill serve

# HTTP로 열기 (기본은 내 컴퓨터에서만, 포트 8000)
uv run syncmill serve --http
uv run syncmill serve --http --port 8765   # 포트 바꾸기 (--host로 바인드 주소도 변경)
```

> ⚠️ HTTP로 열면 **인증이 없어요.** 내 컴퓨터(loopback)에서만 쓰거나, 믿을 수 있는
> 네트워크/인증 프록시 뒤에서만 쓰세요.

### `session` — 대화형 세션 오래 유지하기

에이전트의 **대화형 모드**(직접 타이핑하는 그 화면)를 tmux 세션 안에서 띄워 줘요.
이렇게 하면 터미널을 닫거나 SSH가 끊겨도 **세션이 살아 있어서 이름으로 다시 붙을 수**
있어요. (tmux가 설치돼 있어야 하고, `start`/`attach`는 실제 터미널이 필요해요.)

```bash
uv run syncmill session start [에이전트]   # 시작(또는 재연결) 후 화면 붙기
uv run syncmill session list              # 살아 있는 세션 목록
uv run syncmill session attach 대상        # 다시 연결하기
uv run syncmill session stop 대상          # 하나 종료
uv run syncmill session stop --all        # 전부 종료
```

화면에서 빠져나올 때는 **`Ctrl-b` 누른 뒤 `d`** 를 누르세요. 에이전트는 계속 돌아가고,
`session attach`로 다시 붙으면 같은 세션이 그대로 있어요.

---

## 설정하기

설정은 세 군데에서 읽어 오고, **위에 있을수록 우선**이에요.

1. **환경 변수 `SYNCMILL_*`** (가장 우선)
2. **현재 폴더의 `syncmill.toml` 파일**
3. **기본값** (가장 낮음)

즉 **환경 변수가 파일을 이기고, 파일이 기본값을 이겨요.** 그리고 `--repo` /
`--strategy` / `--timeout` 같은 명령줄 옵션은 이 셋보다 더 우선합니다(그때 한 번만).

> 💡 `syncmill.toml`은 **명령을 실행하는 현재 폴더**에서 찾아요(`--repo` 폴더가
> 아니에요). 파일이 없으면 그냥 기본값 + 환경 변수만 써요.

### 환경 변수로 빠르게 바꾸기

자주 쓰는 것만 추려 봤어요.

| 변수                               | 기본값                       | 뜻                                            |
|-----------------------------------|------------------------------|-----------------------------------------------|
| `SYNCMILL_DEFAULT_STRATEGY`        | `route`                      | 기본 전략 고르기.                              |
| `SYNCMILL_AGENT_PRIORITY`          | `["claude","codex","kimi-code"]` | 에이전트 순서 (JSON 목록).                  |
| `SYNCMILL_REPO_ROOT`               | `.`                          | 기본 대상 폴더.                                |
| `SYNCMILL_CLAUDE__ENABLED`         | `true`                       | 에이전트 켜기/끄기.                            |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS` | `300`                        | 에이전트 제한 시간(초).                        |
| `SYNCMILL_CLAUDE__MAX_TURNS`       | `10`                         | 에이전트 최대 턴 수(claude만 적용).            |

> 한 가지 팁: 하이픈이 든 `kimi-code`는 환경 변수에서 밑줄로 바뀌어요
> → `SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS`.

### `syncmill.toml` 파일로 설정 고정하기

프로젝트마다 같은 설정을 쓰고 싶다면 파일로 적어 두면 편해요.
[`syncmill.toml.example`](../syncmill.toml.example)을 복사해서 시작하세요.

```toml
default_strategy = "compete"
repo_root = "."

[claude]
timeout_seconds = 600
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
  uv run syncmill run "데이터 로더 최적화" -c python
  ```

---

## 자주 하는 작업

### 실시간으로 지켜보기 (`--monitor`)

`compete`·`council`·`decompose`처럼 동시에 도는 작업은, 각 에이전트의 화면을
tmux로 **실시간**으로 볼 수 있어요(에이전트마다 칸이 하나씩 생겨요).

```bash
uv run syncmill run "캐시 계층을 설계해 줘" -s compete --monitor
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
uv run syncmill session start claude

# 터미널 B: 나머지는 syncmill이 알아서
uv run syncmill run "이 기능을 만들어 줘" --exclude claude
```

대화형 쪽에서 한 가지만 지키면 돼요: **메모리를 쓸 때 `shared`를 꼭 지정**하세요.
(`session start`가 정확한 사용법을 화면에 안내해 줘요.)

- 기록하기: `mem_add(content=..., namespace="shared")`
- 읽어 오기: `mem_agent_search(query=..., include_shared=True)`

내가 직접 한 수정은 내 실제 코드에 반영되고, syncmill 에이전트들은 격리된 폴더에서
일해요. 즉 **파일을 합치는 게 아니라 "지식"을 공유**하는 방식이에요.

---

## 문제가 생겼을 때

| 이런 메시지가 나오면…                                          | 이런 뜻이고, 이렇게 하세요                                                                 |
|-------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `memory unavailable — continuing without shared context`     | 메모리를 못 열었어요(보통 `~/.memtomem/config.json`의 임베딩 백엔드 설정 문제). 작업은 됐어요 — 백엔드를 고치면 공유 컨텍스트가 복구돼요. `syncmill status`로 확인해 보세요. |
| 에이전트가 건너뛰어지거나 "All agents failed:"                  | 프로그램이 없거나, 로그인이 안 됐거나, 꺼져 있을 수 있어요. `syncmill agents`로 확인하세요.   |
| `unknown strategy: …`                                        | 전략 이름이 틀렸어요. `route`/`pipeline`/`compete`/`council`/`decompose` 중에서 고르세요.   |
| `--monitor`가 실시간 화면 없이 그냥 실행됨                      | 동시 전략 + 실제 터미널 + tmux가 모두 있어야 해요. 안내 메시지가 무엇이 빠졌는지 알려 줘요.   |
| `session needs tmux …`                                       | `session` 기능은 tmux가 꼭 필요해요. tmux를 설치하고 실제 터미널에서 실행하세요.            |
| 작업 내용이 `-`로 시작해서 옵션으로 잘못 읽힘                    | 앞에 `--`를 붙이세요: `syncmill run -- "--고칠 내용"`                                       |
| `../.syncmill-worktrees/` 폴더가 남아 있음                     | 비정상 종료로 남은 거예요. 다음에 `syncmill run`을 돌리면 자동으로 정리돼요.                 |

---

더 자세한 모든 옵션은 [영어 사용 안내서](user-guide.md)에, 설계 원리는
[CLAUDE.md](../CLAUDE.md)에 있어요. 즐겁게 사용하세요! 🚀
