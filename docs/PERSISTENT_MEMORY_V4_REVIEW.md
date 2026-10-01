# Persistent Memory v1 + Reset — 4차 수정

작업 기준: `wip/interaction-manager-v2`, HEAD
`ad0f85a44256d60eadd720f47cfd8c68987e8f43`. 기존 uncommitted diff 위에서 수정했다.
commit/push, reset/revert/restore/clean, Pi 배포, 운영 Firebase 변경은 실행하지 않았다.
1차·3차 문서는 이력이며 현재 규격은 이 문서와 `DB_SCHEMA.md`다.

## 1. Reset consistency contract

**Guardian이 SUCCESS를 표시한 이후에는 reset 이전 persistent memory를 포함한 새 LLM
request가 시작될 수 없다.** Guardian은 operation의 `status=completed`를 확인해야만 성공한다.
API를 이미 시작한 응답의 재생, 기존 대화 기록, 돌봄 상태 삭제를 의미하지 않는다.

TTL lease와 요청별 generation GET을 dispatch 허가 수단에서 제거했다. Pi worker가 같은
프로세스의 exclusive barrier를 획득한 상태에서만 reset을 실행하고 완료를 기록한다.
프로세스가 callback 직전 정지하면 shared 권한도 유지되므로 SUCCESS가 앞설 수 없다.

## 2. MemoryDispatchBarrier와 runtime 소유권

`memory_dispatch.py`는 shared/read와 exclusive/write 임계구역을 제공한다.
`MemoryRequest.dispatch()`는 shared를 얻은 후 마지막 cache validity 판단·선택·직렬화를
수행하고, `responses.create()`를 호출하는 동기 callback이 반환할 때까지 shared를 유지한다.
SDK 응답 대기까지 보호하는 보수적인 범위다. callback이 예외를 던져도 finally로 해제한다.
callback은 동기적으로 API를 시작해야 하며 deferred coroutine/future를 반환하는 용도가 아니다.

reset 대기자가 생기면 새 shared 획득은 즉시 실패하고 새 턴은 persistent memory 없이 진행한다.
reset 시도는 기본 250ms 후 반환하지만 기존 reader를 강제로 만료시키지 않는다. writer intent를
유지한 채 다음 poll에서 재시도한다. SDK에는 기존 유한 timeout과 `max_retries=0`을 유지했다.
영구 정지한 reader에 대해서는 완료를 거짓으로 보장하지 않으며 Guardian은 대기 상태다.

독립 검토에서 같은 Pi의 두 프로세스가 서로 다른 barrier를 만들 수 있는 경계도 재현했다.
`memory_runtime.py`는 backend+elder URL의 hash를 이름으로 사용하는 OS advisory lock으로
하나의 memory runtime만 허용한다. Linux `flock`, Windows byte lock을 쓰며 TTL이 없다.
중복 runtime은 기억 없이 진행하고 reset worker를 시작하지 않는다. 종료 timeout 때 기존
dispatch/reset이 남으면 소유권을 유지한다. 실제 프로세스 종료 시 OS가 해제한다.

## 3. Guardian → Pi lifecycle

1. 기존 2단계 확인과 정확한 확인 문구 후 브라우저가 operation별 recovery marker를 먼저 저장한다.
2. Guardian은 create-if-absent transaction으로 `requested` operation만 만든다.
3. Pi가 requested/processing을 각각 최대 32개 조회한다. offline 동안에는 요청이 남는다.
4. Pi가 exclusive를 얻고 operation을 processing으로 기록한다.
5. `_control.pendingReset` CAS로 이전 extraction publication을 fence한다.
6. 소유권을 확인한 conversation body만 archive하고 generation/manifest/receipt를 CAS한다.
7. local cache, observed generation, durable local reset epoch를 설치한다.
8. 동일 exclusive 구간에서 외부 operation을 completed로 기록한 후 barrier를 해제한다.
9. Guardian이 completed를 읽으면 SUCCESS와 marker 제거를 수행한다.

전송/권한/응답 유실 오류는 processing 및 같은 ID의 복구로 처리한다. 유효하지 않은 요청처럼
확정 가능한 경우만 failed다. cleanup 실패는 이미 게시된 generation을 되돌리지 않는다.

## 4. Offline, restart, idempotency, 동시 요청

Pi offline/중복 runtime이면 Guardian은 REQUESTED/WAITING_FOR_DEVICE이며 SUCCESS가 아니다.
Pi 재시작 후 requested, processing, control pending owner를 재조회한다. reset CAS 뒤 완료 ACK가
유실돼도 control receipt로 재설치 후 완료를 기록하므로 generation을 다시 올리지 않는다.
외부 completed operation은 영구 멱등성 기록이고 내부 receipt는 최대 32개다.

동시에 들어온 요청은 실제 reset CAS 직전에 읽은 operation ID 목록 `resetCoveredIds`(최대64)에
속할 때만 같은 generation으로 coalesce한다. 시각이 같은 밀리초라는 이유로 묶지 않는다.
따라서 첫 reset 이후 새로 요청한 reset은 시각이 같아도 별도 generation을 얻어, 아직 저장되지
않은 extraction까지 fence한다. 요청이 query bound보다 많으면 여러 bounded batch로 처리된다.

## 5. M2 preference identity

Unicode NFKC, 대소문자, whitespace/punctuation을 정규화하며 조사라고 추측해 글자를 자르지 않는다.
정규화 subject가 같거나 검증된 evidence fingerprint가 같으면 우선 동일 target/duplicate 후보다.
compact subject의 단어 경계는 실제 evidence에 나타난 완전한 token 배열로만 보완한다.
`매운음식`과 evidence의 `매운 음식`을 인식하면서 `연자`를 `김연자`의 별칭으로 만들지 않는다.

관련 후보 전체를 검사한 뒤 변경한다. broad가 여러 specific target과 연결되면 ambiguous이며
어떤 independent specific도 병합/삭제하지 않는다. 모두 더 최신이면 delayed generalization은
버린다. 최신 broad는 별도로 유지할 수 있다. exact/evidence duplicate가 broad를 통해 다른
specific을 간접적으로 archive하지 못하도록 별도 회귀로 검증했다. 같은 target에서는 발언
timestamp가 최신인 내용이 이긴다. 김연자/나훈아 등 named entity를 트로트 category로 합치지 않는다.

## 6. M5 reconciliation

operation 경로가 authoritative source다. verify는 쓰지 않으며 읽기 권한 거부, 네트워크 장애,
timeout, unavailable, 누락된 receipt는 UNKNOWN이다. recovery marker를 유지하고 새 operation을
발급하지 않는다. UI는 “초기화 결과를 확인할 수 없습니다. 연결/권한을 확인한 후 다시 확인하세요.”와
“결과 확인”을 표시한다. requested/processing은 WAITING_FOR_DEVICE다.

marker 제거는 server completed/failed만 허용한다. reload/reconnect 시 해당 scope의 모든 marker를
조회하고 같은 operation을 reconcile한다. 확인창을 먼저 열어 둔 다른 탭도 최종 요청 직전 marker를
다시 열거한다. 권한 복구 후 같은 ID가 completed로 확인되면 SUCCESS와 marker 제거가 이루어진다.
브라우저 transport timeout은 요청 자체 취소의 증거가 아니므로 UNKNOWN을 유지한다.

## 7. Low 방어와 기존 PASS 보존

- Low1 수정: active body를 streaming으로 읽고 4KiB 초과 시 JSON 해석 전에 닫는다.
  1KiB chunk 기준 malformed body당 최대5KiB를 소비한다. 2MiB unknown metadata 회귀를 추가했다.
  정상 writer의 field projection/4KiB 제한, active manifest 128, cache 128KiB를 유지한다.
- Low2 수정: manifest ID만으로 삭제하지 않는다. body의 source=conversation, elder 소유권과
  body schema를 확인한다. user/foreign body와 해당 active manifest entry는 보존한다.
- cold-cache WAITING→READY, reset→INVALIDATED, durable drain/recovery, 최신 발언 retention,
  서울 날짜, prompt injection boundary와 care separation을 회귀 검증한다.
- Interaction Manager, AUDIO_LOCK, radio pause/no-auto-resume, display/game/device reset은
  기존 동작을 유지하고 전체 suite로 검증한다.

## 8. Firebase와 로컬 schema 변화

```text
memoryResetOperations/{elderId}/{operationId}
  status: requested | processing | completed | failed
  deviceId, requestedBy: guardian_dashboard, requestedAt: server ms
  baseGeneration, processingAt: server ms
  generation/resultingGeneration, completedAt: server ms, coalesced
  failureCode (확정 실패 시)

memories/{elderId}/_control          # <=64KiB, schemaVersion 2 유지
  generation, activeIds <=128, archiveIds <=64
  pendingReset: {operationId, createdAt}
  resetOperations <=32              # 내부 멱등성 receipts
  resetCoveredIds <=64              # reset 전에 확인된 operation cohort
  lastResetAt, resetCutoffAt         # Firebase server timestamps
memories/{elderId}/active/{id}       # 정상 body <=4KiB
memories/{elderId}/archive/{id}      # <=64 / reset 시 30일 pruning
```

이전 leases는 호환 읽기만 허용하고 새 lease를 발급하지 않는다. reset에서 비운다.
새 operation collection에는 `.indexOn: ["status"]`가 필요하다. Guardian은 자신의 요청 생성과
조회, Pi는 요청 처리·memory mutation·완료 쓰기 권한이 필요하다. 이 권한과 인덱스는 배포 전
검증 대상이며 운영 규칙을 변경하지 않았다. elder root GET/PUT은 없고 삭제는 명시적 leaf PATCH다.

로컬 outbox에는 job JSON 외 `reset-epoch.state`가 추가된다. reset generation별로 원자적으로
갱신하며 WAITING job과 살아 있는 session을 clock skew와 무관하게 invalidate한다.
같은 generation의 재확인은 새 작업을 무효화하지 않는다. OS lock에는 URL/개인정보를 쓰지 않는다.

## 9. 이번에 실제 수정한 파일

- Pi: `persistent_memory.py`, `memory_storage.py`, `voice_ai.py`;
  새 `memory_dispatch.py`, `memory_reset.py`, `memory_runtime.py`.
- Guardian: `src/memoryReset.js`, `src/services/firebase.js`, `src/components/ResetSettings.jsx`,
  `src/App.jsx`의 deviceId 전달.
- Python: 기존 memory cache/protocol/storage/persistent/voice integration fixture 조정;
  새 barrier, reset worker, body transport, identity v4, runtime ownership, service ownership,
  reset epoch 회귀 테스트와 `memory_protocol_fake.py`.
- Guardian: `memoryReset.test.js`, `memoryResetProtocol.test.js`, 새 `memoryResetOperations.test.js`.
- 문서: `DB_SCHEMA.md`, 이 보고서, 이전 V1/V3 보고서의 이력 표기, Pi README.

기존 `.gitignore`, display/device reset/Interaction Manager/care/radio 관련 user diff는 보존했다.

## 10. Regression-first 증거

수정 전 barrier 5개 미구현 오류, M2 신규 case 8개 실패 및 추가 bridge 1개 실패,
Guardian operation 5개 실패(permission verify가 FAILED가 되는 사례 포함), worker 미구현 오류를
확인했다. 추가 검토에서는 stream body 2개, OS owner 미구현, archive retention/크기/same-ms 경계,
다중 탭 요청과 background FAILED 표시도 먼저 실패를 재현한 뒤 수정했다.
Pi가 서버보다60초 빠른 WAITING job과 callback 전 재시작도 먼저 실패를 재현했고,
로컬 epoch/state와 session 등록으로 해결했다.

## 11. 전체 테스트

최종 실행 결과 (2026-09-28): 모든 테스트는 로컬 fake/offline이며 운영 호출이 없다.

| 실행 | 결과 |
| --- | --- |
| Python full discovery | **645 실행, 643 PASS, 2 skip** |
| Memory + persistent 하위 suite | **187/187 PASS**, 위 full suite 포함 |
| Voice memory integration | **27/27 PASS**, 위 full suite 포함 |
| Pi browser / game JS | **65/65 PASS** |
| Guardian dashboard JS | **97/97 PASS** |
| Guardian production Vite build | **PASS**, 61 modules |
| git diff --check | **PASS**; Windows LF/CRLF 안내만 존재 |
| 신규 untracked 소스/문서 trailing whitespace 검사 | **0건** |

Python full suite에 Interaction Manager, medication/inactivity, device reset,
display/audio focus/radio, voice/controller/session 회귀가 모두 포함된다.
2 skip은 Windows 번들 환경에 설치되지 않은 native `webrtcvad-wheels` detector와 실제 OpenAI SDK
serialization contract다. PASS로 계산하지 않았다. 신규 reset epoch 회귀는14개, reset worker21개,
barrier5개, runtime OS 소유권7개와 service 소유권2개, identity v4 10개, stream body2개다.

## 12. 남은 limitations

- v1은 한 elder당 한 configured device, 같은 OS 계정/temp namespace와 canonical backend URL이다.
  multi-device는 각 device의 barrier ACK를 모아 완료하는 coordinator가 필요하다. 호스트 로컬 OS
  lock은 다른 물리 장치를 막지 않는다. Linux flock/Pi 하드웨어는 아직 실물 확인하지 않았다.
- 과거 TTL Guardian/Pi와 혼용할 수 없다. 전환 시 모든 작성자를 같이 갱신하고 Guardian의 memory
  직접 쓰기를 금지해야 한다. legacy orphan pendingReset/새 operation 경로에 없는 오래된 marker는
  안전하게 미확정으로 남으며 별도 offline 조정이 필요하다. 임의로 성공/실패를 추정하지 않는다.
- reset은 동기 SDK 반환까지 기다리므로 느린 응답만큼 완료가 늦어질 수 있다. timeout은 허위
  완료를 만들지 않는다. 이미 시작한 응답 및 별도 short-term conversation history는 별도 영역이다.
- pending operation을 초과량으로 쌓으면 여러 batch가 필요하다. 외부 operation history와 물리적
  orphan 정리는 관리자 정책이 필요하다. 일반 retrieval payload는 이들의 크기와 무관하다.
- 비정상 body의 상세 정리/reset ownership 검사는 음성 hot path와 분리한다. 정상 active 조회는
  stream 제한이 있지만 explicit maintenance/reset의 비정상 원본 진단은 큰 body를 읽을 수 있다.
- identity는 lexical policy다. evidence에도 단어 경계가 없는 compact subject의 의미 관계,
  동의어, 한국어 형태소는 임의 추정하지 않는다.
- 실제 Firebase rules/index/권한, 실제 네트워크, Pi audio 및 브라우저 하드웨어 검증은 남았다.

## 13. Git 상태

branch/HEAD는 시작 시점과 같고 모든 기존·신규 변경은 uncommitted다.
최종 상태는 tracked 수정 13개, untracked 31개다. 아래에는 작업 시작 전부터 있던
변경도 포함된다. commit/push하지 않았다.

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
?? apps/guardian-dashboard/test/memoryResetOperations.test.js
?? apps/guardian-dashboard/test/memoryResetProtocol.test.js
?? docs/PERSISTENT_MEMORY_V1.md
?? docs/PERSISTENT_MEMORY_V3_REVIEW.md
?? docs/PERSISTENT_MEMORY_V4_REVIEW.md
?? raspberry-pi-voice-hub/device_reset.py
?? raspberry-pi-voice-hub/memory_dispatch.py
?? raspberry-pi-voice-hub/memory_reset.py
?? raspberry-pi-voice-hub/memory_runtime.py
?? raspberry-pi-voice-hub/memory_storage.py
?? raspberry-pi-voice-hub/persistent_memory.py
?? tests/memory_protocol_fake.py
?? tests/test_device_reset.py
?? tests/test_memory_body_transport.py
?? tests/test_memory_cache.py
?? tests/test_memory_dispatch_barrier.py
?? tests/test_memory_extraction_regressions.py
?? tests/test_memory_identity_v3.py
?? tests/test_memory_identity_v4.py
?? tests/test_memory_protocol_v3.py
?? tests/test_memory_reset_epoch.py
?? tests/test_memory_reset_worker.py
?? tests/test_memory_runtime.py
?? tests/test_memory_service_ownership.py
?? tests/test_memory_shutdown.py
?? tests/test_memory_storage_safety.py
?? tests/test_memory_voice_integration.py
?? tests/test_persistent_memory.py
```

## 14. Reviewer exact interleavings와 재실행

| 시나리오 | 결정적 순서와 검증 |
| --- | --- |
| H1 A | `test_a_pause_immediately_before_callback_cannot_be_overtaken_by_reset`: shared 내 마지막 selection 직후 Event로 정지 → 서버 clock +1시간 → 실제 Pi worker에 요청 → writer intent 관찰, operation 미완료 → reader 재개/API initiation → reset CAS/local install → completed → 다음 dispatch에는 old context 없음 |
| H1 B | `test_b_exclusive_reset_new_turn_falls_back_without_old_context`: exclusive 보유 → 새 턴 즉시 [] → old memory 없음 |
| H1 C | worker `test_offline_request_remains_pending` + Guardian `regression H1 C`: 읽기 실패/기기 부재 → requested/WAITING_FOR_DEVICE → SUCCESS 금지 |
| H1 D | `test_restart_after_completed_write_loss_does_not_reset_twice`: reset CAS 성공 → completed PUT 유실 → worker 재생성 → 기존 receipt 재설치 → generation 1회, completed |
| H1 E | `test_two_simultaneous_requests_coalesce_to_one_generation`와 `test_request_arriving_during_archive_is_coalesced_after_restart`: 두 requested 또는 archive 중 두 번째 요청 → durable cohort → reset 1회/양쪽 completed |
| same-ms | `test_equal_timestamp_new_request_without_published_body_is_not_coalesced`: 첫 완료 → 아직 body 없는 새 요청이 동일 timestamp → cohort에 없음 → 새 generation으로 pending extraction fence |
| 중복 프로세스 | `test_child_process_blocks_other_owner_until_killed` + service ownership: 첫 owner 정지 → 두 번째 실패 → stop timeout으로도 이양 금지 → 실제 종료/reader drain 후 이양 |
| WAITING skew | `test_ahead_clock_waiting_job_is_discarded_after_reset`와 `test_control_reset_publication_before_callback_still_fences_waiting_job`: Pi +60초 → old WAITING job 저장 → reset CAS/epoch → worker 재시작 → extract/save 없이 stale job 제거 |
| M2 | `test_memory_identity_v4.py`: 아침/저녁 + delayed 산책, 매운음식 최신 + 음식 과거, evidence duplicate, broad exact bridge를 양쪽 arrival order로 검사 |
| M5 | `regression M5: permission errors preserve UNKNOWN marker...`: server completed → verify permission_denied → UNKNOWN/marker/새 요청 금지 → 권한 복구 → 같은 ID SUCCESS/marker 제거 |

```powershell
$memoryPython = 'C:\Users\YOUR_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $memoryPython -B -m unittest discover -s tests -p 'test_memory*.py' -v
& $memoryPython -B -m unittest discover -s tests -v
node --test tests/display_browser.test.js tests/memory_game.test.js
npm --prefix apps/guardian-dashboard test
npm --prefix apps/guardian-dashboard run build
git diff --check
git status --short
```
