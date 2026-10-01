
# 제출본에 남긴 배포·Auth/Rules 참고 자료

원본 deployment-prep 전체가 아니라 제품 이해와 기존 테스트에 필요한 도구·공개 예제만 남겼습니다. 실제 credential, OpenAI 키 파일, 실행 profile, outbox/state, 빌드, 로그와 과거 검사 결과는 없습니다.

- `isolated_pi.py`, `isolated_http_smoke.py`: 별도 테스트 Pi 환경을 준비·확인하는 기존 도구
- `isolated_guardian_build.mjs`: 별도 Guardian 테스트 빌드 도구
- `firebase-isolated.rules.json`: 격리 테스트 환경용 Rules 기준 자료. 실제 운영 Rules를 내려받은 파일이 아닙니다.
- `firebase-access.example.json`, `firebase-device-credential.example.json`, `isolated-test.example.json`: 실제 값이 없는 예제
- `firebase.emulator.json`, `firebase_emulator_*`, `run_auth_emulator_checks.py`: 기존 emulator 검사 도구. 이번 제출 정리에서는 실행하지 않았습니다.
- `PHYSICAL-CHECKLIST.md`: 기존 실물 확인 항목. 완료 증명이 아닙니다.
- `isolated-runtime.sha256`: 현재 제출본 Pi payload의 SHA-256 목록. 원본의 오래된 manifest는 그대로 복사하지 않았습니다.

실행 절차와 주의 범위는 [DEPLOY-RUNBOOK.md](DEPLOY-RUNBOOK.md)를 보세요. 루트의 tests/test_isolated_auth_launch.py가 이 폴더의 launcher와 공개 예제를 참조하므로 폴더 이름을 유지했습니다.
