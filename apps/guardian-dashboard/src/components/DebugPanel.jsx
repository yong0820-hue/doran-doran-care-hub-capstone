import { useState } from 'react'

const PANEL_LIMIT = 5

function fmtTs(ts) {
  if (!ts) return '-'
  try {
    return new Date(ts).toLocaleString('ko-KR', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch {
    return String(ts)
  }
}

const cellStyle = {
  padding: '4px 8px',
  fontSize: 11,
  borderBottom: '1px solid #334155',
  whiteSpace: 'nowrap',
  maxWidth: 180,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
}

const headStyle = {
  ...cellStyle,
  fontWeight: 700,
  color: '#94a3b8',
  borderBottom: '1px solid #475569',
  background: '#1e293b',
}

const SEVERITY_COLOR = {
  critical: '#ef4444',
  warning:  '#f59e0b',
  success:  '#22c55e',
  info:     '#3b82f6',
}

export default function DebugPanel({ isLive, elderId, deviceId, events }) {
  const [open, setOpen] = useState(false)

  const sorted = [...(events ?? [])].sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0))
  const latest = sorted.slice(0, PANEL_LIMIT)
  const hasMed = (events ?? []).some((e) => e.type === 'medication_reminder_sent')
  const listenPath = `events/${elderId}`

  return (
    <div style={{
      marginTop: 24,
      border: '1px solid #334155',
      borderRadius: 8,
      background: '#0f172a',
      color: '#e2e8f0',
      fontFamily: 'monospace',
      fontSize: 12,
    }}>
      {/* Header */}
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 14px',
          cursor: 'pointer',
          borderBottom: open ? '1px solid #334155' : 'none',
          userSelect: 'none',
        }}
      >
        <span style={{ fontSize: 14 }}>🔬</span>
        <span style={{ fontWeight: 700, color: '#f8fafc', letterSpacing: 0.3 }}>
          Firebase 이벤트 디버그
        </span>
        <span style={{ marginLeft: 'auto', color: '#64748b', fontSize: 11 }}>
          {open ? '▲ 접기' : '▼ 펼치기'}
        </span>
      </div>

      {open && (
        <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>

          {/* Config rows */}
          <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 16px', alignItems: 'center' }}>
            {[
              ['Firebase',       isLive ? '🟢 실시간 연결됨 (LIVE)' : '🔌 Mock 모드 (미연결)'],
              ['elderId',        elderId],
              ['deviceId',       deviceId],
              ['Listen path',    listenPath],
              ['Raw count',      String((events ?? []).length)],
              ['Rendered count', String((events ?? []).length)],
              ['medication_reminder_sent', hasMed
                ? `✅ 포함 (${(events ?? []).filter(e => e.type === 'medication_reminder_sent').length}건)`
                : '❌ 없음'],
            ].map(([label, value]) => (
              <>
                <span key={label + '_l'} style={{ color: '#94a3b8', whiteSpace: 'nowrap' }}>{label}</span>
                <span key={label + '_v'} style={{
                  color: label === 'Firebase'
                    ? (isLive ? '#4ade80' : '#facc15')
                    : label === 'medication_reminder_sent'
                    ? (hasMed ? '#4ade80' : '#f87171')
                    : '#f1f5f9',
                  wordBreak: 'break-all',
                }}>{value}</span>
              </>
            ))}
          </div>

          {/* Events table */}
          <div style={{ overflowX: 'auto' }}>
            <div style={{ color: '#94a3b8', marginBottom: 6, fontSize: 11 }}>
              최근 {PANEL_LIMIT}개 raw events (createdAt 내림차순)
            </div>
            {latest.length === 0 ? (
              <div style={{ color: '#ef4444', padding: '6px 0' }}>이벤트 없음 — Firebase에서 데이터가 수신되지 않았습니다</div>
            ) : (
              <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: 700 }}>
                <thead>
                  <tr>
                    {['id', 'type', 'title', 'status', 'severity', 'createdAt', 'formatted'].map((h) => (
                      <th key={h} style={headStyle}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {latest.map((evt, i) => (
                    <tr key={evt?.id ?? i} style={{ background: i % 2 === 0 ? '#1e293b' : '#0f172a' }}>
                      <td style={cellStyle} title={evt?.id}>{evt?.id ?? '-'}</td>
                      <td style={{ ...cellStyle, color: evt?.type === 'medication_reminder_sent' ? '#34d399' : '#e2e8f0' }}>
                        {evt?.type ?? '-'}
                      </td>
                      <td style={cellStyle}>{evt?.title ?? '-'}</td>
                      <td style={cellStyle}>{evt?.status ?? '-'}</td>
                      <td style={{
                        ...cellStyle,
                        color: SEVERITY_COLOR[evt?.severity] ?? '#e2e8f0',
                        fontWeight: 600,
                      }}>
                        {evt?.severity ?? '-'}
                      </td>
                      <td style={cellStyle}>{evt?.createdAt ?? '-'}</td>
                      <td style={cellStyle}>{fmtTs(evt?.createdAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

        </div>
      )}
    </div>
  )
}
