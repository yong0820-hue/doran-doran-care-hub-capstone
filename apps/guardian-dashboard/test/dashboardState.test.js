import test from 'node:test'
import assert from 'node:assert/strict'

import {
  compareTimestampsDesc,
  getAiCommandView,
  getLatestAiCommandView,
  getMedicationView,
  hasOtherOpenRelatedEvent,
  normalizeTimestamp,
  selectLatestAiConversationCommand,
  selectLatestMedicationReminder,
  sortByCreatedAtDesc,
  summarizeEvents,
} from '../src/dashboardState.js'
import { formatTime, relativeTime } from '../src/utils.js'

test('normalizeTimestamp accepts only non-negative safe-integer numeric Unix milliseconds', () => {
  assert.equal(normalizeTimestamp(0), 0)
  assert.equal(normalizeTimestamp(1_726_465_600_000), 1_726_465_600_000)

  for (const invalid of [
    '1726465600000', true, false, null, undefined, NaN, Infinity, -1, 1.5,
    Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER + 1,
  ]) {
    assert.equal(normalizeTimestamp(invalid), null)
  }
})

test('timestamp comparison sorts newest valid values first and invalid values last', () => {
  assert.equal(compareTimestampsDesc(300, 200), -1)
  assert.equal(compareTimestampsDesc(200, 300), 1)
  assert.equal(compareTimestampsDesc(200, 200), 0)
  assert.equal(compareTimestampsDesc('999', 100), 1)
  assert.equal(compareTimestampsDesc(100, false), -1)
  assert.equal(compareTimestampsDesc('999', false), 0)

  const original = [
    { id: 'invalid-string', createdAt: '999' },
    { id: 'older', createdAt: 100 },
    { id: 'invalid-bool', createdAt: true },
    { id: 'newer', createdAt: 300 },
  ]
  assert.deepEqual(sortByCreatedAtDesc(original).map(({ id }) => id), [
    'newer', 'older', 'invalid-string', 'invalid-bool',
  ])
  assert.equal(original[0].id, 'invalid-string', 'sorting must not mutate caller data')
})

test('timestamp display rejects non-millisecond values and handles past or future times', () => {
  const now = 1_726_465_600_000
  assert.equal(relativeTime(now - 2 * 60_000, now), '2분 전')
  assert.equal(relativeTime(now + 3 * 60_000, now), '3분 후')
  assert.equal(relativeTime('1726465600000', now), '-')
  assert.equal(formatTime(true), '-')
})

test('summarizeEvents treats only open as unresolved and keeps unknown states separate', () => {
  const summary = summarizeEvents({
    resolved: { status: 'resolved', createdAt: 200, severity: 'warning' },
    open: { status: 'open', createdAt: 300, severity: 'critical' },
    processing: { status: 'processing', createdAt: 400 },
    missing: { createdAt: 100 },
  })

  assert.equal(summary.totalCount, 4)
  assert.equal(summary.unresolvedCount, 1)
  assert.equal(summary.openCount, 1)
  assert.equal(summary.resolvedCount, 1)
  assert.equal(summary.unknownCount, 2)
  assert.equal(summary.latestEvent.id, 'processing')
  assert.deepEqual(summary.openEvents.map(({ id }) => id), ['open'])
  assert.deepEqual(summary.resolvedEvents.map(({ id }) => id), ['resolved'])
  assert.deepEqual(summary.unknownEvents.map(({ id }) => id), ['processing', 'missing'])
})

test('an open to resolved realtime replacement immediately clears the unresolved count', () => {
  const open = [{ id: 'event_1', status: 'open', createdAt: 100 }]
  const resolved = [{ id: 'event_1', status: 'resolved', createdAt: 100, resolvedAt: 200 }]

  assert.equal(summarizeEvents(open).unresolvedCount, 1)
  const next = summarizeEvents(resolved)
  assert.equal(next.unresolvedCount, 0)
  assert.equal(next.resolvedCount, 1)
})

test('device emergency state is not cleared while another related event remains open', () => {
  const current = {
    id: 'emergency_a', type: 'emergency_call', status: 'open', deviceId: 'device_001',
  }
  const events = [
    current,
    { id: 'emergency_b', type: 'emergency_call', status: 'open', deviceId: 'device_001' },
    { id: 'emergency_other', type: 'emergency_call', status: 'open', deviceId: 'device_002' },
  ]

  assert.equal(hasOtherOpenRelatedEvent(events, current, 'device_001'), true)
  assert.equal(
    hasOtherOpenRelatedEvent(events.filter((event) => event.id !== 'emergency_b'), current, 'device_001'),
    false
  )
})

test('latest medication reminder uses only numeric timestamps and ignores other event types', () => {
  const latest = selectLatestMedicationReminder([
    { id: 'invalid', type: 'medication_reminder_sent', createdAt: '999' },
    { id: 'other', type: 'long_inactivity', createdAt: 500 },
    { id: 'older', type: 'medication_reminder_sent', createdAt: 100 },
    { id: 'newer', type: 'medication_reminder_sent', createdAt: 300 },
  ])
  assert.equal(latest.id, 'newer')
})

test('medication processing maps to a pending guardian view', () => {
  const view = getMedicationView([{
    id: 'med_1', type: 'medication_reminder_sent', status: 'open', createdAt: 100,
    meta: { voiceCheck: { status: 'processing' } },
  }])
  assert.equal(view.state, 'pending')
  assert.equal(view.label, '복약 확인 중')
  assert.equal(view.badgeClass, 'badge-info')
  assert.equal(view.isPending, true)
  assert.equal(view.event.id, 'med_1')
})

test('completed medication outcomes map actual Pi values to concise guardian states', () => {
  const cases = [
    ['confirmed', 'confirmed', '복용 확인됨', 'badge-success'],
    ['defer', 'deferred', '복용 미룸', 'badge-warning'],
    ['no_speech', 'no_speech', '응답 없음', 'badge-warning'],
    ['unclear', 'unclear', '응답 확인 필요', 'badge-warning'],
    ['cancelled', 'cancelled', '확인 취소됨', 'badge-neutral'],
    ['error', 'error', '확인 실패', 'badge-danger'],
  ]

  for (const [outcome, state, label, badgeClass] of cases) {
    const view = getMedicationView([{
      id: outcome,
      type: 'medication_reminder_sent',
      status: outcome === 'confirmed' ? 'resolved' : 'open',
      createdAt: 100,
      meta: { voiceCheck: { status: 'completed', outcome } },
    }])
    assert.equal(view.state, state)
    assert.equal(view.label, label)
    assert.equal(view.badgeClass, badgeClass)
    assert.equal(view.isPending, false)
  }
})

test('medication view distinguishes legacy and resolved-without-result records without inference', () => {
  const legacy = getMedicationView([{
    id: 'legacy', type: 'medication_reminder_sent', status: 'open', createdAt: 100,
  }])
  assert.equal(legacy.state, 'legacy')
  assert.equal(legacy.label, '복약 안내됨')

  const resolved = getMedicationView([{
    id: 'resolved', type: 'medication_reminder_sent', status: 'resolved', createdAt: 100,
  }])
  assert.equal(resolved.state, 'resolved_without_result')
  assert.equal(resolved.label, '해결됨')
  assert.match(resolved.detail, /결과 기록 없이/)

  const none = getMedicationView([])
  assert.deepEqual(none.event, null)
  assert.equal(none.state, 'none')
})

test('latest AI selection filters command type and puts invalid timestamps behind valid ones', () => {
  const command = selectLatestAiConversationCommand({
    invalid: { type: 'start_ai_conversation', status: 'failed', createdAt: '999' },
    other: { type: 'restart_device', status: 'pending', createdAt: 500 },
    older: { type: 'start_ai_conversation', status: 'processed', createdAt: 100 },
    newer: { type: 'start_ai_conversation', status: 'processing', createdAt: 300 },
  })

  assert.equal(command.commandId, 'newer')
  assert.equal(command.status, 'processing')

  const fromArray = selectLatestAiConversationCommand([
    { id: 'array_command', type: 'start_ai_conversation', status: 'pending', createdAt: 10 },
  ])
  assert.equal(fromArray.commandId, 'array_command')
})

test('latest AI selection uses command ID as a deterministic timestamp tie-breaker', () => {
  const command = selectLatestAiConversationCommand({
    command_a: { type: 'start_ai_conversation', status: 'processed', createdAt: 300 },
    command_b: { type: 'start_ai_conversation', status: 'pending', createdAt: 300 },
  })

  assert.equal(command.commandId, 'command_b')
})

test('AI command statuses expose Korean views and precise busy/conversation flags', () => {
  const cases = [
    ['pending', '대기 중', true, false, 'badge-info'],
    ['processing', '원격 요청 처리 중', true, null, 'badge-info'],
    ['processed', '요청 처리 완료', false, false, 'badge-success'],
    ['failed', '대화 실패', false, false, 'badge-danger'],
  ]

  for (const [status, label, isBusy, isConversationActive, badgeClass] of cases) {
    const command = { commandId: `command_${status}`, status }
    const view = getAiCommandView(command)
    assert.equal(view.command, command)
    assert.equal(view.status, status)
    assert.equal(view.label, label)
    assert.equal(view.isBusy, isBusy)
    assert.equal(view.isConversationActive, isConversationActive)
    assert.equal(view.badgeClass, badgeClass)
  }
})

test('processed AI command with no speech is not presented as a completed conversation', () => {
  const view = getAiCommandView({ status: 'processed', outcome: 'no_speech' })
  assert.equal(view.status, 'processed')
  assert.equal(view.label, '응답 없음')
  assert.equal(view.badgeClass, 'badge-warning')
  assert.equal(view.isConversationActive, false)
})

test('AI helpers return stable none and unknown views', () => {
  const none = getLatestAiCommandView({})
  assert.equal(none.command, null)
  assert.equal(none.status, 'none')
  assert.equal(none.isBusy, false)
  assert.equal(none.isConversationActive, false)

  const unknown = getAiCommandView({ status: 'cancelled' })
  assert.equal(unknown.status, 'unknown')
  assert.equal(unknown.label, '상태 확인 필요')
  assert.equal(unknown.isBusy, false)
  assert.equal(unknown.isConversationActive, false)
})
