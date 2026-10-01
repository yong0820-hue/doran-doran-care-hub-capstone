import { useState } from 'react'
import { relativeTime } from '../utils'
import { isActionableEventStatus, normalizeTimestamp, sortByCreatedAtDesc, summarizeEvents } from '../dashboardState'

// ─── Constants ────────────────────────────────────────────────────────────────

const COMPACT_LIMIT = 5

const SEVERITY_DOT = {
  critical: 'timeline-dot-critical',
  warning:  'timeline-dot-warning',
  success:  'timeline-dot-success',
  info:     'timeline-dot-info',
}

const SEVERITY_LABEL = {
  critical: '긴급',
  warning:  '주의',
  success:  '완료',
  info:     '정보',
}

const SEVERITY_BADGE = {
  critical: 'badge-danger',
  warning:  'badge-warning',
  success:  'badge-success',
  info:     'badge-info',
}

const TYPE_LABEL = {
  medication_reminder_sent: '복약 안내',
  inactivity_voice_check_sent: '미활동 안부 확인',
  ai_conversation_message: 'AI 대화 기록',
  long_inactivity:          '장시간 미활동',
  presence_detected:        '재실 감지',
  absence_detected:         '부재 감지',
  emergency_call:           '응급 호출',
  fall_suspected:           '낙상 의심',
  medication_taken:         '복약 완료',
  medication_delayed:       '복약 지연',
  presence_lost:            '재실 종료',
  button_press:             '버튼 누름',
  pillbox_open:             '약통 열림',
  pillbox_close:            '약통 닫힘',
  mock_test:                '테스트 이벤트',
}

function eventTitle(event) {
  const rawTitle = event?.title || event?.label
  if (typeof rawTitle === 'string' && /[가-힣]/.test(rawTitle)) return rawTitle
  return TYPE_LABEL[event?.type] || rawTitle || event?.type || '-'
}

function eventMessage(event) {
  if (event?.type === 'presence_detected') {
    return `${event?.location ?? '설치 위치'}에서 재실이 감지되었습니다.`
  }
  if (event?.type === 'absence_detected') {
    return `${event?.location ?? '설치 위치'}에서 재실이 감지되지 않습니다.`
  }
  return event?.message || null
}

// ─── Date helpers ─────────────────────────────────────────────────────────────

function toDateKey(ts) {
  const timestamp = normalizeTimestamp(ts)
  if (timestamp === null) return 'unknown'
  const d = new Date(timestamp)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function formatDateKey(key) {
  if (!key || key === 'unknown') return '날짜 없음'
  const todayKey     = toDateKey(Date.now())
  const yesterdayKey = toDateKey(Date.now() - 864e5)
  const [y, m, d]    = key.split('-')
  const label        = `${y}.${m}.${d}`
  if (key === todayKey)     return `${label} 오늘`
  if (key === yesterdayKey) return `${label} 어제`
  return label
}

function groupByDate(events) {
  const map = {}
  for (const evt of events) {
    const k = toDateKey(evt?.createdAt)
    if (!map[k]) map[k] = []
    map[k].push(evt)
  }
  return map
}

// ─── Single event row ─────────────────────────────────────────────────────────

function EventRow({ evt, onResolve }) {
  const isResolved   = evt?.status === 'resolved'
  const isOpen       = isActionableEventStatus(evt?.status)
  const title        = eventTitle(evt)
  const message      = eventMessage(evt)
  const dotClass     = SEVERITY_DOT[evt?.severity] ?? 'timeline-dot-info'
  const typeLabel    = TYPE_LABEL[evt?.type] ?? evt?.type ?? ''
  const metaParts    = [evt?.location, typeLabel].filter(Boolean)

  return (
    <li
      className="timeline-item"
      data-type={evt?.type}
      data-status={evt?.status}
      data-id={evt?.id}
    >
      <span className={`timeline-dot ${dotClass}`} />

      <div className="timeline-body">
        <div className="timeline-label">
          {title}
          {isResolved ? (
            <span className="badge badge-neutral" style={{ marginLeft: 8, verticalAlign: 'middle' }}>
              해결됨
            </span>
          ) : isOpen ? (
            <span className="badge badge-info" style={{ marginLeft: 8, verticalAlign: 'middle' }}>
              미확인
            </span>
          ) : (
            <span className="badge badge-neutral" style={{ marginLeft: 8, verticalAlign: 'middle' }}>
              기록됨
            </span>
          )}
        </div>
        {message && (
          <div className="timeline-meta" style={{ marginTop: 2 }}>{message}</div>
        )}
        {metaParts.length > 0 && (
          <div className="timeline-meta">{metaParts.join(' · ')}</div>
        )}
      </div>

      <div className="timeline-right">
        <div className="timeline-time">
          {relativeTime(evt?.createdAt)}
        </div>
        {isOpen && onResolve && (
          <button className="resolve-btn" onClick={() => onResolve(evt?.id)}>
            확인 완료
          </button>
        )}
      </div>
    </li>
  )
}

// ─── Button styles ────────────────────────────────────────────────────────────

const toggleStyle = {
  marginLeft: 8,
  padding: '2px 8px',
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: 6,
  cursor: 'pointer',
  fontSize: 12,
  color: 'var(--text-muted)',
  whiteSpace: 'nowrap',
}

const navStyle = (active) => ({
  padding: '3px 10px',
  background: 'none',
  border: `1px solid ${active ? 'var(--border)' : 'transparent'}`,
  borderRadius: 6,
  cursor: active ? 'pointer' : 'default',
  fontSize: 16,
  fontWeight: 600,
  color: 'inherit',
  opacity: active ? 1 : 0.25,
  lineHeight: 1,
})

// ─── Main component ───────────────────────────────────────────────────────────

export default function EventTimeline({ events, onResolve }) {
  const [mode, setMode] = useState('compact')
  const [pickedDate, setPickedDate] = useState(null)

  // Step 1: safe array from prop — no filtering of any kind
  const allEvents = Array.isArray(events) ? events : (events ? [events] : [])

  // Step 2: sort newest-first
  const sorted = sortByCreatedAtDesc(allEvents)

  // Step 3: decide which events to display per mode
  const displayedEvents = mode === 'compact' ? sorted.slice(0, COMPACT_LIMIT) : sorted

  const summary = summarizeEvents(sorted)
  const latest = summary.latestEvent
  const openCount = summary.unresolvedCount
  const latestTitle = eventTitle(latest)

  // ── Date grouping (daily mode only) ──────────────────────────────────────────
  const grouped  = groupByDate(sorted)
  const dateKeys = Object.keys(grouped)
    .filter((k) => k !== 'unknown')
    .sort((a, b) => b.localeCompare(a))
  if (grouped['unknown']?.length) dateKeys.push('unknown')

  const activeDateKey = pickedDate && grouped[pickedDate] ? pickedDate : (dateKeys[0] ?? null)
  const dateIdx       = dateKeys.indexOf(activeDateKey)
  const canBack       = dateIdx < dateKeys.length - 1
  const canForward    = dateIdx > 0

  const dayEvents = activeDateKey
    ? sortByCreatedAtDesc(grouped[activeDateKey] ?? [])
    : []

  const dayTotal      = dayEvents.length
  const dayUnresolved = dayEvents.filter((e) => isActionableEventStatus(e?.status)).length
  const dayCritical   = dayEvents.filter(
    (e) => isActionableEventStatus(e?.status) && e?.severity === 'critical'
  ).length

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div className="card">

      {/* Title bar */}
      <div className="card-title">
        📋 이벤트 로그
        {sorted.length > 0 && mode === 'compact' && (
          <button style={toggleStyle} onClick={() => { setPickedDate(dateKeys[0] ?? null); setMode('daily') }}>
            일별 보기
          </button>
        )}
        {mode === 'daily' && (
          <button style={toggleStyle} onClick={() => setMode('compact')}>
            최근 5개 보기
          </button>
        )}
        <span className="badge badge-info" style={{ marginLeft: 'auto', textTransform: 'none' }}>
          {sorted.length}건
        </span>
      </div>

      {latest && (
        <div className="event-summary" aria-label="이벤트 요약">
          <div>
            <span className="event-summary-label">미확인</span>
            <strong>{openCount}건</strong>
          </div>
          <div className="event-summary-latest">
            <span className="event-summary-label">최근 이벤트</span>
            <strong>{latestTitle}</strong>
          </div>
          <span className={`badge ${SEVERITY_BADGE[latest?.severity] ?? 'badge-neutral'}`}>
            {SEVERITY_LABEL[latest?.severity] ?? '상태 정보 없음'}
          </span>
        </div>
      )}

      {/* Empty state */}
      {sorted.length === 0 && (
        <div className="alert-empty">
          <div className="alert-empty-icon">📭</div>
          이벤트 없음
        </div>
      )}

      {/* Compact mode */}
      {sorted.length > 0 && mode === 'compact' && (
        <>
          {sorted.length > COMPACT_LIMIT && (
            <div style={{ marginBottom: 8, fontSize: 12, color: 'var(--text-muted)' }}>
              최근 {COMPACT_LIMIT}개 표시 중
            </div>
          )}
          <ul className="timeline">
            {displayedEvents.map((evt, i) => (
              <EventRow key={evt?.id ?? `idx-${i}`} evt={evt} onResolve={onResolve} />
            ))}
          </ul>
        </>
      )}

      {/* Daily mode */}
      {sorted.length > 0 && mode === 'daily' && (
        <>
          {/* Date navigation */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 12,
            paddingBottom: 10,
            borderBottom: '1px solid var(--border)',
            flexWrap: 'wrap',
            rowGap: 6,
          }}>
            <button style={navStyle(canBack)}    disabled={!canBack}    onClick={() => setPickedDate(dateKeys[dateIdx + 1])}>‹</button>
            <span style={{ fontWeight: 600, fontSize: 14, minWidth: 140, textAlign: 'center' }}>
              {activeDateKey ? formatDateKey(activeDateKey) : '-'}
            </span>
            <button style={navStyle(canForward)} disabled={!canForward} onClick={() => setPickedDate(dateKeys[dateIdx - 1])}>›</button>
            <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
              {'전체 ' + dayTotal + '건'}
              {dayUnresolved > 0 && (' · 미확인 ' + dayUnresolved + '건')}
              {dayCritical   > 0 && (' · 긴급 '   + dayCritical   + '건')}
            </span>
          </div>

          {dayEvents.length === 0 ? (
            <div className="alert-empty">
              <div className="alert-empty-icon">📭</div>
              선택한 날짜의 이벤트가 없습니다.
            </div>
          ) : (
            <ul className="timeline">
              {dayEvents.map((evt, i) => (
                <EventRow key={evt?.id ?? `idx-${i}`} evt={evt} onResolve={onResolve} />
              ))}
            </ul>
          )}
        </>
      )}

    </div>
  )
}
