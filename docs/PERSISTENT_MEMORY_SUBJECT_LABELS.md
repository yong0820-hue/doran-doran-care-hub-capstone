# Persistent Memory — 설명용 subject 표기 불변성

기준 branch: `wip/interaction-manager-v2`

기준 HEAD: `ad0f85a44256d60eadd720f47cfd8c68987e8f43`

최신 미커밋 작업본에서 시작했다. 다른 worktree나 HEAD 버전으로 교체하지 않았다.
이번 수정은 회상 비교 helper 하나에 한정하며, 아래 검사 결과는 이번 작업의 새 실행이다.

## 1. 수정 전 원래 반례

실제 `MemoryExtractor.extract()` → `MemoryStore.save()` → immutable body/control CAS →
cache → `request_context.dispatch()` → callback messages 경로로 재현했다.
모델 응답과 RTDB HTTP만 대역이다. 저장 fixture에는 바둑 선호도 함께 넣었다.

| 발언 시각 | subject | evidence |
| --- | --- | --- |
| t50 | 바둑 | 바둑을 좋아해. |
| t100 | 트로트 가수 김연자 | 나는 트로트 가수 김연자를 좋아해. |
| t200 | 김연자 | 김연자 싫어해. |

query `김연자 좋아해?`의 실제 callback에는 **바둑만** 전달됐다.
t100 subject를 `가수 김연자`로 바꿔도 동일하게 실패했다. 저장에는 김연자의 두 발언이
있었지만 회상에서 모두 보류됐다.

수정 전 helper는 같은 evidence target에 대해 다음처럼 다른 비교값을 만들었다.

| extractor label | 수정 전 비교값 |
| --- | --- |
| 김연자 | `(김연자, 가수)` |
| 가수 김연자 | `None` |
| 트로트 가수 김연자 | `(트로트가수김연자, None)` |

동일한 인물이 label만으로 다르게 비교되면서, 기존 불명확 범위 충돌 보류 분기로 들어갔다.
수정 전 회귀 실행에서 최종 신규 12개 메서드 중 7개에 걸쳐 115개 subtest 실패를 확인했다.
핵심 216개 표기 조합 중 84개가 실패했다. 최종 테스트에 대한 원본 재검사는 시작 시 보관한
소스를 별도 Python 프로세스에 로드해 수행했으며 작업 파일을 되돌리지 않았다.

## 2. label/evidence 비교 변경점

`_recall_subject(subject, target)`만 변경했다. 기존 `role_fields`의 역할/분야 어휘도 그대로다.

1. label을 identity key로 먼저 삼지 않고, evidence에서 이미 검증된 전체 target의 **맨 앞**에서
   지원되는 `역할` 또는 `허용 분야 + 역할`을 확인한다.
2. 그 뒤 남은 **전체 이름 구간**을 비교 key로 쓴다. 마지막 단어만 남기지 않는다.
3. label은 `전체 이름`, `역할 + 전체 이름`, `분야 + 역할 + 전체 이름` 중 실제 target에 있는
   완전한 표기와 정확히 일치해야 한다. 띄어쓰기 비교에는 기존 token join만 사용한다.
4. 역할도 함께 반환하므로 같은 이름의 서로 다른 역할에 대한 기존 보류가 유지된다.
   지원되지 않는 조건·속성은 제거하지 않는다.

위 세 label은 이제 모두 `(김연자, 가수)`로 비교되어 기존 `lastMentionedAt` 최신 우선
정책이 적용된다. 특정 이름, 부분문자열, 공통 장르/직업만으로 동일성을 만들지 않는다.
조사 처리나 명사 끝 제거를 새로 추가하지 않았다.

함수별 시작/종료 소스 비교에서 변경된 production 함수는 `_recall_subject` 하나다.
extractor validator, `_preference_target`, 저장 `_merge`, `select`의 ranking/충돌 정책,
cache, dispatch, reset 및 storage 구현은 그대로다.

## 3. 표기 변형 테스트 범위와 결과

신규 `tests/test_memory_recall_labels.py`: **12개 메서드 PASS**.
기존 helper의 실제 경로 검증을 재사용하며 기존 15개 테스트를 중복 발견하지 않는다.

핵심 테이블은 **216개 실제 callback 조합**이다.

| 축 | 범위 |
| --- | --- |
| 과거 label | 김연자 / 가수 김연자 / 트로트 가수 김연자: 3개 |
| 최신 evidence와 유효 label | 짧은 이름 evidence: 1개 / 가수 포함: 2개 / 트로트 가수 포함: 3개, 합계 6개 |
| 정정 방향 | 긍정→부정 / 부정→긍정: 2개 |
| 저장 도착 순서 | 과거 먼저 / 최신 먼저: 2개 |
| query | 좋아해? / 싫어해? / 내 취향 기억나?: 3개 |

발언 시각 t100/t200은 고정한다. 모든 조합에서 실제 extractor validator를 통과시켜 저장하고,
최신 정정 포함·과거 반대 선호 제외·바둑 유지·저장 root 전체 불변을 검사한다.

짧은 최신 evidence에 없는 `가수 김연자`, `트로트 가수 김연자` 등 **무효 5조합은 별도 거절
테스트**다. validator를 완화하지 않았고 후보/저장 write가 없는 것을 확인했다.

추가로 다른 역할/이름, 다중 token 이름, 이름 부분문자열, 범주 label, 조건·속성,
요가/오이, top-K/importance/문자 예산, warm cache 및 일반 memory를 검증했다.

## 4. 실제 callback payload 전후

아래는 callback의 `<user_memories>` JSON에서 content를 추출한 결과다.

```text
수정 전: ["바둑을 좋아해."]
수정 후: ["김연자 싫어해.", "바둑을 좋아해."]
```

최신 행의 `lastMentionedAt`은 테스트 기준시각 1800000000000 + 200이다.
과거 subject 두 설명형 모두 같은 결과이며, 과거 긍정은 현재 사실로 전달되지 않는다.
반대 방향에서는 최신 긍정이 전달된다. select 반환값만으로 성공을 판단하지 않았다.

## 5. 다른 인물·독립 취향 보존

- 같은 트로트 가수인 김연자/나훈아, 영화 배우 안성기/송강호, 소설 작가 박완서/한강을
  독립 회상한다. 아이유 등 다른 이름에도 같은 정정 규칙을 적용한다.
- 김민/김민수, 김연자/연자, 존 레논/레논의 완전한 이름을 구분한다. 존 레논 전체 이름과
  이상은의 끝 글자를 보존한 채 최신 정정을 회상한다.
- `가수` 범주 label은 서로 다른 이름의 통합 근거가 아니다. validator의 부분문자열 조건을
  통과한 불완전한 `연자` label도 `김연자`의 이름 alias로 승격하지 않는다.
- 매운/단 음식, 아침/저녁 산책, 출근길/퇴근길 조건, 노래/목소리 속성의 독립성을 유지한다.
  서로 다른 역할의 동일 이름 및 불확실한 조건은 기존 보수적 보류를 유지한다.
- 요가/오이 정정, 충돌 없는 바둑 선호·가족 profile·최근 episodic 회상이 유지된다.

16개 방해 후보와 과거 importance 5/최신 1을 넣고 top-K 1/8, 문자 예산 4200/220/80을
검증했다. 최신 행이 예산에 들어가지 못해도 과거 반대 선호로 대체하지 않는다.

## 6. 저장 레코드 및 cache 계약 불변

모든 정상 dispatch는 저장 root 전체를 전후 deep-equal 비교한다. ID, body, evidenceKey,
발언 시각, control/manifest가 회상 때문에 변경되지 않는다. warm dispatch의 추가
GET/PUT/PATCH는 0회다. elder memory root GET/PUT도 없다.

warm cache 테스트는 과거 snapshot → 직접 정정 save → 기존 snapshot 지연 허용 →
`prefetch(force=True)`/완료 대기 → 최신 callback을 구분한다. 기존 15초 refresh throttle,
최대 age 60초, worker 저장 후 갱신 요청 계약은 변경하지 않았다.

## 7. 기존 PASS 항목 회귀

기존 M2 identity 55개, 이전 실제 회상 correction 15개가 이번 full 실행에서 통과했다.
이전 저장 손실·broad 산책·동일 evidence·도착 순서·조사/띄어쓰기·요가/오이 보호를 유지한다.
기존 테스트 파일과 기대값은 이번 작업에서 전혀 수정하지 않았다.

시작 시 SHA-256과 비교해 reset coordinator, shared/exclusive barrier, storage,
runtime/voice 파일이 동일함을 확인했다. H1/M5/M6, cold-cache/epoch, M1 drain/recovery,
oversized body, care, Interaction Manager, AUDIO_LOCK 및 radio 회귀를 full suite에서 실행했다.

## 8. 이번에 추가 변경한 정확한 파일

작업 시작 시 tracked/untracked 전체 hash와 비교한 **4개 파일**이다.
기존 파일 추가 수정 2개와 신규 2개이며 전체 미커밋 파일 수와 다르다.

1. `raspberry-pi-voice-hub/persistent_memory.py` — 회상 helper 하나
2. `docs/DB_SCHEMA.md` — 설명용 label 비교 계약
3. `tests/test_memory_recall_labels.py` — 신규
4. `docs/PERSISTENT_MEMORY_SUBJECT_LABELS.md` — 신규, 이 보고서

## 9. 새 실행 검사와 미검증 범위

| 검사 | 결과 |
| --- | --- |
| 신규 label suite | 12 PASS, 핵심 table 216 payload 조합 |
| 기존 회상 / M2 identity | 15 / 55 PASS |
| H1 barrier / M5 coordinator | 5 / 15 PASS |
| cache / extraction shutdown | 17 / 10 PASS |
| memory/voice integration | 27 PASS |
| manifest / body transport / storage safety | 6 / 2 / 11 PASS |
| Python full (위 Python 항목 포함) | 727 실행: 725 PASS, 2 skip |
| Guardian JS | 114 PASS, skip 0 |
| Pi browser/game JS | 65 PASS, skip 0 |
| production build | PASS, 61 modules |
| git diff --check | PASS (기존 LF/CRLF 안내만) |
| 이번 변경 파일 및 untracked trailing whitespace | 0건 |

2 skip은 번들 Python의 native `webrtcvad-wheels` 및 실제 SDK request serialization
계약 검사 의존성 부재다. skip은 PASS에 포함하지 않았다. 실제 Pi/Linux/audio,
운영 Firebase rules/index와 실제 OpenAI API는 미검증이며 접속/호출하지 않았다.

```powershell
$memoryPython = 'C:\Users\YOUR_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $memoryPython -B -X utf8 -m unittest discover -s tests -p 'test_memory_recall_labels.py' -v
& $memoryPython -B -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/display_browser.test.js tests/memory_game.test.js
npm --prefix apps/guardian-dashboard test
npm --prefix apps/guardian-dashboard run build
git diff --check
git status --short
```

## 10. 알려진 제한사항과 Git 상태

지원 범위는 기존 역할/분야 어휘와 evidence token 경계다. 임의의 직업 표현, 붙여 쓴 설명,
동의어, 생략, 동명이인을 완전히 해석하지 않는다. 설명용 표기와 다른 불확실한 label은
유용한 기억도 보류할 수 있다. 저장·retention에서 이미 없어진 후보를 복구하지 않으며,
기존 cache 지연과 payload 개수/문자 한도도 그대로다.

branch/HEAD는 기준과 동일하고 staged 0개다. 전체 미커밋 상태는 tracked 수정 13개,
untracked 42개(합계 55개)이며 시작 시 13/40개에서 신규 테스트/보고서 두 개가 추가됐다.

git add/commit/push/reset/revert/기존 변경 삭제, Pi 접속·배포, 운영 Firebase 접근,
실제 OpenAI 호출, 환경/인증/서비스 설정 변경, 새 의존성 설치는 하지 않았다.
