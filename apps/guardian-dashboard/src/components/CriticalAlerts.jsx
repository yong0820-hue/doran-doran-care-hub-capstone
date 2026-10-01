import { relativeTime } from '../utils'
import { isActionableEventStatus } from '../dashboardState'

export default function CriticalAlerts({ events }) {
  const safeEvents = (events ?? []).filter((event) => isActionableEventStatus(event?.status))

  if (safeEvents.length === 0) {
    return (
      <div className="card">
        <div className="card-title" style={{ justifyContent: 'center' }}>🚨 주요 알림</div>
        <div className="alert-empty">
          <div className="alert-empty-icon">✅</div>
          현재 알림 없음
        </div>
      </div>
    )
  }

  return (
    <div className="card alert-card-danger">
      <div className="card-title" style={{ color: 'var(--danger)' }}>🚨 주요 알림</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {safeEvents.map((evt) => (
          <div key={evt?.id} className="alert-item">
            <div className="alert-item-title">⚠️ {evt?.title ?? evt?.label ?? '-'}</div>
            <div className="alert-item-meta">
              {evt?.deviceName ?? '-'} · {evt?.createdAt ? relativeTime(evt.createdAt) : '-'}
            </div>
            <div className="alert-item-cta">즉각적인 확인이 필요합니다</div>
          </div>
        ))}
      </div>
    </div>
  )
}
