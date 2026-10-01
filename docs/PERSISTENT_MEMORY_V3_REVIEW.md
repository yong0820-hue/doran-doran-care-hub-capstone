# Persistent Memory + Reset 3차 수정 보고

> 4차 수정 이전의 역사 문서입니다. 현재 contract와 구현은
> [PERSISTENT_MEMORY_V4_REVIEW.md](PERSISTENT_MEMORY_V4_REVIEW.md)를 따릅니다.
> 아래 TTL lease 및 Guardian 직접 reset 설명은 현재 규격이 아닙니다.

작업 기준: `wip/interaction-manager-v2`, HEAD `ad0f85a44256d60eadd720f47cfd8c68987e8f43`.
기존 미커밋 구현 위에 수정했다. commit/push, Pi 배포, 운영 Firebase 접근·migration은 하지 않았다.
이 문서가 기존 v1 문서의 storage/cache/reset 설명을 대체한다.

## 1. H1 consistency contract와 한계

기억을 사용하는 **논리적 요청의 시작점**은 `_control`에 generation lease를 CAS로
등록하는 admission이다. Reset SUCCESS 이후 admission되는 요청은 이전 generation의
기억을 얻지 못한다. Reset이 pending이면 새 admission은 기억 없이 진행한다.
이미 승인된 요청과 reset이 겹치면 승인된 요청이 reset보다 먼저 선형화된다.

**원래 요구를 외부 API의 물리적 호출 시각으로 해석하면 H1 완전 해결은 아니다.**
클라이언트가 마지막 expiry 검사 직후 정지하고 TTL/reset SUCCESS 이후 SDK 호출 직전에
재개되는 경우는 클라이언트 lease만으로 배제할 수 없다. 이 강한 의미의 보장에는 reset과
generation-aware request dispatch를 함께 직렬화하는 신뢰된 실행 서버가 필요하다.
반복 GET으로 해결됐다고 주장하지 않는다. 현재 구현과 테스트의 보장은 위 admission 계약이다.

## 2. 실제 barrier/lease 구현

`memory_storage.py`의 `acquire_lease()`와 Guardian `runMemoryReset()`은 같은 `_control`
CAS 경계에서 경쟁한다. Guardian이 pendingReset을 먼저 publish하면 Pi의 이전 ETag lease
등록은 실패한다. Pi가 먼저 등록하면 Guardian은 lease 해제/만료까지 SUCCESS를 반환하지 않는다.

서버 HTTP Date 기준 TTL은 2초다. Pi의 dispatch deadline은 control GET 시작 시점부터
1초로 제한하여 Date의 초 단위 정밀도를 차감한다. Guardian은 추가 1초 margin 후 만료로
판단한다. Guardian clock은 `.info/serverTimeOffset`으로 샘플링한 뒤 monotonic elapsed를 쓴다.
Pi lease 대기 예산은 150ms, outstanding acquire/release worker는 최대 하나다.
timeout, clock 정보 부재, pending reset, generation 불일치에서는 no-memory다.
release 실패/프로세스 crash는 TTL로 해소된다. Guardian은 한 번에 3초까지만 기다리고,
해결되지 않으면 operation을 보존한 UNKNOWN으로 돌아온다. 개별 Firebase SDK 작업 timeout은 5초다.

`MemoryRequest.dispatch()`는 1회만 호출할 수 있고 SDK 호출 직전에 deadline을 검사한다.
SDK 호출 성공·실패 모두 release를 시도한다. 이전 `context()`의 detached 반환 API는 빈 목록으로
fail closed하고 실제 voice는 `request_context().dispatch()`를 사용한다.

## 3. M2 preference identity

content 안에서 트로트 등의 임의 alias를 찾거나 한국어 마지막 글자를 조사로 제거하지 않는다.
NFKC/casefold로 subject를 정규화한다. 공백 차이의 exact subject 또는 명확한 token subset만
같은 target 후보로 취급한다. 김연자/나훈아는 서로 다르고 요가/오이는 그대로 유지된다.
type와 eventDate 경계 안에서 검증된 evidence digest/정규화 content 중복을 제거한다.
같은 preference target은 발언 timestamp가 최신인 내용이 이긴다. 다른 type끼리는 병합하지 않는다.
본문 저장 key는 copy-on-write UUID이고 논리적 `record.id`와 createdAt은 유지한다.

## 4. Cold/expired cache session 상태

`UNINITIALIZED → WAITING → READY`, reset 관찰 후 `INVALIDATED`를 사용한다.
turn 시작, LLM 요청 전, session 제출 전에 상태를 다시 확인한다. WAITING을 초기화 완료로
고정하지 않는다. 정상 refresh가 끝나면 같은 session이 generation을 획득한다.
끝까지 WAITING이어도 시작 시각을 outbox job에 저장하고 worker가 control을 읽어 해결한다.
`lastResetAt >= sessionStartedAt`이면 이전 대화를 새 generation에 저장하지 않는다.
이미 generation을 획득한 job은 save CAS에서 generation mismatch로 막힌다.
정상 shutdown drain과 durable recovery는 유지한다.

## 5. Multi-tab operation 복구

localStorage key는 `doran.memoryReset.pending.v2:<encoded backend+elder scope>.<operationId>`다.
새 operation은 자신의 key만 기록/삭제한다. reload 시 해당 scope의 모든 marker를 열거하고
각 operation을 reconcile한다. 완료/확정 실패만 정리하고 UNKNOWN은 유지한다.
이전 v1 singleton은 복구용으로 읽지만 새 요청은 v2 key만 쓴다.

서버의 `_control.resetOperations`는 최대 32개의 pending/completed/failed status와
generation/createdAt/completedAt을 유지한다. 진행 중 다른 operation은 BUSY다.
동일 operation의 verify는 이미 잡은 barrier를 이어서 완료할 수 있다. 이는 새 reset을
만드는 재시도가 아니다. ACK-loss 뒤 완료 receipt를 읽으면 SUCCESS다. receipt가 만료되었거나
불명확하면 UNKNOWN을 유지하고 임의로 새 generation을 증가시키지 않는다.

## 6. Firebase schema

```text
memories/{elderId}/
  _control/                       # <=64KiB; CAS boundary
    schemaVersion: 2
    generation: initial|string|integer
    activeIds: {bodyKey: true}     # <=128
    archiveIds: {bodyKey: epochMs} # <=64
    leases: {token: {generation, expiresAt}} # <=16
    pendingReset: {operationId, createdAt}
    resetOperations: {operationId: receipt} # <=32
    lastResetAt/resetAt, resetBy, lastResetOperationId
    legacyMigration, maintenanceRevision
  active/{immutableBodyKey}        # known fields only; <=4096 UTF-8 bytes
  archive/{immutableBodyKey}
  {legacyDirectChild}              # untouched; never normal retrieval
```

normal retrieval은 elder root GET을 하지 않는다. `_control`의 제한된 ID만 따라 개별 active
본문을 읽는다. 서버에서 읽는 경로 자체를 제한하므로 legacy/archive의 크기는 조회량과 무관하다.
단일 수정은 새 active leaf PUT 후 `_control` CAS로 manifest를 publish한다. full elder PUT은 없다.
늦은 PUT은 orphan만 만들 수 있으며 reset 이후 active manifest를 되살리지 못한다.
Reset은 manifest의 최대 128개 active만 읽고 선택된 최대 64개 archive leaf를 먼저 복사한 후,
generation 증가와 activeIds 비우기를 하나의 control transaction으로 commit한다.
그 뒤 명시적 leaf multi-location PATCH로 삭제한다. cleanup 실패는 이미 완료된 reset을 취소하지 않는다.

## 7. Legacy migration

음성 turn/startup에서 legacy를 자동 발견·다운로드하지 않는다. 관리자용
`MemoryStore.migrate_legacy(records, migration_id)`에 사전에 선별한 export를 명시적으로 전달한다.
최대 128개/512KiB 입력을 받고 conversation-owned active만 검증하여 저장한다.
source=user/ai 및 foreign 자료는 자동 신뢰하지 않는다. marker로 재실행을 구분한다.
기존 generation이 initial이 아니거나 reset pending이면 import를 거절한다. 따라서 reset으로
지운 옛 기억을 나중에 legacy import로 부활시키지 않는다. 운영 migration은 실행하지 않았다.

## 8. Retrieval/cache 정책

background refresh 최소 간격 15초, local cache 최대 나이 60초, 최대 128개/128KiB.
각 요청은 관련·최근·중요 기억 중 최대 8개, escaped JSON 최대 4200자를 사용한다.
정상 warm turn은 본문 GET 없이 cache를 사용하며 control lease만 읽고 쓴다.
care 상태 결정에는 어떤 기억도 전달하지 않는다. `<user_memories>`는 escaped JSON을 담은
비신뢰 user reference이고 persona/system instruction이나 단기 history에 저장하지 않는다.

## 9. Retention

active manifest 128, archive manifest 64 및 reset 때 30일 기준 pruning, receipts 32,
leases 16, worker queue 8, durable outbox 128. dated episodic은 사건일/주간 끝 8일 후 서울
자정까지, undated episodic은 발언 후 30일이다. preference/profile은 자동 날짜 만료하지 않는다.
읽기는 만료 자료를 제외하고 저장 시 active manifest를 compact한다.
timeout으로 남은 물리적 orphan은 retrieval 대상이 아니다. explicit
`maintain_orphans(active_ids, archive_ids)`는 외부 inventory의 알려진 최대 128 ID를 받아
control CAS nonce로 과거 publication을 먼저 무효화한 뒤 미참조 leaf만 삭제한다.
삭제 ACK loss/지연 PUT 뒤에는 같은 inventory로 다시 실행할 수 있다.

## 10. 이번 수정 파일

- Python: `persistent_memory.py`, 새 `memory_storage.py`, `voice_ai.py`의 memory 연결 부분.
- Guardian: `memoryReset.js`, `services/firebase.js`, `components/ResetSettings.jsx`.
- 기존 memory/cache/voice/shutdown 테스트의 storage fixture와 API expectation.
- 신규 `memory_protocol_fake.py`, `test_memory_identity_v3.py`, `test_memory_protocol_v3.py`,
  `test_memory_storage_safety.py`, Guardian `memoryResetProtocol.test.js`.
- 문서: 이 보고서, `DB_SCHEMA.md`, `PERSISTENT_MEMORY_V1.md`, Pi README.

기존 user diff의 display/device reset/Interaction Manager/care/radio 구현은 보존했다.

## 11. Regression tests

identity 신규 테스트에서 기존 코드 7개 실패, protocol 신규 테스트에서 기존 코드 3개 실패/4개
미구현 API 오류, voice cold-cache/dispatch regression 실패를 먼저 확인했다. 이후 해당 코드와
새 schema fixture를 수정했다. malformed body는 fail closed, immutable UUID와 논리 ID는 구분한다.
ambiguous CAS가 늦게 도착할 때 삭제된 본문을 publish하던 추가 race도 테스트 후 수정했다.

## 12. 전체 tests

최종 실행 결과 (2026-09-28):

| 실행 | 결과 |
| --- | --- |
| Python full discovery | 584 실행, 582 PASS, 2 skip |
| Pi browser/game JS | 65/65 PASS |
| Guardian dashboard JS | 94/94 PASS (memory 관련 33개 포함) |
| production Vite build | PASS, 61 modules |
| git diff --check | PASS (Windows LF/CRLF 안내만 있음) |

정확한 명령은 아래 재실행 절차를 참조한다.
Python full discovery는 voice sessions, Interaction Manager, medication/inactivity,
memory, device reset, display/radio 관련 모듈 전부를 포함한다.
Windows 번들 환경의 unavailable dependency skip은 native `webrtcvad-wheels` 1개,
실제 OpenAI SDK serialization contract 1개다. 이를 PASS로 세지 않는다.

## 13. Performance 측정

offline path-aware Firebase double의 JSON payload 계측이다(실제 RTT/HTTP 헤더 제외).
2,097,152-byte legacy direct child를 넣어도 초기 control+active retrieval은 281 bytes였다.
warm 10턴은 full elder GET 0, active body GET 0, control read 약 2.6KiB였다.
추가 archive 200개/약 2MB를 만들어도 10턴 control read 약 2.6KiB로 유지되었다.
초기 lease 필드 생성 여부에 따른 수십 byte 차이만 있다. 단일 memory update full elder PUT 0.
Guardian reset regression은 legacy를 읽지 않고 active manifest 범위만 처리하며 archive 64개를 검증한다.

## 14. 남은 제한

H1의 물리적 SDK 호출 시각 문제는 1절에 명시했다. Pi 세션 timestamp와 Firebase 서버 clock의
정상 동기화가 필요하며, Guardian의 serverTimeOffset도 무한 clock uncertainty를 해결하지 않는다.
물리적 orphan 수는 네트워크 장애가 무한 반복되면 증가할 수 있다. 논리 manifest와 voice 조회량은
bounded이며 별도 관리자 inventory/maintenance가 필요하다. migration/maintenance는 라이브 경로에서
자동 실행하지 않는다. pending reset 소유 탭이 닫히면 재접속 reconciliation 전까지 memory는
no-memory로 유지되며 음성 자체는 계속된다. 서로 다른 subject의 의미 동의어 추론은 하지 않는다.
실물 Pi, 실제 Firebase 보안 규칙·권한, 실제 네트워크 및 음성 장치는 이번 작업에서 검증하지 않았다.

## 15. Git

branch와 HEAD는 작업 시작 기준 그대로다. 기존 변경 및 이번 변경 모두 working tree에 남긴다.
reset/revert/restore/clean/commit/push를 실행하지 않았다. exact status는 `git status --short`로 확인한다.

최종 상태는 tracked 수정 13개, untracked 소스·테스트·문서 19개다. baseline diff를 포함한 목록:

```text
 M .gitignore
 M apps/guardian-dashboard/src/App.jsx
 M apps/guardian-dashboard/src/index.css
 M apps/guardian-dashboard/src/services/firebase.js
 M docs/DB_SCHEMA.md
 M raspberry-pi-voice-hub/README.md
 M raspberry-pi-voice-hub/display/app.js
 M raspberry-pi-voice-hub/display/index.html
 M raspberry-pi-voice-hub/display/styles.css
 M raspberry-pi-voice-hub/display_server.py
 M raspberry-pi-voice-hub/voice_ai.py
 M tests/display_browser.test.js
 M tests/test_display_server.py
?? apps/guardian-dashboard/src/components/ResetSettings.jsx
?? apps/guardian-dashboard/src/memoryReset.js
?? apps/guardian-dashboard/test/memoryReset.test.js
?? apps/guardian-dashboard/test/memoryResetProtocol.test.js
?? docs/PERSISTENT_MEMORY_V1.md
?? docs/PERSISTENT_MEMORY_V3_REVIEW.md
?? raspberry-pi-voice-hub/device_reset.py
?? raspberry-pi-voice-hub/memory_storage.py
?? raspberry-pi-voice-hub/persistent_memory.py
?? tests/memory_protocol_fake.py
?? tests/test_device_reset.py
?? tests/test_memory_cache.py
?? tests/test_memory_extraction_regressions.py
?? tests/test_memory_identity_v3.py
?? tests/test_memory_protocol_v3.py
?? tests/test_memory_shutdown.py
?? tests/test_memory_storage_safety.py
?? tests/test_memory_voice_integration.py
?? tests/test_persistent_memory.py
```

## 16. Reviewer exact concurrency scenarios

PowerShell (저장소 루트):

```powershell
$memoryPython = 'C:\Users\YOUR_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $memoryPython -B -m unittest discover -s tests -p test_memory_protocol_v3.py -v
& $memoryPython -B -m unittest discover -s tests -p test_memory_storage_safety.py -v
& $memoryPython -B -m unittest discover -s tests -p test_memory_identity_v3.py -v
& $memoryPython -B -m unittest discover -s tests -p test_memory_voice_integration.py -v
node --test apps/guardian-dashboard/test/memoryResetProtocol.test.js
& $memoryPython -B -m unittest discover -s tests -v
node --test tests/display_browser.test.js tests/memory_game.test.js
npm --prefix apps/guardian-dashboard test
npm --prefix apps/guardian-dashboard run build
git diff --check
git status --short
```

H1 A: `H1 A: reset cannot succeed until an existing generation lease is released`는 lease를
심고 reset pending에서 promise가 미완료임을 검사한 후 release하여 commit/SUCCESS를 확인한다.
H1 B는 fake server clock을 TTL+margin 이후로 진행하여 crash owner 없이 reset 완료를 검사한다.
H1 C는 pending barrier에서 두 번째 operation BUSY/단일 generation 증가를 검사하고,
Python `test_pending_reset_cannot_acquire_old_memory`가 Pi no-memory를 검증한다.
`test_delayed_old_control_cannot_grant_lease_after_reset`은 control read와 lease CAS 사이에 reset을 넣는다.
`test_reset_between_body_write_and_manifest_cannot_resurrect`는 active leaf PUT 뒤 reset을 넣는다.
`test_ambiguous_publication_never_deletes_staged_bodies`는 timeout된 CAS를 read-back 이후 늦게 전달한다.
M5 marker interleaving은 A/B가 빈 storage를 본 뒤 각각 기록, A만 SUCCESS, B UNKNOWN,
reload에서 모든 marker를 개별 reconcile하는 순서를 재현한다.
Cold-cache 테스트는 Event로 active_read를 막아 WAITING을 강제하고 refresh 해제 또는 reset을 삽입한다.
