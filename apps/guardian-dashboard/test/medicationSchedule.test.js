import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildTodayMedicationRows,
  getMedicationApplyView,
  getSeoulDateParts,
  normalizeMedicationScheduleDocument,
  toMedicationSchedulePayload,
  validateMedicationScheduleEntry,
} from '../src/medicationSchedule.js'

const NOW = Date.parse('2026-09-16T10:00:00+09:00')

function document(entries = {}, revision = 'revision_1') {
  return {
    schemaVersion: 1,
    timezone: 'Asia/Seoul',
    revision,
    entries,
  }
}

function entry(overrides = {}) {
  return {
    enabled: true,
    time: '08:00',
    medicationName: '',
    weekdays: [1, 2, 3, 4, 5, 6, 7],
    ...overrides,
  }
}

test('Guardian serializes ISO weekdays for every day and Sunday', () => {
  assert.deepEqual(toMedicationSchedulePayload(entry()).weekdays, [1, 2, 3, 4, 5, 6, 7])
  assert.deepEqual(toMedicationSchedulePayload(entry({ weekdays: [7] })).weekdays, [7])
  assert.deepEqual(toMedicationSchedulePayload(entry({ weekdays: [5, 1, 3, 2, 4] })).weekdays,
    [1, 2, 3, 4, 5])
})

test('schedule normalization distinguishes missing from an explicit empty schedule', () => {
  assert.equal(normalizeMedicationScheduleDocument(null).state, 'missing')
  const empty = normalizeMedicationScheduleDocument(document())
  assert.equal(empty.state, 'ready')
  assert.deepEqual(empty.entries, [])
})

test('schedule normalization accepts strict entries and trims optional medicine names', () => {
  const result = normalizeMedicationScheduleDocument(document({
    schedule_a: entry({ medicationName: '  혈압약  ', weekdays: [3, 1] }),
  }))
  assert.equal(result.state, 'ready')
  assert.deepEqual(result.entries[0], {
    scheduleId: 'schedule_a', enabled: true, time: '08:00',
    medicationName: '혈압약', weekdays: [1, 3],
  })
})

test('invalid times, weekdays, booleans, IDs, and names reject the whole document', () => {
  const invalidEntries = [
    entry({ time: '8:00' }),
    entry({ time: ['09:00'] }),
    entry({ time: '24:00' }),
    entry({ weekdays: [] }),
    entry({ weekdays: [true] }),
    entry({ enabled: 1 }),
    entry({ medicationName: '약'.repeat(81) }),
  ]
  for (const [index, invalid] of invalidEntries.entries()) {
    assert.equal(
      normalizeMedicationScheduleDocument(document({ [`schedule_${index}`]: invalid })).state,
      'invalid'
    )
  }
  assert.equal(
    normalizeMedicationScheduleDocument(document({ 'bad.id': entry() })).state,
    'invalid'
  )
})

test('enabled schedules cannot overlap on a shared weekday and time', () => {
  const result = normalizeMedicationScheduleDocument(document({
    monday: entry({ weekdays: [1] }),
    monday_tuesday: entry({ weekdays: [1, 2] }),
  }))
  assert.equal(result.state, 'invalid')

  const safe = normalizeMedicationScheduleDocument(document({
    monday: entry({ weekdays: [1] }),
    tuesday: entry({ weekdays: [2] }),
    disabled: entry({ enabled: false, weekdays: [1] }),
  }))
  assert.equal(safe.state, 'ready')
})

test('form validation catches duplicate active slots but permits editing the same entry', () => {
  const existing = [{ scheduleId: 'a', ...entry({ weekdays: [3] }) }]
  assert.match(
    validateMedicationScheduleEntry(entry({ weekdays: [3] }), existing).join(' '),
    /이미 있습니다/
  )
  assert.deepEqual(
    validateMedicationScheduleEntry(entry({ weekdays: [3] }), existing, 'a'),
    []
  )
})

test('Seoul date logic does not depend on the browser local timezone', () => {
  assert.deepEqual(getSeoulDateParts(NOW), {
    date: '2026-09-16', time: '10:00', weekday: 3,
  })
})

test('today rows connect schedule IDs to actual confirmed, deferred, and no-speech results', () => {
  const schedule = document({
    confirmed: entry({ time: '08:00', medicationName: '아침약', weekdays: [3] }),
    deferred: entry({ time: '09:00', weekdays: [3] }),
    silent: entry({ time: '10:00', weekdays: [3] }),
    future: entry({ time: '12:00', weekdays: [3] }),
  })
  const events = ['confirmed', 'defer', 'no_speech'].map((outcome, index) => ({
    id: `event_${outcome}`,
    type: 'medication_reminder_sent',
    status: outcome === 'confirmed' ? 'resolved' : 'open',
    createdAt: NOW + index,
    meta: {
      scheduleId: outcome === 'defer' ? 'deferred' : outcome === 'no_speech' ? 'silent' : 'confirmed',
      scheduledFor: `2026-09-16_${String(8 + index).padStart(2, '0')}:00`,
      voiceCheck: { status: 'completed', outcome },
    },
  }))
  const rows = buildTodayMedicationRows(schedule, events, NOW)
  assert.deepEqual(rows.map(({ state }) => state), [
    'confirmed', 'deferred', 'no_speech', 'scheduled',
  ])
  assert.equal(rows[0].medicationName, '아침약')
})

test('today rows retain actual deleted or rescheduled history and never infer a confirmation', () => {
  const schedule = document({
    moved: entry({ time: '12:00', weekdays: [3] }),
  })
  const events = [
    {
      id: 'moved_event', type: 'medication_reminder_sent', status: 'resolved', createdAt: NOW,
      meta: { scheduleId: 'moved', scheduledFor: '2026-09-16_08:00' },
    },
    {
      id: 'deleted_event', type: 'medication_reminder_sent', status: 'open', createdAt: NOW + 1,
      meta: {
        scheduleId: 'deleted', scheduledFor: '2026-09-16_09:00', medicationName: '점심약',
        voiceCheck: { status: 'completed', outcome: 'defer' },
      },
    },
  ]
  const rows = buildTodayMedicationRows(schedule, events, NOW)
  assert.deepEqual(rows.map(({ time, state }) => [time, state]), [
    ['08:00', 'resolved_without_result'], ['09:00', 'deferred'],
  ])
})

test('event snapshots preserve an explicitly empty medicine name after schedule edits', () => {
  const schedule = document({
    renamed: entry({ time: '08:00', medicationName: '새 약 이름', weekdays: [3] }),
  })
  const rows = buildTodayMedicationRows(schedule, [{
    id: 'old_event', type: 'medication_reminder_sent', status: 'resolved', createdAt: NOW,
    meta: {
      scheduleId: 'renamed', scheduledFor: '2026-09-16_08:00', medicationName: '',
      voiceCheck: { status: 'completed', outcome: 'confirmed' },
    },
  }], NOW)
  assert.equal(rows[0].medicationName, '')
  assert.equal(rows[0].state, 'confirmed')
})

test('malformed execution markers never masquerade as completed announcements', () => {
  const schedule = document({ malformed: entry({ weekdays: [3] }) })
  for (const meta of [
    { scheduleId: 'malformed', scheduledFor: '2026-09-16_08:00', voiceCheck: { status: 'bad' } },
    { scheduleId: 'malformed', scheduledFor: '2026-09-16_08:00', reminderDelivery: 'bad' },
  ]) {
    const rows = buildTodayMedicationRows(schedule, [{
      id: JSON.stringify(meta), type: 'medication_reminder_sent', status: 'open',
      createdAt: NOW, meta,
    }], NOW)
    assert.equal(rows[0].state, 'unknown')
  }
  const legacy = buildTodayMedicationRows(schedule, [{
    id: 'legacy', type: 'medication_reminder_sent', status: 'open', createdAt: NOW,
    meta: { scheduleId: 'malformed', scheduledFor: '2026-09-16_08:00' },
  }], NOW)
  assert.equal(legacy[0].state, 'unknown')
})

test('Pi apply status distinguishes applied, pending, fallback, and last-valid modes', () => {
  const schedule = document({}, 'revision_current')
  assert.equal(getMedicationApplyView({ state: 'remote', revision: 'revision_current' }, schedule).state, 'applied')
  assert.equal(getMedicationApplyView({ state: 'remote', revision: 'older' }, schedule).state, 'pending')
  assert.equal(getMedicationApplyView({ state: 'fallback' }, schedule).state, 'fallback')
  assert.equal(getMedicationApplyView({ state: 'last_valid' }, schedule).state, 'last_valid')
})

test('today rows exclude other devices before matching schedules and retaining history', () => {
  const schedule = document({ shared_id: entry({ weekdays: [3] }) })
  const event = (id, deviceId, scheduleId, outcome, createdAt = NOW) => ({
    id, deviceId, createdAt, type: 'medication_reminder_sent',
    status: outcome === 'confirmed' ? 'resolved' : 'open',
    meta: {
      scheduleId, scheduledFor: '2026-09-16_08:00',
      voiceCheck: { status: 'completed', outcome },
    },
  })
  const foreignEvents = [
    event('foreign_match', 'device_002', 'shared_id', 'confirmed', NOW + 1),
    event('foreign_history', 'device_002', 'deleted', 'confirmed'),
    event('unknown_device', undefined, 'unknown', 'confirmed'),
  ]
  const pending = buildTodayMedicationRows(schedule, foreignEvents, NOW, 'device_001')
  assert.deepEqual(pending.map(({ state }) => state), ['scheduled'])
  const rows = buildTodayMedicationRows(schedule, [
    ...foreignEvents,
    event('own_match', 'device_001', 'shared_id', 'defer'),
    event('own_history', 'device_001', 'deleted', 'no_speech'),
  ], NOW, 'device_001')
  assert.equal(rows.length, 2)
  assert.deepEqual(new Set(rows.map(({ event }) => event.id)),
    new Set(['own_match', 'own_history']))
  assert.equal(rows.find(({ scheduleId }) => scheduleId === 'shared_id').state, 'deferred')
  assert.equal(rows.find(({ scheduleId }) => scheduleId === 'deleted').state, 'no_speech')
})
