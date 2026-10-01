import { relativeTime } from '../utils'

const STATUS_BADGES = {
  online:  { label: '온라인', badgeClass: 'badge-online', dotClass: 'dot-green' },
  offline: { label: '오프라인', badgeClass: 'badge-offline', dotClass: 'dot-gray' },
}

const INACTIVITY_LABELS = {
  normal: '정상',
  inactive: '미활동 감지',
}

const FALL_RISK_LABELS = {
  low: '낮음',
  high: '높음',
}

const EMERGENCY_LABELS = {
  normal: '정상',
  active: '응급 호출 중',
}

function hasTimestamp(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function textOrFallback(value, labels) {
  if (value === undefined || value === null || value === '') return '-'
  return labels?.[value] ?? String(value)
}

export default function DeviceStatusCard({ device }) {
  if (!device) {
    return (
      <div className="card">
        <div className="card-title">📻 기기 상태</div>
        <div className="alert-empty">
          <div className="alert-empty-icon">📻</div>
          기기 데이터 없음
        </div>
      </div>
    )
  }

  const radar = device.sensors?.radar ?? {}
  const status = STATUS_BADGES[device.status]
    ?? (device.status ? { label: String(device.status), badgeClass: 'badge-neutral' } : null)
  const hasMotionLevel = typeof radar.motionLevel === 'number' && Number.isFinite(radar.motionLevel)

  return (
    <div className="card">
      <div className="card-title">📻 기기 상태</div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>{device.name ?? '기기명 없음'}</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>{device.location ?? '-'}</div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="card-row">
          <span className="card-row-label">연결 상태</span>
          {status ? (
            <span className={`badge ${status.badgeClass}`}>
              {status.dotClass && <span className={`dot ${status.dotClass}`} />}
              {status.label}
            </span>
          ) : (
            <span className="badge badge-neutral">데이터 없음</span>
          )}
        </div>
        <div className="card-row">
          <span className="card-row-label">마지막 신호</span>
          <span style={{ fontSize: 13 }}>
            {hasTimestamp(device.lastSignal) ? relativeTime(device.lastSignal) : '-'}
          </span>
        </div>
        <div className="card-row">
          <span className="card-row-label">마지막 기기 활동</span>
          <span style={{ fontSize: 13 }}>
            {hasTimestamp(device.lastSeen) ? relativeTime(device.lastSeen) : '-'}
          </span>
        </div>
        <div className="card-row">
          <span className="card-row-label">움직임 수치</span>
          <span style={{ fontSize: 13 }}>{hasMotionLevel ? radar.motionLevel : '-'}</span>
        </div>
        <div className="card-row">
          <span className="card-row-label">미활동 상태</span>
          <span style={{ fontSize: 13 }}>{textOrFallback(radar.inactivityStatus, INACTIVITY_LABELS)}</span>
        </div>
        <div className="card-row">
          <span className="card-row-label">마지막 움직임</span>
          <span style={{ fontSize: 13 }}>
            {hasTimestamp(radar.lastMotionAt) ? relativeTime(radar.lastMotionAt) : '-'}
          </span>
        </div>
        <div className="card-row">
          <span className="card-row-label">낙상 위험</span>
          <span style={{ fontSize: 13 }}>{textOrFallback(radar.fallRisk, FALL_RISK_LABELS)}</span>
        </div>
        <div className="card-row">
          <span className="card-row-label">응급 상태</span>
          <span style={{ fontSize: 13 }}>{textOrFallback(device.emergencyStatus, EMERGENCY_LABELS)}</span>
        </div>
        <div className="card-row">
          <span className="card-row-label">기기 유형</span>
          <span className="badge badge-neutral" style={{ background: '#f1f5f9', color: '#475569' }}>
            {device.type ?? '-'}
          </span>
        </div>
      </div>
    </div>
  )
}
