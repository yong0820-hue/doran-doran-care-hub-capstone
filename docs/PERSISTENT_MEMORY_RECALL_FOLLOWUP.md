# Persistent Memory — 최신 preference 회상 수정

기준: `wip/interaction-manager-v2`, HEAD
`ad0f85a44256d60eadd720f47cfd8c68987e8f43`.
기존 미커밋 변경 위에서 회상 선택만 수정했다. 저장 identity, reset/barrier/storage,
cache 일관성 계약은 변경하지 않았다. 아래 결과는 이번 작업에서 새로 실행했다.

## 1. 수정 전 원래 반례

실제 `MemoryExtractor.extract()` → `MemoryStore.save()` → immutable body/control CAS →
cache → `request_context.dispatch()` → 동기 LLM callback messages로 재현했다.
모델 응답과 HTTP transport만 대역이며 실제 SDK/API나 운영 Firebase를 호출하지 않는다.

| 발언 시각 | subject | content | 수정 전 저장 | 수정 전 callback |
| --- | --- | --- | --- | --- |
| t100 | 김연자 | 나는 트로트 가수 김연자를 좋아해. | active | 포함 |
| t200 | 김연자 | 김연자 싫어해. | active | 누락 |

query는 `김연자 좋아해?`. 두 발언의 별도 ID/body가 저장돼 있어도 과거 좋아함만 전달됐다.
production 수정 전 신규 초기 13개 테스트를 실행해 8개 메서드에서 21개 failing subtest를
확인했고, 5개 메서드는 통과했다. 후속 검토에서 독립 출근길/퇴근길 조건을 지우는 4개
subtest와 서로 다른 역할의 같은 이름을 단정하는 2개 subtest도 먼저 실패시킨 뒤 보강했다.

## 2. 결함 위치와 수정 범위

`_preference_target()`가 각각 `트로트/가수/김연자`, `김연자`라는 전체 대상을 도출한다.
기존 `select()`는 token 포함 관계가 있으면 시각과 무관하게 짧은 쪽을 제외했다.
따라서 최신 짧은 정정은 relevance/top-K 이전에 사라졌다. 저장 손실이나 cache 미갱신과는
별개의 선택 결함이며, 이미 올바르게 갱신한 cache에서도 재현됐다.

production 변경은 신규 `_recall_subject()`와 `MemoryStore.select()`뿐이다.
시작 시 소스 사본과 함수별 원문을 비교해 `_merge`, `_preference_target`, extractor,
MemoryReadCache, MemoryRequest.dispatch, 서비스 lifecycle/serialization이 그대로임을 확인했다.

## 3. 현재 preference 선택과 보류 규칙

1. 기존 evidence에서 도출한 전체 대상이 같으면 실제 발언 시각 `lastMentionedAt`이 최신인
   행을 선택한다. 저장 도착/updatedAt/cache 시각, 문자열 길이, query 극성, importance는
   최신 정정을 뒤집지 못한다.
2. 같은 subject가 완전한 말미 token 구간으로 지지되고 명확한 역할 설명이 붙은 경우,
   회상에서만 bare subject와 같은 대상으로 볼 수 있다. 예: `트로트 가수 김연자 ↔ 김연자`,
   `소설 작가 박완서 ↔ 박완서`. 지원 역할과 분야/장르 명사는 작은 명시적 어휘표로 제한한다.
   인물·음식 이름은 하드코딩하지 않으며 임의의 설명어/수식어를 삭제하지 않는다.
3. 한 subject에 서로 다른 역할이 경합하면 같은 사람이라고 단정하지 않고 관련 후보를 보류한다.
   `출근길/퇴근길 가수 김연자`는 역할의 분야/장르가 아닌 조건이므로 각각의 전체 대상을 유지한다.
4. 대상이 같아도 발언 시각이 같고 내용이 다르면 양쪽을 보류한다. 긴 문장이나 긍정·부정 중
   하나를 임의로 고르지 않는다.
5. 단순 범주/부분집합 관계는 동일성 근거가 아니다. 오래된 general과 더 최신인 specific은
   general을 제외한다. general이 최신이거나 동시각이면 양쪽을 보류하여 과거 specific만
   현재 사실로 보내지 않는다. 한쪽 파싱이 불확실하고 label이 관련돼도 양쪽을 보류한다.
6. 이 처리는 bounded 후보 전체에 대해 ranking/top-K/문자 예산보다 먼저 실행한다. 이후 예산에
   최신 행이 들어가지 못하더라도 제거된 과거 행을 대체로 복구하지 않는다.

저장 행을 archive/overwrite하지 않으며 ID/body/evidenceKey/발언 시각은 회상으로 변하지 않는다.
추가 네트워크·LLM 호출이나 전체 Firebase bucket 조회, schema 변경은 없다.

## 4. 실제 LLM callback payload 전후

아래는 callback이 받은 `<user_memories>` JSON 중 해당 대상 행이다. 테스트의 기준시각은
1800000000000이며 t100/t200을 더했다.

수정 전:

```json
[{"type":"preference","subject":"김연자","content":"나는 트로트 가수 김연자를 좋아해.","lastMentionedAt":1800000000100}]
```

수정 후:

```json
[{"type":"preference","subject":"김연자","content":"김연자 싫어해.","lastMentionedAt":1800000000200}]
```

저장에는 두 원문이 그대로 남는다. 테스트는 select 반환값만 검사하지 않고, 실제 request
context의 callback 호출 및 전달 messages를 파싱해 최신 포함/과거 제외를 확인한다.
매 dispatch 전후 저장 root deep-equal, warm GET/PUT/PATCH 추가 0회도 검사한다.

## 5. query·길이·시각·순서·예산 및 warm cache

신규 `tests/test_memory_recall_corrections.py`: **15개 테스트 메서드 PASS**.

- `좋아해?`, `싫어해?`, `내 취향 기억나?` query 모두 동일한 최신 preference.
- 긴 긍정 → 짧은 부정, 긴 부정 → 짧은 긍정, 최신이 더 긴 경우, 두 표현 모두 역할 설명인
  경우와 각각의 저장 도착 순서. importance 높은 과거와 낮은 최신도 최신을 사용한다.
- 박완서·아이유·안성기 및 서로 다른 역할 설명 fixture에 동일 규칙 적용.
- 16개 관련/무관 후보를 추가해 top-K 8개와 전체 후보 문자량 4200자를 초과시킨다.
  실제 payload는 기본 4200자 및 축소 220/80자 예산을 지킨다. 220자에서는 최신이 남고,
  최신조차 들어가지 못하는 80자에서는 과거로 되돌아가지 않는다.
- 동일 시각의 long/short 충돌, 최신이 파싱 불가 복문인 경우, 조건부 표현, 서로 다른 역할의
  같은 이름은 해당 보류 정책을 검증한다. 무관한 바둑 기억은 계속 회상한다.
- warm cache: 과거 snapshot 로드 → 정정 직접 save → 갱신 전에는 과거 payload가 허용됨 →
  `prefetch(force=True)` 및 완료 대기 → 다음 실제 dispatch에는 최신 정정이 들어간다.

기존 계약은 15초 refresh throttle, 최대 cache age 60초다. 직접 store.save는 이미 로드한
snapshot을 동기 교체하지 않는다. 서비스 extraction worker는 저장 성공 후 강제 갱신을
요청한다. 이번 수정은 갱신 전 허용된 지연을 없애는 변경이 아니며, 갱신 후 선택 오류를 고친다.

## 6. 독립 기억 보존

김연자/나훈아, 매운 음식/단 음식, 아침 산책/저녁 산책, 수채화/유화,
김연자의 노래/목소리, 출근길/퇴근길 조건은 독립 대상·속성·조건으로 유지한다.
일반 profile 관계·충돌 없는 취향·최근 episodic 사건도 payload에서 확인한다.
모든 preference를 비우거나 memory를 비활성화하는 해결책을 사용하지 않았다.

이전 음식 broad → 동일 evidence specific → 독립 단 음식, 요가/오이, 조사·띄어쓰기,
산책 정정과 stale replay도 새 end-to-end 테스트 및 기존 identity 전체에서 통과했다.

## 7. 이전 PASS 항목과 기존 테스트 변경

기존 identity **55개 PASS**. 저장 loss, 도착 순서, evidence owner, 외부 source/elder,
type 구분과 latest/stale 보호를 유지한다.

기존 `test_memory_identity_v3.py::test_newer_broad_subject_preserves_independent_specific_preference`
한 개의 회상 기대를 변경했다. 예전 기대는 새 general이 들어와도 **과거 specific만** 보내는
것으로, 이번 요청의 old-only 금지와 충돌한다. 저장 ID/body/evidence/time 보존 검증은 그대로
두고 회상만 양쪽 보류로 강화했다. 기존의 잘못된 기대까지 그대로 PASS했다고 주장하지 않는다.

시작 시 SHA-256과 비교해 reset coordinator, dispatch barrier, storage protocol,
runtime/voice 및 관련 기존 테스트가 동일함을 확인했다. H1/M5/M6, cache/epoch,
extraction drain/recovery, care/audio/Interaction Manager/radio 회귀는 full suite에서 재실행했다.

## 8. 이번 작업의 정확한 추가 변경 파일

시작 시 모든 tracked/untracked 파일의 hash와 비교했다. 기존 파일 3개 추가 수정과
신규 2개, **총 5개**이며 전체 미커밋 파일 수와 다르다.

- `raspberry-pi-voice-hub/persistent_memory.py`
- `tests/test_memory_identity_v3.py`
- `docs/DB_SCHEMA.md`
- `tests/test_memory_recall_corrections.py` — 신규
- `docs/PERSISTENT_MEMORY_RECALL_FOLLOWUP.md` — 신규, 이 보고서

이전 M2 보고서·v4/v5/v6 테스트는 그대로 유지했다.

## 9. 이번에 새로 실행한 검사

| 검사 | 결과 |
| --- | --- |
| 신규 retrieval/payload | 15 PASS |
| 기존 M2 identity | 55 PASS |
| H1 dispatch / M5 coordinator | 5 / 15 PASS |
| cache / shutdown lifecycle | 17 / 10 PASS |
| memory/voice integration | 27 PASS |
| manifest / bounded body / storage safety | 6 / 2 / 11 PASS |
| Python full discovery (위 Python 항목 포함) | 715 실행: 713 PASS, 2 skip |
| Guardian JS | 114 PASS, skip 0 |
| Pi browser/game JS | 65 PASS, skip 0 |
| Guardian production build | PASS, 61 modules |
| git diff --check | PASS (기존 LF/CRLF 안내만) |
| 이번 변경 파일 및 untracked trailing whitespace | 0건 |

full Python은 memory/reset 외 Interaction Manager, care, audio-focus, radio/display/game
회귀도 포함한다. skip 2개는 Windows 번들 Python에 없는 native `webrtcvad-wheels` 검사와
OpenAI SDK request serialization 계약 검사 의존성 때문이다. skip은 PASS에 포함하지 않았다.
실제 Pi/Linux/audio, 운영 Firebase rules/index, 실제 외부 OpenAI API는 검증하지 않았다.

```powershell
$memoryPython = 'C:\Users\YOUR_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $memoryPython -B -X utf8 -m unittest discover -s tests -p 'test_memory_recall_corrections.py' -v
& $memoryPython -B -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/display_browser.test.js tests/memory_game.test.js
npm --prefix apps/guardian-dashboard test
npm --prefix apps/guardian-dashboard run build
git diff --check
git status --short
```

## 10. 제한사항 및 Git 상태

- 한정된 evidence 구문과 역할/분야 어휘표만 지원한다. 임의의 한국어 수식어, 생략, 동의어,
  붙여 쓴 역할 설명, 동명이인을 완전히 해석하지 않는다. 지원 범위 밖 충돌은 유용한 최신
  정보까지 보류될 수 있다. 서로 다른 역할을 가진 실제 동일 인물도 보수적으로 보류한다.
- 판단 범위는 현재 bounded cache에 함께 존재하는 후보다. 이전 저장/retention에서 이미
  제거된 과거 정보나 동일 시각 충돌을 회상만으로 복원하지 않는다. 저장 정책은 그대로다.
- 최신 대상도 relevance/top-K/문자 예산에서 제외될 수 있지만, 그 때문에 제거한 과거
  선호를 대신 전달하지 않는다. cache refresh 이전의 허용된 지연도 그대로다.
- branch/HEAD는 기준과 동일하다. staged 0개, 전체 tracked 수정 13개·untracked 40개
  (합계 53개, 시작 시 13/38개). 이번 신규 테스트/보고서 두 개가 추가됐다.
- git add/commit/push/reset/revert/삭제, Pi 접속·배포, 운영 Firebase 접근, 실제 OpenAI 호출,
  .env/credentials/systemd 설정 변경은 하지 않았다.
