> 제출본 안내(2026-10-01): 아래는 개발 중 실행 참고 문서입니다. 과거 실물 점검 명령과 시나리오는 완료 증명이 아닙니다. 실제 계정·사용자 기록은 제외했으며 Pi/클라우드/음성 장치에는 이번 정리에서 접속하지 않았습니다. 이번 검사 결과와 최신 기능 범위는 루트 README_SUBMISSION.md를 보세요. 문서의 계정·호스트 경로는 일반 예시로 바꾸었습니다.

# Raspberry Pi Voice Hub

도란도란 케어 허브의 복약 안내, 미활동 음성 확인, 선택형 AI 손주 대화 모듈입니다. 기존 안내는 gTTS와 `mpg123`를 사용합니다. AI 대화는 터미널 입력 또는 Firebase 명령 → “말씀해주세요.” 안내 → Jabra 녹음 → OpenAI STT → Responses 멀티턴 대화 → OpenAI TTS → `mpg123` 순서입니다.

이번 복구 범위는 Raspberry Pi 런타임과 그 테스트/설정/문서입니다. 웨이크워드는 기본 비활성화(`AI_WAKE_WORD_ENABLED=false`) 상태로 유지합니다. 복구한 `wake_word.py`는 제한 grammar가 없는 기존 Vosk 구현이며, 제한 grammar 개선과 보호자 대시보드 변경은 다음 단계입니다. 웨이크워드를 끈 이번 실행에는 `requirements-wakeword.txt` 설치나 모델 다운로드가 필요하지 않습니다.

Persistent Memory 초기화는 Guardian의 요청을 Pi가 처리합니다. Pi offline이면 완료가 표시되지
않으며, 재시작 후 pending operation을 이어서 처리합니다. 동일 backend/elder의 중복 음성
프로세스는 OS 잠금으로 기억 사용과 reset worker 실행을 제한합니다. 필요한 Python 모듈은
`persistent_memory.py`, `memory_storage.py`, `memory_dispatch.py`, `memory_reset.py`,
`memory_runtime.py`입니다. 단일 장치/동일 OS 계정 v1이며, operation status index와 권한 요구는
[현재 schema](../docs/DB_SCHEMA.md)를 따릅니다. 이번 작업에서는 배포하지 않았습니다.

## Pi 설치와 설정

```bash
cd ~/doran-doran-care-hub/raspberry-pi-voice-hub
python3 -m venv .venv
source .venv/bin/activate
sudo apt install -y alsa-utils mpg123
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

기존 가상환경이 있다면 해당 환경을 활성화합니다. **기존 `.env`와 API 키를 유지하고 없는 설정만 추가**하세요. `.env.example`로 덮어쓰지 않습니다. `FIREBASE_DATABASE_URL`, `ELDER_ID`, `DEVICE_ID`, `LOCATION_NAME`은 현재 장치 값을 사용하며 AI에는 유효한 `OPENAI_API_KEY`가 필요합니다.

Jabra를 연결하고 `arecord -l`로 입력 장치를 확인합니다. `AI_AUDIO_DEVICE`는 녹음, `AI_PLAYBACK_DEVICE`는 `mpg123` ALSA 출력에 사용합니다. 둘은 독립 설정이며 기본 출력은 Jabra USB(`plughw:CARD=USB,DEV=0`)입니다. 다른 마이크와 스피커를 쓰면 각각의 장치명을 지정합니다.

## 실행 모드

아래 명령은 현재 실행에만 설정을 적용하며 `.env`를 변경하지 않습니다.

| 명령 | 동작 |
|---|---|
| `python main.py` | `.env`의 `REMINDER_MODE` 사용. 미설정 시 `demo` |
| `REMINDER_MODE=demo python main.py` | 30초 후 복약 안내 1회 |
| `REMINDER_MODE=schedule python main.py` | 복약 일정 안내, 날짜/시간별 1회 |
| `REMINDER_MODE=watch python main.py` | 새 `long_inactivity` 이벤트 감시와 안부 안내 |
| `REMINDER_MODE=hub python main.py` | 복약 일정 + 미활동 이벤트 감시 |
| `REMINDER_MODE=hub AI_CONVERSATION_ENABLED=true AI_WAKE_WORD_ENABLED=false python main.py` | 기존 허브와 AI 대화 함께 실행. 트리거 기본값은 `auto` |
| `REMINDER_MODE=hub AI_CONVERSATION_ENABLED=true AI_CONVERSATION_TRIGGER=firebase AI_WAKE_WORD_ENABLED=false python main.py` | 터미널 없이도 Firebase 명령으로 AI 대화 시작 |
| `REMINDER_MODE=chat python main.py` | 터미널 AI 대화만 실행. 활성화 플래그/트리거 설정과 무관하게 터미널 방식으로 시작 시도 |

`MEDICATION_TIMES` 기본값은 `08:00,12:00,19:00`이며 Pi의 로컬 시간을 사용합니다. 기존 `watch`/AI 비활성 `hub` 안내는 시작 시점의 10초 전부터 생성된 열린 `long_inactivity` 이벤트를 대상으로 합니다. 이미 안부 확인 이벤트 또는 `meta.voiceCheck`가 있는 원본은 건너뜁니다. AI가 정상 시작한 `hub`는 아래의 영구 중복 방지와 자동 듣기 흐름을 사용합니다. `hub`의 복약 일정 검사는 각 작업 완료 후 5초 쉬고 반복합니다.

AI는 기본적으로 꺼져 있습니다(`AI_CONVERSATION_ENABLED=false`). `demo`, `schedule`, `watch`는 이 플래그와 관계없이 기존 기능만 실행하며 OpenAI 키가 필요하지 않습니다. 기존 안내 실패 시에도 `medication_reminder_sent` 또는 `inactivity_voice_check_sent` 기록을 시도합니다.

## 복약 음성 확인: 센서 없이 한 번 묻기

`hub`에서 기존 AI가 정상 시작하고 `MEDICATION_VOICE_CONFIRMATION_ENABLED=true`일 때만 복약 안내 후 음성을 한 번 듣습니다. 기본값은 `false`이며 `demo`, `schedule`, `watch` 및 기존 안내 전용 실행은 그대로입니다. AI/복약 확인 초기화에 실패하면 로그를 남기고 기존 안내 전용 흐름을 유지합니다. 새 패키지는 필요하지 않습니다.

**약통 센서는 사용하지 않습니다.** 복용 확인은 어르신의 명확한 음성 응답에 따른 자기 보고이며 실제 복용을 측정하지 않습니다. 약 이름·용량·복용법을 추론하거나 의료 조언을 추가하지 않습니다.

1. 기존 `MEDICATION_TIMES` 검사에서 예약된 날짜/시각을 별도 복약 작업자의 대기열에 전달합니다. 스케줄 검사는 음성·네트워크 처리를 기다리지 않으므로 대화 중에도 다음 예약 건을 받습니다. `hub`는 각 작업 후 5초 대기하며, `schedule`의 기존 60초/10초 대기도 바꾸지 않습니다. 과거 시각을 소급 실행하는 새 스케줄러는 없습니다.
2. 기존 `events/{ELDER_ID}` 아래 **같은 `medication_reminder_sent` 이벤트 형식**으로 예약 건을 저장합니다. 장치·날짜·시각별 고정 ID와 ETag 조건부 생성으로 음성 시도 기록을 먼저 확보합니다.
3. 진행 중인 대화/미활동 확인이 끝나면 복약 카드와 “어르신, 약 드실 시간이에요. 드셨으면 먹었어요라고 말씀해 주세요.” 안내를 제공합니다. 기존 `AUDIO_LOCK`을 안내 재생부터 녹음 끝까지 연속 유지합니다.
4. 기존 VAD·말끝 감지·STT로 **한 번만** 듣습니다. 최대 발화 20초, `marin`/1.15와 현재 녹음 설정을 그대로 사용합니다. 재질문·추가 확인 음성·자동 재안내 반복은 없습니다.
5. 규칙으로 판정해 같은 이벤트의 `meta.voiceCheck`에 결과를 기록합니다. 이 판정에 LLM을 호출하지 않습니다.

| 결과 | 응답 예시 | 이벤트 변경 |
|---|---|---|
| `confirmed` | `먹었어`, `먹었어요`, `약 먹었습니다`, `복용했어요` | `status=resolved`, `resolvedAt`; `meta.voiceCheck.outcome=confirmed` |
| `defer` | `조금 있다 먹을게`, `아직 안 먹었어` | `status=open` 유지; 시도/결과 기록 |
| `no_speech` | 침묵, VAD 검증 탈락, 빈 STT | `status=open` 유지; 시도/결과 기록 |
| `unclear` | `네`만 말함, `먹었어요?`, `먹었는지 모르겠어요`, 인용·타인의 복용·상충하는 답변 | `status=open` 유지; 시도/결과 기록 |
| `cancelled` / `error` | 기다리는 중 보호자가 해결 / 안내·녹음·STT 실패 | 기존 상태 보존; 취소/오류 기록. 예외 내용 대신 종류만 저장 |

`meta.voiceCheck`에는 `status=processing → completed`, `attemptedAt`, `attemptedBy`, `claimToken`, `completedAt`, `outcome`을 사용합니다. `meta.scheduledFor`는 Pi 로컬 시각의 `YYYY-MM-DD_HH:MM`입니다. 보호자가 이미 해결한 이벤트는 다시 열지 않습니다. 외부에서 `severity=critical`로 올린 이벤트도 `confirmed` 결과만으로 해결하지 않습니다. 원본 이벤트에 음성 원문을 추가하지 않고, 인식된 응답은 기존 대화 저장소/outbox에 별도 세션으로 기록합니다. 일상 대화의 기억은 유지됩니다. `devices/{DEVICE_ID}/lastSeen`, `lastSignal` 갱신도 시도하며 센서 상태는 쓰지 않습니다.

화면의 기존 복약 카드는 음성 확인 중 “안내가 끝나면 드셨는지 말씀해 주세요.”를 표시하고, 안내·듣기·처리 중 유지되다가 작업 종료 시 사라집니다. 보류 이벤트는 Firebase에서 열린 상태로 남습니다. 카드가 사라지는 것을 복용 완료 표시로 사용하지 않습니다. 별도 결과 유지 화면이나 애니메이션은 추가하지 않았습니다.

### 중복 방지와 장애 시 절충

- 예약 대기열은 메모리에 있습니다. 작업자가 Firebase에 시도 기록을 저장하기 전에 종료하면 대기 중 예약 건이 사라질 수 있습니다. 작업자는 순서대로 각 예약 건을 한 번 처리하고, 대기 중에도 허브의 스케줄 검사·Firebase 명령·미활동 작업자는 계속 동작합니다. 결과 저장 재시도는 복약 작업자가 한 번에 한 건씩 하며 스케줄 검사를 막지 않습니다.
- 같은 장치·날짜·시각의 기록이 이미 있으면, `open`이나 `processing`이어도 재시작 후 다시 말하거나 듣지 않습니다. 다음 날짜/예약 시각은 별도 건입니다. 이 보장은 음성 확인 옵션을 유지한 실행에 적용되며 기존 안내 전용 모드에는 적용되지 않습니다.
- Firebase 조건부 생성/소유 확인에 실패하면 해당 실행에서는 음성을 생략합니다. 저장 전 실패해 서버에 기록이 없다면 새 프로세스가 같은 예약 분에 다시 시도할 수 있지만, 첫 프로세스는 음성을 시작하지 않습니다.
- 시도 기록 직후 전원이 꺼지면 실제 안내가 생략될 수 있습니다. 음성 결과 저장 실패 시 허브가 실행 중인 동안 **결과 쓰기만** 재시도합니다. 결과 저장 전에 재시작하면 `processing`이 남을 수 있습니다. 같은 복약 안내를 자동 반복하지 않기 위한 선택입니다.
- 기록된 `error`는 복용 실패를 뜻하지 않습니다. `defer`나 불명확한 답변에 새 시간/복용법을 제안하지 않습니다.

`[Medication] slot=...` / `[Medication] event=...`와 `[MedicationCare] event=... stage=... outcome=... session=...` 로그로 예약, 중복 생략, 음성 결과, 저장 재시도를 확인합니다. 로그 접두사와 이벤트 ID를 함께 확인하세요.

### Pi 적용: 복사할 런타임 파일 5개

터치스크린 MVP 실물 검증을 완료한 Pi가 기준입니다. **Pi에서** 기존 서비스와 수동 허브를 종료합니다. 서비스가 없다면 첫 명령은 생략하고 수동 실행 창에서 `Ctrl+C`를 누릅니다.

```bash
systemctl --user stop doran-voice-hub.service
```

다음은 **Windows PowerShell**에서 실행합니다.

```powershell
Set-Location C:\doran-doran-care-hub
scp .\raspberry-pi-voice-hub\main.py .\raspberry-pi-voice-hub\voice_ai.py .\raspberry-pi-voice-hub\display_state.py .\raspberry-pi-voice-hub\medication_care.py .\raspberry-pi-voice-hub\firebase_medication.py pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/
```

README와 테스트는 실행에 필수가 아닙니다. 기존 `display/`, `display_server.py`, 미활동 케어 파일은 이미 설치된 MVP 버전을 사용합니다. `.env`, 키, outbox 파일은 복사하지 않습니다.

### +2분 예약으로 실물 테스트

아래는 **Pi 터미널** 명령입니다. 현재 장치/어르신 설정과 API 키를 그대로 사용하고 이 실행의 복약 시각만 임시 지정합니다. 이미 실행 중인 Chromium은 같은 `http://127.0.0.1:8765`로 다시 연결됩니다.

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
TEST_MEDICATION_TIME="$(date -d '+2 minutes' +%H:%M)"
printf '복약 테스트 예약: %s (Pi 로컬 시각)\n' "$TEST_MEDICATION_TIME"

MEDICATION_TIMES="$TEST_MEDICATION_TIME" \
MEDICATION_VOICE_CONFIRMATION_ENABLED=true \
DISPLAY_ENABLED=true \
DISPLAY_HOST=127.0.0.1 \
DISPLAY_PORT=8765 \
REMINDER_MODE=hub \
AI_CONVERSATION_ENABLED=true \
AI_CONVERSATION_TRIGGER=firebase \
AI_WAKE_WORD_ENABLED=false \
AI_SESSION_MAX_TURNS=5 \
AI_SESSION_MAX_NO_SPEECH=2 \
AI_SPEECH_START_TIMEOUT_SECONDS=4 \
AI_SPEECH_SILENCE_SECONDS=1.8 \
AI_MAX_UTTERANCE_SECONDS=20 \
OPENAI_TTS_VOICE=marin \
OPENAI_TTS_SPEED=1.15 \
.venv/bin/python -B main.py
```

안내가 끝나고 `듣고 있어요`가 표시되면 답합니다. 서로 다른 예약 시각으로 다음을 검사합니다.

1. `먹었어요` → 같은 `medication_reminder_sent`가 `resolved`, `voiceCheck.outcome=confirmed`.
2. `조금 있다 먹을게` 또는 `아직 안 먹었어` → `open`, `outcome=defer`.
3. 침묵 → `open`, `outcome=no_speech`; 기다려도 음성을 반복하지 않음.
4. `잘 모르겠어요` → `open`, `outcome=unclear`.
5. 중복 검사: 응답 처리가 끝난 **같은 예약 분 안에** `Ctrl+C` 후 `TEST_MEDICATION_TIME`을 다시 계산하지 않고 `MEDICATION_TIMES=...`부터의 실행 블록만 재실행합니다. 같은 이벤트에서 음성/듣기가 반복되지 않는지 확인합니다. 예약 분이 지났다면 기존 스케줄 자체가 실행되지 않으므로 재시작 중복 검사로 볼 수 없습니다.
6. 대화 중 예약 시각이 되면 대화가 끝난 뒤 복약 안내가 이어지고, 이후 터치 대화·기존 대화 기록·미활동 확인이 정상인지 확인합니다.

다음 결과를 검사할 때는 허브를 종료하고 `TEST_MEDICATION_TIME`을 다시 +2분으로 계산해 전체 실행 블록을 실행합니다. 시험 후 허브를 종료하고 임시 `MEDICATION_TIMES` 없이 원래 서비스를 재시작합니다. 서비스에도 새 기능을 계속 사용하려면 실물 검증 후 `MEDICATION_VOICE_CONFIRMATION_ENABLED=true`를 서비스 실행 환경에 추가해야 합니다. 위 명령은 `.env`나 서비스 설정을 수정하지 않습니다.

### 오프라인 검증

저장소 루트에서 실행합니다. 마이크·OpenAI·Firebase는 모의 객체를 사용하며 실제 설정 파일/키를 읽지 않습니다.

```bash
python -B -m unittest discover -s tests -p 'test_*medication*.py' -v
python -B -m unittest discover -s tests -v
```


## 장시간 미활동 능동 케어

`REMINDER_MODE=hub AI_CONVERSATION_ENABLED=true`에서 AI가 정상 시작하면, 터미널/Firebase 트리거 모두 별도 미활동 작업자를 실행합니다. 기본 `events/elder_001`의 열린 `long_inactivity` 중 아직 확인하지 않은 이벤트를 생성 시각/ID 순서로 5초마다 한 개씩 처리합니다. 시작 전 생성된 열린 이벤트도 포함하며, 센서 `deviceId`는 Pi ID와 달라도 됩니다. AI 활성화를 요청했지만 AI 또는 케어 초기화에 실패하면 로그를 남기고 미활동 이벤트는 음성 없이 열린 상태로 둡니다. 복약 일정 검사는 계속합니다.

1. 원본 이벤트에 ETag 조건부 쓰기로 `meta.voiceCheck` 실행 기록을 먼저 저장합니다.
2. 진행 중인 연속 대화가 끝나면 “어르신, 한동안 움직임이 없으신데 괜찮으세요?”를 재생합니다.
3. 별도 버튼이나 “말씀해주세요” 없이 기존 VAD/말끝 감지/STT로 **한 번** 듣습니다. 기본 발화 시작 대기 4초, 침묵 1.8초, 최대 발화 20초입니다.
4. 규칙으로 답을 판정하고 원본 이벤트에 결과를 기록합니다. 케어 판정에는 LLM을 호출하지 않습니다.

| 결과 | 예시 / 원본 이벤트 변경 |
|---|---|
| `okay` | `괜찮아`, `네, 괜찮아요`, `문제없어요`처럼 명확한 전체 답변 → `status=resolved`, `resolvedAt` |
| `help` | `도와줘`, `도와주세요`, `가슴이 아파요`, `숨을 못 쉬겠어요`, `119 불러주세요` → `severity=critical`, 도움 요청 제목. 기존 `long_inactivity` 종류 유지 |
| `unclear` | `네`만 말함, `괜찮은 것 같아요`, `괜찮아요?`, 인용·부정·가정·알 수 없는 문장 → 열린 상태 유지 |
| `no_speech` | 발화 없음, 음성 검증 탈락, 빈 STT → 열린 상태 유지 |
| `cancelled` | 대화 종료를 기다리는 사이 보호자가 해결함 → 안내 전 재확인 후 음성 생략 |
| `error` | 안내/녹음/STT 실패·종료 요청 → 열린 상태 유지, 예외 종류만 기록 |

일부만 긍정인 긴 문장은 자동 해결하지 않습니다. 문장 단위로 명시적 도움 요청이 함께 있으면 도움을 우선합니다. 음성 판정은 제한된 문구 규칙이며 진단이나 모든 긴급 표현의 탐지를 보장하지 않습니다. 보호자가 확인 도중 해결한 이벤트는 다시 열지 않고, `critical` 이벤트는 긍정 답변만으로 해결하지 않습니다. 센서가 관리하는 실제 움직임 상태는 변경하지 않습니다.

`meta.voiceCheck`가 있으면 재시작해도 같은 이벤트의 음성을 재시도하지 않습니다. 기존 `inactivity_voice_check_sent.sourceEventId`가 가리키는 이벤트도 제외합니다. Firebase claim 응답이 불확실하면 자기 토큰 소유를 확인한 뒤에만 말합니다. 결과 저장 실패는 실행 중 결과 쓰기만 재시도하며 음성을 반복하지 않습니다. **claim 직후 전원이 꺼지면 실제 안내가 생략될 수 있고, 결과 저장 전에 재시작하면 `processing` 기록만 남을 수 있습니다.** 자동 재확인보다 이벤트당 최대 한 번을 선택한 절충입니다. 기존 안내 전용 모드는 claim을 작성하지 않으며 완료 안내 기록이 저장되지 못하면 재시작 후 중복을 완전히 막지 못합니다.

복약 검사와 Firebase 명령 처리는 별도 작업자로 유지합니다. 새 공유 잠금은 대시보드 연속 세션 전체와 안부 확인이 서로 끼어들지 않도록 하고, 기존 `AUDIO_LOCK`은 안내 재생부터 녹음 끝까지 연속 유지합니다. 복약 안내 재생은 현재 녹음·재생의 잠금을 기다릴 수 있습니다. 케어용 대화 객체는 별도 세션/기억을 쓰며 정상 대화 기억을 초기화하지 않습니다. 인식된 답변은 기존 대화 메시지/outbox에 보관합니다. 확인 질문은 제어 안내이므로 메시지로 저장하지 않고, 원본 이벤트에는 음성 원문을 추가하지 않습니다. 콘솔의 원본 이벤트 ID와 케어 `session` ID로 기록을 연결합니다.

추가 확인/작별 음성은 없습니다. 도움은 보호자 대시보드 긴급 목록에 표시하는 단계이며 실제 전화나 별도 알림 전송은 하지 않습니다. `[Inactivity] source=...` 로그로 claim·최종 저장·재시도를, `[InactivityCare] event=... stage=... outcome=...`로 음성 처리 결과를 확인합니다.

### Pi 적용 및 실물 검사

새 Pi 의존성은 없습니다. 아래 **런타임 4개**를 함께 교체해야 합니다. Pi에서 기존 서비스/포그라운드 프로세스를 먼저 종료합니다.

```bash
systemctl --user stop doran-voice-hub.service
```

Windows PowerShell:

```powershell
scp C:\doran-doran-care-hub\raspberry-pi-voice-hub\main.py C:\doran-doran-care-hub\raspberry-pi-voice-hub\voice_ai.py C:\doran-doran-care-hub\raspberry-pi-voice-hub\firebase_inactivity.py C:\doran-doran-care-hub\raspberry-pi-voice-hub\inactivity_care.py pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/
```

Pi 포그라운드:

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
REMINDER_MODE=hub \
ELDER_ID=elder_001 \
AI_CONVERSATION_ENABLED=true \
AI_CONVERSATION_TRIGGER=firebase \
AI_WAKE_WORD_ENABLED=false \
AI_SESSION_MAX_TURNS=5 \
AI_SESSION_MAX_NO_SPEECH=2 \
AI_SPEECH_START_TIMEOUT_SECONDS=4 \
AI_SPEECH_SILENCE_SECONDS=1.8 \
AI_MAX_UTTERANCE_SECONDS=20 \
OPENAI_TTS_VOICE=marin \
OPENAI_TTS_SPEED=1.15 \
.venv/bin/python -B main.py
```

매 시나리오마다 새 `long_inactivity` ID를 발생시킵니다. `괜찮아요` → 해결, `도와주세요` → 긴급, 침묵/`잘 모르겠어요` → 열린 상태 및 확인 기록을 검사합니다. 마지막 두 결과의 원본 ID를 그대로 남겨 재시작해도 재안내하지 않는지 봅니다. 별도 대시보드 대화를 실행하는 중 새 이벤트를 발생시켜 대화 완료 뒤 확인이 이어지고, 복약 시각 및 기존 대화 기억/기록이 유지되는지도 확인합니다. 실제 Pi/Jabra 오디오와 배포된 Firebase 쓰기 권한은 로컬 모의 테스트로 검증할 수 없습니다.

## 터치스크린: 스와이프 페이지·음악·라디오·인지 놀이

Pi의 `main.py`가 `REMINDER_MODE=hub`에서 선택형 로컬 HTTP 서버를 같은 프로세스에서 실행합니다. 브라우저는 정적 HTML/CSS/JS와 실제 런타임의 메모리 상태를 사용하며, Pi에 Node나 새 Python 패키지를 설치할 필요가 없습니다. Raspberry Pi OS Desktop의 7–10인치 화면을 대상으로 큰 글씨·버튼과 짧은 수평 전환을 제공합니다. 가로 800×480 / 1024×600과 사용하는 세로 방향에서 실제 가독성·터치 동작을 확인하세요. 보호자 대시보드는 별도이며, 복약 현황 등 대시보드의 실시간 상태 개선은 후속 작업입니다.

화면 빈 영역을 좌우로 밀거나 하단 페이지 표시를 눌러 이동합니다. 홈으로 돌아가는 버튼도 제공합니다. 음량 막대나 재생 버튼 조작은 페이지 스와이프와 구분합니다.

| 페이지 | 이번에 제공하는 기능 |
|---|---|
| 홈 | 큰 시계·날짜, 실제 음성 상태, 복약/미활동 카드, 기존 로컬 대화 시작 |
| 음악·라디오 | 설정된 방송 선택, 재생/일시정지, 이전/다음 방송, 큰 음량 조절 |
| 인지 놀이 | 왼쪽·오른쪽·양손 카드로 2–6개 순서 기억, 짧은 터치 놀이 |
| 추억·사진 | 준비 중 안내만 표시. 사진 업로드·저장소·샘플 가족 사진 없음 |

| 화면 | 실제 처리 단계 |
|---|---|
| `도란도란`, 현재 시간/날짜, `곁에 있어요` | 현재 캡처·API 처리·플레이어 단계 없음. 작업 사이/잠금 대기 중에도 표시될 수 있으며, 진행 중인 카드와 버튼 비활성 상태는 유지. 시계는 Pi의 로컬 시간 사용 |
| `듣고 있어요` | 기존 마이크 캡처 경로에서 녹음 중 |
| `생각하고 있어요` | STT, LLM 요청 또는 TTS 음성 생성 중 |
| `말하고 있어요` | 실제 `mpg123` 재생 경로 실행 중. 스피커 소리의 물리적 측정값은 아님 |
| 미활동 확인 / 복약 안내 카드 | 해당 케어 작업이 진행 중. 처리 완료 시 사라짐 |

화면은 `/api/state` 조회 완료 후 약 250ms 뒤 다시 조회합니다. 각 로컬 상태 조회는 최대 400ms 기다리며, 응답이 지연되면 라디오를 멈추고 연결이 돌아와도 자동 재개하지 않습니다. 음성 상태를 시간표로 흉내 내지 않으며, 250ms보다 짧은 단계는 화면에서 보이지 않을 수 있습니다. 연결이 끊기면 연결 상태를 알리고 대화/라디오 재생을 막은 뒤 자동으로 다시 연결합니다. 미활동과 복약 작업이 겹치면 미활동 카드를 우선 표시하고, 해당 작업이 끝났을 때 복약 작업이 여전히 진행 중이면 복약 카드로 돌아갑니다. 케어 카드는 복약 완료·보호자 확인 완료를 뜻하지 않으며, 지속 알림이나 확인 버튼은 이번 범위에 없습니다. 긴급 호출과 실제 전화 기능은 제공하지 않습니다.

### 인지 놀이: 순서 기억

`인지 놀이` 페이지에서 **왼쪽·오른쪽·양손** 큰 카드로 짧은 순서 기억 놀이를 합니다. 지금은 터치로만 사용하며 소리나 응답 제한 시간은 없습니다. 인지 자극을 위한 놀이이고 진단·치료·의료 점수를 제공하지 않습니다.

1. `놀이 시작`을 누르면 2개 동작으로 시작합니다. 500ms 뒤부터 카드 하나를 850ms 동안 강조하고, 350ms 동안 모두 중립으로 바꾼 다음 다음 카드를 보여줍니다. 같은 카드가 연속되어도 사이에 불이 꺼집니다.
2. 시범이 끝나면 카드를 모두 중립으로 바꾸고 입력을 받습니다. 본 순서대로 누르면 됩니다. 시범 중 터치는 답으로 받지 않습니다.
3. 한 순서를 맞히면 짧게 격려하고 1.4초 뒤 동작을 하나 더 붙여 보여줍니다. 최대 6개 순서를 맞히면 놀이를 마칩니다(최대 5라운드).
4. 다른 카드를 누르면 편안한 재도전 안내와 이번 성공 라운드 수·이번 화면 세션의 최고 기록을 보여줍니다. 다시 시작하면 새로운 2개 순서로 시작합니다. 최고 기록은 재시작·페이지 이동·돌봄 중단 후에도 현재 브라우저 메모리에만 남으며 새로고침하면 지워집니다. 서버·Firebase·파일·브라우저 저장소에 놀이 기록을 저장하지 않습니다.

페이지를 떠나거나 대화/복약/미활동 케어가 시작되면 놀이 타이머를 취소하고 초기화합니다. 음성 우선권 변경 번호로 조회 사이의 짧은 음성 작업도 확인합니다. 화면 숨김·창 포커스 이탈·연결 끊김에서도 초기화하며, 다시 돌아와 직접 시작해야 합니다. 라디오 재생과 음성 우선권 규칙은 그대로이고 게임은 오디오를 추가하지 않습니다.

#### 나중에 mmWave 입력 연결하기

순서 생성·시범·정답 판단·타이머는 `display/memory_game.js`에 두고, 화면과 입력 허용 여부는 `display/app.js`에서 연결합니다. 터치 카드는 공통 입력 함수 `window.DoranGame.submitGameInput(action, source)`를 사용합니다. 향후 IWR6843AOPEVM 동작 분류기 어댑터도 같은 함수를 호출할 수 있습니다.

| 향후 분류기 동작 | 게임의 추상 action | 화면 카드 |
|---|---|---|
| `LEFT_ARM` | `left` | 왼쪽 |
| `RIGHT_ARM` | `right` | 오른쪽 |
| `BOTH_ARMS` | `both` | 양손 |

연결할 때의 호출 형태는 `window.DoranGame.submitGameInput('left', 'motion')`입니다. `source`는 입력 출처이며 동작 이름을 바꾸지 않습니다. 게임 페이지가 활성화되고 응답 차례이며, 로컬 상태 연결이 최신이고 화면이 보이고 대화/케어가 없을 때만 입력을 받습니다. 향후 어댑터가 센서 전송 경로, 완료된 동작당 1회 발생, 중립 복귀 후 재입력, 중복 제거를 담당해야 합니다. 라운드 변경·재시작 전에 수집한 지연 이벤트는 버리고 현재 입력 차례에서 새로 완료된 동작만 전달하세요.

**현재 센서는 연결하지 않았습니다.** 레이더 데이터나 동작 인식을 흉내 내지 않고, 센서용 백엔드 API도 추가하지 않습니다. 이 훅은 향후 실제 분류기를 연결할 경계입니다.

#### 이번 인지 놀이만 Pi에 적용

스와이프·라디오 실물 검증을 마친 Pi에는 다음 **화면 파일 4개와 서버 파일 1개**만 복사합니다. Windows PowerShell에서:

```powershell
Set-Location C:\doran-doran-care-hub
scp .\raspberry-pi-voice-hub\display\index.html .\raspberry-pi-voice-hub\display\styles.css .\raspberry-pi-voice-hub\display\app.js .\raspberry-pi-voice-hub\display\memory_game.js pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/display/
scp .\raspberry-pi-voice-hub\display_server.py pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/
```

`display_server.py`에 새 화면 파일 경로가 추가되므로 **Pi 허브를 기존 실행 설정 그대로 재시작한 뒤 Chromium을 새로고침**합니다. 수동 실행 중이면 로그 창에서 `Ctrl+C` 후 이전 `main.py` 실행 명령을 다시 실행합니다. 새 패키지나 설정은 필요하지 않으며 방송 설정·실제 `.env`·키는 복사하지 않습니다.

최소 실물 확인:

1. 시작 → 2개 카드가 하나씩 켜지고 사이에 꺼지는지 확인. 시범 중 눌러도 입력하지 않아야 합니다.
2. 맞히기 → 다음 순서가 1개 늘어남. 틀리기 → 편안한 안내·성공/최고 기록. 다시 시작 → 2개부터, 최고 기록 유지.
3. 시범/입력 중 페이지를 나갔다 돌아오면 정지 상태. 빈 영역 스와이프·화살표·홈·라디오 음량·대화 버튼도 확인.
4. 라디오를 켠 채 놀이 중 기존 대화 또는 예약 케어가 시작되면 홈 전환·놀이 중단·라디오 정지. 종료 후 둘 다 자동 재개하지 않음.

### 음악·라디오 설정

Chromium의 HTML5 `audio`로 직접 스트림을 재생합니다. 방송 목록은 `raspberry-pi-voice-hub/display/radio_presets.json`의 UTF-8 JSON 파일이며, 처음에는 다음처럼 비어 있습니다.

```json
{
  "presets": []
}
```

설정된 방송이 없으면 방송을 추가하라는 안내를 표시하고 재생 컨트롤을 숨깁니다. 사용할 권한이 있고 Chromium이 재생할 수 있는 **직접 오디오 HTTP(S) 주소**를 확인한 뒤 다음 구조로 추가하고 kiosk 화면을 새로고침합니다. 앞에서부터 최대 24개 항목을 읽고 잘못된 항목은 건너뜁니다. 아래 주소는 구조 설명용으로 재생되지 않습니다.

```json
{
  "presets": [
    {"name": "내가 고른 방송", "url": "https://radio.example.org/live.mp3"}
  ]
}
```

방송국 웹페이지나 로그인 페이지 주소는 오디오 주소가 아닙니다. 이 MVP에는 HLS 전용 재생기, 스트림 프록시, 유료 서비스 SDK가 없습니다. 직접 MP3/AAC 등의 주소와 실제 Pi Chromium의 지원 여부를 확인하세요. 계정·비밀번호·비밀 토큰이 필요한 URL을 넣지 마세요. 설정은 로컬 브라우저에 전달되며 비밀 저장소가 아닙니다. 방송 이름은 설정에 적힌 이름을 표시하며 곡명/방송 프로그램 메타데이터는 가져오지 않습니다. 연결/코덱 실패 시 오류 안내를 표시하고 자동 재생이나 무한 재시도를 하지 않습니다. 음량은 브라우저 라디오에만 적용되고 음성 안내의 시스템 음량은 바꾸지 않습니다.

### 음성 대화·케어가 라디오보다 우선

듣기·생각하기·말하기, 진행 중인 대화, 복약/미활동 케어 상태가 시작되면 홈 상태를 전경에 표시하고 라디오를 일시정지합니다. 홈의 대화 버튼을 눌렀을 때도 요청 전 즉시 라디오를 멈춥니다. 기존 상태 API의 오디오 우선권을 사용하며 별도 라디오 백엔드는 없습니다. 대화·케어가 끝나면 `대화 중 일시정지됨`과 같은 안내를 남기고, **음악·라디오 페이지에서 재생을 다시 눌러야** 소리가 납니다. 화면 새로고침·재연결·방송 선택만으로 재생하지 않습니다. 브라우저 창이 숨겨지거나 상태 연결이 끊겨도 라디오를 멈춥니다.

브라우저의 재생 의도를 같은 화면 서버에 알리고, 재생 중 음성 작업이 시작될 때 브라우저의 정지 확인을 최대 0.75초 기다립니다. 재생 의도가 없으면 기다리지 않고, 기한 안에 확인하지 못해도 음성 케어를 계속합니다. 상태 조회 사이에 짧은 음성 작업이 끝나도 변경 번호로 감지해 라디오를 멈춥니다. 화면이 없거나 종료되어도 음성 케어는 계속 동작합니다. 브라우저와 프로세스 사이의 상태 통신이므로 완전히 응답이 멎은 Chromium의 소리까지 중단한다고 보장하지는 못합니다. 실제 Pi/Jabra에서 원격 대화와 예약 케어의 시작 순간에 소리가 겹치지 않는지 확인해야 합니다. 기존 `AUDIO_LOCK`, Firebase 명령, 복약 스케줄러/음성 확인, 미활동 케어, 대화 기억/outbox, 최대 발화 20초, `marin`/1.15와 웨이크워드 비활성 기본값을 유지합니다.

### 터치로 같은 연속 대화 시작

`도란이와 대화하기`는 기존 Firebase 대화 작업자에 로컬 요청 한 개를 전달하고, 그 작업자가 기존 `VoiceConversation.run_session`을 실행합니다. Firebase 명령을 새로 쓰지 않으며 같은 세션 기억, 대화 기록/outbox, 상한, 종료 표현과 잠금을 그대로 사용합니다. 중복 터치나 진행 중인 대화/케어 요청을 누적하지 않습니다.

터치 시작 요청 자체는 메모리에만 대기하므로 시작 전에 허브가 종료되면 사라집니다. 이미 생성된 대화 메시지는 기존 outbox 정책을 따릅니다.

이 버튼은 화면 서버와 AI의 **Firebase 작업자 모드가 정상 시작했을 때만** 활성화됩니다. `AI_CONVERSATION_TRIGGER=firebase`를 명시하세요. `hub`에서 터미널 트리거를 선택하면 기존 Enter 입력 구조를 유지하므로 터치 시작은 비활성화되고 화면 상태만 표시합니다. 별도 `chat`, `demo`, `schedule`, `watch` 모드에서는 이번 화면 서버를 시작하지 않습니다. 요청 대기 중에는 최대 200ms 간격으로 로컬 요청을 확인하지만, 진행 중인 Firebase 조회/쓰기가 끝날 때까지 기다릴 수 있습니다. 대화·케어의 공유 잠금과 기존 `AUDIO_LOCK` 대기도 있으므로 터치 직후 음성이 항상 즉시 시작되는 것은 아닙니다.

### Pi에 복사할 파일

먼저 **Pi에서** 기존 서비스와 수동 실행을 종료합니다. 서비스가 없다면 서비스 명령은 생략하고, 수동 실행 터미널에서 `Ctrl+C`를 누릅니다.

```bash
systemctl --user stop doran-voice-hub.service
```

복약 음성 확인까지 실물 검증한 Pi에 화면 기능을 처음 설치할 때의 절차입니다. **런타임 파일 3개와 화면 파일 5개**를 함께 설치합니다. 스와이프·라디오 설치가 끝났다면 위의 인지 놀이 적용 절차만 사용하세요. 다음 명령은 **Windows PowerShell**에서 실행합니다. 빈 기본 방송 설정은 임시 위치에 복사해 Pi에서 이미 설정한 방송을 덮어쓰지 않습니다. `.env`, 키, `conversation_outbox/`는 복사하지 않습니다.

```powershell
Set-Location C:\doran-doran-care-hub
scp .\raspberry-pi-voice-hub\display_state.py .\raspberry-pi-voice-hub\display_server.py .\raspberry-pi-voice-hub\radio_presets.py pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/
scp .\raspberry-pi-voice-hub\display\index.html .\raspberry-pi-voice-hub\display\styles.css .\raspberry-pi-voice-hub\display\app.js .\raspberry-pi-voice-hub\display\memory_game.js pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/display/
scp .\raspberry-pi-voice-hub\display\radio_presets.json pi-user@PI_HOST:/tmp/doran-radio-presets.initial.json
```

**Pi 터미널**에서 기본 설정을 최초 한 번만 설치합니다.

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
test -f display/radio_presets.json || cp /tmp/doran-radio-presets.initial.json display/radio_presets.json
```

기존 `main.py`, `voice_ai.py`, 복약/미활동 런타임은 검증된 버전을 유지합니다. 자동 시작 보조 스크립트를 아직 설치하지 않았다면 아래 두 명령도 **Windows에서** 실행합니다. README와 테스트 파일은 Pi 런타임에 필수는 아닙니다.

```powershell
ssh pi-user@PI_HOST "mkdir -p /home/pi-user/doran-doran-care-hub/scripts"
scp .\scripts\run-pi-kiosk.sh pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/scripts/run-pi-kiosk.sh
```

### 비밀 정보 없는 테스트 방송 1개 만들기

외부 방송 주소가 없어도 Pi의 Python 표준 라이브러리로 **저음량 합성 시험음 5분**을 만들어 재생/정지/음량/대화 전환을 확인할 수 있습니다. 음악 파일은 포함하지 않습니다. **Pi 터미널**에서 실행하면 기존 방송 설정을 고유 이름으로 백업하고 테스트 설정을 기록합니다. 출력되는 복원 명령을 보관하세요.

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
python3 - <<'PY'
import datetime
import json
import math
from pathlib import Path
import struct
import wave

config = Path('display/radio_presets.json')
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
backup = config.with_name('radio_presets.before-test-' + stamp + '.json')
with backup.open('xb') as saved:
    saved.write(config.read_bytes() if config.exists() else b'{"presets": []}\n')

audio_dir = Path('/tmp/doran-radio-test')
audio_dir.mkdir(exist_ok=True)
rate = 22050
second = bytearray()
for sample in range(rate):
    fade = min(1.0, sample / (rate * 0.05), (rate - 1 - sample) / (rate * 0.05))
    value = int(1500 * fade * math.sin(2 * math.pi * 220 * sample / rate))
    second.extend(struct.pack('<h', value))
with wave.open(str(audio_dir / 'test-tone.wav'), 'wb') as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(rate)
    audio.writeframes(bytes(second) * 300)

config.write_text(json.dumps({'presets': [
    {'name': '로컬 시험음', 'url': 'http://127.0.0.1:8766/test-tone.wav'}
]}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('설정 백업:', backup)
print('테스트 후 복원: cp -- ' + str(backup) + ' display/radio_presets.json')
PY
```

다음 명령은 **별도의 Pi 터미널**에서 실행하고 시험 중 켜둡니다. 이 서버는 시험음 파일만 제공하며, 화면과 실제 상태 API는 계속 기존 허브의 8765번 포트를 사용합니다.

```bash
python3 -m http.server 8766 --bind 127.0.0.1 --directory /tmp/doran-radio-test
```

허브를 시작한 뒤 kiosk를 새로고침하고 음악·라디오에서 `로컬 시험음`을 재생합니다. 처음에는 작은 음량으로 확인하세요. 이전/다음 방송도 검사하려면 테스트 JSON의 `presets`에 같은 로컬 URL과 다른 이름으로 항목 하나를 더 추가합니다. 실제 방송 파일이나 키는 필요하지 않습니다. 시험이 끝나면 위에서 출력한 `cp -- ...` 명령으로 원래 방송 설정을 복원하고 kiosk를 새로고침한 뒤, 시험음 서버 터미널에서 `Ctrl+C`를 누릅니다. `/tmp` 시험 파일은 재부팅 후 없어질 수 있으므로 실제 방송으로 사용하지 않습니다.

### 수동 실행: 음성 허브와 화면 서버

아래 명령을 **Pi 터미널**에서 실행합니다. 화면 서버만 별도로 실행하지 않습니다. `python -m http.server`로 정적 파일만 열면 실제 상태/터치 API가 연결되지 않습니다. 기존 서비스와 수동 허브를 동시에 실행하지 마세요.

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
MEDICATION_VOICE_CONFIRMATION_ENABLED=true \
DISPLAY_ENABLED=true \
DISPLAY_HOST=127.0.0.1 \
DISPLAY_PORT=8765 \
REMINDER_MODE=hub \
AI_CONVERSATION_ENABLED=true \
AI_CONVERSATION_TRIGGER=firebase \
AI_WAKE_WORD_ENABLED=false \
AI_SESSION_MAX_TURNS=5 \
AI_SESSION_MAX_NO_SPEECH=2 \
AI_SPEECH_START_TIMEOUT_SECONDS=4 \
AI_SPEECH_SILENCE_SECONDS=1.8 \
AI_MAX_UTTERANCE_SECONDS=20 \
OPENAI_TTS_VOICE=marin \
OPENAI_TTS_SPEED=1.15 \
.venv/bin/python -B main.py
```

`DISPLAY_ENABLED` 기본값은 `false`입니다. 켜면 기본 주소는 `http://127.0.0.1:8765`이며 `DISPLAY_HOST`는 `127.0.0.1` 또는 `localhost`만 허용합니다. 다른 컴퓨터의 브라우저에서 Pi의 LAN 주소로 접근하는 용도가 아닙니다. `DISPLAY_PORT`는 1–65535 범위입니다. 잘못된 설정, 포트 충돌 또는 화면 서버 시작 실패는 로그를 남기고 음성 허브를 계속 실행합니다. Chromium이나 물리 화면이 없어도 음성 케어는 동작합니다.

### Chromium 전체 화면

한글이 네모(□)로 표시되지 않도록 Pi에 `sudo apt install -y fonts-noto-cjk`를 설치한 뒤 Chromium을 다시 시작합니다.

허브가 실행 중인 상태에서 **Pi Desktop의 다른 터미널**을 열어 실행합니다. 일반 SSH 터미널에는 그래픽 세션이 없으므로 여기서 Chromium을 띄우지 않습니다.

```bash
chromium --kiosk --no-first-run --noerrdialogs \
  --user-data-dir="$HOME/.config/doran-kiosk" \
  http://127.0.0.1:8765
```

설치된 실행 파일 이름이 `chromium-browser`이면 첫 단어만 바꿉니다. 또는 다음 보조 스크립트를 사용합니다. 스크립트는 그래픽 세션과 Chromium을 확인하고, 실제 상태 API가 준비될 때까지 최대 120초 기다립니다. 실패 시 안내를 출력하고 종료하며 음성 서비스에는 영향을 주지 않습니다. 별도 대화 엔진이나 HTTP 서버를 실행하지 않습니다.

```bash
sh /home/pi-user/doran-doran-care-hub/scripts/run-pi-kiosk.sh
```

위 두 방식 중 하나만 사용합니다. 키보드의 `Alt+F4`로 전체 화면 창을 닫을 수 있습니다. 포트를 바꿨다면 허브와 같은 `DISPLAY_PORT=...`를 스크립트 실행에도 지정하세요.

### 권장 자동 시작: 음성 서비스 + 그래픽 로그인 후 Chromium

먼저 수동 검증을 마친 뒤 기존 `doran-voice-hub.service`를 사용합니다. 서비스는 허브/HTTP 서버를, Desktop의 자동 시작은 Chromium만 실행하도록 나눕니다. `.env`를 수정하지 않고 기존 사용자 서비스에 실행 환경을 추가하려면 **Pi 터미널**에서 아래 명령을 실행합니다.

```bash
systemctl --user edit doran-voice-hub.service
```

편집기에 다음 내용을 넣고 저장합니다. 기존 서비스의 작업 경로·Python 실행 경로·기존 설정은 유지합니다.

```ini
[Service]
Environment="DISPLAY_ENABLED=true"
Environment="DISPLAY_HOST=127.0.0.1"
Environment="DISPLAY_PORT=8765"
Environment="REMINDER_MODE=hub"
Environment="MEDICATION_VOICE_CONFIRMATION_ENABLED=true"
Environment="AI_CONVERSATION_ENABLED=true"
Environment="AI_CONVERSATION_TRIGGER=firebase"
Environment="AI_WAKE_WORD_ENABLED=false"
Environment="AI_SESSION_MAX_TURNS=5"
Environment="AI_SESSION_MAX_NO_SPEECH=2"
Environment="AI_SPEECH_START_TIMEOUT_SECONDS=4"
Environment="AI_SPEECH_SILENCE_SECONDS=1.8"
Environment="AI_MAX_UTTERANCE_SECONDS=20"
Environment="OPENAI_TTS_VOICE=marin"
Environment="OPENAI_TTS_SPEED=1.15"
```

수동 허브를 `Ctrl+C`로 종료한 뒤 서비스를 시작합니다.

```bash
systemctl --user daemon-reload
systemctl --user enable doran-voice-hub.service
systemctl --user restart doran-voice-hub.service
systemctl --user status doran-voice-hub.service
```

현재 Raspberry Pi 공식 kiosk 안내는 Desktop이 로드된 뒤 실행되는 `~/.config/labwc/autostart`를 사용합니다. 해당 **labwc 세션**에서 다음 파일을 열고 기존 내용을 보존하며 아래 실행 줄을 **한 번만** 추가하세요. 이전에 만든 다른 Chromium kiosk 자동 시작 항목이 있다면 하나로 정리합니다. [Raspberry Pi 공식 kiosk 안내](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/)

```bash
mkdir -p ~/.config/labwc
nano ~/.config/labwc/autostart
```

추가할 한 줄:

```bash
sh /home/pi-user/doran-doran-care-hub/scripts/run-pi-kiosk.sh &
```

로그아웃 후 Desktop에 다시 로그인해 확인합니다. 자동 로그인은 Pi 설정에서 Desktop 자동 로그인을 선택한 경우에만 적용되며, 이 변경은 OS 설정을 자동으로 수정하지 않습니다. 다른 Desktop/창 관리자에서는 해당 세션의 자동 시작 설정을 사용하세요. 서버가 120초 안에 시작하지 못한 경우 음성 서비스 로그를 확인하고 위 스크립트를 다시 실행합니다. 화면의 꺼짐 시간과 회전은 실제 터치스크린의 Desktop 표시 설정에서 조정합니다.

### 스와이프·라디오 실물 테스트 체크리스트

1. **대기·이동·홈 복귀:** 큰 현재 시간/날짜와 `곁에 있어요`를 확인합니다. 홈 → 음악·라디오 → 인지 놀이 → 추억·사진을 좌우로 밀어 왕복하고 페이지 표시가 맞는지 봅니다. 하단 표시와 홈 버튼으로도 이동하고, 첫/마지막 페이지에서 화면을 벗어나지 않는지 확인합니다. 인지 놀이의 순서 기억 게임을 확인하고, 준비 중인 추억·사진 페이지에는 실제 사진을 암시하는 데이터가 없어야 합니다. 800×480 / 1024×600 및 사용하는 세로 방향에서 글자·버튼이 잘리지 않는지 봅니다.
2. **빈 설정·재생·음량:** 빈 `presets`에서 안내가 나타나고 재생 컨트롤이 숨겨지는지 확인한 후, 위 로컬 시험음을 설정하고 새로고침합니다. Play/Pause, 작은 음량부터 큰 음량까지 조절, 0에서 음소거를 확인합니다. 음량 막대를 좌우로 움직여도 페이지가 바뀌면 안 됩니다. 항목을 2개 설정한 경우 이전/다음에서 이름이 바뀌고, 선택만으로 재생하지 않으며 Play로 시작되는지 봅니다. 시험음 서버를 멈춘 상태로 재생해 오류 안내도 확인한 후 서버를 다시 시작합니다.
3. **로컬 대화 우선:** 라디오를 재생하고 홈으로 이동해 `도란이와 대화하기`를 누릅니다. 라디오가 즉시 멈추고 실제 `생각하고 있어요` → `말하고 있어요` → `듣고 있어요` 상태가 기존 연속 대화와 맞아야 합니다. 대화 중에는 홈 상태를 유지합니다. `그만` 또는 무응답/턴 상한으로 끝난 뒤 음악·라디오로 이동해 정지 안내와 **자동 재개 없음**을 확인하고, Play를 눌러 다시 재생합니다. 빠른 중복 터치로 세션이 두 번 예약되지 않아야 합니다.
4. **기존 원격 대화 우선:** 음악·라디오에서 재생 중 보호자 대시보드의 기존 대화 시작을 누릅니다. 홈 전경 전환, 첫 안내/마이크 시작 전 라디오 중단, 대화 종료 후 자동 재개 없음을 확인합니다. 터치/원격 대화의 메시지/outbox, 같은 세션의 기억·턴 번호는 기존 방식대로 남아야 합니다.
5. **복약 우선:** 수동 허브를 종료한 뒤 위 실행 블록의 `MEDICATION_VOICE_CONFIRMATION_ENABLED=true` 앞에 `MEDICATION_TIMES="$(date -d '+2 minutes' +%H:%M)" \` 한 줄을 추가해 실행합니다. 라디오를 켜두고 예정 시각에 홈/복약 카드가 나타나며 안내 전에 라디오가 멈추는지 봅니다. `먹었어요` 또는 `아직 안 먹었어`의 기존 이벤트 결과를 확인하고, 처리 종료 후 카드가 사라져도 라디오가 재개되지 않아야 합니다. 카드 종료가 복용 완료 표시를 뜻하지 않습니다. 시험 후 임시 `MEDICATION_TIMES` 없이 원래 실행으로 돌아갑니다.
6. **미활동 우선:** 라디오를 다시 켜고 이전 실물 검사와 같은 방법으로 **새 ID**의 열린 `long_inactivity` 테스트 이벤트를 만듭니다. 홈/미활동 카드 전경과 라디오 중단, 말하기/듣기 상태, 처리 종료 후 카드 종료와 자동 재개 없음을 확인합니다. `괜찮아요`/무응답 등의 기존 결과를 유지해야 합니다. 이미 확인한 ID는 중복 방지 때문에 실행되지 않습니다.
7. **연결·화면 부재:** 재생 중 허브를 종료하면 연결 끊김과 재생 중단을 확인합니다. 재시작하면 현재 상태에 연결되되 자동 재생하지 않아야 합니다. Chromium을 닫아도 Firebase 명령·미활동·복약 작업이 계속되고 다시 열면 현재 상태가 보여야 합니다. 창을 숨겼다 다시 표시한 경우도 Play를 다시 눌러야 합니다. 대화 중 새 미활동 이벤트와 복약 시각이 기존 잠금 순서대로 처리되는지 확인합니다.

화면/방송 설정과 전체 회귀 테스트는 저장소 루트의 기존 가상환경에서 실행합니다. 자동 테스트는 상태 계약·오디오 우선권·설정 검증과 화면 로직을 검사하며, 실제 Pi/Jabra 소리·터치 입력·Chromium/그래픽 로그인·실시간 Firebase 통합은 위 실물 검사로 확인해야 합니다.

```bash
cd /home/pi-user/doran-doran-care-hub
source raspberry-pi-voice-hub/.venv/bin/activate
python -m unittest discover -s tests -p 'test_display*.py' -v
python -m unittest discover -s tests -p 'test_radio*.py' -v
python -m unittest discover -s tests -v
```

JavaScript 개발 검사는 Node가 있는 Windows/개발 컴퓨터의 저장소 루트에서 실행합니다. Pi 런타임에 Node를 설치할 필요는 없습니다. `test_display_frontend.py`는 Node가 있으면 아래 동작 검사를 함께 실행하고, 없으면 해당 검사만 건너뜁니다.

```bash
node --check raspberry-pi-voice-hub/display/app.js
node --check raspberry-pi-voice-hub/display/memory_game.js
node --test tests/display_browser.test.js tests/memory_game.test.js
```

## AI 대화

`hub`에서 AI를 활성화하면 `AI_CONVERSATION_TRIGGER`로 시작 방식을 선택합니다.

| 값 | 동작 |
|---|---|
| `auto` (기본값) | 표준입력이 TTY이면 터미널, 아니면 Firebase 명령 사용 |
| `terminal` | TTY가 필요한 Enter/n/q 방식. TTY가 없으면 AI 시작에 실패하고 기존 허브는 계속 실행 |
| `firebase` | `commands/{ELDER_ID}/{DEVICE_ID}`에서 명령 한 개로 짧은 연속 대화 세션 실행. TTY 불필요 |

`chat`은 항상 `terminal` 방식을 사용합니다. `auto`는 한 번 선택한 트리거가 실행 중 실패했다고 다른 방식으로 전환하지 않습니다. 시작 시 도란이와 음성이 AI임을 콘솔에 안내합니다.

터미널 방식에서 **Enter**를 누르면 “말씀해주세요.”를 끝까지 재생한 다음 최대 약 4초 동안 발화 시작을 기다립니다. 목소리가 감지되면 계속 녹음하고, 기본 1.35초 침묵에서 말끝 후보로 표시한 뒤 계속 듣다가 총 1.8초 동안 말이 없으면 자동으로 마칩니다. 발화는 최대 약 20초까지 받습니다. Firebase 명령도 같은 녹음 방식을 사용합니다. 터미널에서는 응답 재생 후 Enter로 다음 말을 이어갑니다. Firebase에서는 첫 안내 이후 응답 재생이 끝날 때마다 자동으로 다시 듣습니다.

- `n`: 현재 대화 context를 비우고 새 세션 시작. Persistent Memory는 유지
- `q`: 음성 대화 종료. `hub`의 일정/감시는 계속 실행되며 `chat`은 종료
- `Ctrl+C`: 프로그램 전체 종료

“말씀해주세요.”는 터미널의 매 턴, Firebase 세션의 첫 듣기 시도에만 OpenAI TTS로 생성하며 응답과 같은 음색/속도를 사용합니다. 안내 재생부터 녹음 종료까지 오디오 잠금을 이어서 유지하므로 그 사이에 다른 안내 음성이 끼어들지 않습니다. 이 안내 문구는 대화 기록이나 모델 기억에 넣지 않습니다. 안내 생성/재생이 실패하면 녹음을 시작하지 않고 그 턴을 종료합니다.

녹음은 기존 `arecord`와 WebRTC VAD(모드 3)를 사용하며 16 kHz/16-bit/모노, 30ms 프레임으로 처리합니다. VAD가 음성으로 판단하고 RMS가 300 이상인 프레임이 150ms 연속되어야 발화 시작으로 봅니다. 첫 음절을 보존하려고 그 150ms 확인 구간보다 **앞선 최대 300ms**를 함께 저장합니다. 녹음 시작 직후 말하면 아직 모이지 않은 앞부분까지 만들 수는 없으므로 실제 보존 길이는 짧을 수 있습니다.

발화 시작 후에는 VAD가 음성으로 판단하고 RMS가 200 이상이면 말이 이어지는 것으로 봅니다. 시작 기준보다 낮은 유지 기준을 쓰는 히스테리시스로 작은 목소리를 보존하되, VAD와 RMS 조건은 모두 유지합니다. 이어지는 음성이 한 프레임이라도 감지되면 누적 침묵을 즉시 0으로 되돌리고 말끝 후보도 취소합니다. 기본적으로 45프레임인 1.35초 침묵에서 말끝 후보를 기록하고, 추가 0.45초 동안 계속 들어 총 60프레임인 1.8초 침묵에서 종료합니다. 따라서 1.0~1.5초 숨 고르기 뒤 말이 다시 감지되면 후보 이후라도 같은 녹음에 이어 담습니다. 실제 판정은 들리는 쉼의 길이뿐 아니라 VAD와 목소리 크기에도 영향을 받습니다.

최종 침묵은 `AI_SPEECH_SILENCE_SECONDS`로 조절합니다. 후보 기준은 최종 침묵과 1.35초 중 짧은 값이며, 최종 침묵을 1.35초 이하로 설정하면 별도 후보 로그나 추가 대기 없이 설정한 침묵에서 종료합니다. 후보 단계에서 STT/LLM/TTS를 미리 호출하지 않으므로 기본 정상 말끝 대기는 기존 1.35초보다 0.45초 늘어납니다. 1.8초 종료 뒤 또 다른 고정 대기는 없습니다.

20초 제한은 시작 확인에 쓰인 150ms부터 중간 쉼과 말끝 침묵까지 포함한 구간에 적용하며, 그보다 앞선 최대 300ms는 별도입니다. 기본 최대 구간은 20초를 30ms 프레임 단위로 올림한 667개인 20.01초입니다. 말끝 침묵과 최대 길이에 동시에 도달하면 `max_duration`으로 기록합니다. 장치 버퍼 때문에 실제 종료를 체감하는 시점은 달라질 수 있습니다.

녹음 후에도 기존 VAD+RMS 300 검증을 다시 적용하여 기본 총 음성 300ms 이상·연속 음성 150ms 이상인 경우에만 STT를 호출합니다. 발화 유지용 RMS 200은 이 최종 검증을 낮추지 않습니다. 무음이나 짧은 잡음은 STT/응답 생성/대화 기록을 건너뜁니다. 첫 듣기 시도라면 안내 TTS는 이미 재생된 상태입니다. 마이크 오류·조기 종료·데이터 정지 시 부분 녹음을 전송하지 않으며, 정지 감시 제한은 발화 대기+최대 발화+5초입니다(기본 29초). 종료 요청은 0.1초 간격으로 확인하고 녹음 프로세스를 정리한 뒤 오디오 잠금을 해제합니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `AI_CONVERSATION_ENABLED` | `false` | `hub`에서 AI 대화 활성화 |
| `AI_CONVERSATION_TRIGGER` | `auto` | `auto`, `terminal`, `firebase` 중 선택 |
| `AI_COMMAND_POLL_SECONDS` | `5` | Firebase 명령 처리 후 대기 시간. 유한한 1~60초 값 |
| `AI_WAKE_WORD_ENABLED` | `false` | 이번 복구 단계에서는 비활성화 유지 |
| `OPENAI_TEXT_MODEL` | `gpt-5.6-luna` | Responses 대화 모델 |
| `OPENAI_STT_MODEL` | `gpt-4o-transcribe` | 한국어 음성 인식 |
| `OPENAI_TTS_MODEL` | `gpt-4o-mini-tts` | 응답 음성 생성 |
| `OPENAI_TTS_VOICE` / `OPENAI_TTS_SPEED` | `marin` / `1.15` | 음색과 속도 |
| `AI_AUDIO_DEVICE` | `plughw:CARD=USB,DEV=0` | ALSA 녹음 장치 |
| `AI_PLAYBACK_DEVICE` | `plughw:CARD=USB,DEV=0` | TTS·안내 음성의 ALSA 출력 장치. 브라우저 라디오에는 적용하지 않음 |
| `AI_SPEECH_START_TIMEOUT_SECONDS` | `4` | 발화 시작 대기 시간, 0.3~30초 |
| `AI_SPEECH_SILENCE_SECONDS` | `1.8` | 발화 중 연속 침묵 최종 종료 기준, 0.15~3초 |
| `AI_MAX_UTTERANCE_SECONDS` | `20` | 시작 확인·중간 쉼·말끝 침묵을 포함한 발화 최대 길이, 1~30초 |
| `AI_MIN_SPEECH_MS` | `300` | 최종 검증의 총 음성 최소 길이, 150~3000ms이며 최대 발화 길이 이하 |
| `AI_HISTORY_TURNS` | `10` | 성공적으로 재생한 최근 사용자/AI 대화 쌍 수 |
| `AI_SESSION_IDLE_SECONDS` | `300` | 터미널/웨이크워드 단일 턴의 유휴 기억 초기화. 진행 중인 Firebase 세션에는 적용하지 않음 |
| `AI_SESSION_MAX_TURNS` | `5` | Firebase 세션의 최대 사용자 대화 턴 수, 정수 1~20. 마지막 응답까지 재생한 뒤 종료 |
| `AI_SESSION_MAX_NO_SPEECH` | `2` | Firebase 세션의 연속 무응답 허용 한도, 정수 1~5. 이 횟수에 도달하면 종료 |
| `AI_API_TIMEOUT_SECONDS` | `30` | OpenAI 요청 제한 시간, SDK 자동 재시도 없음 |
| `AI_PLAYBACK_TIMEOUT_SECONDS` | `45` | “말씀해주세요.”와 AI 응답 각각의 재생 제한 시간, 최대 45초 |

새 녹음 설정은 미지정 시 위 기본값을 사용하므로 기존 `.env` 수정이 필요 없습니다. 이전 `AI_RECORD_SECONDS`는 더 이상 사용하지 않으며 값이 `5`로 남아 있어도 5초에 끊기지 않습니다. 시간은 30ms 프레임 단위로 올림합니다. 기존 모델·음색·장치 기본값은 유지합니다. 이번 로컬 테스트로 실제 계정의 모델 접근 권한이나 Pi/Jabra 통합 실행을 검증한 것은 아닙니다.

현재 세션 history는 프로세스 메모리에 두고 Responses 요청마다 다시 전달하며 `store=False`를 사용합니다. 세션 종료 시 사용자 발언 중 약속·관계·취향 같은 기억을 별도 worker에서 추출해 `memories/{elderId}`에 저장합니다. 새 세션에서는 관련 기억 일부만 참고 데이터로 검색하며 전체 Firebase 대화 기록을 prompt에 넣지 않습니다. 각 Firebase 명령 시작, 터미널의 `n`, 단일 턴 방식의 유휴 시간 초과, 재시작 시 새 `pi_chat_...` 세션과 빈 단기 history를 만들고 Persistent Memory는 유지합니다. 배포 파일·초기화·제한·실물 검증은 [Persistent Memory 4차 수정 안내](../docs/PERSISTENT_MEMORY_V4_REVIEW.md)를 따르세요. 아래 과거 연속 대화 단독 배포 절차의 `voice_ai.py` 한 파일만 복사해서는 이번 기능이 동작하지 않습니다.

### 대시보드 연속 대화 세션

“도란이 대화 시작”을 한 번 누르면 새 세션과 빈 기억으로 시작합니다. 첫 “말씀해주세요.” 이후 **사용자 발화 → AI 응답 재생 → 안내 없이 다시 듣기**를 반복합니다. 각 턴은 같은 `VoiceConversation`의 `sessionId`와 증가하는 `turnIndex`를 사용하고, 성공적으로 들려드린 대화 쌍을 기존 `AI_HISTORY_TURNS` 범위 안에서 다음 요청에 전달합니다. 진행 중에는 유휴 시간 때문에 기억을 초기화하지 않습니다. 다음 대시보드 명령은 새 세션입니다. 터미널 Enter/n/q와 선택형 웨이크워드의 단일 턴 동작은 유지합니다.

- **말로 종료:** `그만`, `대화 종료`, `이제 됐어`, `여기까지`, `그만할게요`, `대화 종료해 주세요`, `이제 됐어요`, `여기까지 할게요` 등을 인식하면 종료합니다. `도란아, 이제 그만할게요.` 같은 호칭·존댓말과 띄어쓰기·문장 끝 구두점을 허용합니다. 전체 문장이 정해진 종료 표현과 맞을 때만 처리하므로 `여기까지 걸어왔어요`, `그만하지 말고 계속 이야기해요`, `대화 종료 버튼은 어디 있나요?`는 일반 대화로 보냅니다. 종료 발언은 제어 명령으로 취급해 LLM, 대화 기억, 메시지 outbox에 넣지 않습니다. 종료 표현은 녹음/STT 뒤 판별하므로 응답 재생 중 끼어들어 중단하는 기능은 아닙니다.
- **무응답 종료:** 기본 연속 2회입니다. 각 듣기는 기존 발화 시작 대기 기본값 4초를 사용하므로, 계속 조용하면 첫 안내가 끝난 뒤 약 8초에 종료합니다(장치/잠금 대기 제외). 로컬 음성 검증 탈락과 빈 STT도 같은 횟수에 포함하고, AI 응답이 정상 재생되면 연속 횟수를 0으로 되돌립니다. 중간 무응답에도 전체 안내를 반복하지 않습니다.
- **최대 턴 종료:** 기본 사용자 5턴의 응답을 모두 재생한 뒤 종료합니다. 짧은 시연을 위한 상한이며 20초 발화 제한과는 별도입니다. API/오디오 오류는 즉시 세션을 끝냅니다. 두 상한 때문에 듣기 시도는 최대 `AI_SESSION_MAX_TURNS × AI_SESSION_MAX_NO_SPEECH`회입니다(기본 10회). 세션 전체의 고정 초 단위 제한은 없고, 기존 API/녹음/재생 시간 제한을 유지합니다.
- **종료 확인:** 추가 작별 TTS는 없으며 `[Voice Session] session=... event=end reason=... turns=... attempts=...` 로그와 명령의 최종 상태로 확인합니다. 시작은 `event=start`, 듣기 번호는 `event=turn turn=... attempt=...`, 연속 무응답은 `event=no_speech consecutive=...`입니다. `turn`은 다음 사용자 대화 번호, `attempt`는 무응답도 포함한 듣기 시도 번호입니다. 종료 이유는 `stop_phrase`, `no_speech`, `max_turns`, `error`, `cancelled`입니다. `turns`는 정상 재생을 마친 대화 턴 수입니다. 새 세션 로그에는 발언 내용이나 예외 본문을 넣지 않습니다.

새 설정은 환경변수로 덮어쓸 수 있고 `.env`를 열거나 수정할 필요가 없습니다. 기본 5턴/2회는 시연 길이를 제한하면서 한 번의 침묵 후 다시 말할 기회를 주기 위한 값입니다.

### 응답 지연 진단 로그

매 시도에 `[Voice Timing] turn=... stage=... at_ms=...` 로그를 출력합니다. `turn`은 무음 시도를 포함한 진단용 식별자이고, `at_ms`는 그 시도의 시작부터 지난 시간입니다. 구간 로그는 `status=start`와 완료 시 `status=ok/error/cancelled`, `elapsed_ms`를 남깁니다. 오류는 종류만 기록하며 새 타이밍 로그에는 대화 내용이나 키를 넣지 않습니다. 기존 `[Mic Debug]`와 사용자/도란이 대화 로그는 그대로 유지합니다.

| `stage` | 확인할 값과 의미 |
|---|---|
| `capture_config` | 실제 적용한 `silence_ms`, `candidate_silence_ms`, 후보 이후 추가 청취 시간인 `hangover_ms`, `max_utterance_ms`, `pre_roll_ms`, 시작/유지 RMS와 VAD 모드. 이전 실행 설정이 남았는지 확인 |
| `speech_start` | `wait_audio_ms`: 발화 확인까지 읽은 오디오 길이. `pre_roll_ms`: 실제 보존한 발화 앞부분 |
| `endpoint_candidate` | `status=start`, `trailing_audio_ms`: 말끝 후보 진입과 그때의 침묵 길이. `status=resumed`, `gap_audio_ms`: 후보 이후 말이 다시 감지되어 취소한 쉼의 길이. 최종 침묵이 후보 기준보다 길 때만 기록 |
| `endpoint` | `reason=silence/max_duration/no_speech`: 녹음 종료 이유. `trailing_audio_ms`: 마지막 음성 판정 뒤 연속 비음성 오디오 길이. `endpoint_wall_ms`: 마지막 음성 프레임 수신부터 종료 판정까지 실제 경과 시간 |
| `capture` | 녹음 호출 전체 시간. 종료 판정 이후 WAV 저장과 녹음 프로세스 정리도 포함 |
| `speech_validation` | 저장한 WAV의 VAD/RMS 재검증 시간 |
| `stt` | 음성 인식 요청 시간. 현재 요청의 `language=ko`도 출력 |
| `llm` | Responses 요청부터 전체 텍스트 응답을 받을 때까지의 시간 |
| `response_tts` | 응답 음성 요청부터 MP3 파일을 끝까지 내려받는 시간 |
| `response_audio_lock` | 응답 재생용 오디오 잠금을 얻기까지 기다린 `elapsed_ms` |
| `response_playback_start` | `observation=process_spawn`: `mpg123` 실행 직후 표식. `launch_ms`: 프로세스 실행 소요 시간. `from_endpoint_ms`: 종료 판정부터, `from_last_voice_ms`: 마지막 음성 프레임 수신부터 실행까지 걸린 시간 |
| `response_playback` | 응답 재생 호출 전체 시간. 말하기 시작까지의 대기와 구분 |

`endpoint`에는 저장된 WAV 길이인 `pcm_ms`(앞부분과 말끝 침묵 포함), 발화 대기까지 포함해 읽은 전체 길이인 `stream_audio_ms`, 녹음 시작부터 종료 판정까지의 `capture_wall_ms`도 나옵니다. `soft_voice_ms`는 RMS 200 이상 300 미만으로 유지한 음성 길이이며, `resumed_pauses`와 `longest_resumed_gap_ms`는 말을 다시 이어서 종료하지 않은 쉼의 횟수와 최대 길이입니다. `endpoint_candidates`는 말끝 후보 진입 횟수, `resumed_candidates`는 후보 이후 말이 이어져 후보를 취소한 횟수입니다. `no_speech`에는 해당 없는 음성/침묵 필드 대신 전체 수신 길이와 녹음 경과 시간만 기록합니다. 첫 안내에는 `prompt_tts`, `prompt_audio_lock`, `prompt_playback_start`, `prompt_playback`이, 안내 없는 후속 듣기에는 `capture_audio_lock`이 기록되므로 발화 이후 응답 지연과 구분할 수 있습니다.

먼저 `reason`이 `silence`인지 `max_duration`인지 확인합니다. 기본 `silence`의 `trailing_audio_ms`는 1800ms이고, 이 시간은 후보 확인을 포함해 중간 쉼을 보존하기 위한 대기입니다. `endpoint_wall_ms`가 크게 길면 프레임 수신 지연도 살펴봅니다. 종료 이후 오래 걸리면 같은 `turn`의 STT, LLM, 응답 TTS, 오디오 잠금 시간을 비교합니다. `from_endpoint_ms`에는 그 구간들과 파일 저장/프로세스 정리 등 사이 작업도 포함되므로 주요 구간의 합과 차이를 확인할 수 있습니다. 현재 TTS는 파일을 모두 받은 뒤 재생하므로 긴 `response_tts`는 바로 체감 응답 지연에 더해집니다.

사용자가 제공한 이전 Pi/Jabra 턴은 종료부터 플레이어 실행까지 약 5945ms였으며 STT 약 1034ms, LLM 약 2675ms, TTS 약 2043ms였습니다. 이 사례에서는 오디오 잠금보다 클라우드 단계가 지연의 대부분을 차지했습니다. 이번 변경은 숨 고르기와 긴 발화를 보존하며, 해당 클라우드 지연을 줄이는 변경은 아닙니다.

`endpoint_wall_ms`와 `from_last_voice_ms`의 기준은 **PCM 프레임 수신 시각**이며 실제 입에서 말이 끝난 시각은 아닙니다. ALSA 버퍼나 VAD 판정 차이가 있을 수 있습니다. `response_playback_start`도 **플레이어 실행 시각**이며 스피커에서 소리가 나온 시각을 측정하지 않습니다. 숫자와 실제 들리는 순간을 함께 비교해야 합니다. STT의 한국어 지정은 기존에도 적용되어 있었으며, 이번 진단만으로 우크라이나어 오인식의 원인이 확정되거나 해결된 것은 아닙니다.

## Firebase 명령 트리거

명령 작성 측은 `commands/{ELDER_ID}/{DEVICE_ID}/{고유한 commandId}`에 `type=start_ai_conversation`, `status=pending`, 숫자 Unix 밀리초 `createdAt`을 기록합니다. 선택 항목인 `commandId`, `elderId`, `deviceId`를 넣으면 경로 값과 일치해야 합니다. 명령 예시와 완료 필드는 [DB_SCHEMA.md](../docs/DB_SCHEMA.md#firebase-ai-명령)에 있습니다. 기존 대시보드 “도란이 대화 시작”이 작성하는 명령을 그대로 사용합니다.

장치는 유효한 `pending` 명령을 생성 시간, 명령 ID 순서로 한 번에 하나 처리합니다. ETag와 조건부 `PUT`으로 `processing` 상태를 확보한 뒤에만 안내/녹음을 시작합니다. 명령 한 개가 세션 전체를 감싸며 내부 턴 중에는 `processing`을 유지합니다. 세션 종료 후 결과를 한 번 확정합니다. `processed/completed`는 정상 응답이 한 번 이상 재생된 세션 또는 명시적인 종료 발언(첫 발언이어도 해당), `processed/no_speech`는 정상 응답이나 종료 발언 없이 연속 무응답 한도에 도달한 세션, `failed/failed`는 이전 턴의 성공 여부와 관계없이 세션 중 발생한 예외입니다. 세부 종료 이유는 `[Voice Session]` 로그에 남기고 기존 Firebase 결과 필드 형식은 유지합니다. 새 원격 종료 명령은 추가하지 않습니다.

명령 조회/조건부 쓰기에는 각각 10초 제한 시간이 적용됩니다. 완료 기록 전송이 실패하면 다음 순회에서 기록만 재시도하고 같은 음성을 다시 실행하지 않습니다. 확인되지 않은 claim과 완료 기록의 재시도 정보는 프로세스 메모리에만 있습니다. 처리 중 종료되면 Firebase에 `processing`이 남아 재시작 후 자동 실행되지 않을 수 있습니다. 따라서 이 방식은 명령당 최대 한 번의 실행 시도를 위한 것이며 완료를 보장하지 않습니다. 명령 ID와 상태를 재사용/초기화하지 말고, 실제 결과 확인 후 별도 ID로 다시 요청합니다. `pending` 명령에는 만료 시간이 없으므로 오래된 명령도 처리 대상입니다. 잘못된 형식/대상 명령은 수정하지 않고 건너뜁니다.

## 동시 실행과 실패 처리

AI 대화, Firebase 대화 기록 전송, 기존 허브 루프는 별도 작업입니다. AI의 STT/응답/TTS 네트워크 요청과 대화 기록 전송은 허브의 일정/이벤트 검사를 기다리게 하지 않습니다. 기존 허브 자체의 Firebase 요청과 안내 생성은 기존 흐름을 유지합니다.

녹음과 모든 음성 재생은 같은 잠금으로 겹치지 않게 합니다. AI 녹음/재생 중 안내 시간이 되면 해당 오디오 작업 종료까지 안내가 지연될 수 있고, 그동안 허브의 다음 검사도 지연될 수 있습니다. AI 재생은 기본 최대 45초입니다. 연속 세션 전체를 잠그지 않으며 턴 사이와 STT/응답/TTS 처리 중에는 잠금을 놓습니다. 우선순위 선점이나 재생 중 사용자 발화 감지는 지원하지 않습니다. 따라서 즉시 후속 듣기를 시작하기 전에 기존 복약/안부 안내가 재생될 수 있고, 허브 안내가 잠금을 기다리는 동안 검사 지연도 남습니다. 새 Firebase 명령 조회와 선택형 웨이크워드 감지는 현재 세션이 끝난 후 재개합니다.

인식 결과가 비어 있으면 터미널에서는 다음 입력을 기다리고, Firebase 세션에서는 무응답 한도 안에서 다시 듣습니다. 안내, 녹음, OpenAI 요청, 재생 실패 후에는 다음 Enter 또는 새 Firebase 명령으로 재시도할 수 있습니다. 안내 실패/빈 인식 결과에는 대화 메시지가 생기지 않습니다. 종료 제어 표현을 제외한 인식된 사용자 발언은 기록합니다. 완성된 AI 응답은 음성 생성/재생 실패 시에도 `audioStatus=failed`로 기록하며 중단은 `cancelled`로 기록합니다. 성공적으로 재생한 대화 쌍만 다음 요청의 기억에 포함합니다. AI 설정/키/의존성 문제로 시작에 실패해도 기존 `hub`는 계속 실행합니다.

## Firebase 기록

메시지마다 다음 경로를 하나의 원자적 다중 경로 `PATCH`로 갱신합니다. 상세 필드는 [DB_SCHEMA.md](../docs/DB_SCHEMA.md)를 참고하세요.

- `conversations/{ELDER_ID}/{sessionId}/messages/{messageId}`: 발언, 역할, 턴 번호, 오디오 상태
- `conversations/{ELDER_ID}/{sessionId}`: 장치/어르신 ID 및 세션 생성/수정 시간
- `events/{ELDER_ID}/pi_ai_conversation_message_{messageId}`: `ai_conversation_message` 이벤트. `sessionId`, `messageId`, `role`로 원본 메시지 연결
- `devices/{DEVICE_ID}/lastSeen`, `lastSignal`: 전송 시 Firebase 서버 시각(Unix 밀리초)

전송 전에 `conversation_outbox/`에 기록을 저장합니다. 별도 작업이 한 번에 최대 5개를 순서대로 처리하고 각 순회 후 2초 대기합니다. 먼저 연결 이벤트를 `GET`으로 조회해 동일 메시지가 이미 전송됐으면 outbox만 제거하고, 없으면 `PATCH`합니다. 응답 유실 후 재시도해도 중복 이벤트를 만들거나 보호자가 해결한 이벤트를 다시 열지 않습니다. 이벤트 충돌, 조회/쓰기 실패 시 첫 실패에서 멈춰 다음 순회에서 재시도합니다. Firebase 읽기와 쓰기 권한이 모두 필요합니다. HTTP 제한 시간은 GET/PATCH 각각 10초이며, **최대 5회 재시도 제한이 아니라 순회당 최대 5개**입니다.

미전송 기록은 재시작 후 AI 대화를 다시 활성화하면 재전송합니다. `hub`의 터미널 방식에서 `q`로 대화를 끝내도 전송 작업은 유지됩니다. 로컬 저장 공간 부족 등으로 outbox 저장 자체가 실패하면 경고를 출력하며 해당 기록의 보존은 보장하지 못합니다. outbox는 대화 텍스트를 포함하므로 개인 데이터로 관리하며 저장소에 올리지 않습니다. AI 입력 WAV, “말씀해주세요.” MP3, 응답 MP3는 각 턴의 성공/실패와 관계없이 삭제를 시도하고 삭제 실패는 로그에 남깁니다. 강제 종료나 진행 중인 API 요청을 남긴 채 프로세스가 종료되면 임시 파일이 남을 수 있습니다. 기존 안내 MP3 파일은 유지합니다.

## 검증

### 연속 대화 배포와 Jabra 재검증

실행에 필요한 교체 파일은 `raspberry-pi-voice-hub/voice_ai.py` 하나입니다. 문서와 테스트도 맞추려면 이 `README.md`, `docs/DB_SCHEMA.md`, `tests/test_voice_controllers.py`, `tests/test_voice_sessions.py`, `tests/test_firebase_voice_sessions.py`를 추가로 복사합니다. `requirements.txt` 변경이나 새 의존성은 없으며 기존 `webrtcvad-wheels==2.0.14`를 그대로 사용합니다. 아래 명령은 사용자가 Pi에서 재검증할 때 실행하며, 문서를 작성한 데스크탑에서 실제 복사나 배포를 수행했다는 뜻은 아닙니다.

먼저 **Pi 터미널**(`david@doran-pi:... $`)에서 서비스를 중지합니다. 이미 수동 실행 중이면 그 터미널에서 `Ctrl+C`로 종료합니다.

```bash
systemctl --user stop doran-voice-hub.service
```

**Windows PowerShell**(`PS C:\...>`)에서 런타임 파일을 복사합니다.

```powershell
Set-Location C:\doran-doran-care-hub
scp .\raspberry-pi-voice-hub\voice_ai.py pi-user@PI_HOST:/home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub/voice_ai.py
```

다시 **Pi 터미널**에서 Firebase 허브를 수동 실행합니다. 이번 실행에 세션 상한과 발화 설정을 명시하여 이전 설정이 남아 있어도 재검증 값이 적용되게 합니다. `.env`를 열거나 수정할 필요가 없습니다. 웨이크워드는 끄고 서비스와 수동 실행을 동시에 켜지 않습니다.

```bash
cd /home/pi-user/doran-doran-care-hub/raspberry-pi-voice-hub
REMINDER_MODE=hub \
AI_CONVERSATION_ENABLED=true \
AI_CONVERSATION_TRIGGER=firebase \
AI_WAKE_WORD_ENABLED=false \
AI_SESSION_MAX_TURNS=5 \
AI_SESSION_MAX_NO_SPEECH=2 \
AI_SPEECH_START_TIMEOUT_SECONDS=4 \
AI_SPEECH_SILENCE_SECONDS=1.8 \
AI_MAX_UTTERANCE_SECONDS=20 \
.venv/bin/python -B main.py
```

대시보드의 “도란이 대화 시작”을 세션마다 한 번만 누르고, 첫 안내 뒤 다음 항목을 확인합니다. 같은 세션에서는 답변이 끝나면 다음 말을 이어갑니다. 첫 턴의 `capture_config`에서 `silence_ms=1800`, `candidate_silence_ms=1350`, `hangover_ms=450`, `max_utterance_ms=20010`, `pre_roll_ms=300`도 확인합니다.

1. **무음:** 말하지 않으면 각 약 4초 대기 두 번 뒤 `[Voice Session] event=end reason=no_speech`가 나오고 STT/AI 응답과 새 대화 메시지가 생기지 않는지 확인합니다. Firebase 결과는 `processed/no_speech`입니다.
2. **숨 고르기와 첫 음절:** “오늘은 학교에 가서 팀원들과 센서 테스트 결과를 확인한 다음” 뒤 약 1.0초, 다음 턴에서는 약 1.2초, 그다음 턴에서는 약 1.5초 쉬고 “대시보드 문제도 함께 수정해 보려고 해요”를 이어 말합니다. 첫 음절과 쉼 뒤 내용이 한 인식 결과에 들어가고 `resumed_pauses`와 `longest_resumed_gap_ms`가 남는지 확인합니다. VAD 판정상 쉼이 1.35초를 넘긴 턴에는 `endpoint_candidate status=start` 뒤 `status=resumed`와 `resumed_candidates` 증가도 확인합니다. 말이 끝나면 마지막 후보 뒤 `reason=silence`, 기본 `trailing_audio_ms=1800`을 확인합니다. 종료부터 소리가 들리기까지 오래 걸리면 같은 `turn`의 STT/LLM/TTS/잠금 시간과 `from_endpoint_ms`를 함께 봅니다.
3. **긴 문장:** 발화 시작부터 약 18초 동안 앞뒤 내용이 다른 문장을 말한 뒤 멈춥니다. 15초 이후 부분을 포함해 처음과 끝이 모두 인식되는지 확인합니다. 20초 안에 말끝 침묵 1.8초까지 들어가려면 시작 후 18.2초보다 일찍 말을 마쳐야 합니다. 별도 턴에서는 20초 넘게 계속 말해 약 20초에 `reason=max_duration`으로 종료되는지 확인합니다. 최대 길이 종료는 자연스러운 말끝 판정과 구분합니다.
4. **종료/복구:** 새 턴의 발화 대기 중 또는 말하는 중 `Ctrl+C`로 종료하고 같은 명령으로 다시 시작합니다. 마이크를 다시 쓸 수 있고 다음 Firebase 명령을 정상 처리하는지 확인합니다. 테스트 후 수동 실행은 `Ctrl+C`로 종료합니다.

5. **연속 대화/기억:** 한 번 클릭한 뒤 “오늘 뉴스 봤어요” → 응답 → “무슨 뉴스를 봤다고 했지요?”처럼 2~3턴을 이어갑니다. 두 번째부터 안내 음성이 없고, 이전 발언을 기억하며 동일 `sessionId`의 `turnIndex`가 증가하는지 확인합니다. 내부 턴 동안 명령은 `processing`, 세션 종료 후에만 `processed`인지 확인합니다.
6. **종료 표현:** 별도 세션에서 `그만`, `대화 종료해 주세요`, `이제 됐어요`, `여기까지 할게요`를 말합니다. `reason=stop_phrase`, `processed/completed`이고 그 문장에 대한 LLM 응답·메시지가 없는지 확인합니다. `여기까지 걸어왔어요`는 대화가 이어져야 합니다.
7. **상한/다음 명령:** 5턴까지 이야기하면 마지막 응답 후 `reason=max_turns`로 마치는지 확인합니다. 다음 클릭은 다른 `sessionId`, 비어 있는 단기 history로 시작해야 합니다. Persistent Memory의 관련 사실은 계속 참고할 수 있습니다. 이전 명령은 다시 실행되지 않아야 합니다.
8. **허브 동시 동작:** 대화 중 복약 예정 시각과 새 미활동 이벤트가 처리되는지 확인합니다. 오디오 잠금 대기는 허용하되 세션이 끝나야만 허브 루프가 재개되는 구조가 아닌지 확인합니다. 실시간 오디오와 응답 지연은 실제 Pi/Jabra 청취로 판단합니다.

### 자동 테스트와 기존 통합 검증

저장소 루트에서 실행하는 로컬 자동 테스트는 실제 API/Firebase/오디오 장치 대신 테스트 대역을 사용합니다.

```bash
cd ~/doran-doran-care-hub
source raspberry-pi-voice-hub/.venv/bin/activate
python -m unittest discover -s tests -v
```

Pi에서 다음 항목을 확인해야 통합 검증이 완료됩니다. **이번 데스크탑 복구에서는 아래 실기 검증을 수행하지 않습니다.** 웨이크워드는 계속 끄고(`AI_WAKE_WORD_ENABLED=false`) 다음 단계에서 검증합니다.

1. `chat`에서 Enter로 “오늘 TV 봤어” → “뉴스를 봤어” → “내가 아까 뭘 봤다고 했지?” 3턴을 말합니다. “말씀해주세요.”가 끝난 뒤 녹음되는지, Jabra 입력, 한국어 인식, 이전 말 기억, 음색/속도, 응답 종료 후 재녹음을 확인합니다.
2. `n` 이후 새 세션 ID와 단기 history 초기화를 확인합니다. Persistent Memory는 유지합니다. 300초 유휴 후 다음 발화, 프로그램 재시작 후에도 새 세션인지 확인합니다.
3. `hub` + AI를 실행하고 `MEDICATION_TIMES`를 가까운 시간으로 지정합니다. 대화 중 복약 안내가 한 번 실행되고 `medication_reminder_sent`가 기록되는지 확인합니다. AI 재생 중 예정 시각이 되면 재생 종료 후 안내되는지 확인합니다.
4. 같은 실행 중 현재 시각의 `createdAt`(숫자 Unix 밀리초), `type=long_inactivity`, `status=open`인 테스트 이벤트를 생성합니다. 안부 안내와 `inactivity_voice_check_sent.sourceEventId`가 원본을 가리키고 중복 안내되지 않는지 확인합니다. `q` 이후에도 일정/감시가 작동하는지 확인합니다.
5. Firebase 콘솔에서 정상 3턴의 사용자/AI 메시지 6개와 연결 이벤트 6개를 확인합니다. 이벤트의 `sessionId`, `messageId`, `role`을 대조하고 사용자 `recorded`, AI `played`, 증가하는 `turnIndex`를 확인합니다. 임시 AI 오디오 파일도 삭제됐는지 확인합니다.
6. 테스트용 Firebase에서 일시적인 쓰기 실패를 만들고 대화가 계속되는지 확인합니다. outbox가 남고 복구 후 동일 ID로 전송되며 비워지는지 확인합니다. 미전송 상태에서 재시작한 경우에도 AI 재활성화 후 재전송되는지 확인합니다.
7. 실행 환경에서만 `OPENAI_API_KEY=invalid`를 지정하거나 Jabra를 분리해 실패 시 허브가 살아 있는지 확인합니다. `AI_CONVERSATION_ENABLED=false`인 `hub`와 기존 `demo`/`schedule`/`watch`도 각각 실행해 OpenAI 없이 안내/이벤트 기능을 확인합니다. 실제 `.env`의 키는 바꿀 필요가 없습니다.
8. 테스트용 Firebase에서 명령 트리거의 정상/빈 인식/실패 결과와 ETag 조건부 쓰기를 확인합니다. 같은 명령이 다음 순회와 재시작 후 재생되지 않는지, 완료 쓰기 실패 시 음성 없이 기록만 재시도하는지, `processing` 상태에서 종료한 명령이 자동 재실행되지 않는지 확인합니다. `auto`의 TTY/비TTY 선택도 확인합니다.
