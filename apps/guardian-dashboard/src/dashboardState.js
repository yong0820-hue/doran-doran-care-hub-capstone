const EVENT_STATUSES = new Set(['open', 'unread', 'resolved'])
const MAX_DATE_TIMESTAMP = 8_640_000_000_000_000

const MEDICATION_VIEWS = {
  none: {
    label: '복약 안내 없음',
    detail: '최근 복약 안내가 없습니다.',
    badgeClass: 'badge-neutral',
    isPending: false,
  },
  pending: {
    label: '복약 확인 중',
    detail: '음성 확인 처리가 아직 끝나지 않았습니다. 오래 지속되면 기기를 확인해 주세요.',
    badgeClass: 'badge-info',
    isPending: true,
  },
  confirmed: {
    label: '복용 확인됨',
    detail: '어르신이 복용했다고 응답했습니다.',
    badgeClass: 'badge-success',
    isPending: false,
  },
  deferred: {
    label: '복용 미룸',
    detail: '어르신이 나중에 복용하겠다고 응답했습니다.',
    badgeClass: 'badge-warning',
    isPending: false,
  },
  no_speech: {
    label: '응답 없음',
    detail: '복약 안내 후 음성 응답이 없었습니다.',
    badgeClass: 'badge-warning',
    isPending: false,
  },
  unclear: {
    label: '응답 확인 필요',
    detail: '복용 여부를 명확하게 확인하지 못했습니다.',
    badgeClass: 'badge-warning',
    isPending: false,
  },
  cancelled: {
    label: '확인 취소됨',
    detail: '복약 음성 확인이 취소되었습니다.',
    badgeClass: 'badge-neutral',
    isPending: false,
  },
  error: {
    label: '확인 실패',
    detail: '복약 음성 확인을 완료하지 못했습니다.',
    badgeClass: 'badge-danger',
    isPending: false,
  },
  legacy: {
    label: '복약 안내됨',
    detail: '음성 확인 결과가 없는 기존 안내 기록입니다.',
    badgeClass: 'badge-info',
    isPending: false,
  },
  resolved_without_result: {
    label: '해결됨',
    detail: '음성 확인 결과 기록 없이 해결 처리되었습니다.',
    badgeClass: 'badge-neutral',
    isPending: false,
  },
  unknown: {
    label: '상태 확인 필요',
    detail: '복약 확인 상태를 확인할 수 없습니다.',
    badgeClass: 'badge-neutral',
    isPending: false,
  },
}

const AI_COMMAND_VIEWS = {
  none: {
    label: '대화 요청 없음',
    detail: '최근 도란이 대화 요청이 없습니다.',
    badgeClass: 'badge-neutral',
    isBusy: false,
    isConversationActive: false,
  },
  pending: {
    label: '대기 중',
    detail: '도란이가 요청을 받을 때까지 기다리고 있습니다.',
    badgeClass: 'badge-info',
    isBusy: true,
    isConversationActive: false,
  },
  processing: {
    label: '원격 요청 처리 중',
    detail: 'Pi가 요청을 처리 중입니다. 오래 지속되면 기기 상태를 확인해 주세요.',
    badgeClass: 'badge-info',
    isBusy: true,
    isConversationActive: null,
  },
  processed: {
    label: '요청 처리 완료',
    detail: '원격 대화 요청 처리가 끝났습니다.',
    badgeClass: 'badge-success',
    isBusy: false,
    isConversationActive: false,
  },
  failed: {
    label: '대화 실패',
    detail: '도란이 대화 요청을 처리하지 못했습니다.',
    badgeClass: 'badge-danger',
    isBusy: false,
    isConversationActive: false,
  },
  unknown: {
    label: '상태 확인 필요',
    detail: '도란이 대화 상태를 확인할 수 없습니다.',
    badgeClass: 'badge-neutral',
    isBusy: false,
    isConversationActive: false,
  },
}

/**
 * Accept only non-negative, safe-integer Unix millisecond timestamps.
 * Numeric strings and booleans are intentionally rejected instead of coerced.
 */
export function normalizeTimestamp(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) &&
    value >= 0 && value <= MAX_DATE_TIMESTAMP
    ? value
    : null
}

/** Sort valid timestamps newest-first and place invalid values last. */
export function compareTimestampsDesc(left, right) {
  const leftTimestamp = normalizeTimestamp(left)
  const rightTimestamp = normalizeTimestamp(right)

  if (leftTimestamp === rightTimestamp) return 0
  if (leftTimestamp === null) return 1
  if (rightTimestamp === null) return -1
  return leftTimestamp > rightTimestamp ? -1 : 1
}

export function compareCreatedAtDesc(left, right) {
  const timestampOrder = compareTimestampsDesc(left?.createdAt, right?.createdAt)
  if (timestampOrder !== 0) return timestampOrder

  const leftId = left?.id ?? left?.commandId ?? ''
  const rightId = right?.id ?? right?.commandId ?? ''
  return String(rightId).localeCompare(String(leftId))
}

function toRecordArray(records, idField = 'id') {
  if (Array.isArray(records)) {
    return records
      .filter((record) => record && typeof record === 'object')
      .map((record) => (
        idField !== 'id' && record[idField] == null && record.id != null
          ? { ...record, [idField]: record.id }
          : record
      ))
  }
  if (!records || typeof records !== 'object') return []

  return Object.entries(records)
    .filter(([, record]) => record && typeof record === 'object')
    .map(([id, record]) => ({ ...record, [idField]: id }))
}

export function sortByCreatedAtDesc(records) {
  return [...toRecordArray(records)].sort(compareCreatedAtDesc)
}

// Stored open/unread SOS and fall events are the source of truth. A missing
// deviceId is never assigned to whichever device a guardian happens to view.
export function getEmergencyState(events, deviceId) {
  const related = toRecordArray(events).filter((event) => (
    typeof deviceId === 'string' && deviceId.length > 0
    && event.deviceId === deviceId
    && isActionableEventStatus(event.status)
    && (event.type === 'emergency_call' || event.type === 'fall_suspected')
  ))
  return {
    emergencyStatus: related.length ? 'active' : 'normal',
    fallRisk: related.some((event) => event.type === 'fall_suspected') ? 'high' : 'low',
  }
}

export function hasOtherOpenRelatedEvent(events, currentEvent, fallbackDeviceId) {
  if (!currentEvent?.type) return false
  const targetDeviceId = currentEvent.deviceId ?? fallbackDeviceId

  return toRecordArray(events).some((event) => (
    event.id !== currentEvent.id
    && event.type === currentEvent.type
    && isActionableEventStatus(event.status)
    && (event.deviceId == null || event.deviceId === targetDeviceId)
  ))
}

/**
 * Summarize guardian events without treating unknown states as unresolved.
 * Both open care alerts and legacy unread sensor records need acknowledgement.
 */
export function summarizeEvents(events) {
  const sortedEvents = sortByCreatedAtDesc(events)
  const openEvents = []
  const unreadEvents = []
  const resolvedEvents = []
  const unknownEvents = []

  for (const event of sortedEvents) {
    if (event.status === 'open') openEvents.push(event)
    else if (event.status === 'unread') unreadEvents.push(event)
    else if (event.status === 'resolved') resolvedEvents.push(event)
    else unknownEvents.push(event)
  }

  return {
    events: sortedEvents,
    latestEvent: sortedEvents[0] ?? null,
    openEvents,
    unreadEvents,
    resolvedEvents,
    unknownEvents,
    totalCount: sortedEvents.length,
    openCount: openEvents.length,
    unreadCount: unreadEvents.length,
    unresolvedCount: openEvents.length + unreadEvents.length,
    resolvedCount: resolvedEvents.length,
    unknownCount: unknownEvents.length,
  }
}

export function selectLatestMedicationReminder(events) {
  return sortByCreatedAtDesc(events)
    .find((event) => event.type === 'medication_reminder_sent') ?? null
}

function medicationState(event) {
  if (!event) return 'none'

  const marker = event.meta?.voiceCheck
  if (marker?.status === 'processing') return 'pending'

  if (marker?.status === 'completed') {
    if (marker.outcome === 'confirmed') return 'confirmed'
    if (marker.outcome === 'defer') return 'deferred'
    if (['no_speech', 'unclear', 'cancelled', 'error'].includes(marker.outcome)) {
      return marker.outcome
    }
  }

  if (event.status === 'resolved') return 'resolved_without_result'
  if (!marker) return 'legacy'
  return 'unknown'
}

export function getMedicationView(events) {
  const event = selectLatestMedicationReminder(events)
  const state = medicationState(event)
  return { event, state, ...MEDICATION_VIEWS[state] }
}

export function selectLatestAiConversationCommand(commands) {
  return toRecordArray(commands, 'commandId')
    .filter((command) => command.type === 'start_ai_conversation')
    .sort(compareCreatedAtDesc)[0] ?? null
}

export function getAiCommandView(command) {
  const status = command && ['pending', 'processing', 'processed', 'failed'].includes(command.status)
    ? command.status
    : command ? 'unknown' : 'none'
  const view = status === 'processed' && command?.outcome === 'no_speech'
    ? {
        ...AI_COMMAND_VIEWS.processed,
        label: '응답 없음',
        detail: '음성 응답 없이 원격 요청 처리가 끝났습니다.',
        badgeClass: 'badge-warning',
      }
    : AI_COMMAND_VIEWS[status]
  return { command: command ?? null, status, ...view }
}

export function getLatestAiCommandView(commands) {
  return getAiCommandView(selectLatestAiConversationCommand(commands))
}

export function isKnownEventStatus(status) {
  return EVENT_STATUSES.has(status)
}

export function isActionableEventStatus(status) {
  return status === 'open' || status === 'unread'
}
