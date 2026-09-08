# SyncMill 안정화 및 향후 개선 방향 — 2026-09-08

## 기준과 조사 범위

기준은 `main`의 `96c36d98c1d2922640e3c144eb8cd9914bc6ad5a`다. 저장소의 추적 Markdown 27개(9,324줄)를 목록화·검색하고, handoff, 사용자 가이드, 아키텍처·생태계 계획, 과거 Codex/Claude 메모리와 실제 `mm search` 및 memtomem MCP 검색 결과를 교차 검토했다. 과거 메모리는 탐색 단서로 사용했으며 구현 여부는 Git 객체·현재 소스로 판정했다. 전체 파일을 동등한 깊이로 검증했다는 의미는 아니다.

| 문서·증거 | 현재 해석 |
| --- | --- |
| README, CLAUDE, 영문/국문 사용자 가이드, CHANGELOG | 현재 지원 동작과 운영 한계를 우선 반영 |
| 상위 Cowork handoff, 과거 작업 큐·PR 리뷰 메모리 | 당시 스냅샷; 미푸시·CI 차단·완료 주장을 현재 사실로 승계하지 않음 |
| MillFlow 설계·PR #104/#106/#107/#108 | 미출시 설계/브랜치; 현재 main 기능과 구분 |
| 생태계 협업·Gate E/P4 기록 | 당시 실험 증거; 현재 provider live 검증으로 간주하지 않음 |
| 메모리 GC PR #109/#111 및 #112 | 현재 main의 회귀 방지를 포함해 이번 변경으로 통합 검증 |

기준 main의 [CI 34198954921](https://github.com/memtomem/syncmill/actions/runs/34198954921)은 7개 작업 성공이었다. 이는 이번 변경의 원격 CI 증거와 별개다. 공개 릴리스 v0.1.4와 main의 Unreleased 변경도 구분한다.

## 이번 구현: 메모리 정리 안정화

#109의 파일명 보정과 #111의 namespace purge를 현재 main 위에 통합했다. 기존 R1–R6 안전성 수정, 실행 결과 이후/빈 claim에서의 GC, 보드별 비차단 GC 잠금, 실패 backoff 및 성공 후 degraded 해제 동작을 보존했다.

- 모든 인덱스 루트에서 날짜를 포함한 정확한 legacy/native namespace 일별 파일명을 찾는다. 비슷한 이름·소유권을 입증할 수 없는 DB source·symlink는 삭제 성공으로 처리하지 않는다.
- memtomem의 선택적 `async purge_namespace(ns) -> int`는 SyncMill writer가 날짜/파일명을 선택하기 전 취하는 namespace 잠금을 공유한다. 디스크와 DB source의 합집합에 실제 memtomem L2 sidecar 잠금을 취해, 읽기는 끝났으나 DB commit 전인 indexer도 기다린다.
- 소스 파일 → DB namespace 행 → 현재 프로세스 검색 캐시 순서로 제거한다. 마지막 소스 검증까지 완료해야 성공이다. 반환 정수는 이번 호출에서 삭제한 파일 수이며, DB만 남은 재시도에서는 0도 성공이다.
- 부분 unlink, DB 실패, 캐시 무효화 실패, 잠금/시간 한도 초과는 미완료로 남긴다. 보드 ledger는 성공으로 응답한 run만 tombstone 처리하고 나머지를 재시도한다. 이미 지운 파일은 rollback하지 않으며 재시도는 멱등적이다.
- 소스 잠금 상한은 256개다. 실제 프로세스 `RLIMIT_NOFILE`과 현재 사용량에서 32개 여유를 남길 수 없는 경우에도 삭제 전에 거부한다. macOS의 낮은 기본 soft limit에서는 256개보다 적어도 거부될 수 있다. 제품 코드가 프로세스 한도를 자동으로 높이지 않는다.
- 한 purge는 upstream 잠금 예산을 전체 단계에서 확인한다. GC 반복에는 협력적 30초 예산을 적용한다. 동기 파일/SQLite 호출이 이벤트 루프를 막는 동안에는 강제 wall-clock 중단을 보장하지 않는다. 바깥의 advisory timeout이 먼저 발생하면 완료된 일부 삭제도 미확인 상태로 남을 수 있으나, 다음 sweep에서 안전하게 재시도한다.

이 잠금은 SyncMill writer와 같은 L2 프로토콜을 쓰는 indexer 사이의 상호 배제를 보장한다. native `mm add`/MCP writer는 SyncMill namespace 잠금을 공유하지 않으므로 최종 스캔 이후의 외부 쓰기는 배제하지 못한다. 다른 프로세스의 검색 캐시까지 무효화하지 않는다. 정상 live lease는 보호하지만 reclaimed lease의 유예가 끝난 느린 run은 여전히 수집될 수 있다. finalizer는 늦은 쓰기의 ledger를 다시 활성화하며, 그 전에 프로세스가 죽는 경우는 아래 수동 점검 대상이다.

## 과거 잔여 데이터: 읽기 전용 점검과 복구

자동 처리는 현재 ledger의 정리 대상에 한정한다. 과거 tombstone 전체 초기화, 공용 일별 파일 wildcard 삭제, migration snapshot 삭제는 수행하지 않는다.

1. 실제 운영 설정의 **보드 DB**, **메모리 DB**, **모든 index root**를 확인한다. 기본 경로를 추측하지 말고 사용 중인 `syncmill.toml`과 환경 override를 기준으로 한다. DB 내용과 개인 메모리는 외부 보고서에 복사하지 않는다.
2. 보드 DB를 읽기 전용으로 열어 후보 run을 확인한다. 예시는 소유권 원장만 보여 주며 삭제하거나 재시도 상태를 바꾸지 않는다. 테이블 전체가 없으면 해당 구버전의 백업/스키마부터 확인한다.

   ```bash
   sqlite3 -readonly /absolute/path/to/board.db \
     'SELECT run_id, item_id, claimed_at, reclaimed_at, purged_at FROM board_run_namespaces ORDER BY claimed_at;'
   ```

3. 해당 `board-run:<run_id>`와 정확히 대응하는 파일명 및 메모리 DB의 source 소유권을 비교한다. 이름이 유사하다는 이유만으로 삭제 대상으로 확정하지 않는다. 날짜만 있는 과거 shared 파일, symlink, 인덱스 밖 source, 소유권 불명 파일은 별도로 검토한다. 현재 실행·lease와 과거 tombstone을 함께 확인한다.
4. 정상 미완료 후보는 원인을 해결한 뒤 기존 backoff에 따른 다음 sweep을 관찰한다. 파일 디스크립터 부족이면 프로세스 실행 환경의 한도·동시 사용량을 검토한다. 256개 초과 source는 임의 분할 삭제로 잠금 계약을 우회하지 않는다.
5. 이미 tombstone된 잔여 데이터나 과거 혼합 파일은 해당 namespace를 사용하는 writer/watcher를 운영자가 통제할 수 있는 유지보수 시간에 백업과 항목별 복구 계획을 먼저 만든다. SQLite는 일관된 backup을 사용하고 source 파일도 함께 보존한다. 정확한 소유권이 입증된 항목만 별도 승인된 복구로 처리한다. shared 내용과 migration snapshot은 보존한다.
6. 복구 후 재색인하여 scratch가 `shared`에 나타나지 않는지, 원래 shared 결과가 유지되는지, 해당 ledger 상태가 실제 삭제 결과와 일치하는지 확인한다. 현재 제품에는 이 수동 복구를 일괄 실행하는 명령을 추가하지 않았다.

## 다음 단계의 순서와 완료 조건

| 순서 | 범위 | 착수/완료 조건 |
| --- | --- | --- |
| 1 | 이번 GC 안정화, #109/#111/#112 | hermetic·실제 프로세스 테스트와 현재 변경의 Linux/macOS CI 확인 후 기존 PR과 대체 관계 정리 |
| 2 | #122 보드 자동 compaction | 기본 OFF; 이미 있는 수동 compaction의 집계 보존 계약을 유지하고 보존 기간·잠금·실패 재시도를 검증 |
| 3 | #117 메모리 성능 | backend 단위 batching부터; strategy 단계의 무조건 `gather` 재도입은 취소·프로세스 회수 보장 없이는 금지 |
| 4 | #113/#114 selfbench | 환경/인프라 실패와 제품 실패를 구분하고 재현 가능한 새 baseline 확보 |
| 5 | #104/#106/#107/#108 MillFlow | manifest, lease/attempt, receipt schema를 먼저 하나로 합의하고 strict preflight 실패를 해결 |
| 이후 | A2A 및 자동 모델 선택 | A2A 확장은 보류; 자동 routing은 calibration 증거를 요구하며 기본 실행/적용을 자동 활성화하지 않음 |

#106의 과거 sealed control-execution receipt / `workflow verify` 완료 메모는 현재 브랜치와 일치하지 않는다. seal commit이 rebase 과정에서 빠졌고 현재 main에는 MillFlow 모듈이 없다. #107/#108의 control-plane 설계는 #104의 dispatch/lease/attempt 계약과 충돌하므로 기능을 더 얹기 전에 정리해야 한다.

내부 `ToolCallRecord.retry_of_sequence`, retry causality 테스트와 synthetic canary는 현재 구현되어 있다. 이를 실제 provider가 explicit retry 관계를 제공한다는 증거로 확대하지 않는다. 과거 “모든 CI가 billing으로 차단” 주장 역시 현재 기준 main CI 성공으로 갱신한다. #103의 Windows 작업은 현재 플랫폼 거부 정책과 대조하고, 이번 GC와 무관한 #97/#99는 범위를 넓혀 함께 수정하지 않는다.

## 검증 기록

- 메모리 단위 테스트: 151 passed.
- 실제 memtomem 통합: 14 passed, 임시 DB와 임시 source만 사용. 독립 writer-before/during-purge, 두 purge 직렬화, 실제 watcher read-before-commit, unlink 후 프로세스 강제 종료와 재시도, 255/256/257개 source 및 실제 FD 여유 부족 거부·잠금 반환을 검증했다.
- 최초 macOS 실험은 255/256개 잠금에서 `EMFILE`을 재현했다. 이 증거에 따라 descriptor admission 검사를 추가했으며 높은 한도에서의 성공과 낮은 한도에서의 선행 거부를 각각 검증했다. watcher는 sandbox 밖의 실제 파일 이벤트로 검증했다.
- CI에 Linux/macOS의 opt-in 실제 백엔드 작업을 추가했다. 로컬 통과를 원격 통과로 간주하지 않는다.
- 전체 hermetic 회귀: 종료 코드 0(2,381개 수집, opt-in 테스트는 기본 skip). 문서 테스트 31개, 수정 문서 로컬 링크 검사, Ruff 및 `git diff --check` 통과. wheel/sdist 빌드와 wheel에서 직접 import하는 smoke 통과.
- 이번 변경의 원격 CI는 아직 실행 전이며 PR 전달 기록에서 별도로 확인한다. 실제 provider 호출, 운영 메모리 삭제, migration 및 자동 apply는 실행하지 않았다.
