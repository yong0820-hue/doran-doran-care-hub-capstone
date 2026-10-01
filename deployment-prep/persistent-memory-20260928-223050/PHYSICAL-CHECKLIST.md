# Persistent Memory v1 + Reset: 제한된 Pi 실물 검증 체크리스트

작성일: 2026-09-28. 현재 최신 미커밋 소스의 UI·로그·저장 프로토콜을 읽어 작성한 **사용자 실행용 절차**다. 이 문서 작성 과정에서는 Pi 접속, 서비스 조작, Firebase 접근, 실제 OpenAI 호출을 하지 않았다. 아래 실물 항목은 모두 **NOT RUN**이다. 로컬 mock 테스트 결과를 실물 통과로 대체하지 않는다.

## 1. 먼저 확인할 조건

- 배포 절차의 코드 백업·해시·구문/import 검사와 실행 전제를 먼저 완료한다. 서비스와 수동 `main.py`를 동시에 실행하지 않는다.
- Firebase 환경, Pi `ELDER_ID`, Guardian의 elder 선택, device 선택이 **테스트 전용 scope**를 가리키는지 확인한다. 운영 설정·키·사용자 데이터 내용을 이 기록에 붙여 넣지 않는다.
- 기억은 device별이 아닌 elder별 scope다. device ID만 교체하거나 발화/이름에 `[테스트]`를 붙이는 것으로 reset을 격리할 수 없다. `events/{elder}`를 처리하는 inactivity worker도 이벤트 선정 시 device ID 일치를 요구하지 않는다.
- 테스트 환경 격리가 확인되지 않았다면 **기억 초기화와 care 모의 이벤트 실물 테스트를 보류**한다. 운영 elder에 연습 기록을 넣고 나중에 한 행만 지울 수 있다고 가정하지 않는다.
- 기존 복약·응급·미활동 기록을 초기화하지 않는다. care 실물 검증은 별도 테스트 scope에서만 수행한다.
- 현재 UI의 `기기 초기화 · 준비 중`은 비활성화된 준비 항목이다. 이 절차의 기억 초기화와 구분한다.

실시간 로그는 이 Pi에서 사용한 다음 명령부터 사용한다. `journalctl --user`나 화면을 지우는 `watch`를 우선 사용하지 않는다.

```bash
journalctl _SYSTEMD_USER_UNIT=doran-voice-hub.service -f -o cat
```

로그에는 실제 인식한 사용자 발화가 포함될 수 있다. 외부 보고에는 전체 journal 대신 필요한 session ID·operation ID·상태·시각과 비식별 연습 사실만 기록한다. `.env`, credentials, 키 내용을 읽거나 출력하지 않는다.

## 2. 추출·저장·cache 관찰의 한계

현재 runtime에는 **추출 성공, 저장 성공, cache READY를 직접 표시하는 성공 로그나 HTTP 상태 endpoint가 없다.** `[Voice Session] ... event=end`는 비동기 추출 제출 뒤의 session 종료 로그다. 추출 완료나 저장 완료를 뜻하지 않는다.

후보 저장 성공 시 `prefetch(force=True)`가 실행된다. 일반 prefetch 간격 제한은 15초, cache 최대 나이는 60초다. 오래되거나 준비되지 않은 cache에서는 memory 없이 대화가 진행될 수 있다. 단순히 15초 기다렸거나 오류 로그가 없다는 이유로 cache READY를 확정하지 않는다.

세션 A 저장에서 B 회상으로 넘어갈 때 다음 세 단계를 구분한다.

1. journal의 `session=... event=end`에서 A의 종료와 ID를 확인한다.
2. **격리된 테스트 Firebase 관리 화면에서만**, `memories/{testElder}/_control/activeIds`에 committed된 키와 대응하는 `memories/{testElder}/active/{key}` body를 확인한다. `sourceSessionId`, `content`, `subject`, `lastMentionedAt`, `status`를 연습 사실과 대조한다. `_control`에 게시되지 않은 고립 body만으로 저장 성공을 판정하지 않는다. `memory_outbox` 파일 소멸만으로도 판정하지 않는다. 빈 추출·reset 취소 등으로 작업이 끝날 수 있기 때문이다.
3. 저장 확인 뒤 B를 새로 시작해 실제 회상 응답을 검사한다. 최초 회상이 누락됐다면 “저장 확인 전”, “저장 확인 후이나 cache 준비 직접 관찰 불가”, “refresh 기회 이후 재시도”를 구별해 기록한다. 근거 없이 cache 실패나 preference 실패로 단정하지 않는다.

실제 callback의 LLM messages와 서비스 내부 cache READY는 현재 UI/journal에 노출되지 않는다. 실물 응답의 행동 검증과 기존 mock callback payload 검증을 분리한다. 이 제한을 해소하려고 runtime 로깅이나 진단 endpoint를 임의로 추가하지 않는다.

실재하는 오류 로그의 예:

```text
[PersistentMemory] Memory extraction unavailable; bounded retry.
[PersistentMemory] Memory job incomplete; retained for recovery when configured.
[PersistentMemory] Memory prefetch unavailable; conversation continues without memory.
```

오류가 있다면 명령·핵심 오류·발생 단계를 기록하고 해당 항목을 FAIL 또는 미검증으로 남긴다. Firebase 권한을 공개 read/write로 완화하지 않는다.

## 3. 세션 A 저장 → 세션 B 회상

1. Pi 화면의 **도란이와 대화하기**, 또는 테스트 Guardian의 **실제 Pi 음성 대화 시작**을 누른다. 로그의 `[Voice Session] session=... event=start`를 보고 A의 ID를 기록한다.
2. 실제 개인 정보 대신 연습 사실을 말한다. 예: `내 친구는 이정희야.`, `나는 트로트 가수 김연자를 좋아해.`, `나는 바둑을 좋아해.` 각 발화가 STT에서 정확하게 인식됐는지 확인한다. 사용자의 발화만 추출 후보가 되어야 한다.
3. `대화 종료`라고 말해 A를 마친다. `event=end reason=stop_phrase` 등 실제 종료 이유를 기록한다. 이전 절의 committed manifest/body 확인을 마친다. 관계나 profile 후보가 실제로 추출되지 않았다면 해당 type의 실물 회상은 **미검증**으로 남긴다.
4. B를 새로 시작하고 A와 다른 session ID인지 확인한다. `내 친구 이름 기억나?`, `김연자에 대한 내 취향 기억나?`, `내가 좋아하는 놀이 기억나?`처럼 정답을 다시 알려 주지 않는 질문을 한다.
5. 저장된 연습 사실을 회상하는지 확인한다. 특히 관계 사실은 일반 지식으로 답할 수 없는 사실이어야 한다. 바둑 등 독립 preference가 유지되는지 확인한다.
6. 충돌 없는 episodic 검증이 필요하면 실제 날짜를 명시한 연습 방문/약속 사실을 추가 세션에서 말하고, 해당 `episodic` body가 committed됐음을 먼저 확인한 다음 새 세션에서 회상한다. 일정 실행이나 care 판단에 이용하지 않는다.

## 4. 최신 preference 정정과 설명형 subject

각 정정은 기존 발언과 **다른 session**에서 한다. 매번 종료·저장 확인을 마친 뒤 또 다른 session에서 묻는다.

| 순서 | 새 사용자 발화 | 다음 새 세션 질문 | 기대 |
|---|---|---|---|
| C | `김연자 싫어해.` | `김연자 좋아해?`, `김연자 싫어해?`, `김연자에 대한 내 취향 기억나?` | A의 과거 긍정을 현재 사실로 되살리지 않고 최신 부정 반영 |
| D | `김연자 좋아해.` | 위와 같은 세 질문 | 최신 긍정 반영, C의 부정이 현재 사실로 대체하지 않음 |
| 각 회상 | `내가 바둑을 좋아한다고 했지?`보다 답을 포함하지 않는 `내가 좋아하는 놀이 기억나?` 사용 | 독립 취향 회상 | 바둑 유지 |

가능하면 세션별로 `나는 가수 김연자를 좋아해.`와 `나는 트로트 가수 김연자를 좋아해.`도 사용하고, 같은 방식으로 짧은 이름의 정정을 이어 간다. **음성 실물 테스트에서 extractor subject를 강제할 수는 없다.** 테스트 DB에서 실제 관측한 `트로트 가수 김연자`, `가수 김연자`, `김연자`만 기록한다. 관측되지 않은 조합을 실물 PASS로 쓰지 않는다. extractor validator를 통과하지 않는 label/evidence를 억지로 저장하지 않는다.

다른 인물·조건의 독립성도 확인한다. 예: `나훈아 싫어해.`, `매운 음식을 좋아해.`, `단 음식을 싫어해.`, `아침 산책을 좋아해.`, `저녁 산책을 싫어해.` 저장 확인 뒤 대상별 질문에서 각각의 사실이 유지되어야 한다. 요가·오이 정정도 동일한 절차로 수행할 수 있다.

회상으로 저장 레코드가 바뀌지 않는지 관찰하려면, 이전 추출 작업이 끝난 안정된 단계에서 committed body의 `id`, `content`, `evidenceKey`, `lastMentionedAt`를 전후 비교한다. 새 세션의 사용자 발화가 정상적으로 새 추출을 만들 수 있으므로, concurrent extraction 때문에 바뀐 결과를 순수 회상 변경으로 오판하지 않는다. sourceSessionId와 변경 시각으로 원인을 구분할 수 없으면 이 실물 항목은 미검증으로 남긴다.

## 5. 대화 초기화: 현재 session만 초기화

1. 연습 사실 저장을 먼저 확인한다.
2. Pi에서 **대화 초기화 → 대화 초기화 확인**을 누른다.
3. 화면의 `초기화를 요청했어요... 저장된 기억은 그대로예요.`는 요청 접수다. 실제 적용은 다음 journal로 확인한다.

```text
[VoiceAI] 대화 초기화 완료. 저장된 기억은 유지합니다.
```

4. 말하기·듣기·care 진행 중이면 안전한 경계까지 기다린다. 기존 재생/녹음을 중간에 끊거나 care를 건너뛰는 것을 기대하지 않는다.
5. 새 대화에서 session ID가 바뀌고 현재 대화 문맥이 초기화됐는지, 이전 persistent memory는 회상되는지 확인한다. 대화 초기화는 기억 초기화가 아니다.

## 6. 기억 초기화: 테스트 전용 elder에서만

격리 확인이 없으면 이 절 전체를 **보류**한다. 테스트 전용의 기존 대화 기록·care 이벤트·복약 일정·기기 자료에 대해 비밀정보 없는 식별자/개수/상태만 전후 기준으로 기록한다.

1. 테스트 Guardian의 **대화와 기기 관리**에서 **기억 초기화**를 누른다.
2. **내용 확인 · 다음**을 누르고 확인 문구 `기억 초기화`를 정확히 입력한다.
3. **다시 확인 · 기억 초기화**를 한 번 누른다. 같은 요청에 반복 클릭하거나 다른 탭에서 새 operation을 만들지 않는다.
4. `기기에 기억 초기화를 요청했습니다...` 또는 **기기 초기화 완료 대기**는 완료가 아니다. `requested`/`processing`도 완료가 아니다.
5. 필요하면 **결과 확인**을 누른다. authoritative operation `memoryResetOperations/{testElder}/{operationId}`의 `status=completed`와 다음 UI 완료 문구를 확인한다.

```text
대화에서 저장한 기억을 비활성화했습니다. 이후 새 응답 요청부터 반영됩니다.
```

6. 새 세션을 시작해 연습 관계·취향의 이전 기억을 알려 달라고 묻는다. 현재 질문에 정답을 넣지 않는다. 일반 대화는 정상적으로 계속 가능해야 한다. 초기화 이전 이미 시작한 응답은 이어질 수 있으므로, 완료 이후의 새 요청을 대상으로 판단한다.
7. 기존 care·복약 일정·기기·대화 기록이 보존되는지 테스트 scope에서 확인한다. 기억 초기화는 이런 자료를 지우는 버튼이 아니다.

Reset worker에는 완료 성공 로그가 없다. 가상의 `reset completed` 로그를 기다리지 않는다. Guardian의 SUCCESS는 operation `completed`를 읽었을 때만 나온다.

### archive와 물리 삭제

초기화는 소유한 conversation memory를 archive로 옮기고 active manifest에서 제외하며, generation을 바꾸고 로컬 사용을 무효화한다. 이후 active body cleanup을 실행한다. archive는 최대 64개·30일 정책으로 유지/정리되며, 이것은 모든 흔적의 즉시 영구 삭제를 보장하는 기능이 아니다. 화면에서 되돌리는 기능도 없다. **코드 백업 롤백은 cloud 초기화 결과를 되돌리지 않는다.** 사용자 데이터·outbox·memory 디렉터리를 롤백 수단으로 삭제하지 않는다.

## 7. Pi offline 및 같은 operation 복구

완료 여부를 더 잘 관찰하려면 테스트 scope에 별도의 연습 기억을 다시 저장한 뒤 시작한다.

1. 진행 중 대화/care가 없는 것을 확인하고, 배포 안내의 기존 서비스 중지 절차를 사용해 Pi runtime만 중지한다. Guardian은 테스트 Firebase에 연결된 상태를 유지한다. 네트워크·서비스 설정은 임의 변경하지 않는다.
2. 위 기억 초기화 확인 단계를 한 번 수행한다. 테스트 DB에서 생성된 operation ID를 기록한다. UI 자체는 operation ID를 직접 표시하지 않는다.
3. Pi가 처리하지 못한 동안 `WAITING_FOR_DEVICE`/요청 대기여야 한다. 단순 요청 저장만으로 SUCCESS가 나오면 실패다.
4. 같은 브라우저에서 닫기/새로고침 후 **결과 확인**을 수행한다. 브라우저의 reset 복구 저장소를 지우지 않는다. 재접속은 기존 operation을 확인해야 하며 새 reset을 자동 생성하면 안 된다.
5. 기존 서비스로 Pi를 다시 실행하고 같은 operation ID가 완료되는지 확인한다. 새 operation ID로 요청을 다시 보내는 방식으로 복구를 대신하지 않는다.
6. 완료 뒤 새 세션에서 reset 이전 기억이 재주입되지 않는지 확인한다.

Pi unavailable과 Guardian 연결 불확실성은 다르다. 보호자 브라우저의 read/권한 실패·ACK 손실이면 `UNKNOWN`이 맞으며 성공 또는 확정 실패라고 단정하면 안 된다. 별도로 UNKNOWN을 실험하려면 **테스트 환경에서만** 브라우저 개발자 도구의 네트워크 offline 등으로 응답 확인을 막고, 복구 후 같은 브라우저의 **결과 확인**으로 같은 operation을 추적한다. 작업의 적용 여부를 확인하지 못했을 때 새 요청을 만들지 않는다. Firebase 규칙을 고장 내거나 공개 권한으로 바꾸는 방법은 사용하지 않는다.

## 8. care, session 분리, AUDIO_LOCK, radio 회귀

모든 care 실험은 별도 테스트 scope에서 한다. Guardian의 live **장시간 미활동** 모의 버튼은 Firebase에 실제 이벤트를 만든다. `[모의]` 명칭이 Pi 처리를 막는다는 보장은 없다.

1. Pi 일반 대화를 시작하고 일반 chat session ID를 기록한다. 말하기/듣기 중 Guardian에서 **장시간 미활동**을 한 번 생성한다.
2. 현재 playback/capture가 겹침 없이 끝난 뒤 care가 턴 경계에서 실행되는지 확인한다. `AUDIO_LOCK` 자체의 보유 상태는 UI에 없으므로 실제 오디오 겹침과 단계 로그를 관찰하고, 내부 잠금의 실물 직접 관찰은 별도 미검증으로 남긴다.
3. care 질문에는 `괜찮아요`처럼 완전한 답을 한다. 실제 journal에서 다음 형식의 event ID·session ID·outcome을 확인한다.

```text
[InactivityCare] event=<test-event-id> stage=start session=<care-session-id>
[InactivityCare] event=<test-event-id> stage=end outcome=okay session=<care-session-id>
```

4. care session이 일반 chat session과 다르고, care 질문·답변이 일반 preference memory로 들어가지 않는지 확인한다. 정상 일반 대화가 이어질 때 기존 일반 문맥이 care 답변으로 대체되지 않아야 한다.
5. 새 일반 대화 중 별도의 새 **장시간 미활동** 이벤트를 만든 뒤 care 질문에 `도와주세요`라고 답한다. 테스트 이벤트의 help 결과와 일반 session의 아래 종료를 확인한다.

```text
[Voice Session] session=<chat-session-id> event=end reason=care_help ...
```

6. help 이후 일반 연속 대화의 다음 capture가 자동으로 열리지 않아야 한다. 기존 critical event 처리 결과를 테스트 scope에서 확인한다. 실제 도움이 필요한 상황을 흉내 내는 테스트를 운영 elder에 수행하지 않는다.
7. 복약은 테스트 scope의 **실제 일정 관리 기능**으로 예약한 reminder로 검사한다. Guardian의 **복약 안내** 모의 버튼이 실제 복약 음성을 시작한다고 가정하지 않는다. 예약된 care 질문에 `약 먹었어요`라고 답하고 해당 복약 event와 독립 session에만 결과가 연결되는지 확인한다. 일반 대화에서의 `네`나 과거 기억이 복약 확인으로 사용되면 안 된다.
8. Pi의 **음악 · 라디오 → 재생**으로 라디오를 재생한 다음 대화와 care를 각각 시작한다. 라디오가 멈추고 오디오가 겹치지 않아야 한다. 종료 후 라디오가 자동으로 재개되지 않아야 하며, 사용자가 **재생**을 눌러야 재개한다. 화면 연결 끊김/복구 뒤에도 자동 재개가 없어야 한다.

## 9. 비식별 증거 기록표

모든 결과는 실제 실행 뒤에만 바꾼다. `PASS`는 기대 행동을 직접 관찰한 경우, `FAIL`은 관찰된 위반, `BLOCKED`는 격리·권한·준비 미충족, `NOT RUN`은 미실행을 의미한다. cache READY, 실제 callback payload, OS 내부 lock처럼 현재 관찰 수단이 없는 항목은 실물 PASS로 바꾸지 않는다.

| 항목 | 시작 상태 | 실행 시각 | 익명 session/operation ID | 관찰 결과·제한 |
|---|---|---|---|---|
| 테스트 Firebase/elder/device 격리 확인 | NOT RUN | — | — | — |
| 세션 A 인식·종료 | NOT RUN | — | — | — |
| A committed active manifest/body 확인 | NOT RUN | — | — | — |
| cache READY 직접 관찰 | NOT RUN | — | — | 현행 UI/journal 관찰 수단 없음 |
| 세션 B 관계/profile 회상 | NOT RUN | — | — | — |
| 충돌 없는 episodic 회상 | NOT RUN | — | — | — |
| 긍정→부정 최신 정정 | NOT RUN | — | — | — |
| 부정→긍정 최신 정정 | NOT RUN | — | — | — |
| 설명형 subject→짧은 subject 실측 | NOT RUN | — | — | 실제 추출 표기만 기록 |
| 다른 인물·독립 취향 보존 | NOT RUN | — | — | — |
| 순수 회상 전후 저장 불변 | NOT RUN | — | — | concurrent extraction을 구분할 수 없으면 미검증 |
| 실물 callback payload 직접 관찰 | NOT RUN | — | — | 현행 UI/journal 관찰 수단 없음 |
| 대화 초기화 실제 적용·memory 유지 | NOT RUN | — | — | — |
| 기억 초기화 operation completed | NOT RUN | — | — | 격리 미확인 시 보류 |
| completed 뒤 새 session에서 옛 기억 미사용 | NOT RUN | — | — | — |
| care/일정/device/대화 기록 보존 | NOT RUN | — | — | — |
| Pi offline: 거짓 SUCCESS 없음 | NOT RUN | — | — | — |
| UNKNOWN 연결 복구·동일 operation 확인 | NOT RUN | — | — | — |
| Pi 재실행 뒤 동일 operation completed | NOT RUN | — | — | — |
| 턴 경계 care·독립 session/reply | NOT RUN | — | — | — |
| care_help 일반 대화 종료 | NOT RUN | — | — | — |
| 실제 예약 복약 care | NOT RUN | — | — | — |
| radio pause/no-auto-resume·오디오 겹침 없음 | NOT RUN | — | — | — |

## 10. 읽은 소스 근거

- `raspberry-pi-voice-hub/display/index.html`: 대화·대화 초기화·라디오 버튼 이름.
- `raspberry-pi-voice-hub/display/app.js`: 초기화 HTTP 202 요청 안내, radio pause와 사용자 재생.
- `raspberry-pi-voice-hub/voice_ai.py`: session 종료 후 비동기 memory 제출, 안전한 reset 적용 로그, care 전용 conversation/session, care_help 종료, 실제 dispatch.
- `raspberry-pi-voice-hub/persistent_memory.py`: cache 주기·나이, 저장 후 force prefetch, 실제 callback 경로, 오류 로그.
- `raspberry-pi-voice-hub/memory_reset.py`: operation 완료 공개, active→archive, bounded retention과 cleanup, 불확실한 작업 재시도.
- `raspberry-pi-voice-hub/firebase_inactivity.py`: elder scope 이벤트 선정과 help 결과.
- `apps/guardian-dashboard/src/components/ResetSettings.jsx`, `src/memoryReset.js`: 확인 UI, completed/SUCCESS, WAITING/UNKNOWN, 같은 operation 복구.
- `apps/guardian-dashboard/src/components/DemoControls.jsx`, `src/hooks/useDashboardData.js`: 실제 Pi 음성 시작과 live 모의 이벤트 저장.

이 문서는 배포 승인이나 실물 통과 보고가 아니다. 실패 시 검증된 runtime을 임의 수정하지 말고 실패 단계·핵심 오류·배포 영향을 보고한다.
