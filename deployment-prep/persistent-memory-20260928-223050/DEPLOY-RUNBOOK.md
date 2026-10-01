
# 교수님 제출본 실행·통합 참고

이 문서는 제출본을 읽고 별도 시험 환경을 준비할 때 쓰는 안내입니다. 이번 제출 정리에서는 실제 배포를 하지 않았습니다.

1. 코드 검토는 루트 README_SUBMISSION.md부터 시작합니다. Pi는 main.py → voice_ai.py → persistent_memory.py, 보호자 화면은 App.jsx → useDashboardData.js → services/firebase.js 순서로 볼 수 있습니다.
2. 로컬 화면 확인은 Guardian README를 따릅니다. 실제 Firebase 값을 비워 두고 예시 데이터로 화면을 볼 수 있습니다.
3. 실제 통합 시험은 운영과 다른 Firebase 프로젝트와 비어 있는 별도 Pi 작업 폴더에서 준비합니다. 장치 ID만 바꾸어도 기존 기억/대화 데이터가 완전히 분리되는 것은 아닙니다.
4. 각 .env.example, config.example.h, 이 폴더의 *.example.json에 필요한 설정 이름이 있습니다. 실제 키와 기기 계정 파일은 로컬에만 준비하고 제출·커밋하지 않습니다. 예제 파일 자체에 실제 값을 채우지 않습니다.
5. isolated_pi.py의 `--help`로 기존 명령을 확인합니다. `--dry-run`은 오프라인 준비 확인입니다. 실제 auth-check/start와 HTTP smoke는 별도로 준비된 시험 환경에서 수행하는 작업이며 이번 제출 검사에 포함되지 않았습니다.
6. Firebase Auth의 계정별 범위, Rules와 인덱스가 시험 환경에서 맞는지 확인합니다. firebase-isolated.rules.json은 참고용 기준이며 운영에 자동 적용하지 않습니다. 권한 문제를 공개 읽기/쓰기로 해결하지 않습니다.
7. 실제 서비스 시작은 대화/기억 재전송과 복약/미활동 이벤트 쓰기를 일으킬 수 있습니다. 운영 outbox·기억·사용자 데이터를 시험 폴더로 복사하지 않습니다. 실제 기기/클라우드 검증은 PHYSICAL-CHECKLIST.md를 참고하여 별도로 기록합니다.

현재 snapshot의 isolated-runtime.sha256는 제출본 코드만 검증합니다. 실제 Pi 설치나 운영 배포가 일치한다는 뜻이 아닙니다. Raspberry Pi/Jabra·디스플레이·60GHz 레이더·외형 통합은 각각 실물 검증이 필요합니다.
