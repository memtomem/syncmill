# toolgraph, tracegraph, syncmill 통합 구현 설계

> **Canonical tracked design.** 이 문서의 PR 라벨은 역사적 설계 라벨이다.
> 실제 landed 순서와 현재 완료/유보 경계는 [canonical integration plan](integration-plan.md)
> 및 [smoke runbook](smoke-runbook.md)에 기록한다. 이 reconciliation은 production
> runtime API나 schema를 변경하지 않는다.

**상태:** 검토 반영
**작성일:** 2026-07-11
**상위 문서:** [통합 실행 계획](integration-plan.md)
**대상:** toolgraph, tracegraph, syncmill maintainers

## 1. 문서 목적

이 문서는 상위 실행 계획의 P0(공통 계약)과 P1(관측 계측)을 파일/인터페이스 수준으로
상세화하고, P2(통합 보고서)와 P3(advisory preflight)의 방향과 결정사항을 정리한다.
모든 설계는 세 저장소의 실제 코드를 대조해 검증한 seam 위에 세웠다. 계획 리뷰에서
발견한 모순과 미기술 결정은 §8에 전문을 수록한다.

### 1.1 설계를 지배하는 세 가지 발견

1. **tracegraph는 src 변경 없이 syncmill trace를 ingest할 수 있다.**
   `OTLPSpanAdapter`(tracegraph `src/tracegraph/adapters/otlp_spans.py:66-68`)가 소비하는
   속성은 `openinference.span.kind`, `graph.node.id`, `graph.node.parent_id` 셋뿐이며
   모두 OpenInference/graph 표준 vocabulary다. syncmill이 이 셋을 직접 emit하면
   tracegraph-전용 adapter가 필요 없고, tracegraph 계획의 "syncmill 전용 속성이 core
   causal model을 오염하지 않는다" 원칙도 자동으로 지켜진다. syncmill 고유 의미는
   전부 additive `syncmill.*` 속성에 실리고 tracegraph는 이를 읽지 않는다.
2. **syncmill에는 cross-cutting 주입 seam이 이미 있다.** gate/breaker/monitor가
   `Strategy.execute(..., gate=, breaker=, monitor=)` kwarg로 주입되고
   (`strategies/base.py:65`), monitor 헬퍼 `_monitor_phase`/`_monitor_streaming`/
   `_monitor_status`(base.py:257-282)가 계획의 span 경계(phase/agent attempt)와 정확히
   일치한다. tracer는 같은 패턴의 네 번째 kwarg다. 실패 격리도 기존 degrade 관용구
   (catch → warning → degraded flag → continue)를 그대로 쓴다.
3. **toolgraph의 selector가 preflight 엔진이다.** `eligible_tools(agent, candidates,
   profile)`(`toolgraph/graph/selector.py`, ADR-0005)가 이미 eligible/rejected/
   reason/evidence-path를 배치로 산출한다. net-new는 CLI wrapper + artifact export +
   redaction pass뿐이다.

## 2. P0 — 공통 계약

### 2.1 run_id 발급과 전파

**형식:** `str(uuid4())`. board `run_item`이 이미 발급하는 run_id
(syncmill `supervisor.py:300`)와 동일한 형식이므로 lease fencing token,
`board-run:{run_id}` memory scope, envelope run_id가 마이그레이션 없이 하나의
식별자로 통합된다.

**해석 규칙 — 단일 resolver (`report.py::resolve_run_id`):**

```python
RUN_ID_ENV = "SYNCMILL_RUN_ID"

def resolve_run_id(explicit: str | None = None) -> str:
    """explicit > monitor-inner env(sentinel 이중검사 통과 시) > fresh uuid4."""
    if explicit:
        return explicit
    if inner_dir() is not None:          # env var + .syncmill-inner sentinel, monitor.py:108
        env = os.environ.get(RUN_ID_ENV)
        if env:
            return env
    return str(uuid4())
```

env 읽기를 `inner_dir()`로 게이트하는 이유: monitor의 env var + sentinel 파일
이중검사를 재사용해, 셸에 떠도는 `SYNCMILL_RUN_ID`가 무관한 두 실행을 같은 id로
오염시키지 못하게 한다. `SYNCMILL_RUN_ID`는 pydantic-settings와 충돌하지 않는다
(env source는 선언된 필드만 읽는다 — `SYNCMILL_MONITOR_DIR`로 검증된 패턴).
단, `run_id`를 `SyncmillConfig` 필드로 선언해서는 안 된다.

**세 실행 경로:**

| 경로 | 발급 지점 | 변경 |
| --- | --- | --- |
| direct `run` | `Supervisor.run` 진입부 | `run(..., run_id: str | None = None)` kwarg 추가, `run_id = resolve_run_id(run_id)` |
| monitor outer/inner | **OUTER** `cli.run`, `launch_monitored_run` 호출 전 | `_write_env_snapshot`(monitor.py:599)이 `INNER_ENV`를 주입하는 지점(:614)에서 `snapshot[RUN_ID_ENV] = run_id`를 함께 주입. `--run-id`는 의도적으로 `_inner_argv`에 넣지 않는다(argv는 결정적·id-free 유지). inner의 `Supervisor.run`이 env 분기로 해석. outer는 같은 run_id로 report 경로를 출력 |
| board `run_item` | 기존 발급 지점(supervisor.py:300) 유지 | `self.run(task, memory=board_memory, run_id=run_id)`로 전달 — lease, memory scope, envelope가 증명 가능하게 동일 id |

주의: board run의 run_id는 `board-run:{run_id}` memtomem namespace 이름으로도 잠시
나타났다가 `_gc_board_run_namespaces`가 지운다. **namespace는 GC 대상이지만 run
디렉터리(`~/.syncmill/runs/{run_id}/`)는 별개 수명(§4 retention)** — 둘을 연동하지
않는다.

### 2.2 실행 envelope 계약

위치: `~/.syncmill/runs/{run_id}/envelope.json`. 대상 repo 밖(home)에 두는 것이
"artifact directory는 commit 대상 제외" 보안 요구를 구조적으로 해결하는 메커니즘이다.
`schema_version`은 **정수** — tracegraph의 `ARTIFACT_SCHEMA_VERSION = 1`
(artifact.py:17, 엄격 `!=` 검사)과 일치시킨다.

```json
{
  "schema_version": 1,
  "kind": "syncmill.run-envelope",
  "run_id": "3f2c…",
  "created_at": "2026-07-11T00:00:00+00:00",
  "finished_at": "2026-07-11T00:03:12+00:00",
  "strategy": "compete",
  "team": null,
  "task_id": "uuid-of-Task.id",
  "agents": ["codex", "claude", "kimi-code"],
  "board": null,
  "preflight": {
    "status": "ok | disabled | timeout | error | unresolved_identity",
    "mode": "advisory",
    "profile": "review",
    "artifact": "preflight.json",
    "digest": "sha256:…",
    "graph_generation": 42,
    "warning_count": 1
  },
  "trace": {
    "status": "ok | disabled | error",
    "protocol": "otlp-json-file",
    "trace_id": "32-hex",
    "artifact": "trace.otlp.json",
    "digest": "sha256:…"
  },
  "result": {
    "status": "completed | failed",
    "agent_id": "codex",
    "winner": "codex",
    "exit_code": 0,
    "duration_seconds": 187.4,
    "gate_passed": true,
    "files_changed_count": 3,
    "artifact": "result.json",
    "digest": "sha256:…",
    "patch_digest": "sha256:…"
  },
  "degraded": ["memory"]
}
```

규칙:

- `artifact` 필드는 **envelope 디렉터리 기준 상대경로**다.
- `board`는 board run에서 `{"item_id": "...", "goal": "..."}`, 아니면 `null`.
- 연계가 꺼져 있어도 하위 블록은 `status: "disabled"`로 **항상 존재**한다 — 소비자가
  필드 부재로 분기하지 않는다.
- envelope에는 본문이 없다: prompt, output, patch 본문, 파일 경로 목록은 금지이고
  digest와 count만 허용한다. `result.json`(monitor `_result_to_json` 재사용,
  monitor.py:159)은 output/patch를 담는 유일한 artifact이며 envelope는 digest로만
  연결한다.

### 2.3 span naming 계약

**규칙 (모두 load-bearing):**

1. **span `name`은 구조 라벨** — tracegraph `diff`(AHU)의 라벨이
   `step.name or kind`(ahu.py:37)이므로 name은 **실행 간 안정적**이어야 한다.
   agent id, phase 이름, 인덱스만 허용. uuid/timestamp/run_id를 넣으면 모든 diff가
   divergence로 판정된다.
2. **`graph.node.id`는 trace 내 유일 id** (유일성만 요구; 인덱스 포함 가능).
3. **`traceId`/`spanId`는 무작위 32/16-hex** (OTLP 규격, tracegraph는 불투명 문자열로
   취급).
4. `parentSpanId` = 포함관계(run root 아래 평면), `graph.node.parent_id` = 단일 논리
   cause, span **links** = 추가로 소비된 cause(fan-in). tracegraph의 link → cause 승격
   (otlp_spans.py:300-341)은 같은 trace를 명시한 in-trace span만 승격하므로 links에는
   반드시 traceId를 채운다.

**span 표 (5개 전략 전체):**

| span name | kind → StepKind | `graph.node.id` | `graph.node.parent_id` | links (추가 cause) |
| --- | --- | --- | --- | --- |
| `syncmill.run` | `CHAIN` | `run` | — (root) | — |
| `attempt:{agent}` (route) | `AGENT` | `attempt:{agent}:{i}` | i=0이면 `run`; i>0이면 **직전 실패 attempt** (fallback = 이전 실패가 원인) | — |
| `stage:{agent}` (pipeline) | `AGENT` | `stage:{i}:{agent}` | stage 0은 `run`; 이후 `stage:{i-1}:…` | — |
| `attempt:{agent}` (compete fan-out) | `AGENT` | `attempt:{agent}` | `run` (**공통 부모 — 완료 순서는 인과가 아님**) | — |
| `gate:{agent}` | `TOOL` | `gate:{agent}[:{i}]` | 해당 attempt 노드 | — |
| `select` (compete/council/decompose 수렴) | `CHAIN` | `select` | **winner attempt 노드** (승격된 단일 cause → 승리 경로가 깨끗한 TREE_PARENT); winner 없으면 `run` | `_select_winner`가 **실제 검토한** 다른 COMPLETED 후보들 |
| `propose:{agent}` (council) | `AGENT` | `propose:{agent}` | `run` | — |
| `critique:{agent}` round r | `AGENT` | `critique:{agent}:{r}` | 자신의 직전 phase 노드 (worktree 연속성 = 실제 소비) | 직전 phase에서 shared에 **게시된** 노드들 (§8 발견 6의 근사) |
| `synthesize:{agent}` (council) | `AGENT` | `synthesize:{agent}` | 자신의 마지막 critique 노드 | 게시된 critique 노드들 |
| `plan:{agent}` (decompose) | `AGENT` | `plan` | `run` | — |
| `subtask:{i}:{agent}` | `AGENT` | `subtask:{i}` | `plan` (계획으로부터 fan-out) | — |
| `synthesize:{agent}` (decompose) | `AGENT` | `synthesize:{agent}` | `plan` | **COMPLETED** subtask 노드들 (완료분만 shared에 게시 = 소비 가능했던 것) |

timeout/cancellation: attempt span에 OTLP `status: {code: STATUS_CODE_ERROR, message:
"timeout" | "cancelled" | "exit_code=N" | "gate: <요약>"}` — 경계된 allowlist 메시지,
원문 출력 금지 — 와 `syncmill.status`를 기록한다. cancellation에 합성 인과 edge를
만들지 않는다: winner 선택으로 취소된 compete sibling은 `run` 아래 ERROR 상태 leaf일
뿐이다.

### 2.4 `syncmill.*` 속성 allowlist

| 속성 | 타입 | 비고 |
| --- | --- | --- |
| `syncmill.schema_version` | int | `1` |
| `syncmill.run_id` | str | **모든** span에 (envelope 상호참조) |
| `syncmill.strategy` | str | run span에 |
| `syncmill.phase` | str | `attempt\|stage\|propose\|critique\|synthesize\|plan\|subtask\|gate\|select` |
| `syncmill.agent_id` | str | run/select span은 `supervisor` |
| `syncmill.attempt` / `syncmill.round` | int | 인덱스 / council round |
| `syncmill.status` | str | `completed\|failed\|timeout\|cancelled` |
| `syncmill.exit_code` | int | |
| `syncmill.gate.passed` | bool | gate span에 |
| `syncmill.worktree_slot` | int | **concurrent 전략 전용 optional** (§8 발견 9) |
| `syncmill.winner` | bool | 승리 attempt에 `true` |
| `syncmill.files_changed_count` | int | **count만, 경로 금지** (로컬 경로 = redaction 대상) |

전면 금지: prompt, output/stdout/stderr, patch 본문, 파일 경로, 환경변수, memory
chunk 내용. `include_output` opt-in조차 v1에서는 warn+ignore로 유보한다(§3.4).

### 2.5 contract fixture 배치

| 저장소 | 위치 | 역할 |
| --- | --- | --- |
| syncmill | `contracts/` (신규 최상위): `run-envelope.schema.json`, `preflight-request.schema.json`, `fixtures/envelope-*.json`; `tests/fixtures/toolgraph/preflight-*.json` (**vendored** toolgraph golden, 헤더 주석에 원본 commit 기록) | envelope 생산자, preflight 소비자 |
| tracegraph | `examples/syncmill_otlp_traces.py` (생성기 = 단일 진실원, 기존 `otlp_agent_trace.py` 패턴) + `tests/fixtures/syncmill/*.otlp.json` golden | trace 소비자 — golden ingest test가 곧 contract test |
| toolgraph | `contracts/preflight.schema.json` + `contracts/fixtures/preflight-*.json` | preflight 생산자 |

cross-repo 규칙 (상위 계획 §8 "공통 패키지 조기 추출 금지" 준수): 생산자의 생성기가
golden을 만들고, 소비자는 **vendored 사본**을 자기 hermetic 스위트에서 검증한다.
재생성은 수동이며 fixture 헤더에 기록한다. 각 소비자는 `schema_version: 2` fixture로
**거부**를, 알 수 없는 additive 필드 fixture로 **관용**을 각각 1개씩 검증한다.
syncmill 쪽 contract test는 **Neo4j 없이** 실행 가능해야 한다(toolgraph 테스트만
testcontainers 사용).

### 2.6 P0 완료물 (Gate A)

문서 + 스키마 + fixture만. 상위 계획과 세 저장소 계획 문서를 다음으로 정정한다:
정수 `schema_version`, 기본 `protocol: "otlp-json-file"`, §2.3 span 표, §2.1 run_id
규칙, §2.4 redaction allowlist. 실패 모드 matrix는 상위 계획 §9가 이미 옳으므로 각
저장소 문서로 복사한다.

## 3. P1 — syncmill → tracegraph 관측 계측

### 3.1 exporter 결정: OTLP-JSON 파일 (신규 의존성 0)

상위 계획의 `endpoint = "http://127.0.0.1:4318"`은 모순이다 — tracegraph에는 HTTP
수신기가 없고 `OTLPSpanAdapter.from_file`의 파일 ingest뿐이다(§8 발견 1). 따라서:

- 기본 exporter는 **파일**: `~/.syncmill/runs/{run_id}/trace.otlp.json`에 OTLP
  protobuf-JSON(`ExportTraceServiceRequest`) 문서를 기록한다. tracegraph가 그대로
  `ingest-otlp` 한다. OTel SDK 불요.
- `otlp-http`는 예약 옵션으로 남긴다(v1에서 지정 시 경고). 운영 collector가 필요해질
  때 optional extra(`opentelemetry-sdk`)로 추가한다.
- `OTLPSpanAdapter`는 dangling parent/중복 spanId/미해석 논리 parent에 **raise**하므로
  (§8 발견 7) exporter는 buffer 후 **atomic write**(temp + `os.replace` — breaker/board
  의 기존 레시피)로 all-or-nothing을 보장한다. crash 경로(`_crash_result` 유사)도 run
  span을 닫은 뒤 flush한다.

### 3.2 tracer 인터페이스 (`src/syncmill/tracer.py`, 신규 flat module)

**retrospective recorder 방식.** 라이브 span context manager가 아니라, 전략이 각
fan-out **직후**(메모리 I/O가 이미 앉아 있는 shared-loop 경계)에 완결된 span 기록을
쌓는다. 근거: (a) fan-out 내부에 tracer를 넣으면 agent 계층으로 침투해 agent 순수성
계약을 깬다, (b) 전략은 fan-out 직후 `TaskResult.duration_seconds`와 phase 벽시계를
모두 보유한다, (c) 근사 timestamp(start = phase_end − duration)는 tracegraph 설계상
안전하다 — timestamp는 절대 인과로 승격되지 않고 prerank tiebreak일 뿐이다.

```python
# tracer.py
@dataclass(frozen=True)
class SpanRecord:
    name: str                    # 안정 라벨 (diff/AHU 키) — uuid 금지
    kind: str                    # AGENT|CHAIN|TOOL (openinference.span.kind)
    node_id: str                 # graph.node.id, trace 내 유일
    node_parent: str | None      # graph.node.parent_id
    links: tuple[str, ...] = ()  # 추가 cause의 node_id (flush 때 spanId로 해석)
    start: float = 0.0           # unix seconds
    end: float = 0.0
    status: str = "completed"    # completed|failed|timeout|cancelled
    error: str | None = None     # 경계된 요약, 원문 출력 금지
    attrs: Mapping[str, str | int | bool] = field(default_factory=dict)

@runtime_checkable
class Tracer(Protocol):
    trace_id: str
    def record(self, span: SpanRecord) -> None: ...   # 절대 밖으로 raise하지 않음
    def flush(self) -> Path | None: ...               # 파일 기록; 실패 시 None + warning

def build_tracer(cfg: TraceConfig, *, run_id: str) -> Tracer | None: ...
    # disabled면 None — 꺼진 경로는 바이트 단위로 기존과 동일 (build_gate/build_breaker 관용)
```

`FileTracer` 구현: 기록 버퍼링; `flush()`가 node_id → 생성 16-hex spanId 매핑, link
해석, 모든 span의 `parentSpanId`를 run root로(평면 포함관계), 소비 속성 3개 +
`syncmill.*` allowlist 주입, atomic write. 약 150줄, 의존성 0. 결정적 id factory
kwarg가 테스트 seam이다.

### 3.3 주입과 배선

- `Strategy.execute(..., tracer: Tracer | None = None)` — gate/breaker/monitor 옆의
  네 번째 kwarg (strategies/base.py:65).
- `strategies/base.py`에 no-op 헬퍼를 `_monitor_*` 옆에 추가: `_trace_record(tracer,
  span)`, 그리고 link 의미가 전략 간 드리프트하지 않도록 빌더 `_attempt_span(...)`,
  `_select_span(winner, examined, ...)`.
- Supervisor(supervisor.py:101-246): `build_monitor` 다음에 `build_tracer(config.
  integration.trace, run_id=run_id)`; `syncmill.run` span은 시작/종료/최종 상태를 아는
  `finally`에서 기록; `flush()`는 `finally`에서 **`pool.cleanup_all()`(:246) 이전에**,
  `try/except → logger.warning`으로 감싸 exporter 장애가 cleanup을 절대 건너뛰지 못하게
  한다. envelope의 `trace` 블록에 trace_id + 파일 digest 기록.
- **첫 PR 범위는 route + compete만** (상위 계획 P1과 일치). pipeline/council/decompose
  span은 §2.3 표를 따르는 후속 PR.

### 3.4 config

```toml
[integration.trace]
enabled = false
exporter = "file"          # "file" | "otlp-http" (예약, v1은 경고)
dir = ""                   # 기본 ~/.syncmill/runs
include_output = false     # v1: 수용하되 warn + ignore
```

`SyncmillConfig`에 `IntegrationConfig`(하위 `TraceConfig`/`ToolgraphConfig`)를 선언적
필드로 추가한다(root `extra="forbid"` 통과). 기본 전부 비활성, env 오버라이드는
`SYNCMILL_INTEGRATION__TRACE__ENABLED` 형태.

### 3.5 tracegraph 측: fixture와 golden test (src 변경 0)

`examples/syncmill_otlp_traces.py` — E2E 시나리오를 반영한 5개 생성기:
`route_success()`, `route_fallback()`(attempt:codex ERROR → attempt:claude가 그를
parent로), `compete_winner()`(attempt 3개가 `run`에서 fan-out, `select`는 winner를
parent + 검토한 패자들을 link), `compete_timeout()`(STATUS_CODE_ERROR "timeout"
sibling 1개), `compete_gate_reject()`(gate TOOL span ERROR, select가 다음 후보로).

`tests/test_syncmill_traces.py` golden test:

- 각 문서 ingest → step 수, `CAUSED_BY` edge 집합 **exact-match** (fan-out 불변식:
  compete sibling 간 edge 부재; fallback 불변식: attempt:2 ← attempt:1).
- 후보를 2개 이상 검토한 `select`는 `projection_lossy`; run 실패에 대한 `explain`이
  **raw** CAUSED_BY로 timeout/gate span까지 역추적.
- `diff compete_winner vs compete_winner'`(spanId/timestamp만 다른 동일 구조) →
  **exit 0**; `diff route vs compete` → **exit 1을 pass 조건으로** 고정(§8 발견 5).
- **redaction test** 1개: fixture 전체에서 allowlist 밖 속성 키와 prompt/patch 마커
  값이 없음을 검사.

cross-repo contract: syncmill hermetic 스위트는 FakeAgent compete 실행 + FileTracer로
생성된 문서가 ingest 가능한 형태인지(속성 존재, parent 해석 가능성) **순수 파이썬
미니 검증기**로 검사한다 — tracegraph를 import하지 않는다. tracegraph는 실제 syncmill
산출 golden 1개를 `tests/fixtures/syncmill/`에 vendor해 ingest한다.

## 4. P2 — 통합 실행 보고서 (개요)

- **신규 flat module `src/syncmill/report.py`** — `monitor.py` 확장이 아니다:
  `result.json`은 inner→outer IPC이고 report는 durable schema-versioned artifact다.
  `report.py`가 `monitor._result_to_json`을 import해(기존 private cross-module 패턴,
  `session.py` 선례) result payload가 드리프트할 수 없게 한다.
- 내용: `resolve_run_id`(§2.1), envelope dataclass + `write_envelope`(atomic),
  `sha256_digest`, `render_report`, retention sweep.
- **흐름:** 실행 시작 시 `~/.syncmill/runs/{run_id}/` 생성 + envelope skeleton 기록;
  `finally`에서 **`pool.cleanup_all()` 이전에** result.json 직렬화(인메모리
  `TaskResult`에서 — patch/files_changed는 `_collect_files_changed`가 이미 캡처했으므로
  worktree 읽기가 cleanup과 경합하지 않음), digest 계산, tracer flush + trace digest,
  최종 envelope 기록. 전 구간 degrade 관용구 — report 실패는 stderr warning + 정상
  결과 출력 + cleanup 지속(상위 계획 §9 마지막 행).
- **3경로 통일:** direct는 Supervisor가 기록; monitor는 INNER(실제 Supervisor)가
  OUTER 발급 run_id로 기록하고 OUTER가 `render_result` 뒤 report 경로를 출력; board는
  `run_item`이 run_id를 전달하고 `apply_update` 뒤 envelope의 `board` 블록을
  best-effort로 patch. 세 경로 동일 schema — Gate C 기준.
- CLI `syncmill report show <run_id>` / `report list`; dashboard의 REPORT pane도
  `list_reports`와 `render_report`를 verbatim으로 재사용한다(`render_item` 선례).
- retention: `[integration.report] keep_last = 50`, 실행 시작 시 best-effort sweep
  (`cleanup_orphaned_worktrees` 슬롯), 현재 run_id는 제외.

## 5. P3 — toolgraph advisory preflight (개요)

### 5.1 adapter: subprocess 권장

| | subprocess `toolgraph preflight` | 수동 artifact handoff |
| --- | --- | --- |
| 신선도 | 실행 시점 graph_generation, run_id 스탬프 | 정의상 stale, run_id 소급 |
| 결합 | argv + JSON stdout, versioned | 파일 경로뿐 |
| 테스트 | `syncmill.preflight.subprocess.run` fake — Neo4j 불요 | 자명 |

subprocess가 결정론 기준("같은 generation + 입력 → 같은 결과"는 toolgraph의 보증이고
subprocess는 **실제** generation을 스탬프)과 bounded-timeout degrade 관용구에 맞는다.
artifact-handoff 모드는 부산물로 지원한다 — subprocess가 `preflight.json`을 쓰고
envelope가 연결하므로, 운영자가 미리 만든 artifact를 놓는 fallback은 `if` 하나다.

### 5.2 toolgraph: 신규 `preflight` CLI 명령

```
toolgraph preflight AGENT [CANDIDATES...] --profile strict|review|explore
                    --run-id RUN_ID [--features] [--out preflight.json]
```

기존 selector surface를 wrapping하고, `_with_generation`(server/app.py:22-43,
double-read bracketing)을 `graph/queries.py`로 추출해 CLI와 MCP가 공유한다. 출력은
`contracts/preflight.schema.json`:

```json
{
  "schema_version": 1,
  "kind": "toolgraph.preflight",
  "run_id": "…",
  "created_at": "2026-07-11T00:00:00+00:00",
  "graph_generation": 42,
  "agent": "codex",
  "agent_found": true,
  "profile": "review",
  "mode": "advisory",
  "eligible": ["filesystem::read_file"],
  "rejected": [{"candidate": "…", "tool_key": "…", "reason": "DENY_GOVERNED", "paths": ["…"]}],
  "features": [],
  "decision": "advisory_allow | advisory_warn | unresolved_identity"
}
```

`decision` 유도: `agent_found` false → `unresolved_identity`; rejected 행 존재 →
`advisory_warn`; 그 외 `advisory_allow`. 명령은 `eligible_tools`(+`--features` 시
`rank_features`)를 하나의 `_with_generation` bracket 안에서 실행하고,
`paths`/provenance 문자열에 **redaction pass**(URI userinfo/query 제거 — §8 발견 8)를
적용한다. exit code는 `advisory_warn`에도 0(advisory는 enforcement가 아님); 운영
실패에만 non-zero.

### 5.3 syncmill: `src/syncmill/preflight.py` + config

```toml
[integration.toolgraph]
enabled = false
mode = "advisory"                      # "strict"는 P3.1 예약 (v1은 명확한 오류로 거부)
profile = "review"
timeout_seconds = 5
command = ["toolgraph", "preflight"]   # 테스트/오버라이드 seam
candidate_tools = []                   # qualified key, 운영자 구성 (§5.4)

[integration.toolgraph.agent_map]      # syncmill agent id -> toolgraph Agent.id
codex = "codex"
"kimi-code" = "kimi"                   # verbatim 키 — [capabilities.agents] 규칙
```

`run_preflight(cfg, *, run_id, agent_ids, out_dir) -> PreflightOutcome` — Supervisor
경계에서 strategy dispatch 전에 1회 호출. unmapped id → `unresolved_identity` 경고 후
실행 계속; subprocess는 `timeout=cfg.timeout_seconds`; `schema_version != 1` major →
degrade + artifact 거부; artifact를 run 디렉터리로 복사 + digest + envelope `preflight`
블록 기입; 경고는 `memory.degraded`와 같은 노란 CLI 표면. 모든 실패 클래스(binary
부재, timeout, non-zero exit, bad JSON) → `status: timeout|error` + warning + **실행
계속**. cleanup은 영향 없음(preflight는 try 블록 이전에 실행).

### 5.4 P3가 실제로 검사하는 것 — 3단계 정직한 범위

syncmill agent는 CLI subprocess이고 MCP tool을 쓰지 않으므로 `candidate_tools`의
유기적 출처가 없다(계획 스스로 인정한 최약점). 범위를 3단계로 정의한다:

1. **항상 (후보 0개여도):** identity 검증 + generation 고정. mapped agent identity의
   `agent_found` 확인과 `graph_generation` 스탬프만으로 Gate D 기준 두 개(unresolved
   identity 경고, "report에서 실행 당시 generation 확인")를 충족한다.
2. **실제 검증 대상 (권장):** 실행의 **ambient MCP surface** — memtomem 서버와
   syncmill 자신의 read-only `serve` gateway(`shared_search`/`shared_read`/
   `shared_stats`/`board_*`)는 외부 클라이언트가 실제 호출하는 진짜 MCP tool이다.
   memtomem-stm의 `server_name_map` + startup `eligible_tools` 선례(toolgraph README,
   `examples/governance-gate.yaml`)를 그대로 따라: 이 서버들을 crawl/manifest-ingest
   하고, syncmill agent별 `Agent` 노드를 manifest에 정의하고, `candidate_tools =
   ["memtomem::mem_search", "syncmill::shared_read", …]`로 구성해 identity mapping을
   문서화된 데모 + toolgraph testcontainers fixture manifest로 end-to-end 검증한다.
   의미는 정직하다: "이 실행의 생태계가 노출하는 지식 표면이 이 identity에 대해 정책
   위반이 없다" — agent가 tool을 호출한다고 가장하지 않는다.
3. **명시적 유보:** per-attempt tool 중재 주장은 syncmill agent가 MCP tool 구성을
   갖게 되는 (별도의) 날까지 유보하고, advisory 출력이 런타임 enforcement로 읽히지
   않도록 문서에 못박는다.

## 6. 시퀀싱과 PR 분해

의존: S1/T1/G1 (P0) → S2 → S3/T2 → S4 → G2 → S5 → P3.1 →
(P4: T3 → SyncMill board import → G3 artifact review, 여기까지 완료).

**syncmill** (hermetic 테스트만, conftest fake):

| PR | 내용 | 테스트 |
| --- | --- | --- |
| S1 | `contracts/` schema + fixture; 계획 문서 정정 | fixture 자기검증; fixture 전체 금지 필드 스캔 |
| S2 | `report.py::resolve_run_id`, `Supervisor.run(run_id=)`, monitor snapshot 전파, `run_item` 전달 | direct 발급; explicit 우선; inner는 sentinel 있을 때만 env 읽음(`inner_dir` monkeypatch); board lease id == run kwarg id; `_write_env_snapshot` 탑재 확인 |
| S3 | `tracer.py`(Protocol/FileTracer/`build_tracer`), `IntegrationConfig`, `Strategy.execute(tracer=)`, base 헬퍼, route+compete 계측, Supervisor run-span+flush | disabled ⇒ 바이트 동일(파일 없음); route fallback parent 사슬; compete fan-out/select/link를 FakeAgent 실행과 대조; flush 실패 ⇒ warning + cleanup 실행(실패 tracer fake); 출력-redaction 검증; 산출 문서가 vendored shape 규칙 통과 |
| S4 | `report.py` envelope writer, digest, CLI `report show/list`, retention, outer의 report 경로 출력 | 경로별 envelope golden(direct/monitor-inner seam/board FakeBoard); digest 안정성; report 실패 degrade; retention이 현재 run 보존 |
| S5 | `preflight.py`, `[integration.toolgraph]`, agent_map, CLI advisory 표면 | `syncmill.preflight.subprocess.run` monkeypatch(**lookup site**): ok/warn/timeout/binary부재/bad-json/major-2 거부 matrix를 **vendored toolgraph golden** 대상으로; 전 케이스에서 실행+cleanup 생존 |
| S6 | pipeline/council/decompose span (마일스톤 후) | 전략별 edge golden |

**tracegraph**:

| PR | 내용 | 테스트 |
| --- | --- | --- |
| T1 | `examples/syncmill_otlp_traces.py` + golden | 각 문서 ingest; edge 집합 exact; sibling 간 edge 부재 불변식 |
| T2 | `inspect`/`explain`/`diff` CLI golden; 실제 syncmill trace vendor | exit code 고정(동일 구조 diff=0, 전략 간=1); select의 projection_lossy; explain이 timeout/gate 원인 도달 |
| T3 (P4, 완료) | preset `pattern_id`/`pattern_version` + `export-review-candidates`(schema: run_id, pattern id/version, qualified tool key, artifact digest만) | producer-derived golden; preset version 스탬프; redaction |

**toolgraph** (graph 테스트는 testcontainers; schema 테스트는 hermetic):

| PR | 내용 | 테스트 |
| --- | --- | --- |
| G1 | `contracts/preflight.schema.json` + fixture + 문서 | schema가 fixture 검증(hermetic, Neo4j 불요) |
| G2 | `preflight` 명령; `_with_generation` → `graph/queries.py` 추출; provenance redaction pass | testcontainers: 동일 generation 결정론; unknown agent/tool/drift 행; 동시 bump 하 generation bracketing; golden == vendored syncmill fixture (created_at 제외); redaction fixture |
| G3 (P4, 완료) | review-candidate intake 문서 + `review-candidates` list/annotate (자동 적용 없음, Toolgraph PR #35 / merge `18bc87a`) | candidate가 manifest/policy를 절대 변경하지 않음; blast-radius, preflight, graph generation 보존 |

E2E (opt-in smoke, CI 밖): 실제 compete 실행 1회 → envelope → `tracegraph ingest-otlp`
→ `explain`; memtomem-stm 방식 manifest를 시드한 docker Neo4j 대상 preflight 1회.

## 7. P4 방향 메모

상위 계획 P4의 producer인 tracegraph T3, SyncMill board import, Toolgraph G3는 완료됐다.
candidate schema는 `run_id`, `pattern_id`/`pattern_version`, qualified tool key,
artifact digest만 담는다. importer는 exact tuple의 UUIDv5로 `review` +
`human-required` item을 만들며 자동 실행, P3.1 재평가, policy/manifest 변경을 하지 않는다.
Toolgraph는 같은 UUIDv5를 correlation에만 사용하고 exact report-byte digest에 묶인 별도
sidecar에 `open`/`accepted`/`dismissed` 이력을 저장한다. 두 review state는 동기화되지
않으며 `accepted`도 자동 적용을 뜻하지 않는다. 2026-07-12
[운영 evidence](gate-e-p4-operating-evidence.md)에서 live qualified single-failure
telemetry와 사람 검토 경로까지 GO를 기록했다. 명시적 retry causality와 별도 수동
governance 변경 workflow는 자동화하지 않는 후속 경계로 남는다.

## 8. 계획 리뷰 발견 전문

구현 전 반드시 반영해야 할 순서로 나열한다. 1~4는 계획 문서 간 또는 문서-코드 간
모순, 5~11은 미기술 결정이다.

1. **trace endpoint 모순.** syncmill 계획은 `endpoint = "http://127.0.0.1:4318"`
   (OTLP/HTTP)를 구성하지만 tracegraph는 HTTP 수신기가 없다 — `OTLPSpanAdapter`는
   파일 ingest 전용. 그대로면 P1은 아무도 듣지 않는 포트로 span을 보낸다. 수정:
   기본 `exporter="file"`(§3.1), `otlp-http`는 optional extra 뒤 예약.
2. **`schema_version` 타입 불일치.** 계획들은 `"1"`(문자열), tracegraph 구현은 int
   `1` + 엄격 `!=` 검사(artifact.py:17,45). **int로 통일** — 아니면 첫 cross-repo
   fixture부터 타입 비일관성이 박제된다.
3. **board run의 run_id 이중 발급 위험.** `run_item`이 이미 발급한 run_id가 lease
   **fencing token**이자 `board-run:{run_id}` scope다. Supervisor.run이 순진하게
   따로 발급하면 board run 하나가 id 2개를 갖고 envelope가 lease/audit에 join되지
   않는다. `run_item`의 id를 `run(run_id=)`로 전달해야 한다(§2.1). 부수 사실:
   board run_id는 memtomem namespace로도 나타나며 GC되므로 run 디렉터리 수명과
   연동하지 않는다.
4. **monitor run_id는 OUTER 발급이 필요.** outer 프로세스는 Supervisor를 만들지
   않으므로 "Supervisor가 발급"만으로는 monitor 요구(동일 run_id)를 충족할 수 없다.
   env snapshot 전파(§2.1)가 메커니즘이고, id는 `_inner_argv`가 아닌 snapshot에
   실린다(argv는 결정적·id-free 설계).
5. **span name은 diff-stable이어야 한다 — 어느 계획에도 없음.** `diff`/AHU 라벨이
   `step.name or kind`(ahu.py:37)이므로 name에 uuid가 들어가면 모든 diff가
   divergence하고 P1 완료 기준("compete 성공 diff")이 조용히 실패한다. §2.3 규칙
   1을 P0 계약에 명문화. 같은 이유로 golden test는 `diff`의 exit 1을 **pass
   조건**으로 고정해야 한다(전략 간 비교는 divergence가 정답).
6. **council/decompose의 "실제 소비한 결과만 fan-in"은 증명 불가.** cross-agent
   handoff가 relevance-ranked best-effort `shared` 검색이라 syncmill은 어떤 제안이
   실제 소비됐는지 알 수 없다. link 의미를 "phase 경계에 shared에 게시된 것"(선언된
   가용성)으로 정의하고 근사임을 문서화한다. P1을 route/compete로 한정한 것은
   올바른 결정이며, 후속 PR이 소비를 과대 주장하지 않아야 한다.
7. **adapter의 엄격성은 생산자 제약이다.** `OTLPSpanAdapter`는 dangling
   `parentSpanId`, 중복 spanId, 미해석 논리 parent에 raise한다. 스트리밍/중단된
   exporter의 partial 문서는 거부된다. 파일 exporter는 buffer + atomic write
   필수이고, crash 경로도 run span을 닫고 flush해야 한다.
8. **toolgraph provenance redaction의 소유자가 없다.** 상위 계획 §11은 resource
   URI/provenance의 credential 위험을 경고하지만 어느 저장소 작업 항목도 이를
   소유하지 않는다. `preflight` 명령의 redaction pass(§5.2)와 redaction fixture
   (상위 계획 §10 요구)를 G2 작업 항목으로 추가한다.
9. **`worktree_slot` 필수 속성은 과잉.** 순차 전략(route/pipeline)에는 의미 있는
   slot이 없다. concurrent 전략 전용 optional로 강등하지 않으면 route contract
   fixture가 검증 불가능하다.
10. **P3의 전제가 얇다 (계획 스스로 인정).** MCP 후보 0개면 advisory preflight는
    identity+generation 스탬프로 퇴화한다. §5.4의 3단계 범위(항상: identity/
    generation; 실제 대상: memtomem + `serve` gateway의 ambient MCP surface,
    memtomem-stm 선례 재사용; 유보: per-attempt 중재)로 Gate D의 "identity mapping이
    실제 사례에서 검증됨"을 충족 가능하게 만든다 — 아니면 그 기준은 충족 불가능하다.
11. **contract fixture 규칙이 미명시.** 생산자가 golden 생성, 소비자는 vendored
    사본을 hermetic 검증(syncmill 쪽은 Neo4j 불요)을 명시한다. envelope의
    `artifact` 경로 기준("envelope 디렉터리 상대")과, run 디렉터리를 대상 repo 밖
    `~/.syncmill/runs`에 두는 것이 "commit 제외" 요구의 실제 메커니즘임도 명시한다.
