# toolgraph, tracegraph, syncmill 연계 계획

**상태:** 검토 반영 (2026-07-11 코드 대조 리뷰)  
**작성일:** 2026-07-11  
**세부 구현 설계:** [구현 설계 문서](../../toolgraph-tracegraph-syncmill-implementation-design.md)

## 요약

syncmill은 세 프로젝트의 실행 제어면이다. toolgraph는 실행 전 도구 정책을 검사하고,
tracegraph는 실행 후 인과관계와 회귀를 분석한다. 연계는 Supervisor 경계에서만 수행하며
agent는 계속 prompt와 worktree만 받는 pure function으로 유지한다.

```text
toolgraph advisory preflight
            |
            v
user -> syncmill Supervisor -> strategy -> isolated agent worktrees
            |
            v
      OpenInference/OTLP -> tracegraph
```

## 현재 기준선

- route, pipeline, compete, council, decompose 전략을 제공한다.
- Supervisor만 memtomem을 읽고 쓰며 agent 간 직접 통신은 없다.
- agent 실행은 CLI subprocess와 격리된 Git worktree를 사용한다.
- 외부 MCP gateway는 `shared` memory와 board의 읽기 전용 surface다.

## 목표

1. Supervisor가 선택적으로 toolgraph preflight를 실행할 수 있게 한다.
2. run, strategy, phase, agent attempt, gate 결과를 표준 span으로 계측한다.
3. 세 시스템의 artifact를 하나의 `run_id`로 연결한다.
4. 연계 장애가 기존 orchestration과 agent isolation을 깨지 않게 한다.

## 비목표

- programmatic agent가 toolgraph, tracegraph 또는 memtomem에 직접 접근하는 것
- 기존 read-only MCP gateway에 쓰기 또는 실행 기능을 추가하는 것
- tracegraph가 winner를 선택하거나 toolgraph가 strategy를 결정하는 것
- 외부 분석기의 데이터베이스를 syncmill의 필수 런타임 의존성으로 만드는 것

## 제안 구성

```toml
[integration.toolgraph]
enabled = false
mode = "advisory"          # advisory | strict
profile = "default"
timeout_seconds = 5

[integration.trace]
enabled = false
exporter = "file"          # "file" | "otlp-http" (예약 — v1은 경고)
dir = ""                   # 기본 ~/.syncmill/runs
include_output = false     # v1: 수용하되 warn + ignore
```

trace 기본 exporter는 **OTLP-JSON 파일**이다. tracegraph는 파일 ingest 전용이며 HTTP
수신기가 없으므로, OTLP/HTTP endpoint 구성은 운영 collector가 생길 때까지 예약
옵션으로만 남긴다. exporter는 buffer 후 atomic write(temp+replace)로 기록한다 —
tracegraph adapter는 partial 문서에 raise한다.

환경 변수와 TOML의 실제 이름은 구현 전 config ADR에서 확정한다. 기본값은 모두
비활성이며 기존 `env > file > defaults` 우선순위를 유지한다.

## 공통 artifact 계약

각 실행은 안정적인 `run_id`(`schema_version`은 **정수** — tracegraph 계약과 일치)를
공유한다. run_id의 발급 규칙은 세 경로로 나뉜다 (단일 resolver, 구현 설계 §2.1):

- **direct run**: `Supervisor.run`이 발급 (`run(run_id=)` kwarg, 미지정 시 fresh uuid4)
- **monitor run**: **OUTER**가 발급해 env snapshot(`_write_env_snapshot`)으로 inner에
  전파한다 — outer는 Supervisor를 만들지 않으므로 Supervisor 발급만으로는 부족하다.
  id는 `_inner_argv`에 싣지 않는다(argv는 결정적·id-free 유지).
- **board run**: `run_item`이 **이미 발급하는** run_id(lease fencing token,
  `board-run:{run_id}` memory scope)를 `run(run_id=)`로 전달한다 — 이중 발급 금지,
  lease/scope/envelope가 하나의 id.

```json
{
  "schema_version": 1,
  "run_id": "stable-run-id",
  "strategy": "compete",
  "preflight_artifact": "artifacts/preflight.json",
  "trace_id": "otlp-trace-id",
  "result_artifact": "artifacts/result.json"
}
```

artifact에는 prompt, credential, 전체 stdout, memtomem chunk, patch 본문을 넣지 않는다.
필요하면 외부 파일의 digest와 redacted summary만 기록한다.

## 단계별 계획

### 0단계: 경계와 계약 고정

- [ ] `run_id` 생성 시점과 monitor inner/outer process 전달 규칙을 정한다.
- [ ] strategy별 phase와 agent attempt span naming을 정의한다.
- [ ] preflight failure mode와 timeout 정책을 정의한다.
- [ ] artifact schema, 보관 위치, redaction 정책을 문서화한다.

### 1단계: 관측 계측

- [ ] no-op tracer interface를 Supervisor 경계에 추가한다.
- [ ] run, strategy, phase, agent execution, gate에 span을 추가한다.
- [ ] compete/council/decompose의 fan-out과 cancellation link를 보존한다.
- [ ] exporter 장애를 warning으로 격리하고 orchestration 결과를 유지한다.
- [ ] 합성 OTLP export를 tracegraph fixture로 검증한다.

### 2단계: toolgraph advisory preflight

- [ ] subprocess 또는 versioned artifact adapter 중 최소 결합 방식을 선택한다.
- [ ] agent/team identity와 qualified MCP tool key mapping을 정의한다.
- [ ] advisory mode에서는 경고와 artifact만 남긴다.
- [ ] strict mode는 명시적 violation만 차단하고 unknown/drift 정책을 설정 가능하게 한다.
- [ ] toolgraph 비가용 시 fail-open/fail-closed를 profile별로 테스트한다.

### 3단계: 통합 실행 보고서

- [ ] `run_id`, preflight 요약, trace id, gate 결과, winner를 한 보고서로 묶는다.
- [ ] worktree cleanup 전 artifact digest를 계산한다.
- [ ] dashboard와 CLI는 같은 report model을 렌더링한다.
- [ ] read-only MCP gateway에는 요약 조회만 검토하고 쓰기는 추가하지 않는다.

### 4단계: 폐루프 검토

- [ ] tracegraph pattern 결과를 board의 review item으로 가져오는 adapter를 검토한다.
- [ ] 자동 재실행과 자동 policy 수정은 별도 승인 없이는 수행하지 않는다.
- [ ] 반복 실패가 breaker, capability matcher와 중복되지 않는지 평가한다.
- [ ] 실제 운영 데이터로 비용과 진단 가치가 확인될 때만 기본 제공 여부를 재검토한다.

## 전략별 계측 요구사항

| 전략 | 필수 관계 |
| --- | --- |
| route | 순차 attempt와 fallback reason |
| pipeline | 이전 stage 결과를 소비한 다음 stage |
| compete | 공통 부모에서 fan-out, winner selection과 cancellation |
| council | propose, critique round, synthesize의 phase 및 memory handoff |
| decompose | planner, subtask fan-out, synthesis fan-in |

timestamp 순서만으로 인과관계를 만들지 않고 실제 orchestration 호출 관계를 span parent
또는 link로 표현한다.

## 검증 기준

- integration을 끄면 기존 CLI 출력, 결과, timing contract가 변하지 않는다.
- agent contract `execute(prompt, workdir, timeout) -> TaskResult`가 변하지 않는다.
- toolgraph와 tracegraph가 설치되지 않아도 core test가 통과한다.
- exporter/preflight timeout 뒤에도 worktree cleanup이 항상 실행된다.
- monitor, board run, direct run이 동일한 `run_id` 및 report schema를 사용한다.
- 민감한 입력과 patch 본문이 span 또는 연계 artifact에 노출되지 않는다.

## 주요 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 연계가 핵심 실행 경로를 불안정하게 함 | opt-in, timeout, no-op adapter, fail-open 기본값 |
| agent isolation 훼손 | 모든 연계는 Supervisor에서만 수행 |
| 중복 telemetry와 높은 비용 | 최소 span set과 sampling 제공 |
| strict preflight의 오탐 | provenance 표시, advisory 선행, 명시적 profile |
| 세 저장소의 schema drift | versioned JSON/OTLP contract와 contract fixture |

## 완료 정의

실제 route 및 compete 실행에서 동일한 `run_id`로 preflight, orchestration result,
tracegraph 분석을 연결하고, 연계가 꺼진 기존 테스트와 failure-path cleanup 테스트가 모두
통과하면 첫 통합 마일스톤을 완료한 것으로 본다.
