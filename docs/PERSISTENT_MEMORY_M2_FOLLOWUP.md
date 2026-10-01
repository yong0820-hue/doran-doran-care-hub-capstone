# Persistent Memory M2 기억 손실 후속 수정

기준 branch `wip/interaction-manager-v2`, HEAD
`ad0f85a44256d60eadd720f47cfd8c68987e8f43`. 이번 작업은 M2 identity/merge와
그에 따른 회상 정책만 변경했다. 기존 V1/V3/V4/V5 보고서는 당시 검증 이력이다.
아래 검사는 이번 작업에서 새로 실행한 결과다.

## 1. 수정 전 재현

새 테스트는 실제 `MemoryExtractor.extract()` 검증 → `MemoryStore.save()` →
immutable body 저장 및 control manifest CAS → body 재조회 경로를 사용한다.
모델 응답과 HTTP transport만 fake이며 외부 서버를 호출하지 않는다.

| 발언 시각 | evidence | extractor subject | 수정 전 문제 |
| --- | --- | --- | --- |
| t100 | 매운 음식을 좋아해. | 음식 | broad label로 첫 기억 저장 |
| t200 | 매운 음식을 좋아해. | 매운 음식 | evidence 중복은 제거하지만 subject가 음식으로 고정 |
| t300 | 단 음식을 싫어해. | 단 음식 | category의 유일한 후보를 같은 대상으로 판단하여 첫 ID를 덮어씀 |

수정 전 최종 상태는 단 음식 기억 하나이며 매운 음식 기억이 사라졌다.
신규 초기 16개 테스트를 구현 변경 전에 실행해 6개 테스트 메서드에서 16개 subtest
실패를 확인했다. 나머지 10개 메서드는 통과했다. 이후 혼합 문장, 회상 충돌,
붙여 쓴 대상, 이름과 조사 경계, 부분 label, 한 음절 명사 나열도 먼저 실패시킨 뒤 수정했다.

## 2. 원인

동일 evidence의 owner를 찾는 것과 그 owner의 broad label을 영구 고정하는 것은 다른 문제다.
기존 코드는 중복 제거 후 label을 그대로 두면서, 다음 merge에서는 broad/narrow 관계와
후보 개수를 identity 근거로 사용했다. 그래서 t200 없이 t100 → t300만 저장해도 손실됐다.
또 독립 행을 늘리면서 immutable storage key와 logical ID가 다르다는 점을 반영하지 않으면
ID가 충돌할 수 있어, 신규 ID 생성은 두 종류의 기존 ID를 모두 확인한다.

## 3. 대상 동일성과 범주 관계

- 동일 evidenceKey 또는 정규화된 동일 content는 기존 owner를 먼저 찾는다. type/source/elder
  경계를 넘지 않는다. owner의 대상과 재추출 대상이 다르면 다른 행을 연결하는 근거로 쓰지 않는다.
- 서로 다른 evidence의 overwrite/archive에는 두 발언에서 도출한 **전체 대상**의 일치가 필요하다.
  한정된 단문 좋아함/싫어함 구문만 인식하며 대상의 수식어를 유지한다. `음식`이라는 extractor
  label이어도 원문이 `매운 음식을 좋아해.`이면 비교 대상은 `매운 음식`이다.
- subject 문자열, topicKey, 범주·부분집합·공통 단어, 후보가 하나뿐이라는 사실로 병합하지 않는다.
  `매운 음식`과 `단 음식`, `클래식 음악`과 `재즈 음악`은 별개다.
- 동일 대상으로 확인된 정정은 기존 ID/createdAt을 유지하고 발언 timestamp가 최신인 것을 쓴다.
  지연된 과거 extraction은 최신 content/evidence/lastMentionedAt을 되돌리지 못한다.
- 불확실한 문장과 독립적인 일반 선호도 별도로 보존한다. 늦은 broad 발언을 버리거나 specific을
  archive하던 경로는 제거했다. 기존 용량/보존기간 제한은 그대로 적용된다.

새 DB 필드, 상태 값, 벡터 DB, 추가 LLM 호출은 없다. 비교 대상은 검증된 content에서 로컬로
계산한다. `topicKey`는 표시 subject의 정규화 값이며 identity의 권위로 사용하지 않는다.

## 4. 동일 evidence의 subject 정제

t100/t200은 하나의 기존 ID를 유지한다. 양쪽 evidence에서 동일한 전체 대상을 확인하고,
더 긴 새 label이 완전한 대상 token 구간으로 지지될 때만 subject/topicKey를 구체화한다.
`음식 → 매운 음식`은 허용하지만 broad 재추출로 되돌리거나 `운 음식`처럼 단어 일부로
정제하지 않는다. 복합/나열 문장의 한 부분만 전체 대상으로 정제하지 않는다.

지연된 extraction에서 안전한 메타데이터 정제가 가능해도 최신 본문·evidenceKey·발언 시각은
보존한다. 단순 label 교체가 해결책은 아니다. t200 없이도 본문에서 전체 대상을 비교해
독립 기억을 지킨다.

## 5. 새 path 테스트와 결과

`tests/test_memory_identity_v6.py`: **23개 메서드 PASS**, 내부 fixture/subtest 포함.

- 원래 반례: 매운 음식은 첫 ID와 createdAt=t100 유지, subject=매운 음식,
  lastMentionedAt=t200. 단 음식은 다른 ID, lastMentionedAt=t300. 둘 다 active.
- 중간 구체화 없음: t100 행의 ID와 전체 metadata를 그대로 보존하고 단 음식 추가.
- 고정된 발언 시각의 6개 도착 순서, 같은 대상 최신 정정과 stale 재실행,
  broad/specific 반복 evidence의 중복 억제 및 일반 preference owner 보호.
- 음식 외 클래식/재즈 음악, 붉은/하얀 장미 fixture. 김연자/나훈아, 요가/오이,
  매운음식/매운 음식 및 조사, 아침/저녁/일반 산책, type 분리 회귀.
- 불명확한 같은 label의 반대 evidence, 혼합 술어/명사 나열, `차와 물`/`꽃과 풀`,
  이상은/이상 이름과 조사 경계, `운 음식` 부분 label 보호.

각 저장 뒤 실제 body를 읽어 ID/subject/content/evidenceKey/발언 시각/status를 확인한다.
무관한 conversation, user/ai source, 다른 elder의 manifest/body도 보존한다.
memory elder root GET/PUT이 없고 leaf write/control CAS를 이용하는지도 검사한다.

## 6. 기존 M2 회귀 및 변경한 테스트 계약

v3/v4/v5 **32개 PASS**, 신규 포함 identity 전체 **55개 PASS**.
기존 테스트 7개에는 새 요청과 충돌하는 기대가 있었다. 단순 count 축소나 검증 삭제 대신
다음과 같이 보존·회상 검증을 추가했다. 기존 기대를 그대로 통과했다고 보고하지 않는다.

| 파일 / 해당 테스트 | 이전 기대 → 새 요청에 맞춘 기대 |
| --- | --- |
| v3 / delayed broad subject | 오래된 일반 음식 폐기 → 별도 보존, specific 원본 불변, 일반 회상 제외 |
| v3 / newer broad subject | 최신 일반 음식이 specific overwrite → 각각 보존, ID/evidence/time 검증 |
| v3 / legacy category record | 일반 행을 specific의 duplicate로 archive → owner 갱신만 허용, archive 없음 |
| v4 / delayed broad target | 오래된 일반 산책 폐기 → 일반 추가, 아침·저녁 원본 불변 및 회상 검증 |
| v4 / compact multiple specifics | 위 폐기를 compact label에도 적용 → 세 ID/본문/시각 보존 |
| v5 / same evidence | 최초 broad subject 고정 → 기존 ID 유지하며 근거가 있는 specific으로 정제 |
| v5 / delayed broad multiple specifics | 일반 산책 폐기 → 별도 보존, specific 불변 및 일반 회상 제외 |

나머지 기존 한국어·이름·띄어쓰기·중복 owner·최신성·type 보호 기대는 유지했다.

## 7. 회상 및 H1/M5/M6 영향

`select()`는 이미 읽은 bounded snapshot에서만 필터링한다. 명시적인 general/specific
범위가 겹치면 general을 회상에서 제외한다. 한쪽 대상이 불명확하고 label이 관련되면
양쪽을 제외한다. 독립적인 명시 대상은 함께 회상할 수 있다. 동일 대상의 legacy 충돌은
최신 발언을 선택하며 같은 시각의 서로 다른 내용은 둘 다 제외한다. 이 처리는 저장 행을
수정하지 않으며, 불명확한 반대 후보를 모두 확정 사실로 넣는 것을 줄이기 위한 보수적 규칙이다.

reset coordinator, dispatch barrier, storage protocol, cache/lifecycle/voice 구현은 변경하지 않았다.
시작 시 SHA-256 snapshot과 비교해 `memory_reset.py`, `memory_dispatch.py`,
`memory_storage.py`, `memory_runtime.py`, `voice_ai.py` 및 관련 기존 회귀 파일이 동일함을 확인했다.
H1 callback 직전/concurrent reset, M5 coalescing·지연 B·crash/ACK loss 복구,
cold-cache/WAITING/epoch, nonconversation manifest, M6 bounded transport 회귀는 full suite에 포함된다.
새 identity/회상 로직은 추가 HTTP 요청 없이 기존 128개 active 한도 안에서 실행한다.

## 8. 이번 작업에서 추가로 변경한 파일

작업 시작 시 전체 tracked/untracked 파일의 hash와 비교한 목록이다. 기존 미커밋 파일 전체와
구분한다. **기존 파일 추가 수정 5개, 신규 2개, 합계 7개**다.

1. `raspberry-pi-voice-hub/persistent_memory.py`
2. `tests/test_memory_identity_v3.py`
3. `tests/test_memory_identity_v4.py`
4. `tests/test_memory_identity_v5.py`
5. `docs/DB_SCHEMA.md`
6. `tests/test_memory_identity_v6.py` — 신규
7. `docs/PERSISTENT_MEMORY_M2_FOLLOWUP.md` — 신규, 이 보고서

## 9. 새로 실행한 전체 검사

| 검사 | 결과 |
| --- | --- |
| Python full discovery | 700 실행: 698 PASS, 2 skip |
| M2 identity (full에 포함) | 55 PASS |
| H1 dispatch barrier / M5 coordinator (full에 포함) | 5 / 15 PASS |
| cache / extraction shutdown (full에 포함) | 17 / 10 PASS |
| memory/voice integration (full에 포함) | 27 PASS |
| manifest / body transport / storage safety (full에 포함) | 6 / 2 / 11 PASS |
| Guardian JS | 114 PASS, skip 0 |
| Pi browser/game JS | 65 PASS, skip 0 |
| Guardian production build | PASS, 61 modules |
| git diff --check | PASS |
| 이번 변경 파일 및 untracked trailing whitespace | 0건 |

full에는 memory/voice, H1/M5/M6, extraction drain/recovery, Interaction Manager,
medication/inactivity/care, display/radio/game/audio-focus 회귀가 포함된다.
2 skip은 번들 Python에 실제 OpenAI SDK/httpx 계약 의존성과 native `webrtcvad-wheels`가
없어서 발생했다. skip을 PASS로 계산하지 않았다. 실제 SDK 테스트도 본래 mock transport
계약 검사이며, 이 작업에서 실제 OpenAI API 호출은 하지 않았다.

재실행 명령:

```powershell
$memoryPython = 'C:\Users\YOUR_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $memoryPython -B -m unittest discover -s tests -p 'test_memory_identity*.py' -v
& $memoryPython -B -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/display_browser.test.js tests/memory_game.test.js
npm --prefix apps/guardian-dashboard test
npm --prefix apps/guardian-dashboard run build
git diff --check
git status --short
```

## 10. 제한사항

- 완전한 한국어 의미 분석기가 아니다. 동의어, 대명사, 임의의 복문·생략·고유명사 경계를
  추정하지 않는다. 제한된 명시 구문 밖에서는 같은 대상의 정정도 별도 기록으로 남을 수 있다.
- 불확실한 관련 label은 양쪽 회상을 막아 유용한 정보도 빠질 수 있다. lexical 관계가 드러나지
  않는 의미 충돌까지 탐지한다고 보장하지 않는다. 저장 손실을 피하는 쪽으로 보수적으로 처리했다.
- 범주 보존으로 active 행이 늘 수 있으며 기존 128개 retention/capacity 정책은 계속 적용된다.
  이번 수정은 보존 한도를 무제한으로 바꾸거나 이미 손실된 기억을 복구하는 migration이 아니다.
- 실제 Pi/Linux/audio와 운영 Firebase rules/index, 실물 동작은 검증하지 않았다.

## 11. Git 상태와 금지 작업

branch/HEAD는 위 기준과 동일하고 staged diff는 비어 있다. 최종 전체 미커밋 상태는
tracked 수정 **13개**, untracked **38개**로 합계 51개다. 시작 시 13/36개였으며 이번 신규
테스트/보고서 2개만 파일 수에 추가됐다. 이번 추가 수정은 위 7개에 한정된다.

git add/commit/push/reset/revert/clean, 기존 변경 삭제, Pi 접속/배포, 운영 Firebase 접근,
실제 OpenAI API 호출, .env/credentials/서비스 설정 변경은 하지 않았다.
