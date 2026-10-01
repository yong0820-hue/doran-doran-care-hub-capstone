# 제출본 파일과 정리 내역

원본 소스·기존 테스트의 실행 코드 바이트는 보존했습니다. 실제 config.h와 .env, 설치/빌드/상태물은 복사하지 않았습니다. 설명 문서와 공개 예제만 아래와 같이 정리했습니다.

## 원본에서 선별한 파일

### .gitignore (1개)

- `.gitignore`

### apps (42개)

- `apps/guardian-dashboard/.env.example`
- `apps/guardian-dashboard/.gitignore`
- `apps/guardian-dashboard/README.md`
- `apps/guardian-dashboard/index.html`
- `apps/guardian-dashboard/package-lock.json`
- `apps/guardian-dashboard/package.json`
- `apps/guardian-dashboard/src/App.jsx`
- `apps/guardian-dashboard/src/components/AiConversationStatus.jsx`
- `apps/guardian-dashboard/src/components/CriticalAlerts.jsx`
- `apps/guardian-dashboard/src/components/DebugPanel.jsx`
- `apps/guardian-dashboard/src/components/DemoControls.jsx`
- `apps/guardian-dashboard/src/components/DeviceStatusCard.jsx`
- `apps/guardian-dashboard/src/components/ElderProfileCard.jsx`
- `apps/guardian-dashboard/src/components/EventTimeline.jsx`
- `apps/guardian-dashboard/src/components/GuardianSignIn.jsx`
- `apps/guardian-dashboard/src/components/Header.jsx`
- `apps/guardian-dashboard/src/components/MedicationScheduleManager.jsx`
- `apps/guardian-dashboard/src/components/MedicationStatus.jsx`
- `apps/guardian-dashboard/src/components/PresenceStatus.jsx`
- `apps/guardian-dashboard/src/components/ResetSettings.jsx`
- `apps/guardian-dashboard/src/dashboardState.js`
- `apps/guardian-dashboard/src/data/eventTemplates.js`
- `apps/guardian-dashboard/src/data/mockData.js`
- `apps/guardian-dashboard/src/guardianAuthState.js`
- `apps/guardian-dashboard/src/hooks/useDashboardData.js`
- `apps/guardian-dashboard/src/index.css`
- `apps/guardian-dashboard/src/main.jsx`
- `apps/guardian-dashboard/src/medicationSchedule.js`
- `apps/guardian-dashboard/src/memoryReset.js`
- `apps/guardian-dashboard/src/services/firebase.js`
- `apps/guardian-dashboard/src/services/guardianAuth.js`
- `apps/guardian-dashboard/src/utils.js`
- `apps/guardian-dashboard/test/dashboardState.test.js`
- `apps/guardian-dashboard/test/eventScenarios.test.js`
- `apps/guardian-dashboard/test/guardianAuth.test.js`
- `apps/guardian-dashboard/test/medicationSchedule.test.js`
- `apps/guardian-dashboard/test/memoryReset.test.js`
- `apps/guardian-dashboard/test/memoryResetCoordinator.test.js`
- `apps/guardian-dashboard/test/memoryResetOperations.test.js`
- `apps/guardian-dashboard/test/memoryResetProtocol.test.js`
- `apps/guardian-dashboard/test/sosConcurrency.test.js`
- `apps/guardian-dashboard/vite.config.js`

### deployment-prep (12개)

- `deployment-prep/persistent-memory-20260928-223050/PHYSICAL-CHECKLIST.md`
- `deployment-prep/persistent-memory-20260928-223050/firebase-access.example.json`
- `deployment-prep/persistent-memory-20260928-223050/firebase-device-credential.example.json`
- `deployment-prep/persistent-memory-20260928-223050/firebase-isolated.rules.json`
- `deployment-prep/persistent-memory-20260928-223050/firebase.emulator.json`
- `deployment-prep/persistent-memory-20260928-223050/firebase_emulator_tests.mjs`
- `deployment-prep/persistent-memory-20260928-223050/firebase_emulator_worker.py`
- `deployment-prep/persistent-memory-20260928-223050/isolated-test.example.json`
- `deployment-prep/persistent-memory-20260928-223050/isolated_guardian_build.mjs`
- `deployment-prep/persistent-memory-20260928-223050/isolated_http_smoke.py`
- `deployment-prep/persistent-memory-20260928-223050/isolated_pi.py`
- `deployment-prep/persistent-memory-20260928-223050/run_auth_emulator_checks.py`

### docs (12개)

- `docs/ARCHITECTURE.md`
- `docs/DB_SCHEMA.md`
- `docs/PERSISTENT_MEMORY_M2_FOLLOWUP.md`
- `docs/PERSISTENT_MEMORY_RECALL_FOLLOWUP.md`
- `docs/PERSISTENT_MEMORY_SUBJECT_LABELS.md`
- `docs/PERSISTENT_MEMORY_V1.md`
- `docs/PERSISTENT_MEMORY_V3_REVIEW.md`
- `docs/PERSISTENT_MEMORY_V4_REVIEW.md`
- `docs/PERSISTENT_MEMORY_V5_REVIEW.md`
- `docs/PRD.md`
- `docs/REVIEW_CHECKLIST.md`
- `docs/ROADMAP.md`

### firmware (3개)

- `firmware/esp32-sensors/esp32_sensors/README.md`
- `firmware/esp32-sensors/esp32_sensors/config.example.h`
- `firmware/esp32-sensors/esp32_sensors/esp32_sensors.ino`

### raspberry-pi-voice-hub (35개)

- `raspberry-pi-voice-hub/.env.example`
- `raspberry-pi-voice-hub/.gitignore`
- `raspberry-pi-voice-hub/README.md`
- `raspberry-pi-voice-hub/audio_playback.py`
- `raspberry-pi-voice-hub/config.py`
- `raspberry-pi-voice-hub/conversation_store.py`
- `raspberry-pi-voice-hub/device_reset.py`
- `raspberry-pi-voice-hub/display/app.js`
- `raspberry-pi-voice-hub/display/index.html`
- `raspberry-pi-voice-hub/display/memory_game.js`
- `raspberry-pi-voice-hub/display/radio_presets.json`
- `raspberry-pi-voice-hub/display/styles.css`
- `raspberry-pi-voice-hub/display_server.py`
- `raspberry-pi-voice-hub/display_state.py`
- `raspberry-pi-voice-hub/firebase_auth.py`
- `raspberry-pi-voice-hub/firebase_client.py`
- `raspberry-pi-voice-hub/firebase_commands.py`
- `raspberry-pi-voice-hub/firebase_inactivity.py`
- `raspberry-pi-voice-hub/firebase_medication.py`
- `raspberry-pi-voice-hub/inactivity_care.py`
- `raspberry-pi-voice-hub/interaction_manager.py`
- `raspberry-pi-voice-hub/main.py`
- `raspberry-pi-voice-hub/medication_care.py`
- `raspberry-pi-voice-hub/medication_schedule.py`
- `raspberry-pi-voice-hub/medication_scheduler.py`
- `raspberry-pi-voice-hub/memory_dispatch.py`
- `raspberry-pi-voice-hub/memory_reset.py`
- `raspberry-pi-voice-hub/memory_runtime.py`
- `raspberry-pi-voice-hub/memory_storage.py`
- `raspberry-pi-voice-hub/persistent_memory.py`
- `raspberry-pi-voice-hub/radio_presets.py`
- `raspberry-pi-voice-hub/requirements-wakeword.txt`
- `raspberry-pi-voice-hub/requirements.txt`
- `raspberry-pi-voice-hub/voice_ai.py`
- `raspberry-pi-voice-hub/wake_word.py`

### scripts (2개)

- `scripts/check-integration-config.py`
- `scripts/run-pi-kiosk.sh`

### tests (54개)

- `tests/.gitignore`
- `tests/display_browser.test.js`
- `tests/memory_game.test.js`
- `tests/memory_protocol_fake.py`
- `tests/test_audio_playback.py`
- `tests/test_conversation_store.py`
- `tests/test_device_reset.py`
- `tests/test_display_audio_focus.py`
- `tests/test_display_frontend.py`
- `tests/test_display_integration.py`
- `tests/test_display_server.py`
- `tests/test_display_state.py`
- `tests/test_event_concurrency.py`
- `tests/test_event_scenarios.py`
- `tests/test_firebase_auth.py`
- `tests/test_firebase_commands.py`
- `tests/test_firebase_inactivity.py`
- `tests/test_firebase_medication.py`
- `tests/test_firebase_voice_sessions.py`
- `tests/test_inactivity_care.py`
- `tests/test_interaction_manager.py`
- `tests/test_isolated_auth_launch.py`
- `tests/test_main_regression.py`
- `tests/test_medication_care.py`
- `tests/test_medication_integration.py`
- `tests/test_medication_schedule.py`
- `tests/test_medication_scheduler_worker.py`
- `tests/test_memory_body_transport.py`
- `tests/test_memory_cache.py`
- `tests/test_memory_dispatch_barrier.py`
- `tests/test_memory_extraction_regressions.py`
- `tests/test_memory_identity_v3.py`
- `tests/test_memory_identity_v4.py`
- `tests/test_memory_identity_v5.py`
- `tests/test_memory_identity_v6.py`
- `tests/test_memory_manifest_v5.py`
- `tests/test_memory_protocol_v3.py`
- `tests/test_memory_recall_corrections.py`
- `tests/test_memory_recall_labels.py`
- `tests/test_memory_reset_coordinator_v5.py`
- `tests/test_memory_reset_epoch.py`
- `tests/test_memory_reset_worker.py`
- `tests/test_memory_runtime.py`
- `tests/test_memory_service_ownership.py`
- `tests/test_memory_shutdown.py`
- `tests/test_memory_storage_safety.py`
- `tests/test_memory_voice_integration.py`
- `tests/test_persistent_memory.py`
- `tests/test_radio_presets.py`
- `tests/test_runtime_diagnostics.py`
- `tests/test_utterance_capture.py`
- `tests/test_voice_ai.py`
- `tests/test_voice_controllers.py`
- `tests/test_voice_sessions.py`

## 새로 작성하거나 설명/예제만 정리한 파일

| 파일 | 이유 |
|---|---|
| `.env.example` | 제출본 루트에 비밀 없는 설정 안내 및 빈 키 목록 추가 |
| `.gitignore` | 실제 설정·비밀파일·실행 데이터·빌드·개발 산출물 재유입 방지 |
| `README.md` | 루트에 최신 제출 안내와 실행 조건 추가 |
| `README_SUBMISSION.md` | 포함/제외·기능 현황·코드 읽는 순서·Git 방식·검증·마지막 확인 안내 새 작성 |
| `apps/guardian-dashboard/.env.example` | Firebase 실제 값 없이 필요한 설정 이름만 제공 |
| `apps/guardian-dashboard/README.md` | 오래된 Phase 1 설명을 현재 코드 상태로 갱신 |
| `deployment-prep/persistent-memory-20260928-223050/DEPLOY-RUNBOOK.md` | 과거 실명/운영 경로/오래된 manifest 의존 runbook을 제출용 안내로 정리 |
| `deployment-prep/persistent-memory-20260928-223050/README.md` | 선별된 배포 자료와 미실행 범위를 쉽게 설명 |
| `deployment-prep/persistent-memory-20260928-223050/isolated-runtime.sha256` | 격리 도구의 현재 제출 snapshot 검증용 해시 재작성 |
| `deployment-prep/persistent-memory-20260928-223050/isolated-test.example.json` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/ARCHITECTURE.md` | 초기 설계 문서의 시점과 최신 상태 구분 |
| `docs/PERSISTENT_MEMORY_M2_FOLLOWUP.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/PERSISTENT_MEMORY_RECALL_FOLLOWUP.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/PERSISTENT_MEMORY_SUBJECT_LABELS.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/PERSISTENT_MEMORY_V1.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈; 개인 Pi 로그인/호스트를 일반 예시로 바꿈 |
| `docs/PERSISTENT_MEMORY_V3_REVIEW.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/PERSISTENT_MEMORY_V4_REVIEW.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/PERSISTENT_MEMORY_V5_REVIEW.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈 |
| `docs/PRD.md` | 초기 설계 문서의 시점과 최신 상태 구분 |
| `docs/REVIEW_CHECKLIST.md` | 초기 설계 문서의 시점과 최신 상태 구분 |
| `docs/ROADMAP.md` | 오래된 24GHz/Claude 계획을 사용자 지정 최신 개발 상태로 정리 |
| `firmware/esp32-sensors/esp32_sensors/README.md` | 초기 24GHz 자료와 현재 60GHz 계획을 명확히 구분 |
| `raspberry-pi-voice-hub/.env.example` | 소스의 설정 이름을 기준으로 빈 비밀값과 공개 기본값만 새로 작성 |
| `raspberry-pi-voice-hub/README.md` | 문서/예제의 개인 계정 및 장치 경로를 일반 예시로 바꿈; 원문 실행 설명 보존 및 실제 검증 범위 안내; 개인 Pi 로그인/호스트를 일반 예시로 바꿈 |
| `SUBMISSION_CONTENTS.md` | 이 파일: 선별 목록과 정리 이유 |

## 제외한 원본 파일 수 (상위 위치별)

| 원본 위치 | 제외 파일 수 |
|---|---|
| `.claude` | 1 |
| `.git` | 376 |
| `.vscode` | 1 |
| `CLAUDE.md` | 1 |
| `apps` | 12957 |
| `deployment-prep` | 45 |
| `docs` | 1 |
| `doran-transfer` | 38 |
| `firmware` | 1 |
| `raspberry-pi-voice-hub` | 17 |
| `tests` | 26 |

원본에서 선별한 파일 161개, 제외한 파일 13464개입니다. 새 문서/예제/Git 데이터가 추가되어 최종 제출본 파일 수와는 다릅니다.

제외 목록은 원본의 파일 이름만 기준으로 집계했습니다. 비밀 내용·사용자 기록·로그 내용은 이 파일에 담지 않았습니다. 원본 .git을 제외한 자리에는 새 제출용 .git을 생성합니다. 세부 범주와 실행 조건은 README_SUBMISSION.md를 보세요.
