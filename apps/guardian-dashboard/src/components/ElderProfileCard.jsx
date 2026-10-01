import { relativeTime } from '../utils'

export default function ElderProfileCard({ elder }) {
  if (!elder) {
    return (
      <div className="card">
        <div className="card-title">👵 어르신 프로필</div>
        <div className="alert-empty">
          <div className="alert-empty-icon">👵</div>
          어르신 데이터 없음
        </div>
      </div>
    )
  }

  return (
    <div className="card">
      <div className="card-title">👵 어르신 프로필</div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 18 }}>
        <div className="elder-avatar">👵</div>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{elder.name ?? '이름 없음'} 할머니</div>
          <div style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>만 {elder.age ?? '-'}세</div>
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="card-row">
          <span className="card-row-label">전반적 상태</span>
          <span className="badge badge-success">✓ {elder.overallStatus ?? '알 수 없음'}</span>
        </div>
        <div className="card-row">
          <span className="card-row-label">마지막 확인</span>
          <span style={{ fontSize: 13 }}>{elder.lastSeen ? relativeTime(elder.lastSeen) : '-'}</span>
        </div>
      </div>
    </div>
  )
}
