# toolgraph, tracegraph, syncmill 통합 실행 계획

> **Canonical tracked plan — 2026-07-11 reconciliation.** 실제 landed 순서는
> S1/T1/G1 → S2 → S3/T2 → S4 → G2 → S5 → Gate-D smoke다. 초기 P/G/S/T
> 라벨은 설계 역사를 설명하기 위해 아래에 그대로 둔다. P0/P1과 첫 마일스톤은
> 완료, P2는 CLI/dashboard 공유 report rendering까지 완료, advisory P3/Gate D는 실제
> `codex`/`kimi-code → kimi` identity와 `syncmill::shared_read`/
> `syncmill::board_stats` smoke로 완료됐다. P3.1 구현은 opt-in preview로
> merge됐고 [Gate E/P4 operating evidence](gate-e-p4-operating-evidence.md)는
> 네 CLI live canary와 strict 9-scenario matrix에서 GO를 기록했다. Gate E의 명시적
> 승인은 계속 열려 있고 strict는 opt-in preview다. Tracegraph T3, SyncMill board
> import, Toolgraph G3와 live single-failure P4 review path는 완료됐으며, 자동 적용은
> 없고 명시적 retry causality 및 별도 수동 governance 변경만 후속 경계로 남는다.
>
> 재현 절차와 body-free 결과 보존 규칙은 [smoke runbook](smoke-runbook.md)을
> 따른다.

**상태:** 검토 반영 (2026-07-11 코드 대조 리뷰)  
**작성일:** 2026-07-11  
**대상:** toolgraph, tracegraph, syncmill maintainers  
**세부 구현 설계:** [구현 설계 문서](implementation-design.md)
— P0/P1 파일·인터페이스 수준 상세와 계획 리뷰 발견 11건 전문

## 1. 문서 목적

이 문서는 세 프로젝트를 하나의 제품으로 합치는 계획이 아니라, 각 프로젝트의 독립성을
유지하면서 에이전트 실행 수명주기를 연결하는 프로그램 수준의 실행 계획이다.

저장소별 상세 계획:

- [toolgraph 계획](toolgraph/docs/ecosystem-integration-plan.md)
- [tracegraph 계획](tracegraph/docs/ecosystem-integration-plan.md)
- [syncmill 계획](syncmill/docs/ecosystem-integration-plan.md)

## 2. 요약

세 프로젝트는 서로 다른 시점과 책임을 담당한다.

```text
                    실행 전
                      |
                      v
              toolgraph preflight
              정책, drift, provenance
                      |
                      v
사용자 요청 -> syncmill Supervisor -> strategy -> agent worktree
                      |
                      v
             OpenInference/OTLP trace
                      |
                      v
                  tracegraph
              RCA, diff, pattern query
                      |
                      v
             운영자 검토 및 개선 후보
```

권장 구현 순서는 공통 계약, 관측 계측, 통합 보고서, advisory preflight, 검토형 feedback
loop다. toolgraph를 먼저 실행 차단기에 연결하지 않는다. 현재 syncmill agent는 MCP를 직접
사용하지 않으므로 agent/tool identity와 정책 대상의 매핑을 먼저 검증해야 한다.

## 3. 프로젝트별 책임

| 영역 | 주 책임 | 제공하는 것 | 제공하지 않는 것 |
| --- | --- | --- | --- |
| 실행 전 정책 분석 | toolgraph | 접근 verdict, drift, provenance, graph generation | 런타임 호출 중재 |
| 실행과 조율 | syncmill | run lifecycle, strategy, worktree, gate, result | 인과 분석, 정책 작성 |
| 실행 후 분석 | tracegraph | raw causal graph, RCA, diff, pattern matches | 실행 제어, winner 선택 |

### toolgraph 책임

- server-qualified tool key를 기준으로 deterministic preflight 결과를 제공한다.
- 판정의 graph generation, path, classification, provenance를 보존한다.
- trace feedback을 정책 변경이 아닌 review candidate로만 수용한다.
- 실행 중단 여부는 소비자인 syncmill의 명시적 profile에 맡긴다.

### syncmill 책임

- 전체 실행의 canonical `run_id`를 생성한다.
- Supervisor 경계에서만 preflight와 telemetry adapter를 호출한다.
- agent pure-function 계약과 worktree isolation을 유지한다.
- 실행 결과, preflight artifact, trace id를 통합 보고서로 연결한다.

### tracegraph 책임

- 명시적 parent/link만 인과관계로 승격한다.
- raw multi-parent graph를 system of record로 유지한다.
- syncmill 전용 속성을 core causal model과 분리한다.
- pattern 결과를 versioned review candidate로 내보낸다.

## 4. 설계 원칙

1. **독립 실행:** 다른 두 프로젝트가 없어도 각 프로젝트의 core 기능이 동작해야 한다.
2. **느슨한 결합:** versioned JSON artifact와 OpenInference/OTLP를 우선 사용한다.
3. **Supervisor-only:** programmatic agent는 연계 시스템이나 memtomem에 직접 접근하지 않는다.
4. **관측과 인과의 구분:** timestamp 순서만으로 causal edge를 만들지 않는다.
5. **Advisory first:** toolgraph 연계는 경고부터 시작하고 strict 차단은 별도로 검증한다.
6. **Human-reviewed feedback:** 실패 패턴은 자동 정책 변경이 아니라 검토 후보가 된다.
7. **데이터 최소화:** prompt, credential, memory, stdout, patch 본문은 기본 수집하지 않는다.
8. **Fail-safe cleanup:** 연계 장애가 worktree cleanup이나 기존 결과 생성을 막지 않는다.

## 5. 공통 계약

### 5.1 식별자

| 식별자 | 생성 주체 | 범위 | 요구사항 |
| --- | --- | --- | --- |
| `run_id` | syncmill Supervisor | 한 번의 전체 실행 | board `run_item`이 이미 발급하는 run_id(lease fencing token, `board-run:{run_id}` scope)와 **동일 식별자로 통합**한다. monitor 실행은 OUTER가 발급해 env snapshot으로 inner에 전파한다(argv 비사용). 단일 resolver 규칙은 구현 설계 §2.1 |
| `trace_id` | tracing provider | 한 실행 trace | `run_id`와 상호 참조 가능 |
| `span_id` | tracing provider | phase 또는 attempt | 전역 식별자로 사용하지 않음 |
| `graph_generation` | toolgraph | graph snapshot | preflight 재현성 판단에 사용 |
| `artifact_digest` | artifact 생산자 | 불변 artifact | SHA-256 등 알고리즘을 함께 명시 |
| `pattern_id` | tracegraph | 분석 규칙 | pattern version과 함께 기록 |

### 5.2 공통 실행 envelope

```json
{
  "schema_version": 1,
  "run_id": "run-opaque-id",
  "created_at": "2026-07-11T00:00:00Z",
  "strategy": "compete",
  "team": null,
  "preflight": {
    "mode": "advisory",
    "artifact": "preflight.json",
    "digest": "sha256:...",
    "graph_generation": 42
  },
  "trace": {
    "trace_id": "trace-opaque-id",
    "protocol": "otlp-json-file"
  },
  "result": {
    "status": "success",
    "winner": "codex",
    "artifact": "result.json",
    "digest": "sha256:..."
  }
}
```

`schema_version`은 **정수**다 — tracegraph의 기존 artifact 계약
(`ARTIFACT_SCHEMA_VERSION = 1`, 엄격 비교)과 일치시킨다. trace의 기본 protocol은
`otlp-json-file`(OTLP protobuf-JSON 파일)이다: tracegraph는 파일 ingest 전용이며 HTTP
수신기가 없으므로 OTLP/HTTP endpoint는 예약 옵션으로만 남긴다. `artifact` 경로는
envelope 디렉터리 기준 상대경로이고, 실행 artifact 디렉터리는 대상 repo 밖
(`~/.syncmill/runs/{run_id}/`)에 둔다. 전체 필드 목록은 구현 설계 §2.2 참조.

### 5.3 버전 정책

- 모든 JSON artifact는 최상위 `schema_version`(**정수**)을 가진다.
- additive field는 같은 major schema에서 허용한다.
- field 의미 변경, 삭제, enum 재해석은 major version을 올린다.
- 소비자는 알 수 없는 additive field를 무시하되 알 수 없는 major version은 거부한다.
- contract fixture는 세 저장소 중 적어도 생산자와 소비자 양쪽에서 검증한다.

### 5.4 데이터 분류

| 데이터 | 기본 처리 |
| --- | --- |
| run/trace/span id | 저장 허용 |
| agent id, strategy, phase, status | 저장 허용 |
| graph verdict와 provenance pointer | 저장 허용, secret redaction 필요 |
| prompt와 사용자 입력 | 저장 금지, 필요 시 redacted hash/summary |
| credential와 environment secret | 항상 저장 금지 |
| memtomem chunk 본문 | 저장 금지 |
| agent stdout/stderr | 기본 저장 금지, 별도 opt-in |
| patch와 파일 본문 | artifact 외부화, digest만 연결 |

## 6. 우선순위와 마일스톤

### P0. 공통 계약 고정

**목표:** 구현 전에 세 시스템이 공유할 최소 vocabulary와 artifact 계약을 고정한다.

주 작업:

- [ ] `run_id` 생성 및 전파 규칙 확정 (board `run_item` 기존 run_id와 통합, monitor는 OUTER 발급 + env snapshot 전파 — 구현 설계 §2.1)
- [ ] strategy, phase, agent attempt span naming 확정 — **span name은 diff-stable**이어야 한다 (tracegraph AHU diff의 라벨이 `name or kind`이므로 uuid/timestamp 금지, 구현 설계 §2.3)
- [ ] trace exporter의 **atomic write** 요구 명문화 (tracegraph adapter는 partial 문서에 raise — buffer 후 temp+replace)
- [ ] 공통 envelope와 preflight artifact JSON Schema 작성
- [ ] redaction allowlist와 artifact 보관 정책 작성
- [ ] failure mode 및 timeout matrix 작성
- [ ] 각 저장소에 contract fixture 배치 방식 결정

완료 기준:

- 세 저장소 계획 문서가 같은 identifier와 상태 용어를 사용한다.
- schema fixture를 생산자와 소비자가 독립적으로 검증할 수 있다.
- 민감 데이터가 명시적인 allowlist 밖에서 artifact에 들어가지 않는다.

### P1. syncmill에서 tracegraph로 관측 연결

**목표:** 실제 실행의 인과 구조를 분석할 수 있는 첫 end-to-end 경로를 만든다.

주 작업:

- [ ] syncmill Supervisor에 no-op tracer interface 추가
- [ ] run, strategy, agent attempt, gate, result span 추가
- [ ] route와 compete를 첫 지원 전략으로 제한
- [ ] fan-out, winner selection, timeout, cancellation link 정의
- [ ] 합성 및 실제 OTLP fixture를 tracegraph로 ingest
- [ ] `inspect`, `explain`, `diff` golden test 추가

완료 기준:

- route 성공, route fallback, compete 성공, compete timeout을 분석할 수 있다.
- 병렬 실행의 완료 시간 순서가 인과관계로 오인되지 않는다.
- exporter가 실패해도 syncmill 결과와 cleanup이 유지된다.
- tracing을 끄면 기존 CLI와 test contract가 변하지 않는다.

### P2. 통합 실행 보고서

**목표:** 운영자가 한 실행의 결과와 분석 근거를 한 위치에서 추적하게 한다.

주 작업:

- [ ] syncmill에 canonical report model 추가
- [ ] `run_id`, `trace_id`, strategy, attempts, gates, winner 기록
- [ ] result 및 patch artifact digest 계산
- [ ] direct run, monitor, board run의 동일 schema 사용
- [x] CLI와 dashboard의 동일 report model 렌더링
- [ ] artifact retention과 cleanup 정책 구현

완료 기준:

- 실행 보고서에서 tracegraph 분석 artifact로 이동할 수 있다.
- monitor inner/outer process가 같은 `run_id`를 사용한다.
- 보고서에 prompt, credential, patch 본문이 포함되지 않는다.

### P3. toolgraph advisory preflight

**목표:** 실행 전 정책 위험을 관측 가능하게 만들되 기존 실행을 차단하지 않는다.

선행 조건:

- 실제 syncmill 작업에서 검사할 MCP tool 후보가 정의되어야 한다.
- syncmill agent/team identity와 toolgraph Agent identity 매핑이 검증되어야 한다.

주 작업:

- [ ] JSON artifact 또는 subprocess 기반 최소 adapter 선택
- [ ] qualified tool key와 identity mapping 작성
- [ ] verdict, classification, graph generation, provenance 수집
- [ ] provenance/path 문자열 **redaction pass** 구현 (resource URI의 userinfo/query 제거 — §11의 위험을 소유하는 작업 항목)
- [ ] advisory warning과 report 연결
- [ ] unknown agent, unknown tool, drift, timeout 동작 테스트
- [ ] 재현 가능한 preflight fixture 추가

완료 기준:

- preflight 장애가 기본 실행을 중단하지 않는다.
- 같은 graph generation과 입력이 같은 결과를 만든다.
- 모든 경고는 toolgraph evidence path로 설명 가능하다.
- report에서 실행 당시 graph generation을 확인할 수 있다.

### P3.1. 선택적 strict profile

다음 조건을 모두 만족할 때만 착수한다.

- advisory 운영 데이터로 오탐률과 미탐 사례가 평가되었다.
- 명시적 `violation`과 unknown/drift의 처리 정책이 분리되었다.
- timeout 및 toolgraph 비가용 시 fail-open/fail-closed 정책이 문서화되었다.
- 사용자가 strict mode를 명시적으로 선택한다.

strict mode는 별도 ADR과 release note 없이 기본값이 될 수 없다.

2026-07-12 P3.1 결정은 [ADR-0001](adr-0001-strict-preflight-enforcement.md)을
따른다. `mode`는 선택한 `profile`의 거부 집합을 enforcement하며 profile 자체를
강제하지 않는다. unknown identity/tool은 기본 block, drift는 기본 warn,
toolgraph unavailable은 기본 block이고 각 범주는 명시적으로 override할 수 있다.
반환 artifact의 contract 오류와 identity 간 graph-generation 불일치는 항상 block한다.
[preview release note](strict-p3.1-release-note.md)에 rollback과 한계를 기록한다.

### P4. 검토형 feedback loop

**목표:** 관측된 반복 실패를 사람이 검토할 수 있는 개선 후보로 만든다.

주 작업:

- [x] tracegraph review candidate schema 정의
- [x] `run_id`, pattern id/version, tool key, artifact digest만 전달
- [x] syncmill board `review`/`human-required` item으로 idempotent 노출
- [x] toolgraph가 별도 digest-bound sidecar에서 후보를 list/annotate하되 자동 적용하지 않음
- [x] breaker 및 capability matcher와 역할 중복 평가 — importer는 둘을 읽거나 수정하지 않음
- [x] annotation 전후 regression fixture, blast radius, preflight, graph generation 보존

SyncMill importer는 strict P3.1 verdict를 복사하거나 재평가하지 않는다. `run_id`는 보존된
`report show` 결과와의 선택적 join key일 뿐이며, unknown/drift/unavailable policy와
manifest, agent priority, breaker, capability state는 import 전후 동일하다.
Toolgraph는 exact tuple UUIDv5를 correlation에만 사용하고 SyncMill board와 독립적인
`open`/`accepted`/`dismissed` 이력을 저장한다. 어느 쪽도 상대 state를 변경하지 않으며
Toolgraph `accepted`도 policy 적용을 승인하지 않는다. 따라서 G3 완료는 live
qualified-tool telemetry와 운영 검토 평가가 남은 전체 P4 완료를 의미하지 않는다.

완료 기준:

- 반복 실패에서 근거가 포함된 review item을 생성할 수 있다.
- 승인 전에는 실행 정책, manifest, agent priority가 바뀌지 않는다.
- accepted annotation은 원래 run과 pattern evidence로 역추적할 수 있다. 실제 governance
  변경은 별도 workflow에서 변경 전후 evidence를 다시 남긴다.

## 7. 의존성

```text
P0 common contracts
  |
  +--> P1 tracing -----------------> P2 integrated report
  |                                      |
  +--> identity/tool mapping ------------+--> P3 advisory preflight
                                               |
                                               +--> P3.1 strict evaluation
  P1 operational traces + P3 evidence --------+--> P4 review feedback
```

- P1은 P0에 의존한다.
- P2는 P1의 식별자와 artifact 계약에 의존한다.
- P3는 P0 외에도 실제 tool 후보와 identity mapping에 의존한다.
- P3.1은 P3의 운영 평가 없이는 시작하지 않는다.
- P4는 P1의 실제 trace가 필요하며 P3는 선택적 선행 조건이다.

## 8. 구현 작업 분배

| 작업 | 주 저장소 | 협력 저장소 |
| --- | --- | --- |
| 공통 envelope 초안 | syncmill | toolgraph, tracegraph |
| span naming과 causal mapping | tracegraph | syncmill |
| Supervisor tracer adapter | syncmill | tracegraph |
| OTLP contract fixture | tracegraph | syncmill |
| 통합 report model | syncmill | tracegraph, toolgraph |
| preflight artifact schema | toolgraph | syncmill |
| identity/tool mapping | toolgraph | syncmill |
| review candidate schema | tracegraph | toolgraph, syncmill |

공통 코드를 별도 네 번째 패키지로 조기에 추출하지 않는다. schema 파일과 fixture의 중복
비용이 실제로 커지고 version drift가 반복적으로 발생할 때만 공통 contract 패키지를
검토한다.

## 9. 실패 모드 정책

| 실패 | 기본 동작 | 기록 |
| --- | --- | --- |
| trace exporter unavailable | 실행 계속 | warning, export status |
| tracegraph ingest 실패 | 실행 결과 유지 | failed analysis artifact |
| toolgraph timeout | advisory에서는 실행 계속 | timeout과 profile |
| unknown agent/tool | 실행 계속, 명확한 경고 | unresolved identity |
| graph drift | advisory warning | graph generation과 drift evidence |
| strict explicit violation | 실행 중단 가능 | verdict와 provenance |
| report write 실패 | 실행 결과는 출력, cleanup 계속 | stderr와 fallback summary |

모든 adapter는 bounded timeout을 가져야 한다. cleanup은 adapter 결과와 관계없이
`finally` 경로에서 수행한다.

## 10. 테스트 전략

### 저장소 단위

- toolgraph: preflight schema, deterministic result, unknown/drift contract
- syncmill: no-op adapter, run id propagation, strategy spans, cleanup failure paths
- tracegraph: OTLP normalization, causal parent/link, projection loss, pattern version

### Contract 테스트

- 생산 저장소가 만든 golden artifact를 소비 저장소가 읽는다.
- 지원하지 않는 major schema를 명확하게 거부한다.
- additive field가 이전 소비자를 깨지 않는다.
- redaction fixture에서 금지 필드가 출력되지 않는다.

### End-to-end 시나리오

1. route 첫 agent 성공
2. route 첫 agent 실패 후 fallback 성공
3. compete 병렬 실행 후 winner 선택
4. compete 일부 timeout과 나머지 성공
5. gate 실패로 결과 거부
6. toolgraph advisory violation 경고 후 실행 계속
7. trace exporter와 toolgraph 모두 비가용하지만 core 실행 성공

실제 유료 agent CLI 실행은 별도 opt-in smoke로 유지하고 기본 CI는 fixture와 fake runner로
완결한다.

## 11. 보안 및 개인정보

- OTLP attribute와 JSON artifact는 allowlist 방식으로 구성한다.
- prompt와 stdout을 활성화하는 옵션은 별도의 명시적 opt-in과 경고를 요구한다.
- resource URI와 provenance evidence에는 credential query 또는 로컬 사용자 경로가 포함될
  수 있으므로 저장 전에 redaction한다.
- HTTP endpoint는 loopback 기본값을 유지하고 원격 노출 시 인증 proxy를 요구한다.
- artifact directory는 repository commit 대상에서 제외하고 retention을 설정한다.
- digest는 무결성 참조이며 비밀정보를 안전하게 만드는 수단이 아니다.

## 12. 운영 지표

첫 통합 마일스톤 이후 다음 지표를 수집한다.

| 지표 | 목적 |
| --- | --- |
| tracing overhead p50/p95 | 실행 경로 비용 확인 |
| exporter/preflight timeout rate | 연계 안정성 확인 |
| trace ingest success rate | 계약 호환성 확인 |
| explainable failure coverage | RCA 가치 측정 |
| advisory violation/false-positive count | strict mode 판단 |
| report completion rate | artifact 신뢰성 확인 |
| redaction violations | 보안 회귀 차단 |

raw prompt나 사용자 코드를 지표 수집에 사용하지 않는다.

## 13. 릴리스 전략

1. **Experimental:** P0/P1, feature flag와 fixture 중심
2. **Preview:** P2, route/compete 실제 smoke와 통합 report
3. **Advisory beta:** P3, 경고 전용 preflight
4. **Strict opt-in:** P3.1 조건 충족 시 별도 release
5. **Feedback preview:** P4, review item만 생성

각 단계는 독립적으로 rollback 가능해야 한다. schema major 변경은 소비 저장소가 준비되기
전에 배포하지 않는다.

## 14. 의사결정 게이트

### Gate A: P0 종료

- 식별자, schema, redaction, timeout 정책 승인
- 각 저장소 owner가 책임 경계를 확인

### Gate B: P1 종료

- route/compete trace golden test 통과
- exporter failure가 core 실행과 cleanup에 영향 없음

### Gate C: P2 종료

- direct/monitor/board run report contract 일치
- 실제 한 실행을 report에서 RCA까지 추적 가능

### Gate D: P3 종료

- identity mapping과 qualified tool key가 실제 사례에서 검증됨
- advisory 오탐 및 미탐 기록 가능

### Gate E: strict mode 검토

- 운영 데이터, failure policy, rollback 절차가 모두 준비됨
- 명시적인 별도 승인 없이는 진행하지 않음

## 15. 첫 통합 마일스톤 완료 정의

다음 조건을 모두 만족하면 첫 통합 마일스톤을 완료한 것으로 본다.

- [ ] syncmill이 canonical `run_id`를 생성하고 모든 실행 경로에 전파한다.
- [ ] route와 compete 실행을 tracegraph가 ingest하고 RCA/diff할 수 있다.
- [ ] 통합 보고서가 result와 trace를 연결한다.
- [ ] toolgraph preflight 결과가 advisory artifact로 보고서에 연결된다.
- [ ] 모든 연계 기능을 끄면 기존 세 프로젝트의 core contract가 유지된다.
- [ ] exporter 및 preflight 장애에도 syncmill cleanup과 결과 출력이 유지된다.
- [ ] 금지된 민감 데이터가 span, report, preflight artifact에 포함되지 않는다.
- [ ] 세 저장소의 단위 테스트와 contract test가 통과한다.

## 16. 즉시 착수 항목

1. P0 용어와 envelope를 세 저장소 maintainer가 검토한다.
2. syncmill에서 `run_id`가 필요한 direct, monitor, board 경로를 목록화한다.
3. tracegraph에서 route/compete 합성 OTLP fixture를 먼저 만든다.
4. fixture를 기준으로 syncmill no-op tracer interface를 설계한다.
5. toolgraph 연계 대상이 되는 실제 MCP tool 후보와 agent identity를 조사한다.

코드 구현은 1~3번의 계약과 fixture가 합의된 뒤 시작한다.
