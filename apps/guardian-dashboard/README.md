
# Guardian Dashboard

보호자가 어르신의 기기·일상 상태, 복약 일정과 이벤트를 확인하는 React/Vite 화면입니다. Firebase 연동 코드와 격리 환경용 로그인·권한 확인 코드가 있습니다. 실제 서버에 적용된 권한은 이번 제출 검사에서 확인하지 않았습니다.

## 로컬 화면

이 폴더에서 `npm ci` 후 `npm run dev`를 실행하고 `http://localhost:5173`을 엽니다. Firebase 설정을 비워 두면 예시 데이터 화면으로 동작합니다. 합성 예시 데이터는 `src/data/mockData.js`에 있습니다.

실제 연결을 준비할 때만 `.env.example`을 `.env`로 복사하여 별도 테스트 프로젝트의 설정을 입력합니다. Vite 환경변수는 빌드 시 포함됩니다. OpenAI 키·기기 비밀번호·서비스 계정 키를 `VITE_` 변수에 넣지 않습니다. Firebase 접근은 Auth와 Rules로 제어합니다.

## 읽을 곳

- `src/App.jsx`: 전체 화면과 인증 경계
- `src/hooks/useDashboardData.js`: 상태 수신과 화면 동작
- `src/services/firebase.js`, `src/services/guardianAuth.js`: Firebase 및 인증 처리
- `src/medicationSchedule.js`, `src/components/MedicationScheduleManager.jsx`: 복약 일정
- `src/memoryReset.js`: 기억 초기화 요청과 완료 확인
- `test/`: 기존 오프라인 테스트

`npm test`는 기존 테스트, `npm run build`는 빌드 명령입니다. 이번 정리에서 실행한 범위는 루트 `README_SUBMISSION.md`를 보세요. 화면 디자인과 UX는 추가 개선 예정입니다.
