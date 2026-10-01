import { relativeTime } from '../utils'

function hasTimestamp(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

export default function PresenceStatus({ device }) {
  const radar = device?.sensors?.radar
  const presence = radar?.presence

  const isKnown = presence === true || presence === false
  const isIn    = presence === true

  const icon  = isKnown ? (isIn ? '🏠' : '🚶') : '❓'
  const label = isKnown ? (isIn ? '재실 중' : '부재 중') : '데이터 없음'
  const color = isIn ? 'var(--success)' : 'var(--text-muted)'
  const lastUpdated = hasTimestamp(radar?.lastUpdated)
    ? relativeTime(radar.lastUpdated)
    : '시각 없음'

  return (
    <div className="card" style={{ textAlign: 'center' }}>
      <div className="card-title" style={{ justifyContent: 'center' }}>🏠 현재 재실 상태</div>

      <div className={`presence-icon ${isIn ? 'presence-icon-in' : 'presence-icon-out'}`}>
        {icon}
      </div>

      <div className="presence-label" style={{ color }}>
        {label}
      </div>

      <div className="presence-meta">
        {isIn
          ? `${device?.location ?? '위치 정보 없음'} 감지 · ${lastUpdated}`
          : isKnown ? `현재 위치 미감지 · 갱신 ${lastUpdated}` : '재실 센서 데이터 없음'}
      </div>
    </div>
  )
}
