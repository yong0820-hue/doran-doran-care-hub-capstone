# Firebase Realtime Database Schema — Doran-Doran Care Hub

## Design Principles

- Flat, denormalized tree — Firebase recommends avoiding deep nesting.
- All timestamps are Unix epoch milliseconds (UTC), stored as `createdAt`.
- Event schema is identical whether the source is a mock script or real hardware.
- Events are keyed under the elder ID, not device ID, for efficient guardian queries.

---

## Tree Structure

```
/
├── users/
│   └── {elderId}/
│       ├── name: string           # "김영자"
│       ├── age: number            # 78
│       ├── overallStatus: string  # "양호" | "주의" | "위험"
│       ├── presenceStatus: string # "in" | "out"
│       ├── presenceLocation: string  # "거실", "침실", etc.
│       └── lastSeen: number       # Unix ms
│
├── devices/
│   └── {deviceId}/
│       ├── name: string           # "도란도란 거실 라디오"
│       ├── type: string           # "button" | "reed_switch" | "radar" | "mock"
│       ├── location: string       # "거실", "침실", etc.
│       ├── status: string         # "online" | "offline"
│       └── lastSignal: number     # Unix ms
│
├── events/
│   └── {elderId}/
│       └── {eventId}/             # Firebase push key or custom ID
│           ├── type: string       # see Event Types below
│           ├── label: string      # Korean display label, e.g. "낙상 의심 감지"
│           ├── severity: string   # "critical" | "warning" | "success" | "info"
│           ├── status: string     # "open" | "resolved"
│           ├── deviceId: string   # e.g. "device_001"
│           ├── deviceName: string # denormalized device name
│           ├── location: string   # denormalized from device, e.g. "거실"
│           ├── createdAt: number  # Unix ms
│           └── meta: object       # optional, type-specific payload
│
├── commands/
│   └── {elderId}/
│       └── {deviceId}/
│           └── {commandId}/     # See Firebase AI 명령 below
│               ├── type: string # "start_ai_conversation"
│               ├── status: string # "pending" | "processing" | "processed" | "failed"
│               └── createdAt: number # Unix ms
│
├── medicationSchedules/
│   └── {elderId}/
│       └── {deviceId}/
│           ├── schemaVersion: 1
│           ├── timezone: "Asia/Seoul"
│           ├── revision: string
│           ├── updatedAt: number
│           ├── updatedBy: "guardian_dashboard"
│           └── entries/
│               └── {scheduleId}/
│                   ├── enabled: boolean
│                   ├── time: string        # strict HH:MM
│                   ├── medicationName: string
│                   └── weekdays: number[]  # ISO Monday=1 ... Sunday=7
│
├── conversations/
│   └── {elderId}/
│       └── {sessionId}/          # Pi: pi_chat_<uuid>
│           ├── elderId: string
│           ├── deviceId: string
│           ├── createdAt: number # Session start, Unix ms
│           ├── updatedAt: number # Latest message time, Unix ms
│           └── messages/
│               └── {messageId}/
│                   ├── messageId: string
│                   ├── role: string       # "user" | "assistant"
│                   ├── text: string
│                   ├── createdAt: number  # Unix ms
│                   ├── turnIndex: number  # 1-based within session
│                   └── audioStatus: string # See AI Conversation Logging
│
├── memoryResetOperations/
│   └── {elderId}/{operationId}/
│       ├── status: "requested"|"processing"|"completed"|"failed"
│       ├── deviceId: string
│       ├── deviceStatus: "{deviceId}|{status}" # indexed pending queue
│       ├── intent: {version: 1, generation, epoch, operationId: string|null}
│       ├── requestedAt: number       # Firebase server timestamp
│       ├── requestedBy: "guardian_dashboard"
│       ├── processingAt/completedAt: number
│       ├── generation/resultingGeneration: number|string
│       ├── coalesced: boolean
│       ├── coalescedTo: string       # follower's permanent leader operation
│       └── resetEpoch/deviceEpoch: number/string # leader-only epoch index
│
└── memories/
    └── {elderId}/
        ├── _control/                  # <=64KiB; sole CAS boundary
        │   ├── schemaVersion: 2
        │   ├── generation: number|string # absent means "initial"
        │   ├── activeIds: {bodyKey: true} # <=128 immutable bodies
        │   ├── archiveIds: {bodyKey: archivedAt} # <=64
        │   ├── pendingReset: {operationId, createdAt}
        │   ├── resetOperations: {operationId: receipt} # <=32 statuses
        │   ├── lastResetAt: number
        │   ├── resetCutoffAt: number    # server reset timestamp
        │   ├── resetCoordinator: {activeOperationId, deviceId, epoch, state,
        │   │                     baseGeneration, resultingGeneration, claimedAt, completedAt}
        │   ├── resetCoveredIds: {operationId: true} # <=64; v4 compatibility read only
        │   ├── resetAt/resetBy/lastResetOperationId: legacy optional metadata
        │   ├── legacyMigration: string
        │   └── maintenanceRevision: string
        ├── active/{immutableBodyKey}/ # <=4096 UTF-8 bytes per validated body
        │   ├── id: string             # stable logical ID; different from storage key
        │   ├── elderId: string
        │   ├── type: "episodic"|"profile"|"preference"
        │   ├── subject: string        # <=60 characters
        │   ├── content: string        # <=280 characters of verified evidence
        │   ├── evidenceKey: string    # SHA-256 evidence digest
        │   ├── topicKey: string       # preference normalized subject
        │   ├── createdAt/updatedAt/lastMentionedAt: number
        │   ├── importance: number     # 1..5
        │   ├── source: "conversation"
        │   ├── sourceSessionId: string
        │   ├── status: "active"
        │   ├── tags: string[]         # optional <=6, each <=30 chars
        │   ├── eventDate: string      # optional Seoul YYYY-MM-DD
        │   ├── eventDatePrecision: "day"|"week"
        │   └── validUntil: number     # optional epoch ms
        ├── archive/{immutableBodyKey}/ # same body + archivedAt/archiveReason/status
        └── {legacyDirectChild}/       # untouched, never normal retrieval
```

## Persistent Memory v1 with storage protocol v2 and Pi reset operations

The bounded control manifest is the only authority for visible memory. Normal
retrieval reads `_control` and at most 128 referenced active leaves. It never
GETs the elder bucket or archive/legacy data. Active body reads stream in 1 KiB
chunks and close after exceeding 4 KiB, before JSON parsing (at most 5 KiB consumed
per malformed body). Writers PUT immutable bounded bodies and CAS `_control` to
publish; late old-generation PUTs can leave invisible orphans but cannot publish
through reset. Conversation writers preserve manifest entries owned by other
sources/elders and entries whose ownership cannot be established within the
bounded body read. Only conversation-owned entries are replaced/pruned, within
the same total 128 slots. There is no full elder PUT.

Before enabling final confirmation, Guardian prepares a read-only intent from
`_control` generation/epoch and any active coordinator. An already-completed
permanent leader is resolved even if its coordinator finish CAS is delayed.
Final confirmation uses that immutable intent, so delayed preflight/network I/O
cannot turn a concurrent click into a later independent destructive reset.
Guardian only creates `memoryResetOperations/{elderId}/{operationId}` with requested
status, deviceStatus, intent and server requestedAt, then observes this same operation. Only completed
means SUCCESS. Requested/processing means WAITING_FOR_DEVICE. Read failures,
including permission_denied, missing receipts and ambiguous writes mean UNKNOWN;
the per-operation local recovery marker stays and no new request is issued.
Reload/reconnect reconciliation is read-only. Only authoritative completed/failed
results remove the marker.

The single-device Pi worker polls deviceStatus requested/processing queries
(32 each); a legacy key cursor scans at most 33 old rows per poll for upgrades.
The elder operation collection needs `.indexOn: ["deviceStatus", "deviceEpoch"]`.
`deviceEpoch` is a leader-only permanent result lookup (at most 2 rows) used when
a delayed preclaim intent outlives the 32 internal receipts. It acquires the local
exclusive dispatch barrier, claims resetCoordinator and pendingReset in the
same `_control` CAS domain, archives only verified conversation-owned bodies,
CASes generation/manifests, installs the local cache/generation and then publishes
completed before releasing exclusivity. Old extraction publication fails its CAS.
Same-ID retry uses a bounded control receipt or the permanent external operation;
restart resumes outstanding processing. Claim, generation/manifests and coordinator
finish are serialized by `_control` CAS. The coordinator progresses processing →
committed → completed; external leader completion follows local invalidation,
while still under the exclusive barrier. Requests captured during an active
epoch bind its leader; preclaim requests bind the next epoch by generation/epoch.
Both coalesce even if their operation arrives after the destructive CAS or after
later resets. Completed followers keep their own operation IDs and point to the
leader's exact generation. A fresh prepared intent after completion can reset
again even in the same millisecond. Legacy requests without intent use conservative
server timestamp coalescing; same-millisecond legacy requests cannot distinguish
a new intent and should be upgraded with the dashboard.

The product contract is: after Guardian SUCCESS, no new LLM request can start
with pre-reset persistent memory. A shared barrier covers final memory selection
through the synchronous Responses SDK return, thus also actual HTTP initiation.
Readers have no TTL. A 250 ms reset attempt timeout retains writer intent and
retries; new turns use no persistent memory immediately. Offline/suspended Pi
cannot publish completion. A host OS advisory lock for the backend+elder permits
only one memory-enabled runtime; paused dispatch/reset retains it on shutdown
timeout. Future multi-device support needs every device to fence and acknowledge
before completion; current v1 supports one configured device and OS account.
All dashboards and Pi runtimes must use this protocol together; legacy TTL reset
clients are incompatible. Guardian needs read permission for the bounded `_control`
and leader operations, in addition to its existing operation create/read permission.
Guardian memory/control writes must be disallowed by
production rules; only the
owning Pi may mutate memory and complete operations. No deployed rules were changed.

Cache refresh remains 15 s, maximum age 60 s, local cache 128 KiB. Warm dispatch
needs no HTTP request. Cold sessions are WAITING then READY; reset makes the old
session INVALIDATED. The durable extraction outbox resolves missing generations
against lastResetAt plus the durable local reset epoch. Registered sessions and
WAITING jobs are fenced by that epoch even with Pi/server clock skew; idempotent
reconciliation of the same generation does not invalidate newly captured work.

Preference identity resolves matching evidence to its existing owner before
considering an extractor's target subject. Normalized evidence duplicates cannot
overwrite an independent broad preference. Destructive merging additionally
requires equal complete targets supported by both quoted statements; a shared
subject, category/subset relation, or single remaining candidate is insufficient.
A bounded recognizer handles simple Korean like/dislike clauses, retaining all
target modifiers and normalizing Unicode, spacing and supported final particles.
Unsupported/compound grammar supplies no inferred identity. Noun endings such as
요가/오이 are not stripped; name/category aliases are not inferred. The comparison
target is derived locally, not added as a schema field; topicKey remains a
normalized display subject and is not an identity authority.

An evidence-supported, more specific subject can refine the same logical ID when
both statements prove the same complete target and the new label spans complete
target tokens. A broad re-extraction cannot redirect an evidence owner into a
different preference. Newest utterance wins only within a proven identity; stale
extraction cannot replace newer content/evidence/timestamps. Independent and
uncertain records, including delayed general preferences, remain separate under
the existing retention bounds. There is no category-based archive/discard step.

Recall resolves current preferences in the bounded local snapshot before query
ranking, top-K and the prompt character budget, without changing stored rows.
Equal evidence-supported full targets use the newest lastMentionedAt. Recall also
recognizes a bare subject and a complete terminal subject with a supported role
description, such as a genre plus singer: only a closed role/field vocabulary is
allowed, never arbitrary modifiers or entity-name aliases. This comparison key is
derived from the complete evidence target, not the extractor label. A supported
role prefix is separated only at the start of that target, retaining the entire
remaining name span. The label must exactly name that whole span, role plus name,
or the complete field/role/name target. These descriptive label variants therefore
select the same current preference without changing extraction validation or
stored identity. Partial names and category-only labels do not qualify. Competing roles for
one subject remain ambiguous. Different contents with equal utterance timestamps
are withheld rather than selected by length, polarity, relevance or importance.

For uncertain general/specific overlap, a newer specific may remain while the
older general is withheld. If the general is newer or equally dated, both are
withheld so the older specific cannot be sent alone as current truth. An unparsed
target with a related label also withholds both candidates. Distinct explicit
targets, attributes and conditions remain eligible together. This conservative
policy can omit useful memories and does not resolve arbitrary Korean grammar,
synonyms or homonyms. Cache refresh remains 15 s / maximum age 60 s; a direct
store.save does not synchronously replace a warm snapshot. After refresh completes,
dispatch applies the same current-preference rules to the new snapshot.
See `PERSISTENT_MEMORY_M2_FOLLOWUP.md` for storage path tests and
`PERSISTENT_MEMORY_RECALL_FOLLOWUP.md` for actual dispatch payload regressions;
`PERSISTENT_MEMORY_SUBJECT_LABELS.md` covers descriptive-label invariance.
Memory remains untrusted user reference data, separate
from medication, inactivity, emergency, fall, device/sensor and event truth.

Active manifest retention is 128, archive retention 64/30 days on reset, internal
receipts 32. External operations remain for authoritative recovery; their cleanup
is a separate administrative policy. Profiles/preferences have no automatic
expiry; dated episodic expiry is eight days after the event/week interval, and
undated episodic expiry is 30 days. Explicit bounded legacy migration and orphan
maintenance do not run on the voice path and were not run against production.

Local `/api/conversation-reset` still clears short-term history at the audio/care
boundary and preserves persistent memories. Device reset remains a non-executing
interface. Current tests, exact races and limitations are in
[Persistent Memory 5th review](PERSISTENT_MEMORY_V5_REVIEW.md).

## Remote medication schedules

The guardian dashboard stores one versioned document at
`medicationSchedules/{elderId}/{deviceId}`. Schedule IDs are immutable Firebase
push IDs: editing a time, name, weekdays, or enabled state must keep the same ID.
This gives the Pi a stable once-per-date identity even after a time edit.

`entries` may be absent in Firebase when the user explicitly saves zero entries;
the surrounding `schemaVersion`, `timezone`, and `revision` fields make this a
valid **no reminders** setting. A missing whole document is different: the Pi
reports `reason=missing` and uses the validated `MEDICATION_TIMES` fallback
(08:00, 12:00, and 19:00 by default). Invalid data or read failures retain the
last valid remote snapshot; on cold start they use the same explicit fallback.

The Pi polls every five seconds by default, validates the complete document, and
publishes the applied state at
`devices/{deviceId}/medicationScheduleStatus`. `state=remote` means the matching
revision is active. `state=last_valid` or `state=fallback` always includes a
`reason` (`missing`, `invalid`, or `unavailable`) and must not be displayed as a
successful remote load.

Enabled entries require at least one ISO weekday. Two enabled entries may not
share both a time and weekday. A new or changed schedule whose time is already
current/past when the Pi sees the revision is not run retroactively. A future
edit is eligible, but a stable schedule ID plus date-based Firebase event ID
prevents a second reminder if that schedule already ran earlier that day.

Scheduled reminder events keep `meta.scheduledFor` and add the schedule snapshot:

```json
{
  "meta": {
    "scheduleId": "-firebasePushId",
    "scheduleRevision": "-revisionPushId",
    "medicationName": "혈압약",
    "scheduledFor": "2026-09-16_08:00",
    "voiceCheck": {
      "status": "completed",
      "outcome": "confirmed"
    }
  }
}
```

Voice confirmation continues to use `voiceCheck` and its existing outcomes.
When confirmation is disabled, the durable claim records
`meta.reminderDelivery` with `outcome=announced`; it never invents a
`confirmed`, `defer`, or `no_speech` voice result. Event IDs for remote schedules
use `{deviceId}_{date}_{scheduleId}`, so a restart or same-day time edit cannot
repeat the attempt.

---

## SOS / fall state consistency

The source of truth is the stored event collection: `emergency_call` and
`fall_suspected` events with `status=open` or legacy `status=unread`. For an
explicit `deviceId`, `devices/{deviceId}/emergencyStatus` is `active` while any
such event remains, otherwise `normal`. `sensors/radar/fallRisk` is independently
`high` while any open/unread fall remains, otherwise `low`. Device IDs are global,
so derivation includes matching events in every elder bucket. Unknown statuses
and events without a device ID are not silently assigned to the viewed device.
Resolving an older event without `deviceId` changes only that event.

The dashboard resolves events using a Firebase RTDB transaction at `/`, the
common ancestor of the unchanged `events` and `devices` paths. The callback
reads the stored type/device, resolves the event, and derives both device fields
from that same candidate snapshot. Concurrent changes cause the server to reject
the stale hash and the SDK to rerun the callback. Caller snapshot hints are
ignored. Repeated resolution keeps the first `resolvedAt` and all metadata.
Missing/non-actionable events do not create phantom records or clear devices.

Dashboard SOS/fall creation uses the same transaction boundary. A push ID is
allocated once per call, outside the callback. An optional fourth `pushEvent`
argument is a stable event ID for retrying the same creation intent: matching
type/device/elder/creation time keeps the existing record, including any later
resolution, and does not replay the sensor patch. A conflicting identity fails.
A new call without the same ID represents a new event. ESP32 already atomically
creates a new open SOS and `active` in a root multi-location PATCH and requires
no change; such external arrivals conflict with and retry pending resolutions.
Pi care and local display API paths do not write SOS/fall state.

Cold/partial browser caches return an unchanged candidate until the server checks
the hash. Results are validated after commit. The SDK's same-client `Error('set')`
cancellation (for example, another command write) is retried up to three total
transaction attempts; permission failures and exhausted retries are surfaced.
Optimistic local transaction events are disabled (`applyLocally=false`).

Limits: this schema-compatible strategy requires root read/write permission and
reads/transmits the root, so unrelated writes may cause contention or eventual
retry failure. Do not broaden production access rules to accommodate it; a
trusted backend or future schema migration is needed for scoped production
access. Local mocks verify interleavings, not deployed rules or server behavior.
Old dashboard clients, out-of-band event-only edits, deletion, or replaying an
old ESP32 event PATCH can bypass this protocol; there is no background repair
job or data migration. Existing mismatches for a target device are repaired on
its next SOS/fall mutation. All writers must follow this contract.

---

## Event Types

| `type` value | Trigger | `meta` payload |
|---|---|---|
| `button_press` | Physical button or mock | `{ durationMs: number }` |
| `pillbox_open` | Reed switch opens | `{}` |
| `pillbox_close` | Reed switch closes | `{}` |
| `presence_detected` | Radar detects motion | `{ confidence: number }` |
| `presence_lost` | Radar detects no motion | `{ durationMs: number }` |
| `fall_suspected` | Radar detects sudden drop pattern | `{ confidence: number }` |
| `medication_taken` | Pillbox closed after scheduled window | `{}` |
| `medication_delayed` | Medication not taken by scheduled time | `{ delayMins: number }` |
| `mock_test` | Mock generator / seed script | `{ note: string }` |
| `medication_reminder_sent` | Pi medication voice reminder attempted | Top-level `runId`, `title`, `message` |
| `inactivity_voice_check_sent` | Pi voice check for a new open `long_inactivity` event | Top-level `sourceEventId`, `runId`, `title`, `message` |
| `long_inactivity` | Sensor inactivity; proactive Pi check updates the same source | Existing sensor fields plus optional `meta.voiceCheck` below |
| `ai_conversation_message` | Pi recognized user speech or completed AI text | Top-level `sessionId`, `messageId`, `role`, `runId`, `title`, `message` |

Pi events use `title`/`message` rather than `label`/`meta`. The new AI event follows the existing Pi event format and is informational (`severity=info`, `status=open`).

## Proactive long-inactivity care

When AI is running in `hub`, the Pi consumes existing open `events/{elderId}/{eventId}`
records with `type=long_inactivity`. Older open events are eligible, with no startup
cutoff. `createdAt` must be a nonnegative integer, not a boolean; `elderId`, if
present, must match the path. `deviceId` remains the sensor's ID. Malformed
metadata is ignored instead of replaced. An existing `inactivity_voice_check_sent`
whose `sourceEventId` matches also prevents another voice check.

The source event's existing optional `meta` object gains one additive field:

```json
{
  "meta": {
    "voiceCheck": {
      "status": "completed",
      "claimToken": "<random-token>",
      "attemptedAt": 1789460000000,
      "attemptedBy": "device_001",
      "completedAt": 1789460010000,
      "outcome": "okay"
    }
  }
}
```

Before audio, ETag GET + `If-Match` PUT writes `status=processing`, `claimToken`,
`attemptedAt`, and `attemptedBy`. Timestamps use Firebase server time. **Any**
existing `meta.voiceCheck` key prevents a new/restarted worker from speaking for
this source, even if malformed or unfinished. An uncertain claim response is
verified against the same random token before audio. Competing claims lose with
HTTP 412. Source identity (`type`, `createdAt`, `elderId`, `deviceId`) is checked
again; a corrected/replaced source is never overwritten.

After an existing conversation finishes, ownership and open status are reread
before the check-in begins. A queued event already resolved by a guardian is
recorded as `cancelled` without audio. An unavailable read produces `error` without
audio. A guardian can still update the event after that final read; completion
uses a fresh ETag and preserves the latest resolution/fields.

| `outcome` | Source event mutation |
|---|---|
| `okay` | If still open and noncritical: `status=resolved`, `resolvedAt=server timestamp` |
| `help` | `severity=critical`, help-request `title` and existing `label`; keep current status, never reopen a guardian resolution |
| `unclear`, `no_speech` | Keep source status/severity; complete the attempt metadata |
| `cancelled` | Source already resolved while queued; no audio |
| `error` | Keep source status/severity; add safe `errorType` class name to the attempt metadata |

Every outcome finalizes marker `status=completed`, `completedAt`, and `outcome`.
Already-critical events cannot be resolved by a reassurance. Unchanged open help
events remain open/critical and appear in the existing guardian urgent panel;
no separate emergency schema, phone call, or sensor-state mutation is introduced.
Other metadata and original timestamps/device fields are retained. Completion
conflicts retry using current data, or preserve an externally changed claim.

Pending completion is retried in memory before another source is accepted. Audio
is never replayed for a completion retry. A crash after claiming but before audio
can skip the check; a crash before result delivery can leave `processing` without
the recognized outcome. Such events stay visible for guardian follow-up and are
not automatically reclaimed. This is an at-most-once attempt guarantee, not
guaranteed delivery. Removing markers/deleting source events manually defeats
deduplication. Legacy announcement-only modes retain their older behavior, but
skip these markers and existing linked announcement records.

Recognized care replies reuse the existing conversation-message/outbox format
under a fresh care session. The check-in question is control audio, like the
listening prompt, and is not saved as an assistant message. No transcript is added
to source metadata. Source and session IDs are linked in care logs; ordinary chat
memory remains separate. New care checks don't create `inactivity_voice_check_sent`.

## AI Conversation Logging

Each recognized user message and completed assistant text creates one message and one linked event. A successful spoken turn therefore creates two messages and two events. The same `turnIndex` links the user and assistant pair; it increases after each nonempty transcription, including turns whose later API or playback step fails.

Message path:

```text
conversations/{elderId}/{sessionId}/messages/{messageId}
```

```json
{
  "messageId": "000001_assistant_<uuid>",
  "role": "assistant",
  "text": "뉴스를 보셨군요. 어떤 소식이 기억에 남으셨어요?",
  "createdAt": 1788822000000,
  "turnIndex": 1,
  "audioStatus": "played"
}
```

Linked event path:

```text
events/{elderId}/pi_ai_conversation_message_{messageId}
```

```json
{
  "elderId": "elder_001",
  "deviceId": "device_001",
  "type": "ai_conversation_message",
  "title": "AI 손주 대화",
  "message": "뉴스를 보셨군요. 어떤 소식이 기억에 남으셨어요?",
  "status": "open",
  "severity": "info",
  "createdAt": 1788822000000,
  "location": "거실",
  "runId": "pi_ai_conversation_run_pi_chat_<uuid>",
  "sessionId": "pi_chat_<uuid>",
  "messageId": "000001_assistant_<uuid>",
  "role": "assistant"
}
```

`audioStatus` describes the message, not the event's delivery status:

| Role | `audioStatus` | Meaning |
|---|---|---|
| `user` | `recorded` | Audio recorded and nonempty transcript obtained |
| `assistant` | `played` | TTS generated and player exited successfully |
| `assistant` | `failed` | Text completed but TTS generation or playback failed |
| `assistant` | `cancelled` | Conversation stopped during the speech stage |

An empty/failed transcription creates no message. The “말씀해주세요.” prompt is played before recording and is not a conversation message or model-history entry. Prompt synthesis/playback failure prevents recording and creates no message. If text generation fails after transcription, only the user message is recorded. The event alone does not prove that the elder heard a reply; read the linked message's `audioStatus`.

For each message, one root-level REST `PATCH` atomically updates the message, session metadata (`elderId`, `deviceId`, `createdAt`, `updatedAt`), linked event, and `devices/{deviceId}/lastSeen` and `lastSignal`. Message/event timestamps reflect creation time, not retry time. Device heartbeats use `{ ".sv": "timestamp" }`, resolved by Firebase to numeric Unix milliseconds at delivery. Existing `events` and `memories` data are not replaced.

Before delivery, the Pi durably queues the patch in `conversation_outbox/`. A separate worker processes up to five queued messages in insertion order per pass, then waits two seconds. It first `GET`s the exact linked event: matching immutable message identity proves prior atomic delivery, so it removes the pending file without replaying the `PATCH`. This preserves a guardian's later `resolved` status after an acknowledgement was lost. A missing event leads to the atomic `PATCH`; a conflict, malformed result, or read/write failure retains the item and stops the pass. Firebase read and write access are both required. Each GET/PATCH has a 10-second HTTP timeout. Retries continue while the controller runs; there is no five-attempt limit. Pending files survive restart and resume when AI is enabled again. If local enqueue itself fails, the program logs a warning and cannot guarantee that message's retention.

The outbox contains transcript text and must be treated as private application data. It contains no API keys or database URL. Raw input, listening-prompt MP3, and generated reply audio are temporary files; cleanup is attempted after each turn, including failed turns. A forced process exit or shutdown during an active API request can leave temporary files behind. Firebase stores text, not audio.

The model's in-memory history includes only successfully played user/assistant pairs, bounded by `AI_HISTORY_TURNS` (default 10). Each Firebase dashboard command starts a fresh bounded session; all internal turns share its history and session ID. Terminal/wake-word single turns still reset on terminal `n`, after `AI_SESSION_IDLE_SECONDS` (default 300) of inactivity, and on process restart. The idle reset does not apply inside an active Firebase session. Old Firebase sessions remain as records and are not loaded into model history. The `conversations` tree is separate from the existing `memories` concept.

## Firebase AI 명령

복구한 Pi 런타임은 `hub`에서 AI가 활성화되고 Firebase 트리거가 선택됐을 때 다음 경로를 읽습니다. 대시보드 명령 작성 UI의 복구는 다음 단계이며 기존 이벤트/대화 기록 형식은 유지합니다.

```text
commands/{elderId}/{deviceId}/{commandId}
```

명령 작성 측의 예시입니다. `commandId`는 매 요청마다 고유하게 만들고, `createdAt`에는 실제 요청 시각의 숫자 Unix 밀리초를 넣습니다.

```json
{
  "commandId": "start_ai_<unique-id>",
  "elderId": "elder_001",
  "deviceId": "device_001",
  "type": "start_ai_conversation",
  "status": "pending",
  "createdAt": 1788822000000
}
```

필수 항목은 `type`, `status`, `createdAt`입니다. `createdAt`은 0 이상의 정수여야 하며 문자열/불리언은 받지 않습니다. Firebase 서버 시각 `{ ".sv": "timestamp" }`으로 작성한 경우 읽을 때 숫자로 해석돼 있어야 합니다. 선택 항목 `commandId`, `elderId`, `deviceId`가 존재하면 경로와 정확히 일치해야 합니다. 잘못된 형식, 다른 명령 종류/대상, `pending` 이외의 상태는 실행하지 않고 그대로 둡니다. 유효한 `pending` 중 `createdAt`, `commandId` 순서로 한 번에 하나 처리하며 명령 만료 시간은 없습니다.

Pi가 추가/갱신하는 필드는 다음과 같습니다. 입력 필드는 유지됩니다.

| 필드 | 의미 |
|---|---|
| `commandId`, `elderId`, `deviceId` | 경로에서 확정한 명령/대상 ID |
| `status=processing` | ETag 조건부 `PUT`으로 실행 권한을 확보한 상태 |
| `claimToken` | 해당 실행 시도를 식별하는 임의 토큰 |
| `acknowledgedBy` | 처리 장치 ID |
| `acknowledgedAt` | 실행 권한 확보 시 Firebase 서버 시각 |
| `status`, `outcome` | 아래 표의 최종 처리 결과 |
| `processedAt` | 최종 결과 기록 시 Firebase 서버 시각 |
| `errorType` | 실패 시 예외 클래스 이름. 예외 본문/음성 내용은 기록하지 않음 |

| `status` | `outcome` | 의미 |
|---|---|---|
| `processed` | `completed` | 정상 응답이 한 번 이상 재생된 세션 종료, 또는 명시적 종료 발언으로 종료 |
| `processed` | `no_speech` | 정상 응답·종료 발언 없이 연속 무응답 한도 도달(음성 검증 탈락/빈 STT 포함) |
| `failed` | `failed` | 안내, 녹음, API 요청, 재생, 중단 등에서 예외 발생 |

`pending → processing → processed/failed` 순서로 처리합니다. 개별 명령을 `X-Firebase-ETag: true`로 다시 읽고 `If-Match` 조건부 `PUT`이 성공한 뒤에만 오디오를 실행합니다. 다른 작업자와 충돌한 HTTP 412에는 실행하지 않습니다. 쓰기 응답이 유실되면 읽어서 자신의 `claimToken`과 장치가 `processing`을 소유한 경우에만 진행합니다. 소유 여부를 확인할 수 없으면 같은 프로세스에서 claim 확인을 재시도합니다.

완료 기록도 ETag로 현재 소유권을 확인해 조건부 `PUT`합니다. 완료 전송이 실패하면 다음 명령을 받기 전에 결과 기록만 재시도하며 오디오는 재생하지 않습니다. 응답 유실 후 읽기에서 동일 최종 결과가 확인되면 완료로 간주합니다. 다른 작업자의 소유권, 명령 작성 측의 수정, 충돌하는 최종 결과는 덮어쓰지 않습니다. 따라서 Firebase 최종 상태가 실제 오디오 결과와 다를 수 있는 충돌은 별도 확인이 필요합니다.

명령당 최대 한 번의 실행 시도를 위한 방식이며 완료를 보장하지 않습니다. claim/완료 재시도 정보는 메모리에만 있습니다. 확보 직후 또는 실행 중 프로세스가 종료되면 `processing`이 남을 수 있으며 재시작 후 자동 재실행/회수하지 않습니다. 명령 상태를 다시 `pending`으로 돌리거나 ID를 재사용하면 중복 방지 전제를 깨뜨립니다. 실제 실행 여부를 확인한 뒤 필요하면 새 ID로 다시 요청합니다. 완료 이력의 자동 삭제/보존 기간 설정은 구현하지 않았습니다.

명령 한 개는 새 연속 대화 세션입니다. 세션 내부에서 같은 `sessionId`와 기억을 유지하며 기본 최대 사용자 5턴(`AI_SESSION_MAX_TURNS`, 정수 1~20), 연속 무응답 2회(`AI_SESSION_MAX_NO_SPEECH`, 정수 1~5), 종료 발언 또는 오류로 끝납니다. 정상 응답 뒤에는 다시 듣고 무응답 횟수를 초기화합니다. 종료 발언은 LLM과 메시지 기록에서 제외합니다. 내부 턴 동안 명령은 `processing`이고 세션 전체에 대해 한 번만 claim/최종 결과를 처리합니다. 이전 응답 성공 후 오류가 나도 전체 명령은 `failed`입니다. 완료 쓰기 재시도는 기록만 전송하며 세션을 재실행하지 않습니다. 자세한 종료 이유는 Pi의 `[Voice Session]` 로그에 남깁니다. 다음 명령은 기억을 초기화합니다. 원격 종료 명령이나 명령과 대화 `sessionId`/`messageId` 연결 필드는 추가하지 않습니다. `processed/completed`는 Firebase 대화 기록의 전송 완료를 뜻하지 않으며, 대화 기록은 독립적인 outbox로 전송합니다.

`AI_COMMAND_POLL_SECONDS`는 기본 5초이며 유한한 1~60초만 허용합니다. 한 명령의 처리나 재시도가 끝난 뒤 지정 시간만큼 대기하므로 요청 간격이 정확히 5초로 고정되지는 않습니다. 각 GET/PUT 제한 시간은 10초입니다. Firebase 명령 경로에 대한 읽기/조건부 쓰기 권한이 필요합니다. 기존 인증/규칙 구성은 이번 복구에서 변경하지 않습니다.

---

## Example Data

```json
{
  "users": {
    "elder_001": {
      "name": "김영자",
      "age": 78,
      "overallStatus": "양호",
      "presenceStatus": "in",
      "presenceLocation": "거실",
      "lastSeen": 1715300000000
    }
  },
  "devices": {
    "device_001": {
      "name": "도란도란 거실 라디오",
      "type": "mock",
      "location": "거실",
      "status": "online",
      "lastSignal": 1715300000000
    }
  },
  "events": {
    "elder_001": {
      "evt_001": {
        "type": "fall_suspected",
        "label": "낙상 의심 감지",
        "severity": "critical",
        "status": "open",
        "deviceId": "device_001",
        "deviceName": "도란도란 거실 라디오",
        "location": "거실",
        "createdAt": 1715300000000,
        "meta": { "confidence": 0.87 }
      }
    }
  }
}
```

---

## Firebase Security Rules (MVP — open for demo)

During the MVP demo phase, rules are open for simplicity. **Must be restricted before any real deployment.**

```json
{
  "rules": {
    ".read": true,
    ".write": true
  }
}
```

Post-MVP: restrict writes to authenticated devices/service accounts and reads to authenticated guardians.
