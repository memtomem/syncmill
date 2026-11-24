# herdr 안에서의 SyncMill 사용성 평가 — 2026-11-24

## 기준

- SyncMill: `main`의 `56064c5`. 작업 트리는 깨끗했다.
- 환경(측정 직후 확인): macOS 27.2.1, Ghostty 위의 herdr 0.9.5(에이전트용 터미널 멀티플렉서),
  tmux 3.6b, Claude Code 2.1.331. `herdr integration status`에서 claude·codex·kimi 연동은
  `current`였다.
- herdr prefix와 tmux prefix는 둘 다 `ctrl+b`다(`~/.tmux.conf`에 prefix 재지정이 없다).
- 이 문서는 **1차 평가**다. 계획은 시나리오 20개(A1–A8 표면 호환성, B1–B6 부작용·간섭,
  C1–C6 작업 흐름)였고, 그중 장치가 단순하고 결론이 큰 것부터 쟀다. 잰 것은 A2, A4, A5, A6,
  B1, B2, B4(A5와 한 실험), B5의 일부, B6, C1, C2다. 나머지와, 잰 항목 안에서 못 잰 부분은
  마지막 절에 ID와 함께 [미실측]으로 적었다.
- 표기: [실측]은 이 날 직접 실행해 값을 확인한 것, [수동]은 사용자가 실제 키보드로 눌러 보고
  알려 준 것, [읽기]는 코드나 설정을 읽은 것, [미실측]은 재지 않은 것이다.

### 측정 방법과 그 한계

- herdr 워크스페이스에 평가용 탭 하나를 만들고 그 안의 pane에서만 실행했다. 조작은
  `herdr pane run`·`send-keys`·`read`로 했다. 이 방식은 키를 pane의 PTY에 직접 쓰므로
  **herdr 클라이언트의 키 처리(prefix 가로채기, `alt+숫자`, 마우스, 입력기)는 거치지 않는다.**
  예외는 A4의 detach 키 하나다. 이것은 사용자가 실제 키보드로 눌러 보고 알려 준 결과이고
  [수동]으로 표시했다.
  아래의 "동작한다"는 판정은 모두 이 경로에 한한다.
- SyncMill 상태 경로는 임시 디렉터리로 돌렸다. `workspace.root`, `runs.root`, `tui.state_dir`,
  `memory.db_path`, `memory.memory_dir`, `board.db_path`, `breaker.state_path`,
  보고서 `runs_dir`, trace `dir` 9개는 `SyncmillConfig()`가 해석한 절대경로를 한 행씩 확인했다.
  마이그레이션 디렉터리와 `TMPDIR`도 환경변수로 돌렸지만 이 둘은 해석 결과를 따로 확인하지 않았다.
- tmux는 PATH 앞에 둔 래퍼로 전용 소켓(`-S`)과 평가용 설정에 묶었다. 평가용 설정은 사용자
  `~/.tmux.conf`에서 `mouse on`, `default-terminal tmux-256color`, `escape-time 0` 세 줄만
  가져왔다. 상태줄은 tmux 기본값이었고, 사용자의 테마·TPM 플러그인(resurrect·continuum)과
  나머지 옵션은 빠져 있다.
- 대부분은 가짜 `codex`/`kimi`(체험 경로의 가짜와 같은 출력 형식)로 돌렸다. 실제 provider의
  headless 실행은 기록이 남은 8건(claude 6, codex 1, kimi-code 1)이고 전부
  `Reply with the single word OK`였다.
- pane의 에이전트 기록은 `herdr pane get`을 반복해 읽어(읽은 뒤 0.15초 쉼) 값이 바뀔 때마다
  초 단위 시각과 함께 적었다. 아래의 "N초 뒤"는 첫 표본과 첫 변화 사이의 간격이고, 명령
  시작에서 잰 시간이 아니다. 자식의 실제 세션 ID는 아티팩트 `manifest.json`의
  `provider_session_id`에서 가져와 값으로 비교했다.
- 증거 파일(명령, 상태 필드, 종료 코드)은 세션 임시 디렉터리에 두었고 저장소에는 넣지 않았다.
  B2와 B5의 명령과 전후 값은 끝의 부록에 옮겨 적었다.
  pane 크기, `q` 뒤의 화면, detach 직후의 tmux 세션 조회처럼 **실행 당시 출력으로만 확인하고
  파일로 남기지 않은 관측**이 있다. 해당 항목에 그렇게 적었다.

## 결론

API로 조작한 범위에서 TUI, `--monitor`, `session`은 herdr pane 안에서 그려지고 전환됐다.
문제는 **두 도구가 서로를 모르는 데서** 생긴다.

1. SyncMill이 띄운 에이전트는 herdr의 pane 식별 변수를 그대로 물려받는다. claude와 codex의
   headless 실행 뒤에는 그 실행의 세션 ID가 pane의 기록에 나타났다. 대화형 Claude가 떠 있는
   pane의 식별자를 가진 채 `syncmill continue`를 실행하자 **그 pane의 세션 기록이 headless
   실행의 세션으로 바뀌었다.** (실행은 다른 pane에서 식별자만 맞춰서 했다. 아래 B2 참조.)
   herdr 연동 hook이 이 보고를 보낸다는 것은 hook 소스와 ID 일치에 근거한 **추론**이다.
   hook이 보낸 내용이나 서버의 수신 기록은 잡지 못했다.
2. 가짜 에이전트 1건에서, 실행 중에 pane을 닫자 SyncMill은 끝났지만 에이전트 프로세스는
   2초·10초 뒤에도 살아 있었다. 실제 provider에서는 재지 않았다.
3. `syncmill session` 안의 Claude를 herdr는 에이전트로 표시하지 않았다(1건).

4. monitor가 안내하는 `Ctrl-b d`는 실제 키보드에서 동작하지 않았다. herdr가 `ctrl+b`를 먼저
   받는다. `ctrl+b`를 두 번 누른 뒤 `d`를 누르면 detach됐다 [수동].

detach 키 외의 실제 키보드 조작과 herdr의 세션 복원 결과는 재지 않았다.

## 판정 요약

판정 기준: `간섭` = 한쪽이 다른 쪽의 상태를 바꾼다, `마찰` = 되지만 손이 더 간다,
`문제없음` = 잰 범위에서 기대대로 동작했다.

| ID | 항목 | 판정 | 근거 |
| --- | --- | --- | --- |
| B6 | 일반 `run`의 에이전트가 herdr 변수 3개를 상속 | 전달됨 (기록 변경과의 연결은 추론) | [실측] 가짜 에이전트 |
| B1 | headless 실행이 빈 셸 pane에 세션 기록을 남김 (claude, codex) | 간섭 | [실측] |
| B1 | 같은 실행, kimi-code | 표본에서 변화 없음 | [실측] |
| B2 | 대화형 Claude가 있는 pane에서 새 `run` | 세션 기록은 바뀌지 않음 | [실측] 셸 모드 1건 |
| B2 | 그 pane의 식별자로 `continue` | **간섭** — pane의 세션 기록이 바뀜 | [실측] 1건, 다른 pane에서 실행. 복원 결과는 [미실측] |
| B2 | herdr 변수 3개를 빼고 `continue` | 기록이 바뀌지 않음 | [실측] 1건, 에이전트 없는 pane |
| B5 | 실행 중 pane 닫기 | **간섭** — 에이전트 프로세스가 남음 | [실측] 가짜 에이전트 1건 |
| B5 | 실행 중 `ctrl+c` | 문제없음 | [실측] 1건 |
| B5 | `session`에 붙은 pane 닫기 | 문제없음 | [실측] 1건 |
| A5 | `session start` 안의 에이전트 인식 | 마찰 — herdr가 에이전트로 표시하지 않음 | [실측] claude 1건 |
| A2 | `tui` 렌더링·탭 전환·폭 변경·종료 | 전환·폭 변경·종료는 문제없음. 에이전트 열이 `kimi-cod`로 잘림 (Board·Rooms는 꺼진 상태) | [실측] API 조작. pane 크기와 종료 뒤 화면은 파일로 남기지 않음 |
| A4 | `run --monitor` 중첩 tmux, 완료 후 detach | 마찰 — 안내된 `Ctrl-b d`가 동작하지 않음. `ctrl+b`를 두 번 누르면 됨 | 화면·종료는 [실측] API 조작, detach 키는 [수동] 1건 |
| A6 | TTY 게이트의 안내문과 종료 코드 | 문제없음 | [실측] |
| C1 | 끝에 알림 명령 붙이기 | 종료 코드가 `rc`에 저장됨, 알림 API는 `shown: true` | [실측]. 실제 표시와 본문, 자동 알림 유무는 [미실측] |
| C2 | `pane wait-output`으로 완료 대기 | 마찰 — 실행마다 고유 표식이 필요함 | [실측] |

## 상세

### B6 — 에이전트는 herdr 변수를 물려받는다

- 기대 [읽기]: `agents.py`의 `_isolated_process_env`는 cwd·git 라우팅 변수, 실행 IPC 변수
  (`SYNCMILL_MONITOR_DIR`, `SYNCMILL_RUN_ID`), workflow job 식별자를 빼고 `PWD`를 worktree로
  다시 쓴다. herdr 변수는 빼지 않는다.
- 관측 [실측]: 가짜 `codex`가 진입점에서 본 값 — `HERDR_ENV`, `HERDR_PANE_ID`,
  `HERDR_SOCKET_PATH` 세 개 모두 있음. 같은 명령을 `env -u`로 세 변수를 빼고 실행하면 세 개
  모두 없음.
- 다른 경로 [읽기]: 게이트는 같은 함수의 결과를 쓰고, `gate.env_allowlist`가 기본값(`None`)이면
  더 좁히지 않는다. 즉 게이트 명령을 설정하면 기본 상태의 게이트도 `HERDR_*`를 받는다.
  게이트에 allowlist를 정하면 공통 변수와 지정한 이름만 남는다. workflow 문맥의 에이전트는
  공통 변수, provider별 인증 변수, 설정한 allowlist만 받고 `PWD`가 다시 쓰인다.
  `start --trial`은 부모 환경에서 로캘 변수만 가져오고 나머지는 새로 만든다. 이 경로들은
  실행해 확인하지 않았다.

### B1 — headless 실행이 pane에 세션 기록을 남긴다

빈 셸 pane(에이전트 없음, 기록 없음)에서 `syncmill run -a claude "Reply with the single word OK"`를
실행했고, `-a codex`와 `-a kimi-code`로도 각각 새 pane에서 한 번씩 실행했다.

| 에이전트 | 실행 전 기록 | 관측 | manifest의 세션 ID | 일치 |
| --- | --- | --- | --- | --- |
| claude | 없음 | 3초 뒤 `claude/b158f481-…`. 실행이 끝난 뒤에도 같은 값(약 1분간 변화 없음) | `b158f481-…` | 일치 |
| codex | 없음 | 5초 뒤 `codex/01a20c32-…`. 실행 종료 직후 조회에서도 같은 값(이 조회는 파일로 남기지 않음). 그 뒤 pane을 닫아 표본은 조회 오류로 끝남 | `01a20c32-…` | 일치 |
| kimi-code | 없음 | 첫 표본부터 35초 동안 변화 없음. 그 뒤 pane을 닫아 표본은 조회 오류로 끝남. 에이전트 실행 시간은 5.92초 | `session_ac1d66ef-…` | 기록 안 됨 |

- 조회에 성공한 표본에서 pane의 `agent`는 `None`, `agent_status`는 `unknown`이었다. herdr는 그 pane에
  에이전트가 있다고 표시하지 않으면서 세션 기록만 가진 상태가 된다.
- kimi-code는 표본에서 변화가 없었다는 것까지만 말할 수 있다. hook이 headless 모드에서 안
  도는 것인지, hook이 세션 ID 없는 이벤트를 받은 것인지, 보고가 거절된 것인지는 가리지 못했다.
  hook이 받은 입력과 전송 여부는 관측하지 않았다.
- herdr 서버 로그에서 hook의 보고(`pane.report_agent_session`)는 찾지 못했다. 로그에 남는
  메서드는 `notification.show`, `pane.split`, `agent.start` 같은 일부였다. 그래서 이번에는
  로그로 hook의 전송 여부를 확인할 수 없었다.

### herdr가 세션 보고를 받아들이는 규칙 (대조 실험)

hook과 같은 형태의 보고를 `herdr pane report-agent-session`으로 평가용 pane에 직접 보냈다.
**이 pane에는 감지된 에이전트가 없었다.** 살아 있는 에이전트가 있는 pane에서 같은 규칙이
적용되는지는 아래 B2의 두 건이 전부다.

| 순서 | 보낸 보고 | 결과 |
| --- | --- | --- |
| 1 | 기록 없는 pane에 첫 보고 (`seq`·시작 유형 없음) | 받아들임 |
| 2 | 다른 ID, `seq`·시작 유형 없음 | 무시 |
| 3 | 다른 ID, `seq`=현재 시각(ns) | 무시 |
| 4 | 다른 ID, `seq`=현재 시각, `session-start-source=startup` | 무시 |
| 5 | 다른 ID, `seq`=현재 시각, `session-start-source=resume` | **받아들임** |
| 6 | 다른 ID, `seq`=현재 시각, `session-start-source=clear` | **받아들임** |
| 7 | 다른 연동(`herdr:codex`)의 `startup`, `seq`=현재 시각. pane은 claude 기록 보유 | 무시 |

이 pane에서 herdr 0.9.5는 이미 있는 기록을 새 `startup` 보고로는 바꾸지 않았고, `resume`과
`clear` 보고로는 바꿨다. 실제 실행 두 건도 같은 방향이었다: 기록이 있는 pane에서의 새
`run -a claude`는 기록을 바꾸지 않았고, `syncmill continue`는 바꿨다(`a3` 대조 ID →
`567dd60c-…`, manifest와 일치). 실제 실행의 hook이 어떤 시작 유형을 보냈는지는 관측하지
않았다. `resume`이라는 것은 대조 실험에서만 확인한 조건이다.

### B2 — 대화형 Claude가 있는 pane

평가용 pane에 대화형 Claude를 띄웠다. herdr는 이 pane을 `agent=claude`, `idle`, 세션
`f44edf37-…`로 표시했다.

- **새 `run`** [실측]: 그 Claude의 셸 모드(`!`)에서 `syncmill run -a claude …`를 실행했다.
  자식 세션은 `de71032a-…`였고 pane 기록은 `f44edf37-…` 그대로였다.
- **`continue`** [실측]: 다른 셸 pane에서 `HERDR_PANE_ID`를 호스트 pane의 값으로 바꿔
  `syncmill continue`(앞선 claude 실행의 run ID 지정)를 실행했다. 3초 뒤 호스트 pane의 기록이 `f44edf37-…`에서
  `b158f481-…`(이어 간 headless 세션)로 바뀌었고, 표본이 끝날 때까지 약 40초간 그대로였다. 그동안 herdr는
  호스트를 계속 `claude`·`idle`로 표시했다. 호스트 Claude가 어느 대화를 하고 있는지는 따로
  확인하지 않았다.
  - 한계: 이 실행은 호스트 Claude의 셸 안에서 한 것이 아니다. hook은 `HERDR_ENV`, 소켓 경로,
    pane ID를 환경에서 읽고, 세션 ID·transcript 경로·시작 유형·서브에이전트 여부를 Claude가
    넘기는 입력에서 읽는다. 셸 모드 안에서 실행해도 같은 보고가 나갈 것이라는 것은 **가설**이다.
- **변수를 뺀 `continue`** [실측]: 에이전트가 없고 대조 기록을 심어 둔 pane에서
  `env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_SOCKET_PATH`를 붙여 같은 `continue`를 실행했다.
  자식 세션은 `b158f481-…`였고 pane 기록은 심어 둔 값 그대로였다(27초간 변화 없음). 같은
  조건에서 변수를 빼지 않았을 때는 기록이 바뀌었다(위 대조 실험 절의 `a3` 사례).
- 영향 [읽기]: herdr 0.9.5의 기본 설정 주석에 따르면 `session.resume_agents_on_restore`의
  기본값은 `true`이고, 서버 재시작 뒤 에이전트 pane을 원래 대화로 되살리며, 세션 참조를
  보고하는 공식 연동이 있어야 한다. 기록이 바뀐 pane이 어떤 대화로 복원되는지는 **재지
  않았다.** 재시작하면 진행 중인 다른 작업이 끊기므로 생략했다.
- 세션을 이어 가는 경로 [읽기]: `continue`, 그리고 대화 연속성이 켜져 있고 쓸 세션이 있을 때
  같은 실행 안에서 한 에이전트가 같은 worktree에서 다시 불리는 단계. Room의 라운드와 repair도
  저장된 세션을 이어 갈 수 있다(`rooms.py`). 실측한 것은 `continue`뿐이다.
- 부수 관측 [실측]: 셸 모드 명령이 끝난 뒤 herdr 상태가 `idle → working → done → idle`로
  바뀌었고 화면에는 `Imagining…` 표시가 나왔다. 이어서 호스트 Claude(2.1.331)가 그 출력에 대한
  응답을 출력하는 것을 봤지만, 그 화면은 파일로 남기지 않았다.

### B5 — pane을 닫으면

일반 `run` 두 경우는 가짜 에이전트(`ctrl+c`는 60초, pane 닫기는 45초 잠듦)로 각 1건씩 쟀다.
에이전트는 SyncMill과 다른 프로세스 그룹에 있었다(에이전트 pid = 자신의 pgid). `session`
경우는 A5의 실제 Claude 세션이다. 60초 설정과 `ctrl+c` 뒤 4초 대기는 실행한 명령 기준이고
증거 파일에는 없다.

| 경우 | SyncMill | 에이전트 | 남은 것 |
| --- | --- | --- | --- |
| 실행 중 `ctrl+c` | 4초 뒤 조회에서 종료, exit 1, `Aborted!` | 종료 | 복구용 workspace 유지, manifest `recovery_required` |
| 실행 중 pane 닫기 | 2초 뒤 조회에서 종료 | **2초·10초 뒤 실행 중.** 51초 뒤 조회에서 종료(잠드는 시간 45초) | manifest가 `running`에 멈춤. workspace는 그 뒤에 돌린 `run`이 지움 |
| `session`에 붙은 pane 닫기 | — | tmux 세션은 detached로 유지 | `session list`에 보이고 `session stop`으로 정리됨 |

- 어떤 신호가 SyncMill을 끝냈는지는 기록하지 않았다.
- workspace 정리는 실행 표식이 있는 디렉터리만 대상으로 하고, 복구 표식이 있으면 건너뛰며,
  보통은 소유한 SyncMill 프로세스의 생존을 본다. 오래된 것은 나이로도 지운다
  (`worktree.py`의 sweep) [읽기]. 에이전트 자식의 생존은 보지 않는다. 남은 에이전트가 아직 그 worktree에서 작업 중일 때 다음 `run`이 그 workspace를
  지우는 경우는 재지 않았다.
- 실제 에이전트 CLI가 pane이 닫힌 뒤에도 작업을 계속하고 토큰을 쓰는지는 재지 않았다.
  가짜 에이전트의 결과로 보면 그럴 가능성이 있다.
- 이것은 pane을 닫는 경우다. herdr에서 Ghostty 창을 닫는 것은 클라이언트 분리이고 pane은
  서버에 남는다(herdr 사용 문서 기준, [읽기]).

### A5 — `syncmill session` 안의 에이전트

실제 `claude`로 `session start claude`를 한 번 실행했다(프롬프트는 보내지 않았다).

- 붙어 있는 동안 pane의 `agent`는 `None`, `agent_status`는 `unknown`이었고 `herdr agent list`
  에 이 pane은 없었다. 같은 탭의 대화형 Claude(중첩 없이 띄운 것)는 목록에 있었다.
  herdr의 알림이나 에이전트 순회가 이 세션에서 어떻게 되는지는 재지 않았다.
- 안쪽 Claude가 시작된 뒤 바깥 pane에 세션 기록(`e5c9bc86-…`)이 생겼다. detach 뒤에도,
  `session stop`으로 tmux 세션을 없앤 뒤의 조회에서도 그 기록은 pane에 있었다.
- 다른 pane에서 `session attach`로 다시 붙었을 때 두 pane 어느 쪽도 에이전트로 표시되지
  않았다. 재접속 뒤에 읽은 그 pane의 기록은 앞선 B1 `continue` 실험이 남긴 값(`567dd60c-…`)과
  같았다. 재접속 직전 값은 읽지 않았다. 이 세션의 기록은 처음 만든 pane에 그대로 있었다.

### A2 — TUI

- 평가용 pane에서 열었다. `herdr pane layout`으로 본 pane 크기는 119×68과 60×68이었다(크기와
  `q` 뒤의 화면은 실행 당시 출력으로만 확인했다).
- 7개 탭 전환, Runs의 Operations/Artifacts 전환, 폭이 바뀔 때 다시 그리기, `q` 뒤 셸 프롬프트
  복귀를 확인했다. 60칸에서는 목록과 상세가 줄바꿈으로 접혔다. Artifacts 목록의 에이전트
  열은 두 폭 모두에서 `kimi-cod`로 잘려 보였다.
- 보드와 Rooms는 꺼진 상태(limited mode)여서 두 탭은 안내문만 확인했다. 탭 안에서의 작업,
  한글 데이터의 폭 정렬, Input 위젯의 한글 입력은 [미실측]이다.

### A4 — `--monitor`

- 가짜 에이전트 2개로 `run -s compete --monitor`를 실행했다. herdr pane 안에 중첩 tmux가 떴고,
  창 크기는 117×65였다(클라이언트 117×66, pane 3개). 세션을 만들 때의 인자는 220×50이다.
- 실행이 끝난 뒤 `ctrl+b d`를 pane에 직접 써서 detach하니 OUTER가 결과를 출력하고 exit 0으로
  끝났다. 직후 조회에서 평가용 tmux 서버는 없었다(실행 당시 출력으로만 확인).
- 완료 후 control pane의 제목은 `run complete · detach: Ctrl-b d`다. herdr의 prefix도
  `ctrl+b`다.
- 실제 키보드 [수동]: 같은 화면을 다시 띄워 사용자가 직접 눌렀다. `ctrl+b` 다음 `d`는 아무
  일도 일어나지 않았다. `ctrl+b`를 두 번 누른 뒤 `d`를 누르자 detach됐고, 그 뒤 pane에는
  `[detached (from session …)]`, 결과 출력, exit 0이 찍혀 있었으며 평가용 tmux 서버는 없었다
  (뒤의 세 가지는 [실측]). herdr 기본 키맵에 `prefix+d`는 없다 [읽기].
- 부수 관측: 이 실행에서 `[syncmill] run:`과 `[syncmill] artifact:` 줄은 INNER 화면에만 있었고
  OUTER의 출력에는 없었다.
- 실행 중 detach와 attach 실패 경우는 [미실측]이다. 코드상으로는 실행 중에 detach하면
  OUTER가 결과를 잠깐 기다린 뒤, 결과가 없으면 작업을 남겨 둔 채 돌아온다 [읽기]. 그때의
  종료 코드는 attach 성공 여부와 결과 도착 여부, 패치 저장 결과에 따라 달라지므로 OUTER의
  종료 코드만으로 작업 완료를 판단할 수 없다.

### A6 — TTY 게이트

평가용 pane의 zsh에서, 표의 조건대로 표준입출력을 바꿔 실행했다. "파일"은 stdout을 파일로
돌린 것, "파이프"는 `| cat`이다. 둘 다 stdout이 TTY가 아니다. 파이프 행의 exit는 `$?`가
아니라 zsh의 `${pipestatus[1]}`로 읽은 SyncMill 단계의 값이다(`$?`는 `cat`의 0이 된다).

| 명령 | stdin / stdout | exit | 출력 |
| --- | --- | --- | --- |
| `start` | `/dev/null` / 파일 | 1 | 터미널이 필요하다 + `start --trial` 안내 |
| `start` | `/dev/null` / 파이프 | 1 | 같음 |
| `tui` | TTY / 파이프 | 1 | `tui needs an interactive terminal (stdout is not a TTY).` |
| `dashboard` (보드 켬) | TTY / 파이프 | 1 | `dashboard needs an interactive terminal (stdout is not a TTY).` |
| `dashboard` (보드 끔) | TTY / 파일 | 1 | 보드를 켜는 방법 안내 (TTY 검사보다 먼저 나옴) |
| `session list` | TTY / 파일, TTY / 파이프 | 0 | `no active sessions` |
| `session attach` | TTY / 파이프 | 1 | `session needs an interactive terminal (stdout is not a TTY).` |
| `session start` | TTY / 파이프 | 1 | 같음 |
| `run -s compete --monitor` | TTY / 파이프 | 0 | 경고 후 monitor 없이 실행 |
| `run -s route --monitor` | TTY / 파일 | 0 | concurrent 전략에만 적용된다는 경고 후 실행 |

- stdout이 TTY인 경우는 A2(`tui`), A4(`--monitor`), A5(`session start`·`attach`·`list`·`stop`)에서
  각각 정상 동작을 확인했다. `dashboard`와 bare `start`는 TTY에서 실행하지 않았다.
- herdr 안의 Claude가 Bash 도구로 부를 때도 `tui`는 같은 오류(exit 1), `session list`는
  exit 0이었다.

### C1 — 완료 알림

가짜 `run`이 끝난 뒤의 조회에서 셸 pane의 `agent_status`는 `unknown`, 제목은 비어 있었다
(B6 실행 뒤 조회, 파일로 남기지 않음). 실행 중의 값은 읽지 않았다. 일반 `run` 경로는 터미널
제목이나 알림 시퀀스를 직접 내보내지 않는다 [읽기]. 실행이 끝났다는 herdr의 자동 알림이
있었는지는 관찰하지 않았다. 끝을 명시적으로 알리고 싶으면 알림 명령을 붙일 수 있다.

```bash
syncmill run "..."; rc=$?; herdr notification show "syncmill 끝" --body "exit $rc"; (exit $rc)
```

마지막의 `(exit $rc)`가 없으면 이 줄 전체의 종료 상태는 알림 명령의 것이 된다. 이 형태로
실행했을 때 성공 fixture는 0, 실패 fixture는 1이 `rc`에 저장되고 줄 전체의 종료 상태로도
나왔다. 알림 명령은 두 번 다 `shown: true`를 돌려줬다. 알림이 실제로 어떻게 보였는지는
확인하지 못했다.

### C2 — `pane wait-output`

- 이번 fixture에서의 출력 끝과 종료 코드: 성공한 `run`과 에이전트가 실패한 `run`(단일·전체
  실패)은 `[syncmill] run: <id>`, `[syncmill] artifact: <경로>` 순으로 끝났고 종료 코드는 0과
  1이었다. 잘못된 옵션은 `Error: …`로 끝났고 2였다.
- 이 두 줄은 계약이 아니다 [읽기]. 아티팩트가 없으면 나오지 않고, 패치가 있으면 `review:`·
  `apply:` 줄이, 복구용 workspace가 남으면 그 안내가 뒤에 붙는다. monitor 경로의 OUTER에는
  없었다(A4).
- `wait-output`은 이미 화면에 있는 출력도 바로 찾는다. 앞선 실행이 남긴 `artifact:` 줄에
  즉시 걸렸다.
- 그래서 SyncMill 출력에 기대지 않고 표식을 붙인다. 표식은 **실행마다 다른 ID**를 쓰고,
  입력한 명령줄 자체와는 일치하지 않게 만든다. 이 표식은 셸에서 그 명령이 돌아왔다는 뜻이다.
  `--monitor`에서 일찍 detach한 경우처럼 명령은 돌아왔는데 작업은 계속되는 때가 있다.

아래는 이번 평가에서 쓴 도우미를 줄인 것이다. 조종하는 쪽 셸에서 ID를 만들고, 표식을 찍는
명령을 대상 pane에 보낸 뒤, 조종하는 쪽에서 기다린다.

```bash
pane=w1:p2                      # 대상 pane ID로 바꾼다
id=$(uuidgen | tr -d -)
herdr pane run "$pane" "syncmill run '...'; printf 'DONE_%s_RC=%s\\n' $id \$?"
herdr pane wait-output "$pane" --regex "^DONE_${id}_RC=[0-9]+\$" \
  --source recent-unwrapped --timeout 600000
```

## herdr 안에서의 권장 사용법

각 항목에 근거의 종류를 적었다.

- **대화를 이어 가고 싶은 에이전트 pane의 식별자를 가진 채로 `syncmill continue`를 돌리지
  않는다.** 전용 셸 pane에서 돌리면 대화형 pane을 겨냥하지 않는다. 그 셸 pane에 어떤 기록이
  남는지는 그 pane이 이미 가진 기록에 따라 다르다(B1과 대조 실험). herdr 변수를 빼고
  실행하는 방법도 있다. 변수를 빼면 가짜 에이전트가 그 변수를 보지 못했고(B6), 실제 claude의
  `continue` 1건에서 pane 기록이 바뀌지 않았다(B2). 대화형 Claude가 떠 있는 pane에서 직접
  확인한 것은 아니다.

  ```bash
  run_id=...                    # 이어 갈 run ID로 바꾼다
  env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_SOCKET_PATH syncmill continue "$run_id" "..."
  ```

- **실행 중인 pane은 닫지 않는다.** 멈추려면 `ctrl+c`를 먼저 누른다. 근거는 가짜 에이전트
  각 1건이다(B5). 실제 provider에서는 확인하지 않았다.
- **herdr의 상태 표시가 필요한 대화형 에이전트는 `herdr agent start`로 띄운다.**
  `syncmill session start` 안의 Claude는 herdr에 에이전트로 표시되지 않았다(A5). 반대로
  `syncmill session`은 tmux 세션이라 pane을 닫아도 남고 다른 pane에서 다시 붙을 수 있다(B5).
  memtomem을 직접 공유해 협업하는 경우라면, 어느 쪽으로 띄우든 같은 저장소를 가리키고 쓸 때
  `shared` namespace를, 읽을 때 shared 포함을 명시해야 한다는 조건은 같다 [읽기]. 협업
  자체는 이번에 시험하지 않았다.
- **긴 `run`의 끝을 기다리거나 알림받고 싶으면 실행별 표식과 알림 명령을 붙인다**(C1, C2).
  표식과 알림은 셸에서 그 명령이 돌아왔다는 뜻이다. `--monitor`에서 일찍 detach하면 작업이
  진행 중이어도 나간다. 그 경우에는 monitor 화면이나 아티팩트의 최종 결과를 확인한다.
- **`--monitor` 화면에서 빠져나올 때는 `ctrl+b`를 두 번 누른 뒤 `d`를 누른다**(A4, [수동]
  1건). 화면에 안내된 `Ctrl-b d`는 herdr 안에서 동작하지 않았다. 다른 pane에서
  `herdr pane send-keys "$pane" ctrl+b d`를 실행해도 detach된다(A4, [실측]). `session attach`
  화면도 같은 tmux detach 키를 쓰지만 실제 키보드로는 확인하지 않았다.
- `tui`는 herdr pane 안에서 그려지고 API로 조작됐다. 실제 키 조작은 확인이 남아 있다.

## 조치 후보

수정이나 이슈 등록은 하지 않았다. 제목 초안과 먼저 정할 것만 적는다.

**SyncMill 쪽**

- headless provider를 띄우는 지점에서 `HERDR_ENV`, `HERDR_PANE_ID`, `HERDR_SOCKET_PATH`를
  빼는 것을 검토하기. 세 hook이 전제로 삼는 변수이고 B6에서 잰 것도 이 셋이다. B1·B2의 관측과
  hook 소스에 근거한 가설이므로, 고친 뒤 일반 실행과 monitor 실행 양쪽에서 B1·B2를 다시 재야
  한다. `_isolated_process_env`는 게이트도 같이 쓰므로 게이트 환경까지 바꿀지는 따로 정한다.
  `session`은 대화형 에이전트를 띄우는 다른 경로다. 그 에이전트가 바깥 pane에 보고하는 것이
  의도된 동작인지도 따로 정한다.
- 일반 `run`이 pane이 닫힐 때 어떤 신호로 끝나는지 확인하고, 그때 에이전트 프로세스 그룹과
  manifest를 정리하기. monitor의 INNER에는 이미 SIGHUP·SIGTERM 핸들러가 있고, 예외 시
  manifest를 복구 상태로 표시하는 경로도 있다 [읽기]. 기존 이슈가 있는지는 확인하지 않았다.
- monitor의 `detach: Ctrl-b d` 안내가 herdr 안에서는 그대로 동작하지 않았다(A4, [수동] 1건).
  `session`의 안내와 문서도 같은 키를 전제로 한다 [읽기]. 바깥 멀티플렉서나 바뀐 prefix를
  안내에 반영할지 정한다.
- monitor를 거친 실행의 OUTER 출력에 `run:`/`artifact:` 줄이 없었다(1건 관측).

**herdr 쪽**

- claude·codex 연동 hook에는 headless 실행(`claude -p`, `codex exec`)을 가르는 검사가 없다
  [읽기]. Claude hook은 Cursor·서브에이전트·이벤트 종류를, Codex hook은 transcript 경로와
  상속된 `CODEX_THREAD_ID`가 다른 세션인지를 검사한다. Kimi hook은 `session` 동작에서는 세션
  ID가 없으면 보고 없이 끝나고, 그 밖의 동작에서는 ID 없이도 상태를 보고한다. 사용자의 `herdr_checkpoint_token.py`는 사이드바 배지를 위해 `CLAUDE_CODE_ENTRYPOINT`로
  headless 실행을 거른다. 연동 hook은 herdr가 관리하는 파일이라 직접 고치면 업데이트 때 덮인다.
- 서로 다른 두 관측이 있다. (1) 에이전트가 없는 pane에서 직접 보낸 `resume`·`clear` 보고가
  기존 기록을 바꿨다. (2) 대화형 Claude가 있는 pane의 식별자로 `continue`를 실행한 뒤 그
  pane의 기록이 바뀌었다(1건). (2)에서 hook이 실제로 무엇을 보냈고 서버가 무엇을 받았는지는
  잡지 못했다. 상류에 알리려면 그 확인과 재시작 뒤의 복원 결과가 먼저 필요하다.

**사용 습관 쪽** — 위 "권장 사용법".

## 평가가 남긴 것

- 실제 provider 프로필: 임시 경로용 Claude 프로젝트 디렉터리 9개, 평가 경로를 언급하는 codex
  세션 파일 2개, `~/.claude.json`의 임시 저장소 신뢰 항목 1개. 지우지 않았다.
- 실행 전후로 같았던 것(잰 지표 그대로): `~/.syncmill`의 깊이 2까지의 mtime 요약,
  `~/.memtomem/memtomem.db`의 mtime과 크기, `~/.tmux/resurrect`의 파일 수(0), 기본 tmux
  소켓의 부재, 저장소 `git status`(문서 작성 전 0줄).
- `/private/tmp/tmux-501/`에는 평가 중에 `dispprobe2`라는 소켓이 하나 늘었다. 평가용 tmux는
  다른 디렉터리의 소켓을 썼고, 이 소켓이 어디서 생겼는지는 확인하지 않았다.
- 실제 `HOME`으로 돌린 claude 실행 1건의 출력에는 memtomem 경고가 있었다: 저장된 임베딩
  모델(`none/`)이 설정(`onnx/multilingual-e5-small`)과 다르고 `embedding_dimension=0`이라
  복구 모드로 계속한다는 내용이다. 원인은 따지지 않았다.

## 측정하지 못한 것

| ID | 항목 | 이유 |
| --- | --- | --- |
| B2 | herdr 서버 재시작 뒤의 세션 복원 결과 | 진행 중인 다른 작업이 끊겨 재시작을 생략했다 |
| A2·A5 | 실제 키보드의 `alt+숫자`, 마우스, 한글 입력, `prefix+z`·`prefix+e`, `session attach` 화면의 detach 키 | API로 보낸 키는 herdr 클라이언트를 거치지 않는다. 사람이 직접 눌러야 한다 |
| B1 | kimi-code에서 기록이 생기지 않은 원인 | 표본 관측만 했다 |
| B2 | 대화형 Claude pane에서의 headless codex | 에이전트 없는 pane의 대조(순서 7)만 있다 |
| B2 | `!` 셸 모드 안에서의 `continue`, 대화형 Claude가 있는 pane에서 변수를 뺀 `continue` | 실 호출을 더 쓰지 않았다 |
| B2 | 기록이 바뀐 뒤 호스트 Claude가 어느 대화를 하고 있었는지, hook이 보낸 내용과 서버의 수신 | 관측 수단을 두지 않았다 |
| B4 | `session start` 뒤 바깥 pane에 남는 기록 | A5와 한 실험으로 쟀다. 기록이 생기고 남는 것까지 확인했고, 그 기록으로의 복원은 재지 않았다 |
| A2 | Board·Rooms 탭 안에서의 작업, 한글 데이터의 폭 정렬 | 두 기능을 켜지 않았고 한글 데이터를 넣지 않았다 |
| A5 | `session` 안 에이전트에 대한 herdr의 알림·에이전트 순회 | 목록과 상태 필드만 봤다 |
| A6 | TTY에서의 bare `start`와 `dashboard` | 실행하지 않았다 |
| B5 | SyncMill을 끝낸 신호, 실제 provider가 pane이 닫힌 뒤 계속 도는지와 토큰 사용 | 가짜 에이전트 1건만 쟀다 |
| C1 | 알림의 실제 표시와 본문, 실행이 끝난 뒤의 자동 알림 | API 응답만 확인했다 |
| B3 | headless 실행 **중**의 자동 알림 | 자동 알림과 실제 클라이언트 화면을 관찰하지 않았다. 로그에는 명시적 `notification.show` 요청만 보였다 |
| A1 | `start --trial` 완주 | 1차 범위 밖 |
| A3 | `dashboard` | 1차 범위 밖 |
| A7 | Rooms `t` 뷰어 | 1차 범위 밖 |
| A8 | 사이드바 표시의 시계열 | 셸 pane의 `unknown`·빈 제목만 봤다. 화면은 보지 않았다 |
| A4 | `--monitor` 실행 중 detach, attach 실패 | 1차 범위 밖 |
| B5 | detach된 `session`의 pane 닫기, `apply --detach` 워커 실행 중 pane 닫기, 남은 에이전트와 다음 `run`이 겹치는 경우 | 1차 범위 밖 |
| B6 | workflow·게이트 환경의 `HERDR_*` 전달 | 읽기만 했다 |
| C3 | herdr 안 Claude의 Bash 도구로 `run`·`diff` 등을 부리는 흐름 | `tui`·`session list`만 확인했다 |
| C4 | 역할 겹침 전체(`--monitor` ↔ 분할 pane, worktree), `session open`·Sessions 탭 | 1차 범위 밖 |
| C5 | `herdr worktree` 워크스페이스, linked worktree에서의 실행 | 1차 범위 밖 |
| C6 | interactive 협업 흐름(`run --exclude`) | 1차 범위 밖 |

## 부록 — B2와 B5의 명령과 전후 값

pane ID와 세션 ID는 이 날 평가용으로 만든 것이다. 세션 ID는 앞 8자리만 적었다.
`<S>`는 임시 상태 디렉터리다.

### B2

| 단계 | 실행 위치 | 명령 | 대상 pane의 기록 (전 → 후) | 자식 세션 (manifest) |
| --- | --- | --- | --- | --- |
| 대조: 새 `run` | 에이전트 없는 pane, 대조 ID `c0de` 보유 | `syncmill run -a claude "Reply with the single word OK"` | `c0de` → `c0de` | `567dd60c` |
| 대조: `continue` | 같은 pane, 대조 ID `a3` 보유 | `syncmill continue d9722a75-… "Reply with the single word OK"` | `a3` → `567dd60c` | `567dd60c` |
| 호스트: 새 `run` | 대화형 Claude(`f44edf37`)의 셸 모드 | `syncmill run -a claude 'Reply with the single word OK'` | `f44edf37` → `f44edf37` | `de71032a` |
| 호스트: `continue` | 다른 셸 pane | `HERDR_PANE_ID=<호스트 pane> syncmill continue c496f529-… "Reply with the single word OK"` | `f44edf37` → `b158f481` | `b158f481` |
| 변수 제거 | 에이전트 없는 pane, 대조 ID `5eed0` 보유 | `env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_SOCKET_PATH syncmill continue c496f529-… "Reply with the single word OK"` | `5eed0` → `5eed0` | `b158f481` |

기록은 `herdr pane get <pane>`의 `agent_session.value`다.

### B5

가짜 에이전트는 `FAKE_SLEEP`초 잠든 뒤 codex 형식의 JSON을 출력하고, 시작할 때 자신의 pid와
pgid를 파일에 적는다.

| 경우 | 명령 | 중단 방법 | 조회 | 결과 |
| --- | --- | --- | --- | --- |
| `ctrl+c` | `FAKE_SLEEP=60 syncmill run -a codex 'say ok'` | 시작 5초 뒤 `herdr pane send-keys <pane> ctrl+c` | 4초 뒤 `kill -0` | 에이전트(pid 73918 = pgid)와 SyncMill(pid 73286) 모두 없음. exit 1. manifest `recovery_required`, workspace 유지 |
| pane 닫기 | `FAKE_SLEEP=45 syncmill run -a codex 'say ok'` | 시작 5초 뒤 `herdr pane close <pane>` | 2초·10초·51초 뒤 `kill -0` | SyncMill(pid 80551)은 2초 뒤 없음. 에이전트(pid 80934)는 2초·10초 뒤 있음, 51초 뒤 없음. manifest `running` |
| 그 뒤의 `run` | `syncmill run -a codex "say ok"` (pane 닫기 약 1분 뒤, 에이전트가 끝난 뒤) | — | `<S>/workspaces` 목록 | 닫힌 실행의 workspace가 없어짐(2개 → 1개). 복구 표식이 있는 것은 남음 |
