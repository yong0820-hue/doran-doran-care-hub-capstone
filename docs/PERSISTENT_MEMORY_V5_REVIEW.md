# Persistent Memory v1 + Reset — 5차 수정

기준: `wip/interaction-manager-v2`, HEAD `ad0f85a44256d60eadd720f47cfd8c68987e8f43`.
시작 시 branch/HEAD/status/diff stat/diff/diff check를 확인했다. 기존 uncommitted
변경 위에 남은 M2/M5/Low 두 항목만 수정했다. commit/push, reset/revert/clean/restore,
Pi 배포, 운영 Firebase 접근/변경은 하지 않았다. V1/V3/V4 보고서는 이전 작업 이력이다.

## 1. M2 root cause와 최종 identity policy

기존 구현은 evidence duplicate와 exact subject를 동시에 모은 후 newest target을 골랐다.
따라서 동일 아침 산책 evidence의 extractor subject가 `산책`으로 바뀌면 독립적인 일반 산책
기록을 덮어썼다. 또 content의 `음식을` token이 `음식`과 달라 `매운음식`의 공백 복원이 실패했다.

동일 normalized content 또는 evidenceKey를 가진 기존 owner를 target보다 먼저 확정한다.
재추출 subject가 broad해져도 그 owner만 갱신하며 독립 broad 기록을 흡수하지 않는다.
같은 captured timestamp의 동일 evidence 재실행은 no-op이다. memory type 사이에는 합치지 않는다.

subject는 그대로 유지하고 NFKC/공백/문장부호 비교를 사용한다. content 비교 view에서만
을/를/은/는의 종성 조건, 두 음절 이상 stem, 전체 supplied subject span 일치를 확인해
조사 suffix 하나를 제외할 수 있다. `요가→요`, `오이→오`, `김연자→연자`는 추론하지 않는다.
여러 specific과 연결되는 broad는 ambiguous이며 specific들을 archive/overwrite하지 않는다.
확실한 동일 target 안에서만 최신 발언 timestamp가 이긴다. 기존 legacy stale alias 정리는
단일 specific 관계와 더 최신인 stored truth가 함께 확인되는 기존 좁은 범위로 유지했다.

## 2. M5 root cause와 reset coordinator policy

operation collection cohort 조회와 `_control` generation CAS가 다른 atomic domain이어서,
조회 후 생긴 B 요청이 A 완료 뒤 별도 destructive reset이 됐다.

Pi는 exclusive barrier를 유지한 채 `_control` CAS로 pendingReset과 coordinator leader를
함께 claim한다. coordinator는 `activeOperationId/deviceId/epoch/baseGeneration/claimedAt`을
보관하고 `processing → committed → completed`로 진행한다. generation/manifest/leader receipt는
동일 control CAS에서 게시된다. local cache 및 durable epoch 설치 후 외부 operation을 completed로
게시하고 마지막 coordinator CAS를 마친다. ACK loss와 재시작은 receipt/영구 operation을 재조회한다.

Guardian은 최종 확인 문구 입력을 허용하기 전에 read-only intent를 준비한다. intent에는
`version:1/generation/epoch/operationId`가 있고 최종 확인 이후의 지연 조회나 create 재시도는
이를 바꾸지 않는다. 준비 중에는 final confirmation을 실행할 수 없고 취소된 준비 응답은 버린다.
준비만 실패한 경우 요청을 보내지 않았으므로 marker를 만들지 않으며 다시 준비할 수 있다.
실제 요청을 보낸 뒤 UNKNOWN이면 기존 marker와 동일 operation 복구 정책을 유지한다.

진행 중 leader를 본 intent는 그 leader에 연결한다. claim 이전 intent는 다음 epoch로 연결한다.
따라서 최종 CAS 전/중/후, 완료 후 늦은 operation 생성도 원래 reset의 결과로 완료된다.
receipt 32개를 초과한 늦은 요청은 영구 leader operation 또는 bounded deviceEpoch 조회로
원래 generation을 찾는다. follower는 자기 ID를 유지하며 `coalescedTo`를 기록한다.

A operation이 이미 completed인데 coordinator finish CAS만 늦어진 경우, Guardian preparation은
영구 leader의 completed 결과를 읽고 새 intent를 준비할 수 있다. 실제 완료 이후 새로 준비·확인한
reset은 동일 millisecond여도 새 epoch/generation을 얻는다. 중간 경계에서 임의 TTL로 성공시키지 않는다.

## 3. nonconversation manifest 처리

동일 bounded body read에서 ownership과 유효한 schema projection을 구분한다. conversation writer는
확인된 자기 소유 항목만 갱신/prune한다. user/ai/다른 elder 및 oversized 등 소유권 확인 불가 항목의
manifest/body를 보존한다. malformed이지만 ownership이 확인되는 conversation body는 정상 evidence로
교체할 수 있다. 보존 항목의 슬롯을 빼고 최신 conversation 항목을 유지하여 총 128개 제한을 지킨다.
128개가 모두 보존 대상이면 쓰기 없이 save를 거절한다. reset 다음 save까지 보존을 검증했다.

## 4. old_device starvation 처리

경로 migration 없이 operation에 `deviceStatus={deviceId}|{status}`를 추가했다. Pi는 status별 32개를
device scope로 서버 조회한다. failed/completed도 index 값을 함께 갱신하여 terminal row가 queue를
막지 않는다. 이전 요청에는 `$key` cursor로 최대 33개씩 접근하여 고정된 첫 32개 반복을 피한다.
70개 old_device 뒤 current-device 요청은 새 schema에서 1 poll, legacy 요청은 3 poll 안에 처리된다.

운영 반영 전 필요한 설정은 elder operation collection의
`.indexOn: ["deviceStatus", "deviceEpoch"]`, Guardian의 bounded `_control` 및 leader operation
읽기 권한이다. Guardian은 control/memory를 쓰지 않는다. 실제 rules/index는 변경하지 않았다.

## 5. 이번에 실제 변경한 파일

이 목록은 이번 5차 작업의 수정이며 기존 working tree 전체 변경 목록과 다르다.

- Pi: `raspberry-pi-voice-hub/persistent_memory.py`, `memory_reset.py`, `memory_storage.py`.
- Guardian: `apps/guardian-dashboard/src/memoryReset.js`, `src/services/firebase.js`,
  `src/components/ResetSettings.jsx`.
- Python: `tests/memory_protocol_fake.py`, `test_memory_reset_worker.py`,
  `test_memory_voice_integration.py`; 신규 `test_memory_identity_v5.py`,
  `test_memory_manifest_v5.py`, `test_memory_reset_coordinator_v5.py`.
- Guardian tests: `apps/guardian-dashboard/test/memoryReset.test.js`,
  `memoryResetOperations.test.js`, `memoryResetProtocol.test.js`; 신규 `memoryResetCoordinator.test.js`.
- 문서: `docs/DB_SCHEMA.md`, 이 보고서.

H1 barrier/runtime/voice 구현, care/Interaction Manager/audio/display/game 구현은 이번 작업에서 바꾸지 않았다.

## 6. 신규 regression tests

- identity v5: 11개. 필수 A–G의 ID/subject/content/evidenceKey/createdAt/updatedAt/lastMentionedAt/status,
  type 분리, normalized evidence, 동일 시각 replay, specific 하나와 독립 broad 보존.
- manifest v5: 6개. source/elder 보존, concurrent foreign CAS, oversized 2 MiB read 제한,
  127/128개 capacity, malformed owned repair.
- reset coordinator v5: 15개. CAS 전/중/후 및 지연 생성, simultaneous, 신규 intent, claim/commit/
  completion/finish ACK loss, CAS conflict, receipt eviction, indexed/legacy starvation, malformed queue,
  reset 이후 nonconversation save, completed operation 뒤 coordinator 지연 중 새 reset.
- Guardian: coordinator/intent, A/B markers, preparation/typed confirmation 순서,
  delayed read, read-only preparation failure, 취소/재확인 및 권한 회귀.

수정 전 identity 신규 10개 중 8 failures, same-time replay 추가 failure를 확인했다.
M5 첫 7개에서 6 failures/3 subcase errors, Low1 첫 5개 모두 failure를 확인했다.
추가 검토에서 malformed repair, preclaim result attribution, Guardian preparation 경계 7개도
먼저 실패시킨 뒤 수정했다. 모두 로컬 fake이며 외부 서버를 호출하지 않았다.

## 7. 전체 test 결과

최종 검증 결과는 아래에 기록한다.

| 실행 | 결과 |
| --- | --- |
| Python full discovery | 677 실행, 675 PASS, 2 skip |
| Memory + persistent subset (위 full에 포함) | 219 PASS |
| Voice memory integration (위 subset에 포함) | 27 PASS |
| Pi browser/game JS | 65 PASS |
| Guardian dashboard JS | 114 PASS |
| Guardian production build | PASS |
| git diff --check | PASS (LF/CRLF 안내만) |
| untracked 포함 변경 파일 trailing whitespace | 0건 |

full Python에는 Interaction Manager, medication/inactivity, device reset,
display/radio/game/audio-focus, memory shutdown/recovery와 voice integration이 포함된다.
2 skip은 Windows 번들 Python에 없는 `webrtcvad-wheels` native detector와 실제 OpenAI SDK
request serialization 계약이다. skip은 PASS에 포함하지 않았다. 실물 Pi/Firebase 검증은 수행하지 않았다.

## 8. H1 재회귀 여부

발견 없음. `MemoryDispatchBarrier` shared/exclusive 구현을 바꾸지 않았다. callback 직전 pause가
reset SUCCESS에 추월되지 않음, reset 후 old context 없음, offline 거짓 성공 금지,
process ownership, durable epoch/WAITING/session invalidation 회귀가 통과했다.

## 9. M6 재회귀 여부

발견 없음. memory elder root GET/PUT 없이 bounded control CAS, immutable active leaves,
128 active/64 archive/32 receipts, 4 KiB body/최대 5 KiB oversized streaming 한도를 유지한다.
foreign manifest 보존은 기존 bounded body read 결과로 판단하며 전체 body 재조회가 없다.
reset 결과 복구도 full history scan 없이 bounded index로 조회한다.

## 10. 남은 known limitation

- 한 elder당 한 configured Pi 및 기존 OS ownership 범위다. 실제 Pi/Linux/audio/Firebase rules/index는
  실물 검증 전이며 운영 서버에 접근하지 않았다.
- 기존 v4 요청처럼 intent가 없는 legacy row는 timestamp 동시성을 보수적으로 합친다.
  legacy의 같은 millisecond 신규 의도는 구분할 수 없으므로 Guardian/Pi를 함께 갱신해야 한다.
- 이미 준비한 오래된 확인창은 원래 epoch에 연결된다. 완료 후 별도 reset을 하려면 새 확인 흐름으로
  준비해야 한다. production component는 준비를 강제하지만 low-level test/legacy caller의 intent 없는
  직접 `runMemoryReset` 호출은 read-at-create 호환 경로다.
- 소유권 불명 manifest가 128개를 차지하면 conversation save가 거절된다. explicit maintenance가 필요하다.
- external operation history/immutable orphan 정리 정책은 기존대로 별도다. 결과 기록을 임의 삭제하면
  오래된 intent는 새 destructive reset 대신 미확정 상태로 남는다.
- 정상 retrieval/save는 bounded streaming을 유지한다. 기존 explicit reset/maintenance의 malformed
  원본 ownership 진단은 큰 body를 읽을 수 있으며 이번 hot path 수정 범위 밖이다.
- 한국어 identity는 제한된 lexical 규칙이며 동의어/전면 형태소 분석을 추정하지 않는다.

## 11. git status

branch/HEAD는 시작 시와 같다. 모든 변경은 uncommitted이며 staged 변경, commit, push, 배포가 없다.
최종 상태는 tracked 수정 13개, untracked 36개다(작업 전부터 있던 변경 포함).
이번 추가 파일도 untracked 상태로 둔다. 최종 status 전체는
작업 완료 응답의 검사 결과와 `git status --short`로 확인할 수 있다.

## 12. reviewer가 다시 실행할 exact interleaving

아래 test들은 sleep 추측 대신 CAS hook/명시적 promise/Event로 순서를 고정한다.

| test | 고정된 순서 / 기대 |
| --- | --- |
| `test_a_intent_created_immediately_before_destructive_cas` | A claim → 최종 control PUT 직전 B intent/create → A completed → 새 memory save → B poll; generation1/새 body 보존/B→A |
| `test_a_intent_created_inside_destructive_cas_transport` | A claim → fake RTDB CAS 안의 ETag 비교 직전 B create → CAS/완료 → 새 memory → B poll; 동일 결과 |
| `test_a_intent_created_immediately_after_destructive_cas_before_completion` | generation CAS 적용 → A 외부 완료 전에 B create → A completed/새 memory → B poll; 동일 결과 |
| `test_a_intent_snapshot_before_cas_arrives_after_success` | active leader snapshot 확보 → A 완료/새 memory → 늦은 B PUT → B poll; B는 원 snapshot에 연결 |
| `test_b_simultaneous_preclaim_intents` | A/B가 initial epoch0을 읽음 → 한 poll; destructive generation1/둘 completed |
| `test_c_new_intent_after_completion_resets_even_same_millisecond` | A completed → 새 memory → 새 C intent; generation2/새 memory reset |
| `test_c_new_intent_after_success_with_coordinator_finish_unacknowledged` | A external completed → finish CAS 전송 실패 → 60초 뒤 영구 leader 완료를 반영한 새 C → recovery; C generation2 |
| `test_d_restart_after_claim_commit_and_completion_ack_loss` | claim/commit/completion/finish 적용 직후 ACK loss 각각 → B → worker 재생성; reset1회/영구 BUSY 없음 |
| `test_delayed_preclaim_intent_keeps_first_epoch_result_after_later_resets` | B epoch0 snapshot → A → 신규 reset33회/receipt eviction → B 생성; 추가 삭제 없이 B는 A/gen1 |
| `test_old_device_cannot_starve_current_device` | old_device70개 + current1개; current는 1 poll 안에 completed |
| `test_legacy_old_device_page_advances_to_current_unindexed_request` | index 없는 동일 배치; cursor 3 poll 안에 current completed |
| `test_b_duplicate_evidence_cannot_overwrite_existing_broad_preference` | 아침100/저녁200/일반300 → 아침 evidence subject=산책400; 아침 owner 갱신/일반·저녁 불변 |

```powershell
Set-Location C:\doran-doran-care-hub
$memoryPython = 'C:\Users\YOUR_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $memoryPython -B -m unittest discover -s tests -p 'test_memory_identity_v5.py' -v
& $memoryPython -B -m unittest discover -s tests -p 'test_memory_reset_coordinator_v5.py' -v
& $memoryPython -B -m unittest discover -s tests -p 'test_memory_manifest_v5.py' -v
& $memoryPython -B -m unittest discover -s tests -p 'test_memory_dispatch_barrier.py' -v
& $memoryPython -B -m unittest discover -s tests -v
node --test tests/display_browser.test.js tests/memory_game.test.js
node --test apps/guardian-dashboard/test/memoryResetCoordinator.test.js
npm --prefix apps/guardian-dashboard test
npm --prefix apps/guardian-dashboard run build
git diff --check
git status --short
```
