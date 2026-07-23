# `/tmp`에서 Syncmill 안전하게 시험하기

이 가이드는 실제 프로젝트 대신 **버려도 되는 `/tmp` Git 저장소**에서 Syncmill을
시험합니다. Claude, Codex, Kimi Code를 각각 한 번 호출하고, 결과가 원본에 바로
반영되지 않으며 `diff`와 `apply`를 거치는지까지 확인할 수 있습니다.

먼저 비용 없는 `doctor`와 dry-run까지만 실행해도 됩니다. 실제 provider 검증 단계는
각 CLI에 **한 번씩, 총 세 번** 요청하므로 사용량·요금·quota가 적용될 수 있습니다.

## 무엇이 격리되나요?

| 항목 | 위치 |
|---|---|
| 시험용 Git 저장소 | `/tmp/syncmill-guide.XXXXXX/demo-repo` |
| 격리 worktree | 같은 임시 폴더의 `state/workspaces` |
| run artifact와 apply 영수증 | 같은 임시 폴더의 `state/runs` |
| TUI job 상태 | 같은 임시 폴더의 `state/tui` |
| Syncmill memory DB·문서 | 같은 임시 폴더의 `state/memory` |
| uv 임시 캐시 | 같은 임시 폴더의 `state/uv-cache` |
| memtomem 임베딩 모델 캐시 | 기본은 기존 `~/.memtomem/cache`; 아래에서 선택적으로 격리 |

실제 호출에서는 로그인된 계정을 사용해야 하므로 provider CLI의 인증·설정·자체
캐시는 기존 홈 디렉터리를 계속 사용할 수 있습니다. **대상 코드와 Syncmill 실행
상태**가 `/tmp`에 격리되는 것이지, Claude/Codex/Kimi의 로그인 저장소까지 복제하는
것은 아닙니다. memtomem의 큰 임베딩·reranker 모델도 기본적으로 기존 캐시를
재사용합니다.

실제 호출이 성공하면 이 임시 memory DB 안에는 생산자별
`agent-runtime:<id>` 원본과 출처가 연결된 `shared` 복사본이 함께 생깁니다. 둘 다
`state/memory` 아래에 있으므로 마지막 정리 단계에서 같이 삭제됩니다.

> macOS에서 명령에 `/tmp`를 썼는데 출력은 `/private/tmp`로 보일 수 있습니다.
> 두 경로는 같은 위치를 가리키므로 정상입니다.

## 0. Syncmill 소스 준비

이미 `uv sync`를 마친 Syncmill 소스의 절대 경로를 지정합니다.

```bash
export SYNCMILL_SRC="/path/to/syncmill"
test -x "$SYNCMILL_SRC/.venv/bin/syncmill"
```

두 번째 명령이 실패하면 먼저 다음을 실행합니다.

```bash
cd "$SYNCMILL_SRC"
uv sync
```

## 1. 일회용 Git 저장소 만들기

아래 블록을 한 번에 복사해 실행합니다.

```bash
export SYNCMILL_TMP_ROOT="$(mktemp -d /tmp/syncmill-guide.XXXXXX)"
export SYNCMILL_TEST_REPO="$SYNCMILL_TMP_ROOT/demo-repo"

mkdir -p "$SYNCMILL_TEST_REPO" "$SYNCMILL_TMP_ROOT/state"
git init -q "$SYNCMILL_TEST_REPO"
git -C "$SYNCMILL_TEST_REPO" config user.name "Syncmill Guide Test"
git -C "$SYNCMILL_TEST_REPO" config user.email "syncmill-guide@example.invalid"
printf '# Syncmill temporary test\n' > "$SYNCMILL_TEST_REPO/README.md"
git -C "$SYNCMILL_TEST_REPO" add README.md
git -C "$SYNCMILL_TEST_REPO" commit -qm "initial temporary fixture"

printf 'Temporary root: %s\n' "$SYNCMILL_TMP_ROOT"
cd "$SYNCMILL_TEST_REPO"
```

다음 shell 함수는 명령은 Syncmill 소스에서 실행하되, 모든 Syncmill 상태 경로를
지금 만든 임시 폴더로 고정합니다. `--no-sync`는 준비된 `.venv`를 그대로 사용합니다.

```bash
sm() {
  SYNCMILL_WORKSPACE__ROOT="$SYNCMILL_TMP_ROOT/state/workspaces" \
  SYNCMILL_RUNS__ROOT="$SYNCMILL_TMP_ROOT/state/runs" \
  SYNCMILL_TUI__STATE_DIR="$SYNCMILL_TMP_ROOT/state/tui" \
  SYNCMILL_MEMORY__DB_PATH="$SYNCMILL_TMP_ROOT/state/memory/memtomem.db" \
  SYNCMILL_MEMORY__MEMORY_DIR="$SYNCMILL_TMP_ROOT/state/memory/files" \
  SYNCMILL_ROOMS__ENABLED="${SYNCMILL_ROOMS__ENABLED:-false}" \
  UV_CACHE_DIR="$SYNCMILL_TMP_ROOT/state/uv-cache" \
  uv run --no-sync --project "$SYNCMILL_SRC" syncmill "$@"
}
```

이후 이 문서의 `sm`은 모두 위 임시 저장소와 상태 경로를 사용합니다.

memtomem 모델 파일까지 `/tmp`에 격리하려면 실제 호출 전에 다음을 추가로
설정하세요. 기존 모델 캐시를 재사용하지 않으므로 처음에는 큰 모델을 다시 내려받을
수 있고, 마지막 정리 단계에서 함께 삭제됩니다.

```bash
export MEMTOMEM_FASTEMBED_CACHE="$SYNCMILL_TMP_ROOT/state/model-cache"
```

빠른 기능 확인이 목적이면 이 변수는 설정하지 않는 편이 좋습니다.

## 2. 초기화와 비용 없는 점검

```bash
sm init
git status --short
```

다음 세 파일이 새로 보이면 정상입니다.

```text
?? .syncmill/
?? syncmill.toml
```

`.syncmill/` 안에는 `project.json`과 `.gitignore`가 있습니다. 시험 기준점을 깨끗하게
만들기 위해 이 설정만 커밋합니다.

```bash
git add syncmill.toml .syncmill/project.json .syncmill/.gitignore
git commit -m "initialize Syncmill temporary test"
```

이제 준비 상태를 확인합니다.

```bash
sm doctor
```

기본 `doctor`는 활성 provider 중 하나 이상이 준비되면 `Ready.`로 끝납니다. 모든
provider의 CLI 자체 로그인 상태까지 강제하려면 `sm doctor --strict`를 사용합니다.
단, Claude를 환경 변수나 별도 외부 인증 방식으로 실행하는 환경에서는
`claude auth status --json`이 로그인되지 않았다고 보고해도 실제 최소 호출은 성공할
수 있습니다. 이 경우 아래의 provider별 강제 호출 결과를 최종 판단 기준으로 삼으세요.

세 provider의 실행 계획을 확인합니다. 이 단계에서는 provider를 호출하지 않고,
소스 파일도 바꾸지 않습니다.

```bash
sm run --dry-run "Create one provider smoke file" --agent claude
sm run --dry-run "Create one provider smoke file" --agent codex
sm run --dry-run "Create one provider smoke file" --agent kimi-code
```

각 출력에서 다음 항목을 확인합니다.

```text
"maximum_agent_calls": 1
No agent was started and no source file was changed.
```

## 3. TUI 먼저 열어 보기 — provider 호출 없음

실제 터미널에서 실행합니다.

```bash
sm tui --tab runs
```

- `Runs / Operations`에는 `no TUI jobs`가 표시됩니다.
- `v`를 누르면 아직 비어 있는 `Runs / Artifacts`로 이동합니다.
- `1`~`7`로 모든 탭을 열어 볼 수 있습니다.
- `q`는 TUI만 정상 종료합니다.

화면의 각 영역과 키는 [TUI 화면 안내서](tui-guide.ko.md)를 함께 참고하세요.

## 4. 실제 Claude 한 번 호출하기

다음 요청은 `provider-claude.txt` 하나만 만드는 작은 작업입니다.

```bash
sm run \
  "Create exactly one repository-root file named provider-claude.txt containing exactly claude ok followed by one newline. Do not modify, create, rename, delete, or commit any other file." \
  --agent claude --timeout 120
```

성공 출력의 `[syncmill] run:` 뒤 UUID를 복사합니다.

```bash
export RUN_ID="<Claude run UUID>"
sm diff "$RUN_ID"
test ! -e provider-claude.txt && echo "not applied yet: OK"
sm apply "$RUN_ID"
printf 'claude ok\n' | diff -u - provider-claude.txt
git add provider-claude.txt
git commit -m "verify Claude provider"
```

`diff` 명령은 새 파일 하나만 보여야 합니다. `apply` 전 `test`가
`not applied yet: OK`를 출력하고, 마지막 `diff`가 아무것도 출력하지 않으면 정확한
내용이 적용된 것입니다.

## 5. 실제 Codex 한 번 호출하기

```bash
sm run \
  "Create exactly one repository-root file named provider-codex.txt containing exactly codex ok followed by one newline. Do not modify, create, rename, delete, or commit any other file." \
  --agent codex --timeout 120
```

```bash
export RUN_ID="<Codex run UUID>"
sm diff "$RUN_ID"
test ! -e provider-codex.txt && echo "not applied yet: OK"
sm apply "$RUN_ID"
printf 'codex ok\n' | diff -u - provider-codex.txt
git add provider-codex.txt
git commit -m "verify Codex provider"
```

## 6. 실제 Kimi Code 한 번 호출하기

Syncmill의 agent ID는 `kimi-code`, 실행 파일 이름은 `kimi`입니다.

```bash
sm run \
  "Create exactly one repository-root file named provider-kimi.txt containing exactly kimi ok followed by one newline. Do not modify, create, rename, delete, or commit any other file." \
  --agent kimi-code --timeout 120
```

```bash
export RUN_ID="<Kimi run UUID>"
sm diff "$RUN_ID"
test ! -e provider-kimi.txt && echo "not applied yet: OK"
sm apply "$RUN_ID"
printf 'kimi ok\n' | diff -u - provider-kimi.txt
git add provider-kimi.txt
git commit -m "verify Kimi provider"
```

## 7. TUI에서 세 결과 확인하기

```bash
sm tui --tab runs
```

처음에는 `Runs / Operations`가 열립니다. 위 세 실행은 CLI에서 동기 실행했으므로
Operations에는 job이 없는 것이 정상입니다.

1. `v`를 눌러 `Runs / Artifacts`로 이동합니다.
2. 왼쪽에 Claude, Codex, Kimi Code의 `applied` artifact 세 개가 있는지 확인합니다.
3. 위·아래 방향키로 artifact를 선택합니다.
4. `d`로 Summary와 Diff를 오갑니다.
5. `q`로 종료합니다.

## 8. 선택: Rooms에서 세 provider를 동시에 비교하기

이 단계는 **새 provider 호출 세 번**을 추가로 발생시킵니다. Room 생성 자체는 호출이
없고, round를 시작할 때 Claude·Codex·Kimi Code가 각각 한 번 실행됩니다.

```bash
SYNCMILL_ROOMS__ENABLED=true sm room create \
  "Add one concise testing note to README.md. Do not change any other file." \
  -a claude -a codex -a kimi-code
```

출력의 room ID를 복사한 뒤 TUI에서 비교하는 방법이 가장 쉽습니다.

```bash
export ROOM_ID="<room UUID>"
SYNCMILL_ROOMS__ENABLED=true sm tui --tab rooms
```

Rooms 화면에서:

1. 방을 선택하고 `x`를 눌러 호출 수를 확인한 뒤 round를 시작합니다.
2. 완료 후 후보별 `d` Diff, `o` Output, `m` Details를 비교합니다.
3. `e`로 후보 하나를 선택합니다.
4. `g`로 정확한 room·candidate·digest를 확인한 뒤 적용합니다.

세 후보 중 **선택한 하나만** 임시 원본 저장소에 적용됩니다.

## 9. 전체 결과와 경로 확인

```bash
git log --oneline --decorate -5
git status --short
sm runs list
find "$SYNCMILL_TMP_ROOT/state" -maxdepth 3 -type d | sort
```

provider 세 파일을 각각 커밋했다면 `git status --short`는 비어 있어야 합니다.
시험 경로는 다음 명령으로 언제든 확인할 수 있습니다.

```bash
printf '%s\n' "$SYNCMILL_TMP_ROOT"
```

## 10. 안전하게 정리하기

먼저 TUI와 detached worker가 모두 종료됐는지 확인하세요. 그다음 임시 경로가 이
가이드에서 만든 이름과 일치할 때만 삭제합니다.

```bash
cd /tmp
case "$SYNCMILL_TMP_ROOT" in
  /tmp/syncmill-guide.*|/private/tmp/syncmill-guide.*)
    rm -rf -- "$SYNCMILL_TMP_ROOT"
    ;;
  *)
    echo "Refusing to remove unexpected path: $SYNCMILL_TMP_ROOT"
    ;;
esac

unset SYNCMILL_TMP_ROOT SYNCMILL_TEST_REPO RUN_ID ROOM_ID
unset MEMTOMEM_FASTEMBED_CACHE
unset -f sm
```

`MEMTOMEM_FASTEMBED_CACHE`는 앞의 선택 단계에서 내보냈을 때만 의미가 있지만,
설정한 채로 두면 같은 셸의 이후 memtomem 실행이 방금 삭제한 임시 캐시 경로를
다시 만들어 사용하므로 함께 해제합니다. 설정한 적이 없어도 `unset`은 무해합니다.

삭제 후에는 run artifact와 apply 영수증도 함께 사라집니다. 결과를 남기고 싶다면
정리 전에 임시 루트 전체를 다른 안전한 위치로 복사하세요.
