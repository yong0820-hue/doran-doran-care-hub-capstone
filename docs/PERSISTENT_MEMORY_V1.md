# Persistent Memory v1 + Reset

> 4차 수정 이전의 역사 문서입니다. 현재 contract와 구현은
> [PERSISTENT_MEMORY_V4_REVIEW.md](PERSISTENT_MEMORY_V4_REVIEW.md)를 따릅니다.
> 아래 TTL lease 및 Guardian 직접 reset 설명은 현재 규격이 아닙니다.

> 이 문서는 2차 수정까지의 구현·배포 검토 이력이다. 3차 수정의 현재 규격·검증·한계는
> [PERSISTENT_MEMORY_V3_REVIEW.md](PERSISTENT_MEMORY_V3_REVIEW.md)를 따른다.
> 아래 flat bucket CAS, 반복 generation GET, cold session 저장 비활성화, singleton marker 설명은
> 더 이상 현재 구현이 아니다. 새 `memory_storage.py`도 필수 파일이다. 배포/migration은 실행하지 않았다.

기준: `wip/interaction-manager-v2`, `ad0f85a44256d60eadd720f47cfd8c68987e8f43`.
이 문서의 배포 명령은 실물 검증을 위한 사용자 실행 절차다. 개발 중 실제 Pi나
운영 Firebase를 변경하지 않았으며, 실물 검증 전 commit/push하지 않는다.

## 구조와 데이터

- Current Conversation: 기존 `VoiceConversation.history`에 성공적으로 재생한 대화 쌍을 유지한다.
- Episodic: 방문·약속 등 일시적 사실과 계획. `eventDate`는 완료 여부를 뜻하지 않는다.
- Profile: 명시적으로 말한 관계·배경 정보.
- Preference: 명시적으로 말한 취향·습관. 같은 주제는 최신 발언으로 갱신한다.
- Truth source: 복약·응급·낙상·미활동·센서·기기 상태는 기존 deterministic/Firebase 경로다.
  care 객체에는 memory service를 주입하지 않고 care 발화를 추출하지 않는다.

`persistent_memory.py`의 `MemoryStore`, `MemoryExtractor`,
`PersistentMemoryService`가 저장·추출·비동기 실행을 분리한다. 음성 경로는
`MemoryReadCache`의 로컬 snapshot을 검색하며, bucket을 읽는 `MemoryStore.retrieve()`는
직접 테스트/비실시간 호출용이다.
새 dependency나 환경변수는 필요 없다. 기존 OpenAI 모델·클라이언트를 사용한다.

기존 `memories/{elderId}/{memoryId}` 예약 경로를 확장했다. 예시:

```json
{
  "id": "conversation_<hash>",
  "elderId": "elder_001",
  "type": "episodic",
  "subject": "영수",
  "content": "2026-09-28 영수랑 점심 먹어.",
  "createdAt": 1790514000000,
  "updatedAt": 1790514000000,
  "lastMentionedAt": 1790514000000,
  "importance": 4,
  "source": "conversation",
  "sourceSessionId": "pi_chat_<uuid>",
  "status": "active",
  "eventDate": "2026-09-28",
  "eventDatePrecision": "day",
  "validUntil": 1791212400000,
  "tags": ["점심"]
}
```

`_control`은 memory 레코드가 아니라 초기화 세대값을 담는 예약 키다.
`{generation, resetAt, resetBy, lastResetOperationId, resetOperations}`를 저장한다.
generation은 트랜잭션 안에서 증가하는 정수이며, 없는 경우 `"initial"`로 읽는다.
기존 문자열 세대는 첫 수정된 reset에서 정수 1로 이행한다. Pi는 관찰한 세대를
낮추지 않는다. `resetOperations`는 최근 32개 operation ID의 성공 영수증이다.
타임스탬프는 Unix ms, 날짜 해석은 Asia/Seoul이다. legacy `source=user/ai` 레코드는
보존하되 v1 검색 대상에서 제외한다. 전체 스키마는 [DB_SCHEMA.md](DB_SCHEMA.md)를 참고한다.

## 추출과 종료

사용자 발언만 timestamp와 함께 복사해 worker queue에 넣는다. assistant 응답,
TTS 안내, 종료 제어어, care 전용 발언을 넣지 않는다. 응답 API가 실패해도 이미
인식한 사용자 발언은 추출 대상에 포함한다. 전체 transcript를 memories에 복사하지 않는다.

LLM은 JSON으로 type/subject/짧은 evidence/발언 index/importance/tags를 제안한다.
코드는 사용자 발언의 연속된 실제 인용인지 검증하고 그 증거만 content로 저장한다.
모델이 붙인 자유로운 설명이나 새로운 관계는 저장하지 않는다. 일시적인 피로·기분,
care·센서 상태, 질문·지시 패턴을 보수적으로 제외한다. 이 필터는 care classifier가 아니다.

`오늘/어제/내일/내일모레/모레/다음 주/다음 주 요일/ISO 날짜`를 발언 당시 서울 날짜에 맞춰
정규화한다. `다음 주`는 일자를 지어내지 않고 주간 범위와 `eventDatePrecision=week`를
저장한다. 단어/표현 경계로만 바꾸며, 미지원 `다음 주말`은 원문 그대로 보존한다.
지원하지 않는 모호한 날짜 표현에는 정확한 eventDate를 추측해 넣지 않는다.

추출 예약 경로: 명시적 종료, 최대 턴, 연속 무응답, 오류/취소, 터미널 `n`/`q`,
다음 세션 시작, 터미널·wake 단일 턴 유휴 만료. 별도 idle poll이 있어 다음 발언을
기다리지 않고 유휴 만료를 처리한다. 진행 중인 bounded session은 기존 정책대로
idle 시간으로 끊지 않는다. 초기화는 현재 capture/playback과 pending care를 끝낸
경계에서 적용하며, 도움 요청과 겹치면 `care_help` 우선순위를 유지한다.

추출 API와 Firebase ETag 조건부 쓰기는 각각 최대 두 번 시도한다. 실제 runtime은
예약 전에 `memory_outbox/<elder URL hash>/`에 user-only 입력·timestamp·generation을
원자적으로 저장/fsync한다. 성공 또는 reset으로 무효화된 job만 제거한다.
실패/timeout job은 다음 시작 때 재처리하며, reset generation을 다시 검증한다.
프로세스/controller 정상 종료는 explicit drain + join을 수행한다. memory drain은
기본 5초, controller 전체 종료 대기 예산은 8초다. timeout은 복구 가능한 pending을
남기고 명시적으로 기록한다. 개별 voice session 종료는 추출 완료를 기다리지 않는다.
실패 로그는 예외 본문·기억 내용·키를 출력하지 않는다.
음성의 정상 종료/명령 결과를 추출 실패 때문에 실패로 바꾸지 않는다.

## 검색과 중복

발언마다 active·미만료 기억 중 관련도 상위 4개, 최근 3개, 중요도 4 이상 상위 2개를
합치고 중복을 제거해 최대 8개를 전달한다. 관련도는 subject/tag/문자열과 오늘 날짜를
이용한다. 직렬화 JSON은 이스케이프 후 최대 4,200자다.

- 시작/세션 경계/검색 시 background prefetch를 예약한다. 일반 refresh 간격은 최소
  15초이며, 저장 성공 후에는 강제 refresh한다. bucket GET은 음성 thread에서 하지 않는다.
- 로컬 cache는 active 최대 128개·UTF-8 JSON 128 KiB, 유효 나이 60초다.
  cold/만료/무효 cache는 기다리지 않고 기억 없이 진행한다. cold session은 그 세션의
  memory 쓰기도 비활성화하므로 시작 직후 발언이 반드시 기억된다고 보장하지 않는다.
- bucket snapshot 도착 후 `_control/generation`만 다시 읽어 reset을 검증한다.
  매 nonempty context 사용 직전에도 같은 작은 endpoint를 읽는다. full bucket 재검증은 없다.
- generation HTTP connect/read timeout은 각각 80ms, 음성 thread의 총 대기 한도는
  150ms다. 이미 정체 중인 검사가 있으면 다음 요청은 즉시 no-memory다. 검사 thread는
  최대 1개이며, timeout/오류/세대 불일치 시 그 결과를 사용하지 않는다.
- background bucket read/write는 connect 250ms/read 500ms timeout을 사용한다.
  네트워크 전체 소요시간은 transport 특성상 더 길 수 있으나 음성 thread를 기다리게 하지 않는다.
- 더 높은 세대를 관찰하면 낮은 응답/진행 중 snapshot/context 직렬화 결과를 거절한다.
  마지막 authoritative generation 검사가 reset과의 순서 기준이다. 이미 AI에 전달한
  request와 기존 단기 history를 원격 reset이 소급 취소하는 프로토콜은 아니다.

현재 서울 날짜와 기억 JSON을 별도 `role=user`의 `<user_memories>`에 넣는다.
persona/system instruction에 저장된 content를 붙이지 않는다. JSON의 `<`/`>`를
이스케이프하고 신뢰하지 말아야 할 참고 데이터라고 고정 prompt에 명시한다.
관련 있을 때만 사용한다. 이는 입력 경계 보호이며 LLM의 지시 준수를 완벽히 증명하는
것은 아니다. [OpenAI 공식 안전 문서](https://developers.openai.com/api/docs/guides/agent-builder-safety)의
비신뢰 데이터를 developer 메시지에 넣지 않는 원칙을 따른다.

같은 type/정규화 content/eventDate 또는 검증된 evidence digest의 사실은 subject가 달라도
중복으로 처리한다. preference의 `topicKey`는 코드가 증거 문장의 알려진 동의어·간단한
목적어 패턴에서 산출한다. 모델이 제안한 key를 신뢰하지 않는다. 같은 topic의 활성 행을 갱신하여 표현을
바꿔 반복해도 새 행이 계속 생기지 않는다. 더 오래된 발언의 늦은 추출은 최신 취향을
되돌리지 않는다. 일부 주제 동의어만 지원하며 profile의 완전한 의미 중복·모순 해결은
v1 범위 밖이다. 서로 다른 날짜의 약속은 별도 기록이다.

## Reset 세 계층

| 표시명 | 진입점 | 동작 | 유지하는 데이터 |
|---|---|---|---|
| 대화 초기화 | Pi 홈 버튼 → 확인, `POST /api/conversation-reset`, controller hook | 안전한 경계에서 history/현재 session 초기화, 다음 대화는 새 ID | persistent memories, care/일정/설정/대시보드 기록 |
| 기억 초기화 | 보호자 관리 카드 → 내용 확인 → `기억 초기화` 입력 → 최종 버튼 | 대화 유래 기억 archive + generation 증가 | care, 일정, 기기, 계정, 원래 대화 이력, legacy memories |
| 기기 초기화 | 대시보드 준비 중 표시, `device_reset.py` | 실행 기능 없음. local cache/context/preferences 범위와 확인 요구만 반환 | 모든 실제 local/cloud 데이터 |

기억 초기화는 `memories/{elderId}` 트랜잭션만 사용한다. 같은 노드를 사용하는 Pi
ETag CAS와 세대 검사가 있어 초기화 전에 시작한 API/queue 작업이 기억을 되살리지
않는다. 기억이 비어 있어도 세대를 변경한다. 초기화 당시 진행 중인 세션은 추출/검색을
중단하며 다음 새 세션에서 새 세대로 시작한다. 기존 active history는 남을 수 있으므로
즉시 단기 맥락도 비우려면 Pi의 대화 초기화를 함께 사용한다. 새 세션에서 다시 말한
사실은 새 기억으로 저장할 수 있다. archive 복원 UI는 v1에 없다.

첫 확인 후 최종 버튼은 입력 전 disabled이며 Enter/Space repeat를 차단한다.
resetOperationId는 한 시도 동안 유지된다. ACK가 유실되면 `_control`만 읽어 operation
영수증을 확인하고 SUCCESS로 확정한다. 확실한 거절은 FAILED, 확정할 수 없는 경우는
UNKNOWN(결과 확인 필요)다. UNKNOWN 화면은 쓰기를 재시도하지 않는 `결과 확인`만 제공한다.
최근 32회 안의 같은 ID 재요청은 generation을 다시 증가시키지 않는다.
미확정 operation ID는 Firebase backend/elder별 localStorage marker로 쓰기 요청 전에
저장·read-back 검증한다. 새로고침/remount 후에도 같은 ID의 UNKNOWN 상태를 복원하고
결과 확인만 허용한다. 확인된 결과에서만 marker를 해제한다. 저장소가 손상되었거나 읽히지
않으면 기존 미확정 요청이 있을 가능성을 보존하며, 새 marker 저장에 실패하면 요청을 시작하지 않는다.
marker에는 대화/기억 내용이 포함되지 않는다.

## 보관 상한

Pi의 memory 저장 CAS와 background prefetch에서 v1 conversation-owned 레코드만 정리한다.
만료 episodic은 삭제하고, archive는 30일 또는 최신 64개까지만 남긴다.
active는 최신 발언 기준 최대 128개다. reset 직후 정리가 실행되기 전에는 archive가
일시적으로 늘 수 있다. legacy/외부 source/다른 elder/미인식 행과 `_control`은 보존한다.
이 정책은 전체 DB 데이터 보존 규칙을 대신하지 않으며, 보존한 foreign 행 크기는 제한하지 않는다.

Pi reset API는 기존 loopback/Host/Origin/`X-Doran-Action`/JSON 검증을 재사용한다.
`X-Doran-Action: conversation-reset`, body `{}`만 받고 202는 예약 접수라는 뜻이다.
기존 cloud `start_ai_conversation` 명령 프로토콜은 변경하지 않는다.
기기 hook은 짧은 누름을 거절하고 8초 초과(권장 9~10초) 누름 후 display/guardian
확인을 요구한다. 모두 확인해도 `unsupported`이며 GPIO·파일 삭제·cloud 요청을 하지 않는다.

보호자 확인 UI는 구현했지만 기존 대시보드에는 인증이 없다. 운영 환경에서 보호자
권한을 보장하려면 별도 인증/RTDB rules가 필요하다. 이번 작업은 접근 규칙이나 자격
증명을 변경하지 않는다. Pi에는 해당 elder memory 노드의 read/write, 대시보드에는
그 노드의 transaction 권한이 있어야 한다. 권한 오류를 해결하려고 rules를 열지 않는다.

## v1 제한

- 실행 queue는 최대 8개, disk outbox는 최대 128개이며 미완료 job을 복구한다.
  한도/디스크 오류로 새 예약이 거절되면 명시적으로 로그를 남긴다. 이미 수락한 job은 보존한다.
  voice는 최근 사용자 발언 40개를 보관하고, 추출은 최근 24개·발언당 1,500자·총 8,000자를
  최신 발언부터 예산에 넣은 뒤 시간순으로 정렬한다. 세션당 최대 8개 facts, evidence는
  최대 280자, 응답 token 한도는 1,600이다. 예약 전의 강제 종료/디스크 오류는 복구를 보장하지 않는다.
- 세션 종료와 추출 완료는 비동기다. 바로 다음 세션은 직전 기억 저장보다 먼저 시작할 수 있다.
  기록 생성 후 확인하거나 잠시 후 다시 질문한다. 백그라운드 추출용 API 요청 비용이 추가된다.
- 기존 conversation messages/outbox는 남는다. memory outbox는 별도 개인정보 파일이므로
  Git에서 제외하며 OS 계정/디스크 접근 정책의 보호를 받는다. outbox_dir 없는 테스트/직접 사용은
  timeout 후 disk 복구를 보장하지 않으며 로그/반환값으로 명시한다.
- background read/CAS는 elder memory bucket 전체를 다룬다. 외부/legacy 데이터가 매우 많으면
  초기 다운로드 비용이 남는다. 페이지화/색인/embeddings는 후속 작업이다.
- reset 영수증은 최근 32회로 제한된다. 이 범위를 벗어난 operation의 자동 재시도는 하지 않는다.
  다른 브라우저/기기와 UNKNOWN 상태를 공유하지는 않는다. 브라우저 저장소를 사용자가 삭제하면
  그 브라우저의 미확정 작업 ID를 복원할 수 없다.
- 날짜·subject 추출은 보수적이며 긴 문장, 대명사, 한국어 표현 변화, 복잡한 모순을 놓칠 수 있다.
  API JSON 출력 품질과 한국어 회상 답변은 실제 모델/계정/Pi에서 확인해야 한다.
- dated episodic은 해당 날짜/주간 종료 +8일 서울 자정, undated episodic은 발언 +30일 이후
  검색에서 제외한다. 이 만료는 일정을 실행하거나 완료 처리하는 기능이 아니다.
- 하드웨어·운영 API/Firebase 권한·guardian 인증·systemd 재시작은 로컬 mock 테스트로 검증되지 않는다.

## Pi 복사 파일과 정확한 배포 절차

이번 기준점 위에서 필요한 runtime 파일은 아래 7개다. 기존 care/InteractionManager,
환경파일, credentials, systemd unit은 그대로 둔다. 새 Python 패키지는 필요 없다.

```text
raspberry-pi-voice-hub/voice_ai.py
raspberry-pi-voice-hub/persistent_memory.py
raspberry-pi-voice-hub/device_reset.py
raspberry-pi-voice-hub/display_server.py
raspberry-pi-voice-hub/display/index.html
raspberry-pi-voice-hub/display/app.js
raspberry-pi-voice-hub/display/styles.css
```

Pi 터미널에서 기존 서비스를 중지한다. 수동 main.py도 실행 중이면 해당 터미널에서
Ctrl+C로 종료해 두 runtime이 동시에 오디오를 소유하지 않게 한다.

```bash
systemctl --user stop doran-voice-hub.service
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
```

Windows PowerShell에서 명시한 파일만 복사한다. 아래 호스트/경로는 기존 저장소 배포
문서의 `pi-user@PI_HOST`을 사용한다.

```powershell
Set-Location C:\doran-doran-care-hub
scp .\raspberry-pi-voice-hub\voice_ai.py .\raspberry-pi-voice-hub\persistent_memory.py .\raspberry-pi-voice-hub\device_reset.py .\raspberry-pi-voice-hub\display_server.py pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/
scp .\raspberry-pi-voice-hub\display\index.html .\raspberry-pi-voice-hub\display\app.js .\raspberry-pi-voice-hub\display\styles.css pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/display/
```

Pi에서 기존 `.env`를 바꾸지 않고 검증용 프로세스 환경을 지정해 수동 실행한다.

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
REMINDER_MODE=hub AI_CONVERSATION_ENABLED=true AI_CONVERSATION_TRIGGER=firebase AI_WAKE_WORD_ENABLED=false DISPLAY_ENABLED=true AI_SESSION_MAX_TURNS=5 AI_SESSION_MAX_NO_SPEECH=2 .venv/bin/python -B main.py
```

Pi 화면의 기존 `http://127.0.0.1:8765/`를 새로고침한다. DISPLAY_PORT를 이미 별도로
설정했다면 기존 포트를 유지한다. 수동 검증 완료 후 Ctrl+C로 종료하고, 기존 서비스가
동일 runtime 경로를 사용하는 환경에서 다시 시작한다. unit 자체는 수정하지 않는다.

```bash
systemctl --user start doran-voice-hub.service
journalctl --user -u doran-voice-hub.service -n 100 --no-pager
```

## 보호자 대시보드 실행/반영

```powershell
Set-Location C:\doran-doran-care-hub\apps\guardian-dashboard
npm.cmd test
npm.cmd run build
npm.cmd run dev -- --host 127.0.0.1
```

기존 설정이 있는 대시보드는 Firebase 연결 후 관리 카드의 `기억 초기화`가 활성화된다.
Mock/오프라인에서는 비활성화된다. 운영 정적 호스팅 반영은 생성된 `dist/`를 기존 배포
방식으로 배포해야 하며, `npm run build`만으로 원격 사이트가 바뀌지는 않는다.
기존 `.env` 수정이나 새 인증정보는 이 기능의 빌드 요구사항이 아니다.

## 실물 테스트 시나리오

1. Session A: “내일 영수랑 점심 먹어. 영수는 군대 때부터 친구야.” → 명시 종료.
   Firebase에서 episodic/profile, 서울 기준 eventDate, sourceSessionId를 확인한다.
   추출 완료 후 Session B: “내일 누구 만나기로 했지?”, 다음 날에는 “오늘 누구 만나기로 했지?”를
   물어 영수와 점심·친구 관계를 회상하는지 확인한다. 계획을 완료로 단정하면 안 된다.
2. “나는 매운 음식 별로 안 좋아해.”를 서로 다른 세션에서 반복해 active preference ID와
   개수가 유지되는지 확인한다. “요즘 매운 음식 좋아해.” 이후 최신 취향으로 갱신돼야 한다.
3. “오늘 좀 피곤하네.”만 말한 세션은 long-term profile을 만들지 않아야 한다.
4. 사실을 말한 뒤 기본 2회 무응답, 5턴 종료, 명시 종료를 각각 실행해 추출이 예약되는지 본다.
   터미널/wake 단일 턴의 유휴 만료도 확인한다. 추출 실패여도 대화 종료는 정상이어야 한다.
5. 말하기/듣기 중 Pi `대화 초기화`를 확인한다. 오디오를 끊지 않고 안전 경계에서 종료하고,
   다음 대화는 빈 history/새 ID여야 한다. 저장된 기억·care/일정은 남아야 한다.
6. 보호자 관리 카드의 확인 첫 단계/문구 입력 단계에서 각각 취소해 쓰기가 없는지 확인한다.
   최종 확인 후 conversation memories만 archived, `_control.generation` 변경, care/일정/
   기기/계정/원래 대화 기록 보존을 확인한다. Pi 새 세션에서 옛 기억을 검색하지 않아야 한다.
7. 추출이 진행 중일 때 기억 초기화를 실행하고 늦게 완료된 추출이 active 기억을 재생성하지
   않는지 확인한다. 이후 새 세션에서 새 사실을 말하면 다시 저장할 수 있어야 한다.
8. 격리된 테스트 환경의 네트워크 실패에서 추출·write·retrieve 각각의 실패를 확인한다.
   운영 credentials/rules를 변경하지 않는다. 기록은 오류 종류만 남고 음성 대화는 계속돼야 한다.
9. 테스트용 기억에 지시 문구가 있어도 persona를 바꾸거나 care 결과를 판단하지 않는지 확인한다.
10. 기존 medication/inactivity turn-boundary, 동시 pending 직렬 처리, stop phrase pending drain,
    help 시 안전 경로와 `care_help`, care별 독립 capture/outcome, 중복 녹음 없음, TTS 후 녹음을 재검증한다.
11. radio 재생 중 AI/care 시작 시 pause, 종료/초기화 후 자동 재생 없음, AUDIO_LOCK 충돌 없음,
    자기 TTS를 사용자 발언으로 저장하지 않는지 확인한다.
12. 기기 초기화가 준비 중이며 실제 삭제가 없는지 확인한다. 800×480/1024×600에서 reset 확인
    문구와 버튼을 읽고 누를 수 있는지 확인한다. GPIO 연결은 이번 검증 범위가 아니다.

이 검증을 사용자가 완료하기 전에는 commit/push하지 않는다.

## 로컬 자동 검증 결과 (리뷰 수정 후 2026-09-28)

| 검증 | 결과 |
|---|---|
| Python 전체 `python -B -m unittest discover -s tests -v` | 542개 실행, 540개 통과, 2개 skip |
| Pi browser/game `node --test tests/display_browser.test.js tests/memory_game.test.js` | 65/65 통과 |
| Guardian `npm test` | 86/86 통과 |
| Guardian `npm run build` | 통과, 61 modules |
| `git diff --check` | 통과 |
| UI 회귀 | rendered component harness의 연속 클릭/키 반복/문구 입력/UNKNOWN 확인; 실제 브라우저 실물 검증 미실행 |

Python skip은 데스크탑 runtime에 `openai`/`webrtcvad-wheels`가 없어 실제 SDK 직렬화와
native VAD smoke test를 실행하지 못한 두 건이다. 기존 dependencies나 requirements는
변경하지 않았다. 나머지 테스트는 외부 API/Firebase/오디오를 대역으로 사용한다.
UI 자동 테스트는 Firebase가 없는 rendered component fixture/JS harness로 수행했다.
운영 memory reset이나 실제 Pi 배포를 실행한 결과가 아니다.

이번 리뷰 수정은 cache/race 13개, extraction 11개, shutdown 10개를 추가했다.
기존 memory core 32개와 voice 통합 18개는 background prefetch 대기/작은 generation endpoint를
mock에서 반영했다. Guardian 회귀 15개를 추가했다. 수정 전에는 cache/race 6개 중
2개 실패·4개 미구현 오류, extraction 11개 중 9개 실패, shutdown 최초 7개 실패,
Guardian 최초 5개 실패를 확인하고 수정 후 통과시켰다. 추가 검토의 context 직렬화 중
generation 변경, cache key를 포함한 크기 한도, UNKNOWN reload/storage 3개 재현도
수정 후 통과했다. 추가 경계 테스트도 모두 통과했다.
기존 InteractionManager,
care classifiers/resolution, radio focus, command protocol, transcript outbox,
장시간 미활동 demo 파일이 기준점 대비 변경되지 않았음을 확인했다.

## 실제 변경 파일 (기존 미커밋 구현 포함 25개)

| 구분 | 파일 (저장소 루트 기준) |
|---|---|
| Pi runtime | `raspberry-pi-voice-hub/voice_ai.py`, `persistent_memory.py`(신규), `device_reset.py`(신규), `display_server.py` |
| Pi 화면 | `raspberry-pi-voice-hub/display/index.html`, `app.js`, `styles.css` |
| Guardian | `apps/guardian-dashboard/src/App.jsx`, `index.css`, `services/firebase.js`, `memoryReset.js`(신규), `components/ResetSettings.jsx`(신규) |
| Guardian 테스트 | `apps/guardian-dashboard/test/memoryReset.test.js`(신규) |
| Python/화면 테스트 | `tests/test_persistent_memory.py`(신규), `test_memory_voice_integration.py`(신규), `test_device_reset.py`(신규), `test_memory_cache.py`(신규), `test_memory_extraction_regressions.py`(신규), `test_memory_shutdown.py`(신규), `test_display_server.py`, `display_browser.test.js` |
| 문서 | `docs/DB_SCHEMA.md`, `docs/PERSISTENT_MEMORY_V1.md`(신규), `raspberry-pi-voice-hub/README.md` |
| 로컬 산출물 제외 | `.gitignore` |

이번 결함 수정에서 직접 편집/추가한 파일은 아래 16개다. 위 표의 나머지 9개는
시작할 때 존재한 미커밋 구현을 그대로 보존했다.

| 구분 | 이번 수정 파일 |
|---|---|
| Runtime | `raspberry-pi-voice-hub/persistent_memory.py`, `raspberry-pi-voice-hub/voice_ai.py` |
| Guardian | `apps/guardian-dashboard/src/App.jsx`, `apps/guardian-dashboard/src/index.css`, `apps/guardian-dashboard/src/services/firebase.js`, `apps/guardian-dashboard/src/memoryReset.js`, `apps/guardian-dashboard/src/components/ResetSettings.jsx` |
| Tests | `tests/test_persistent_memory.py`, `tests/test_memory_voice_integration.py`, `tests/test_memory_cache.py`, `tests/test_memory_extraction_regressions.py`, `tests/test_memory_shutdown.py`, `apps/guardian-dashboard/test/memoryReset.test.js` |
| Docs/ignore | `docs/DB_SCHEMA.md`, `docs/PERSISTENT_MEMORY_V1.md`, `.gitignore` |

## reviewer 재현 순서

외부 서비스 대신 해당 테스트의 Event barrier/FakeFirebase/SDK stub을 사용한다.

1. H1: generation=initial의 bucket GET을 Event에서 정지 → reset commit/archive →
   옛 GET 반환 → retrieve/context가 `[]`. warm cache reset 및 context JSON 직렬화 중
   더 높은 generation을 관찰하는 경우도 결과가 `[]`여야 한다.
2. M1: pending extraction을 enqueue하고 worker를 정지 → 정상 q/stop → worker 해제 시
   저장 완료 후 종료. timeout이면 job 파일 유지 → 새 service 시작 → 한 번 저장 후 제거.
   controller의 voice API가 멈춘 상태에서도 recognized facts를 checkpoint해야 한다.
3. M2: 같은 “나는 매운 음식 별로 안 좋아해.”에서 subject=“매운 음식”/“음식” 두 번 추출 →
   active 1개. 최신 “매운 음식 좋아해” 저장 후 더 오래된 “매운 음식 싫어해”/subject=“음식”
   저장 → 최신 취향 1개 유지. LLM 임의 topicKey는 반영하지 않는다.
4. M3: 긴 옛 발언 여러 개로 8,000자 초과 → 마지막 최신 취향 정정 → extractor JSON에서
   정정이 포함되고 선택된 발언이 시간순인지 확인한다.
5. M4: 내용 확인 버튼 빠른 두 번 클릭 및 Enter/Space 반복 → reset 호출 0회.
   정확한 “기억 초기화” 입력 후 최종 버튼 한 번 → 1회. pending 중 반복은 추가 호출 0회.
6. M5: transaction이 commit 후 reject → `_control` read에서 operation 영수증 → SUCCESS.
   transaction과 read 모두 실패하거나 영수증을 확인할 수 없음 → UNKNOWN.
   UNKNOWN의 결과 확인/닫기/다시 열기/새로고침은 write 0회. ACK 유실 후 새 기억을 넣고
   새 flow를 만들어도 기존 ID로 결과만 확인하며 새 기억은 active여야 한다.
   같은 operation 재시도는 generation 그대로다. 저장소 손상/접근 거부/쓰기 실패는 새 reset 0회.
7. M6: cache prefetch 완료 후 6턴 → bucket GET 0회, generation GET만 6회.
   transport를 멈춰도 첫 context는 150ms 예산 후 `[]`, 다음은 즉시 `[]`.
   300 active/100 archive/만료/31일 archive 입력 → 128 active/64 archive 이하,
   만료/오래된 archive 제거, foreign와 control 보존. cache는 128 KiB/60초 한도.
8. L1: “내일모레 영수를 만나요” → +2일 날짜로만 대체. “다음 주말 영수를 만나요” → 원문 유지.
9. 기존 Interaction Manager/care 회귀: TTS/capture 중 preemption 없음, safe boundary에서
   pending care 직렬 실행, same AI session 복귀, care_help 종료, duplicate capture 없음,
   AUDIO_LOCK 유지, radio pause 및 no auto resume를 전체 suite에서 재실행한다.
