
# 도란도란 케어 허브 — 교수님 제출용 코드

혼자 생활하는 어르신을 위해 AI 음성 대화와 일상 돌봄을 제공하고, 보호자가 상태를 확인하는 서비스의 개발 중 코드입니다.

먼저 **[README_SUBMISSION.md](README_SUBMISSION.md)**를 읽어 주세요. 현재 구현 범위, 미완성 기능, 코드 읽는 순서, 포함·제외 내역과 이번 검사 결과를 정리했습니다.

이 폴더는 **제출용 정리본이며 원본 개발 이력과 분리된 안전한 Git 저장소**입니다. 원본의 현재 파일을 기준으로 만들었으며 미커밋 코드도 포함했습니다. 실제 API 키, 기기 비밀번호, 사용자 기록과 원본 `.git`은 포함하지 않았습니다.

## 코드와 실행 안내

| 위치 | 내용 |
|---|---|
| `raspberry-pi-voice-hub/` | Raspberry Pi 음성 대화·기억·복약/미활동 care·터치 화면 |
| `apps/guardian-dashboard/` | React/Vite 보호자 대시보드 |
| `firmware/esp32-sensors/` | 초기 ESP32 센서 코드 참고 자료. 60GHz 레이더 연동 완료 코드가 아닙니다. |
| `docs/DB_SCHEMA.md` | Firebase 데이터 구조와 기능 설계 |
| `deployment-prep/` | 비밀 없는 Auth/Rules 예제·격리 도구·실물 확인 항목 |
| `tests/` | 합성 데이터와 가짜 외부 연결을 사용하는 기존 테스트 |

대시보드는 `apps/guardian-dashboard`에서 `npm ci`, `npm run dev`로 로컬 화면을 볼 수 있습니다. Firebase 설정을 비워 두면 예시 데이터 화면을 사용합니다. 이 제출본에서는 패키지 설치나 서버 실행을 하지 않았습니다.

Pi 실행에는 Linux/Raspberry Pi, Jabra 입출력 장치, 오디오 프로그램과 Python 의존성이 필요합니다. 자세한 설명은 [Pi README](raspberry-pi-voice-hub/README.md)를 참고하세요. 실제 서비스 시작에는 별도의 설정이 필요하며 Firebase에 데이터를 쓸 수 있습니다. 단순 코드 검토에는 실행이나 실제 계정이 필요하지 않습니다.

설정 이름은 루트 `.env.example`과 각 구성요소의 예제에서 확인하세요. 실제 `.env`, `config.h`, Auth 계정 파일은 각자 로컬에서 준비하며 제출하거나 Git에 추가하지 않습니다. `VITE_`로 시작하는 값은 브라우저 빌드에 포함되므로 기기 비밀번호·OpenAI 키·서비스 계정 키를 넣지 않습니다.

이번 제출 정리는 Pi·Firebase·OpenAI 접속, GitHub 업로드 및 공개를 수행하지 않았습니다. 공개 저장소를 제출하도록 요구받았다면 공개 여부와 제출 방법은 별도 확인이 필요합니다.
