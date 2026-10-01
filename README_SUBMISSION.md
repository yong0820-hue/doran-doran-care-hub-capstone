# 교수님 제출용 코드 안내 — 도란도란 케어 허브

작성 기준: 2026-10-01, 집 데스크탑의 `C:\doran-doran-care-hub` 현재 파일.

## 1. 이 폴더는 무엇인가요?

어르신과 대화하는 AI 기기와 보호자 화면을 만드는 프로젝트의 **개발 중 코드 제출본**입니다. 원본의 현재 코드와 테스트를 골라 복사했고 아직 커밋하지 않은 코드 변경도 반영했습니다. 폴더를 열어 코드를 읽을 때는 실제 계정이나 API 키가 필요하지 않습니다.

이 폴더는 **제출용 정리본이며 원본 개발 이력과 분리된 안전한 Git 저장소**입니다. 원본 `.git`을 복사하지 않고, 정리한 현재 코드와 설명 문서만 새 저장소의 첫 커밋으로 기록했습니다. 제출 폴더와 ZIP 모두 새 `.git`을 포함합니다. 원본 Git 이력을 고치거나 지우지 않았습니다.

원본 파일 13,625개의 내용·크기·수정 시간을 작업 전후 비교했고 변경·추가·삭제가 없었습니다. 원본의 HEAD와 미커밋/미추적 상태도 그대로였습니다. 원본에는 reset/checkout/stash/clean/add/commit/push를 실행하지 않았습니다. GitHub 업로드나 public 공개도 하지 않았습니다.

## 2. 현재 구현된 것과 남은 일

| 구분 | 쉽게 보는 현재 상태 |
|---|---|
| Raspberry Pi AI 음성 대화 | 한국어 음성 녹음 → 인식 → 대화 답변 → 음성 출력 코드가 있습니다. 실물 환경에서 계속 검증하는 단계입니다. |
| Persistent Memory | 대화에서 필요한 내용을 저장·회상하고 정정·초기화하는 코드와 테스트가 있습니다. 사용자 기억이나 실제 대화 기록은 제출하지 않았습니다. |
| Guardian dashboard | 보호자가 기기 상태·이벤트·복약 일정을 보고 대화/기억 관련 작업을 요청하는 화면과 Firebase 연결 코드가 있습니다. |
| 복약 care | 일정에 맞춰 안내하고 음성 답변을 확인합니다. 약을 실제로 복용했는지는 기기가 직접 증명하지 않으며, 명확한 응답을 기록하는 방식입니다. |
| 장시간 미활동 care | 미활동 이벤트를 받아 안부를 묻고 응답을 기록하는 코드가 있습니다. 외부 신고/전화까지 완성된 서비스는 아닙니다. |
| Jabra 입출력·터치 Display | Jabra용 녹음/재생 경로와 터치 화면 코드가 있습니다. 이번 제출 정리에서 장치를 직접 연결해 확인하지 않았습니다. |
| Firebase Auth/Rules | 기기 인증·보호자 로그인/권한 경계 코드와 격리 시험용 Rules/설정 예제가 있습니다. 실제 운영에 적용된 인증·규칙은 별도 검증 대상입니다. |

**개발 예정 또는 미완성:** 추억사진 기능, **60GHz TI IWR6843AOPEVM** 레이더 연동과 레이더 기반 인지게임, 실제 라디오 방송 청취/채널 조작의 최종 동작, Pi 화면·대시보드 UI/UX 개편, 디자인팀 외형과 최종 통합.

`firmware/esp32-sensors/`는 **초기 24GHz/GPIO 센서 코드 참고 자료**입니다. 현재 60GHz 보드 연동 완료 코드가 아닙니다. 터치 방식 메모리게임과 라디오 프리셋/화면 코드가 있어도 위 레이더 인지게임·실제 방송 기능의 완성을 뜻하지 않습니다. 현재 제출본의 라디오 프리셋은 빈 예시 목록입니다. 웨이크워드는 기본 비활성화이며 추가 검증이 남아 있습니다.

기존 설계·검토 문서에는 작성 당시의 계획과 검사 기록이 남아 있습니다. 현재 상태는 이 문서와 `docs/ROADMAP.md`를 우선해 주세요. 예전 검사 수치나 실물 점검 명령을 이번 제출 검증 결과로 해석하지 않습니다.

## 3. 무엇을 남겼나요?

| 위치 | 남긴 이유 |
|---|---|
| `raspberry-pi-voice-hub/` | 음성·기억·돌봄 처리의 핵심 Python 코드, 터치 화면, 의존성 목록과 실행 설명 |
| `apps/guardian-dashboard/` | 보호자 화면 소스·기존 테스트·package.json/package-lock.json. 설치한 라이브러리 폴더는 제외 |
| `firmware/esp32-sensors/` | 초기 센서/응급 버튼 동작을 이해하는 참고 소스와 비밀 없는 config.example.h |
| `tests/` | 기존 Python/Pi 화면 테스트와 합성 데이터 도우미. 가짜 계정·응답을 사용한 테스트용 문자열은 보존 |
| `docs/` | Firebase 데이터 구조, 초기 설계 참고와 Persistent Memory 구현·검토 자료 |
| `scripts/` | 기존 로컬 설정 진단과 Pi 화면 실행 도우미 |
| `deployment-prep/persistent-memory-20260928-223050/` | 기존 테스트가 참조하는 격리 도구, Auth/Rules 공개 예제, 실물 확인 항목, 제출용 실행 안내 |

배포 폴더 전체를 복사하지 않았습니다. 현재 제출 코드에 맞는 격리 runtime 해시 목록만 다시 만들었고, 과거 검사 로그/상태/빌드와 오래된 manifest는 제외했습니다. 자세한 파일 목록과 설명 문서 변경 내역은 **[SUBMISSION_CONTENTS.md](SUBMISSION_CONTENTS.md)**를 보세요.

## 4. 무엇을 뺐나요?

- 실제 설정: `apps/guardian-dashboard/.env`, `firmware/esp32-sensors/esp32_sensors/config.h`와 실제 credential/키 파일.
- 기존 설치/빌드: `node_modules`, `dist`, 배포 점검용 빌드, Python bytecode와 각종 캐시.
- 중복/개발 자료: `doran-transfer`, 개인 편집기·AI 도구 설정, 원본 `.git`, 개발용 CLAUDE.md.
- 배포 점검 산출물: logs, 실행 state/결과 JSON, 과거 hash/manifest, 운영 주소가 있는 오래된 진단 문서.
- 실제 사용자 데이터·음성·사진·대화/기억 outbox/state는 제출 대상에서 제외하며, 새로 만들거나 기존 것을 복사하지 않았습니다. 이런 파일이 원본에 없더라도 `.gitignore`로 재유입을 막습니다.

원본의 실제 `.env`, config.h, 빌드 등은 **삭제하지 않고 그대로 보존**했습니다. Firebase의 실제 설정값이 `.env`와 기존 `dist` 빌드 파일에서도 발견되어 둘 다 제외했습니다. 비밀값 자체는 검사 보고에 기록하거나 출력하지 않았습니다. 새 제출본에는 소스에서 확인한 설정 이름, 빈 비밀값과 공개 기본값만 담은 `.env.example`들을 만들었습니다.

## 5. 교수님이 코드를 보는 순서

1. **전체 서비스와 데이터:** 루트 README.md → 이 문서 → `docs/DB_SCHEMA.md`.
2. **기기 동작:** `raspberry-pi-voice-hub/main.py` → `voice_ai.py`. 복약은 `medication_schedule.py`/`medication_care.py`, 미활동은 `inactivity_care.py`를 봅니다.
3. **대화 기억:** `persistent_memory.py` → `memory_storage.py`, `memory_reset.py`, `memory_dispatch.py`, `memory_runtime.py`.
4. **화면:** Pi는 `display/`와 `display_server.py`; 보호자는 `apps/guardian-dashboard/src/App.jsx`, `hooks/useDashboardData.js`, `services/firebase.js`.
5. **인증/권한과 테스트:** `firebase_auth.py`, Guardian `services/guardianAuth.js`, 배포 폴더의 `firebase-isolated.rules.json`, `tests/`와 Guardian `test/`.

## 6. 실행 설정은 어떻게 준비하나요?

코드 검토에는 실제 값을 채울 필요가 없습니다. 루트 `.env.example`은 설정 위치 안내이며 두 앱이 읽는 공용 설정 파일은 아닙니다.

- Pi: `raspberry-pi-voice-hub/.env.example`과 README.md. 실제 음성 실행에는 Raspberry Pi/Linux, Jabra·오디오 프로그램과 Python 의존성이 필요합니다.
- 보호자 화면: `apps/guardian-dashboard/.env.example`과 README.md. 해당 폴더에서 `npm ci`, `npm run dev`로 로컬 화면을 준비할 수 있습니다. Firebase 값을 비워 두면 예시 데이터 화면입니다.
- ESP32: `config.example.h`를 로컬 config.h로 복사하는 초기 참고 구성입니다. 60GHz 보드 연결 설정은 아직 완성되지 않았습니다.
- Auth: 배포 폴더의 `firebase-device-credential.example.json`과 `firebase-access.example.json`. 실제 계정 비밀번호/토큰/키는 예제나 Git에 저장하지 않습니다.

서비스 시작과 실제 Firebase 화면은 데이터를 쓸 수 있습니다. 별도로 마련한 시험 환경 없이 운영 실행을 코드 검사로 사용하지 않습니다. 설정 예제는 완성된 운영 배포 설정이 아닙니다. 이번 정리에서는 실제 Firebase/Pi/OpenAI 접근이나 패키지 설치를 하지 않았습니다.

## 7. Git 이력을 새로 만든 이유와 검사 범위

원본의 모든 참조 기준 커밋 34개와 Git blob 473개(참조되지 않는 객체 포함)를 읽기 전용으로 패턴 검사했습니다. 검사 보고는 파일 경로·패턴·객체/커밋 식별자 수준으로만 다뤘습니다. 토큰/비밀번호처럼 보이는 테스트 fixture와 dependency 이름도 검사에 걸립니다. 이것을 실제 비밀정보 유출로 단정하지 않았습니다.

패턴 검사만으로 과거 모든 내용과 Git 메타데이터의 안전성을 확정할 수 없어 **원본 이력을 복사하지 않는 방식**을 선택했습니다. 새 `.git`에는 정리본 첫 커밋만 있고 원본 remote/credential/config/reflog는 가져오지 않았습니다. 원본 개발 이력을 제출한 것으로 주장하지 않습니다. 교수님이 원본 변경 이력 자체를 필수로 요구한다면 이 차이를 별도로 설명해야 합니다.

제출본의 파일명/내용, 원본 로컬 credential과의 동일 값 여부, 새 Git에 들어간 객체를 다시 검사합니다. 실제 비밀값·금지 파일은 포함하지 않습니다. 테스트용 가짜 URL/계정/claim token과 `js-tokens` dependency 버전 문자열은 문맥을 확인해 보존했습니다. 자동 검사가 모든 형태의 비밀/개인정보를 수학적으로 보증한다는 뜻은 아닙니다.

## 8. 이번 제출본 검사 결과

| 검사 | 결과 |
|---|---|
| 원본 보존 | 파일 13,625개 내용(SHA-256)·크기·수정 시간, 원본 HEAD와 Git 상태 변화 없음 |
| Python 구문 | **81개 파일 통과**. import/서비스 실행 없이 메모리에서 compile했고 캐시 파일을 만들지 않음 |
| Python 기존 targeted tests | **86개 통과**, 실패/오류/skip 0. 화면 상태·라디오 설정·대화 조정·복약/미활동 응답·일정 검증·Persistent Memory |
| Pi 화면 기존 JS tests | **65개 통과**, 실패/skip 0. 화면 동작과 터치 메모리게임 |
| Guardian 기존 targeted JS tests | **57개 통과**, 실패/skip 0. 대시보드 상태·복약 일정·기억 초기화 요청/동시성/완료 확인 |
| 외부 접근 | 테스트에서 실제 네트워크를 차단하고 합성 데이터/가짜 연결 사용. Python 외부 프로세스도 차단 |
| 제출본 Git | 새 첫 커밋과 .git 포함. 최종 확인에서 `git status --short` 출력이 비어 있는 clean 상태로 제출 |
| 비밀/불필요 파일 | 현재 파일과 새 Git 객체 재검사 후 제출. 실제 .env/config.h·실행 데이터·설치/빌드 폴더 제외 |

Python 테스트 대상: `test_display_state`, `test_radio_presets`, `test_interaction_manager`, `test_medication_care.MedicationReplyTests`, `test_inactivity_care.InactivityReplyTests`, `test_medication_schedule.MedicationScheduleValidationTests`, `test_persistent_memory`.

JS 테스트 대상: `tests/memory_game.test.js`, `tests/display_browser.test.js`; Guardian의 `dashboardState`, `medicationSchedule`, `memoryResetOperations`, `memoryResetCoordinator`, `memoryResetProtocol` 테스트.

초기 Python 검사 도구의 외부 프로세스 차단 설정이 Windows 표준 라이브러리 import와 충돌했습니다. 검사 도구를 수정한 후 위 86개 테스트를 정상 실행했습니다. 제품 소스를 수정해 테스트를 통과시킨 것은 아닙니다.

**이번에 확인하지 않은 것:** 전체 Python/Guardian 테스트, Guardian 전체 빌드, 실제 Firebase Auth/Rules·클라우드 동작, OpenAI 실제 응답, Jabra 녹음/재생, Pi 터치 실물, 60GHz 레이더, 제품 외형 통합. 필요한 third-party 라이브러리는 제출본에 넣거나 새로 설치하지 않았습니다. 위 통과 결과를 전체 제품 완성/실물 검증 완료로 표현하지 않습니다.

## 9. 제출 전에 사용자가 마지막으로 확인할 것 — 3개

1. 교수님이 정한 개인폴더 이름(이름·학번 등)에 맞춰 부모 폴더 이름을 정하고, 그 아래 코드 폴더 전체를 넣습니다. 숨김 .git도 이미 ZIP에 포함됩니다.
2. 교수님께 **현재 정리본의 새 Git 첫 커밋**이며 원본 개발 이력은 별도 보존했다는 점을 알립니다. 원본 이력 필수 여부나 GitHub public 제출이 별도 필수인지 확인합니다. 이번에는 public 공개하지 않았습니다.
3. 이 문서의 구현·미완성·미검증 설명이 발표/제출 설명과 맞는지 읽고, 실제 키·계정·사용자 데이터를 추가하지 않은 제공 ZIP을 제출합니다.
