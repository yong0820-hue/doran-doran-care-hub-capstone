export const MEDICATION_SCHEDULE_SCHEMA_VERSION = 1
export const MEDICATION_TIMEZONE = 'Asia/Seoul'
export const WEEKDAYS = [
  { value: 1, label: '월' },
  { value: 2, label: '화' },
  { value: 3, label: '수' },
  { value: 4, label: '목' },
  { value: 5, label: '금' },
  { value: 6, label: '토' },
  { value: 7, label: '일' },
]

const TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/
const SCHEDULE_ID_PATTERN = /^[A-Za-z0-9_-]{1,120}$/
const SEOUL_FORMATTER = new Intl.DateTimeFormat('en-CA', {
  timeZone: MEDICATION_TIMEZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
  weekday: 'short',
})
const WEEKDAY_NUMBER = {
  Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7,
}

const ROW_VIEWS = {
  scheduled: { label: '예정', badgeClass: 'badge-info', detail: '오늘 복약 예정' },
  processing: { label: '확인 중', badgeClass: 'badge-info', detail: 'Pi가 음성 확인 중입니다.' },
  confirmed: { label: '복용 완료', badgeClass: 'badge-success', detail: '복용 응답이 확인됐습니다.' },
  deferred: { label: '미룸', badgeClass: 'badge-warning', detail: '나중에 복용하겠다고 응답했습니다.' },
  no_speech: { label: '응답 없음', badgeClass: 'badge-warning', detail: '안내 후 음성 응답이 없었습니다.' },
  unclear: { label: '확인 필요', badgeClass: 'badge-warning', detail: '응답을 명확히 확인하지 못했습니다.' },
  cancelled: { label: '취소됨', badgeClass: 'badge-neutral', detail: '설정 변경 또는 해결 처리로 취소됐습니다.' },
  error: { label: '안내 실패', badgeClass: 'badge-danger', detail: '복약 안내를 완료하지 못했습니다.' },
  announced: { label: '안내 완료', badgeClass: 'badge-info', detail: '음성 응답 확인 없이 안내만 완료했습니다.' },
  resolved_without_result: { label: '응답 미기록', badgeClass: 'badge-neutral', detail: '응답 결과 없이 해결 처리됐습니다.' },
  unknown: { label: '상태 확인 필요', badgeClass: 'badge-neutral', detail: '기록 상태를 확인해 주세요.' },
}

function invalidDocument(message) {
  return { state: 'invalid', revision: null, entries: [], error: message }
}

function normalizeWeekdays(value) {
  if (!Array.isArray(value) || value.length === 0) return null
  if (value.some((day) => !Number.isInteger(day) || day < 1 || day > 7)) return null
  const unique = [...new Set(value)].sort((left, right) => left - right)
  return unique.length === value.length ? unique : null
}

function normalizeEntry(scheduleId, value) {
  if (typeof scheduleId !== 'string' || !SCHEDULE_ID_PATTERN.test(scheduleId) ||
      !value || typeof value !== 'object' || Array.isArray(value)) return null
  if (typeof value.enabled !== 'boolean' || typeof value.time !== 'string' ||
      !TIME_PATTERN.test(value.time)) return null
  const medicationName = value.medicationName ?? ''
  if (typeof medicationName !== 'string' || medicationName.trim().length > 80) return null
  const weekdays = normalizeWeekdays(value.weekdays)
  if (!weekdays) return null
  return {
    scheduleId,
    enabled: value.enabled,
    time: value.time,
    medicationName: medicationName.trim(),
    weekdays,
  }
}

export function normalizeMedicationScheduleDocument(value) {
  if (value == null) {
    return { state: 'missing', revision: null, entries: [], error: null }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return invalidDocument('복약 일정 문서 형식이 올바르지 않습니다.')
  }
  if (value.schemaVersion !== MEDICATION_SCHEDULE_SCHEMA_VERSION) {
    return invalidDocument('지원하지 않는 복약 일정 버전입니다.')
  }
  if (value.timezone !== MEDICATION_TIMEZONE) {
    return invalidDocument('복약 일정 시간대가 Asia/Seoul이 아닙니다.')
  }
  if (typeof value.revision !== 'string' || !value.revision || value.revision.length > 160) {
    return invalidDocument('복약 일정 변경 번호가 없거나 올바르지 않습니다.')
  }
  const rawEntries = value.entries ?? {}
  if (!rawEntries || typeof rawEntries !== 'object' || Array.isArray(rawEntries)) {
    return invalidDocument('복약 일정 목록 형식이 올바르지 않습니다.')
  }
  const entries = []
  for (const [scheduleId, rawEntry] of Object.entries(rawEntries)) {
    const entry = normalizeEntry(scheduleId, rawEntry)
    if (!entry) return invalidDocument(`복약 일정 ${scheduleId}의 값이 올바르지 않습니다.`)
    entries.push(entry)
  }
  entries.sort((left, right) => left.time.localeCompare(right.time) ||
    left.scheduleId.localeCompare(right.scheduleId))
  const enabled = entries.filter((entry) => entry.enabled)
  for (let index = 0; index < enabled.length; index += 1) {
    for (let next = index + 1; next < enabled.length; next += 1) {
      const left = enabled[index]
      const right = enabled[next]
      if (left.time === right.time && left.weekdays.some((day) => right.weekdays.includes(day))) {
        return invalidDocument('같은 요일과 시간에 활성화된 복약 일정이 겹칩니다.')
      }
    }
  }
  return { state: 'ready', revision: value.revision, entries, error: null }
}

export function validateMedicationScheduleEntry(value, existingEntries = [], currentId = null) {
  const errors = []
  if (typeof value?.time !== 'string' || !TIME_PATTERN.test(value.time)) {
    errors.push('시간을 HH:MM 형식으로 선택해 주세요.')
  }
  if (typeof value?.enabled !== 'boolean') errors.push('활성 상태가 올바르지 않습니다.')
  const medicationName = value?.medicationName ?? ''
  if (typeof medicationName !== 'string' || medicationName.trim().length > 80) {
    errors.push('약 이름은 80자 이내로 입력해 주세요.')
  }
  const weekdays = normalizeWeekdays(value?.weekdays)
  if (!weekdays) errors.push('요일을 한 개 이상 선택해 주세요.')
  if (errors.length === 0 && value.enabled) {
    const conflict = existingEntries.find((entry) => (
      entry.scheduleId !== currentId && entry.enabled && entry.time === value.time
      && weekdays.some((day) => entry.weekdays.includes(day))
    ))
    if (conflict) errors.push('같은 요일과 시간에 활성화된 일정이 이미 있습니다.')
  }
  return errors
}

export function toMedicationSchedulePayload(value) {
  return {
    enabled: value.enabled,
    time: value.time,
    medicationName: (value.medicationName ?? '').trim(),
    weekdays: [...new Set(value.weekdays)].sort((left, right) => left - right),
  }
}

export function getSeoulDateParts(timestamp = Date.now()) {
  const date = new Date(timestamp)
  if (!Number.isFinite(date.getTime())) return null
  const parts = Object.fromEntries(
    SEOUL_FORMATTER.formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value])
  )
  const weekday = WEEKDAY_NUMBER[parts.weekday]
  if (!weekday) return null
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: `${parts.hour}:${parts.minute}`,
    weekday,
  }
}

function eventScheduleParts(event) {
  const scheduledFor = event?.meta?.scheduledFor
  const match = typeof scheduledFor === 'string'
    ? /^(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2})$/.exec(scheduledFor)
    : null
  if (match) return { date: match[1], time: match[2] }
  if (typeof event?.createdAt === 'number' && Number.isSafeInteger(event.createdAt)) {
    return getSeoulDateParts(event.createdAt)
  }
  return null
}

function eventView(event) {
  const voice = event?.meta?.voiceCheck
  if (voice?.status === 'processing') return { state: 'processing', ...ROW_VIEWS.processing }
  if (voice?.status === 'completed') {
    const state = voice.outcome === 'defer' ? 'deferred' : voice.outcome
    return { state: ROW_VIEWS[state] ? state : 'unknown', ...(ROW_VIEWS[state] ?? ROW_VIEWS.unknown) }
  }
  if (voice !== undefined) return { state: 'unknown', ...ROW_VIEWS.unknown }
  const delivery = event?.meta?.reminderDelivery
  if (delivery?.status === 'processing') return { state: 'processing', ...ROW_VIEWS.processing }
  if (delivery?.status === 'completed') {
    const state = ROW_VIEWS[delivery.outcome] ? delivery.outcome : 'unknown'
    return { state, ...(ROW_VIEWS[state]) }
  }
  if (delivery !== undefined) return { state: 'unknown', ...ROW_VIEWS.unknown }
  if (event?.status === 'resolved') {
    return { state: 'resolved_without_result', ...ROW_VIEWS.resolved_without_result }
  }
  return { state: 'unknown', ...ROW_VIEWS.unknown }
}

export function buildTodayMedicationRows(scheduleDocument, events, now = Date.now(), deviceId = null) {
  const today = getSeoulDateParts(now)
  if (!today) return []
  const schedule = normalizeMedicationScheduleDocument(scheduleDocument)
  const entries = schedule.state === 'ready'
    ? schedule.entries.filter((entry) => entry.enabled && entry.weekdays.includes(today.weekday))
    : []
  const todayEvents = (events ?? [])
    .filter((event) => event?.type === 'medication_reminder_sent'
      && (deviceId == null || event.deviceId === deviceId))
    .map((event) => ({ event, parts: eventScheduleParts(event) }))
    .filter(({ parts }) => parts?.date === today.date)
    .sort((left, right) => (right.event.createdAt ?? 0) - (left.event.createdAt ?? 0))
  const byScheduleId = new Map()
  for (const item of todayEvents) {
    const scheduleId = item.event.meta?.scheduleId
    if (typeof scheduleId === 'string' && !byScheduleId.has(scheduleId)) {
      byScheduleId.set(scheduleId, item)
    }
  }

  const consumed = new Set()
  const rows = entries.map((entry) => {
    const match = byScheduleId.get(entry.scheduleId)
    if (match) consumed.add(match.event.id ?? match.event.runId)
    const view = match
      ? eventView(match.event)
      : { state: 'scheduled', ...ROW_VIEWS.scheduled }
    return {
      key: entry.scheduleId,
      scheduleId: entry.scheduleId,
      time: match?.parts?.time ?? entry.time,
      medicationName: match && Object.prototype.hasOwnProperty.call(
        match.event?.meta ?? {}, 'medicationName'
      )
        ? (typeof match.event.meta.medicationName === 'string'
            ? match.event.meta.medicationName
            : '')
        : entry.medicationName,
      event: match?.event ?? null,
      ...view,
    }
  })

  for (const item of todayEvents) {
    const eventKey = item.event.id ?? item.event.runId
    if (consumed.has(eventKey)) continue
    const view = eventView(item.event)
    rows.push({
      key: `event-${eventKey ?? item.parts.time}`,
      scheduleId: item.event.meta?.scheduleId ?? null,
      time: item.parts.time,
      medicationName: item.event.meta?.medicationName ?? '',
      event: item.event,
      ...view,
    })
  }
  return rows.sort((left, right) => left.time.localeCompare(right.time) ||
    left.key.localeCompare(right.key))
}

export function getMedicationApplyView(status, scheduleDocument) {
  const schedule = normalizeMedicationScheduleDocument(scheduleDocument)
  if (!status || typeof status !== 'object') {
    return { state: 'unknown', label: 'Pi 적용 상태 확인 중', badgeClass: 'badge-neutral' }
  }
  if (status.state === 'remote') {
    const applied = schedule.state === 'ready' && status.revision === schedule.revision
    return applied
      ? { state: 'applied', label: 'Pi 반영 완료', badgeClass: 'badge-success' }
      : { state: 'pending', label: 'Pi 반영 대기', badgeClass: 'badge-info' }
  }
  if (status.state === 'last_valid') {
    return { state: 'last_valid', label: '마지막 정상 설정 사용 중', badgeClass: 'badge-warning' }
  }
  if (status.state === 'fallback') {
    return { state: 'fallback', label: '기본 일정 사용 중', badgeClass: 'badge-warning' }
  }
  return { state: 'unknown', label: 'Pi 적용 상태 확인 필요', badgeClass: 'badge-neutral' }
}
