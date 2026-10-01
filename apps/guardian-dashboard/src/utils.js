import { normalizeTimestamp } from './dashboardState.js'

export { normalizeTimestamp }

export function relativeTime(ts, now = Date.now()) {
  const timestamp = normalizeTimestamp(ts)
  const current = normalizeTimestamp(now)
  if (timestamp === null || current === null) return '-'

  const diff = current - timestamp
  const future = diff < 0
  const mins = Math.floor(Math.abs(diff) / 60000)
  if (mins < 1) return '방금 전'
  if (mins < 60) return `${mins}분 ${future ? '후' : '전'}`

  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}시간 ${future ? '후' : '전'}`
  return `${Math.floor(hrs / 24)}일 ${future ? '후' : '전'}`
}

export function formatTime(ts) {
  const timestamp = normalizeTimestamp(ts)
  if (timestamp === null) return '-'
  return new Date(timestamp).toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDateTime(ts) {
  const timestamp = normalizeTimestamp(ts)
  if (timestamp === null) return '-'
  return new Date(timestamp).toLocaleString('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
