> **역사 자료 — 현재 제품 계약이 아닙니다.** 초기 v0.1 구현 계획에 대한 검토
> 기록으로, CLI·공급자·아키텍처 설명은 현재 상태와 다를 수 있습니다. 현재 사용법은
> [README](README.md)와 [사용자 가이드](docs/user-guide.md), 유지보수 계약은
> [CLAUDE.md](CLAUDE.md)를 기준으로 하세요.

# SyncMill v0.1 구현 플랜 — 종합 검토 의견

## 검토 요약

SyncMill의 핵심 아이디어 — 서로 다른 AI 코딩 에이전트(Claude Code, Gemini CLI, Codex CLI)가 공유 메모리를 통해 협업·경쟁하는 오케스트레이션 시스템 — 는 시의적절하고 아키텍처 방향도 올바르다. memtomem을 재구현하지 않고 의존성으로 사용하겠다는 결정, Strategy 패턴으로 오케스트레이션 전략을 추상화하겠다는 설계, 그리고 "만들지 않는 것"을 명시적으로 제외한 점은 MVP 범위 통제에 효과적이다.

다만 아래 6가지 영역에서 설계 수정 또는 범위 조정이 필요하다.

---

## 1. 에이전트 격리 원칙의 명문화 [Critical]

### 문제

현재 플랜에 "각 CLI는 독립적 인스턴스로 동작한다"는 원칙이 암묵적으로 깔려 있으나, 이를 코드 수준에서 강제하는 장치가 없다. 세 가지 위반 가능 지점이 존재한다.

첫째, AgentRunner의 시그니처가 `Task` 객체를 통째로 받으면, 향후 Task에 memory 참조나 다른 에이전트 정보가 추가될 유혹이 생긴다. 둘째, "CLI 에이전트가 MCP 서버를 통해 memtomem에 직접 쓴다"는 설계는 에이전트 간 암묵적 의존성을 만든다(Claude가 쓴 scratch 값을 Gemini가 읽는 경로). 셋째, worktree 경로 충돌에 대한 잠금 메커니즘이 없다.

### 권장

플랜 상단에 다음 5개 격리 원칙을 추가하고, 각 원칙의 코드 수준 강제 방법을 명시한다.

| 원칙 | 의미 | 강제 방법 |
|------|------|-----------|
| 인증 격리 | SyncMill은 에이전트의 API 키를 관리하지 않는다 | health_check로 인증 상태만 확인. 미인증 시 에이전트 비활성화 |
| 프로세스 격리 | 각 에이전트는 독립 subprocess로 실행된다 | subprocess.run 호출 시 환경변수 상속만 허용. 프로세스 간 IPC 없음 |
| 통신 격리 | 에이전트 간 직접 통신은 불가하다 | Supervisor가 유일한 중재자. 에이전트는 서로의 존재를 모름 |
| 메모리 격리 | AgentRunner는 memtomem의 존재를 모른다 | AgentRunner 시그니처에서 memory 파라미터 제거. prompt in, result out만 허용 |
| 워크스페이스 격리 | 에이전트별 독립 git worktree를 사용한다 | worktree 경로에 agent_id + task_id 포함. 다른 에이전트 경로 접근 불가 |

AgentRunner의 시그니처를 다음과 같이 변경한다:

```python
# Before: Task 객체 전달 — memory, context 등에 접근 가능
class AgentRunner(Protocol):
    async def execute(self, task: Task, workdir: Path) -> TaskResult: ...

# After: 순수 문자열 입출력 — 에이전트는 prompt만 안다
class AgentRunner(Protocol):
    agent_id: str
    capabilities: set[str]

    def execute(self, prompt: str, workdir: Path,
                timeout: int) -> TaskResult: ...
    def health_check(self) -> bool: ...
```

Context 주입(이전 에이전트 결과, 검색된 메모리 등)은 Supervisor가 prompt를 조립하는 시점에 텍스트로 합친다:

```python
# Supervisor 내부에서 context를 prompt에 주입
context = memory.search_context(task.prompt)
enriched_prompt = f"""Context from previous work:
{context}

Task: {task.prompt}"""

result = agent.execute(
    prompt=enriched_prompt,
    workdir=worktree_path,
    timeout=config.timeout
)

# 결과 기록도 Supervisor가 단독으로 수행
memory.share_result(task, result)
```

---

## 2. 동기 실행으로 시작 [High]

### 문제

AgentRunner를 `async def execute`로 정의했으나, v0.1의 RouteStrategy는 에이전트를 1개만 선택하여 실행한다. `asyncio.create_subprocess_exec`는 외부 프로세스 대기만 하는 구조이므로 async의 이점이 전혀 없다. 반면 `asyncio.run()` 진입점 관리, 예외 전파, Click과의 호환성 등 복잡도는 증가한다.

### 권장

v0.1은 `subprocess.run` 동기 방식으로 구현한다. Protocol에는 sync 시그니처를 사용하고, v0.2 CompeteStrategy 도입 시 `asyncio.gather`가 필요해지는 시점에 async로 전환한다. 이를 통해 MVP 복잡도가 절반으로 감소한다.

```python
# v0.1: 동기 실행
class AgentRunner(Protocol):
    def execute(self, prompt: str, workdir: Path,
                timeout: int) -> TaskResult: ...

# v0.2 전환 시: async 추가
class AsyncAgentRunner(Protocol):
    async def execute(self, prompt: str, workdir: Path,
                      timeout: int) -> TaskResult: ...
```

---

## 3. memtomem 동시 쓰기 제거 [High]

### 문제

"동일 SQLite DB를 CLI 에이전트의 MCP 서버와 공유 (WAL 모드)"라는 설계에서, syncmill 프로세스와 MCP 서버 프로세스가 동시에 write하면 `SQLITE_BUSY` 에러가 발생한다. 에이전트 subprocess가 MCP를 통해 memtomem에 쓰는 동안 Supervisor도 `share_result`로 쓰는 시나리오가 이에 해당한다.

또한 이 구조는 에이전트 격리 원칙(1번)에도 위배된다. 에이전트가 memtomem에 직접 접근하면 다른 에이전트의 데이터를 읽을 수 있는 경로가 열린다.

### 권장

v0.1에서는 MCP 서버 공유를 제거한다. Supervisor가 memtomem의 유일한 writer이며, 에이전트는 stdout 결과만 반환한다.

데이터 흐름:
```
User → Supervisor → (prompt 조립 시 memtomem 읽기)
                   → Agent subprocess (prompt in, stdout out)
                   → Supervisor → (결과를 memtomem에 쓰기)
```

에이전트가 실행 중 중간 결과를 공유해야 하는 시나리오(v0.2+)에서는, 에이전트가 worktree 내 약속된 파일(예: `.syncmill/output.json`)에 쓰고 Supervisor가 이를 읽어 memtomem에 기록하는 방식으로 확장한다.

---

## 4. RouteStrategy 범위 축소 [Medium]

### 문제

"task.tags/context에서 필요 capability 추출 → agent.capabilities와 매칭 → 최고 점수 에이전트 선택"은 태스크에서 capability를 추출하는 분류 로직(LLM 호출 또는 키워드 매칭)이 필요하다. MVP 치고 과도한 범위이다.

### 권장

v0.1 RouteStrategy는 우선순위 기반 폴백으로 단순화한다:

```python
class RouteStrategy:
    def execute(self, task, agents, memory):
        # config에 설정된 순서대로 시도
        for agent in agents:
            if agent.is_available():
                return agent.execute(...)
        raise NoAvailableAgentError()
```

기본 순서: Claude → Gemini → Codex (설정으로 변경 가능). 3줄이면 끝나고 end-to-end 데모에는 충분하다. capability 기반 라우팅은 v0.2에서 태스크 분석 경험이 쌓인 뒤 추가한다.

---

## 5. 패키지 구조 단순화 [Medium]

### 문제

5개 서브패키지(`memory/`, `agents/`, `tasks/`, `orchestration/`, `workspace/`)에 15+개 파일은 v0.1 코드 규모(각 파일 30~80줄) 대비 과분리이다. 탐색 오버헤드가 구현 속도를 떨어뜨린다.

### 권장

v0.1은 flat 구조로 진행한다:

```
src/syncmill/
  __init__.py
  config.py        # SyncmillConfig (pydantic-settings)
  cli.py           # Click 진입점
  agents.py        # AgentRunner Protocol + Claude/Gemini/Codex + Registry
  memory.py        # SyncmillMemory (memtomem 래퍼)
  models.py        # Task, TaskResult, TaskStatus
  supervisor.py    # Supervisor + Strategy Protocol + RouteStrategy
  worktree.py      # git worktree 관리
```

8개 파일로 구성한다. v0.2에서 Strategy가 3개로 늘어나고 gates/가 추가될 때 패키지를 분리한다. 코드가 파일 분리를 요구하기 전에 미리 분리하면 리팩토링 비용만 증가한다.

---

## 6. 범위 축소 대상 [Low]

### CircuitBreaker → v0.2로 이동

연속 실패 threshold, reset_timeout, half-open 복구는 프로덕션 패턴이다. v0.1에서 에이전트 실패 시 에러를 올리면 충분하다. CircuitBreaker는 CompeteStrategy/PipelineStrategy에서 에이전트 자동 교체가 필요해지는 v0.2 이후에 의미가 있다.

v0.1에서는 `is_available()` 메서드에서 health_check 결과만 캐싱하는 수준으로 대체한다.

### 검증 시나리오 보강

"현재 디렉토리의 파일 목록을 알려줘"는 `ls`로도 가능한 태스크이다. SyncMill의 오케스트레이션 가치를 증명하려면 다음과 같은 시나리오가 필요하다:

```bash
# 검증 1: 기본 라우팅 동작
syncmill run "이 프로젝트의 README.md를 분석하고 누락된 섹션을 추가해줘"
# → 에이전트 자동 선택 → 파일 분석 + 생성 → memtomem 세션 기록

# 검증 2: 에이전트 폴백
# (Claude 미인증 상태에서)
syncmill run "main.py의 타입 힌트를 추가해줘"
# → Claude 비가용 → Gemini로 폴백 → 정상 실행

# 검증 3: 메모리 연속성
syncmill run "이전 태스크에서 수정한 파일 목록을 요약해줘"
# → memtomem에서 이전 세션 검색 → context로 주입 → 요약 생성
```

---

## 수정 후 v0.1 구현 순서

반영된 변경 사항을 기준으로, 권장 구현 순서는 다음과 같다.

| Step | 내용 | 예상 소요 |
|------|------|-----------|
| 1 | pyproject.toml + flat 프로젝트 구조 (8파일) | 30분 |
| 2 | config.py — SyncmillConfig (agent 순서, timeout, db_path) | 30분 |
| 3 | models.py — Task, TaskResult, TaskStatus | 20분 |
| 4 | agents.py — AgentRunner Protocol(동기) + 3개 구현체 + Registry | 2시간 |
| 5 | memory.py — SyncmillMemory (memtomem 래퍼, Supervisor 단독 쓰기) | 1시간 |
| 6 | supervisor.py — Strategy Protocol + RouteStrategy(우선순위 폴백) + Supervisor | 1시간 |
| 7 | worktree.py — git worktree 생성/정리 | 30분 |
| 8 | cli.py — syncmill run / agents / status | 1시간 |
| 9 | 검증 시나리오 3개 실행 + 디버깅 | 1시간 |

총 예상 소요: 약 8시간 (1일)

---

## v0.2 이후 로드맵 조정

v0.1 수정 사항이 v0.2 이후 계획에 미치는 영향:

| 항목 | 원래 계획 | 조정 |
|------|-----------|------|
| async 전환 | v0.1에 포함 | v0.2 CompeteStrategy 도입 시 전환 |
| CircuitBreaker | v0.1에 포함 | v0.2 PipelineStrategy와 함께 추가 |
| capability 매칭 | v0.1 RouteStrategy에 포함 | v0.2에서 태스크 분류기와 함께 추가 |
| MCP 공유 | v0.1에 포함 | v0.3에서 단방향(Supervisor→MCP) 게이트웨이로 추가 |
| 패키지 분리 | v0.1에 5개 서브패키지 | v0.2에서 strategy/, gates/ 분리 시작 |

---

## 결론

현재 플랜은 방향성은 올바르나, MVP 범위가 v0.2 수준의 기능을 포함하고 있다. 위 6가지 조정을 반영하면 v0.1이 1일 내에 end-to-end 데모가 가능한 수준으로 경량화되며, 에이전트 격리라는 핵심 아키텍처 원칙이 코드 수준에서 강제된다. 특히 "Supervisor가 유일한 중재자, 에이전트는 prompt in / result out의 순수 함수"라는 원칙은 v0.2 Pipeline, v0.3 Council로 확장할 때도 아키텍처 일관성을 유지하는 기반이 된다.
