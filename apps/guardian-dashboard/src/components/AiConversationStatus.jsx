import { formatTime } from '../utils'
import { getAiCommandView } from '../dashboardState'

function hasTimestamp(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

export default function AiConversationStatus({ command }) {
  const view = getAiCommandView(command)

  return (
    <div className="card" data-command-status={view.status}>
      <div className="card-title">🎙️ AI 대화 상태</div>

      {view.status === 'none' || !view.command ? (
        <div className="alert-empty">
          <div className="alert-empty-icon">🎙️</div>
          대화 요청 데이터 없음
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="card-row">
            <span className="card-row-label">최근 요청</span>
            <span className={`badge ${view.badgeClass ?? 'badge-neutral'}`}>
              {view.label}
            </span>
          </div>

          <div className="card-row">
            <span className="card-row-label">현재 대화 여부</span>
            {view.isConversationActive === true ? (
              <span className="badge badge-success">원격 대화 중</span>
            ) : view.isConversationActive === null ? (
              <span className="badge badge-warning">기기 확인 필요</span>
            ) : (
              <span style={{ fontSize: 13 }}>원격 명령 기준 진행 중 아님</span>
            )}
          </div>

          <div className="card-row">
            <span className="card-row-label">요청 시각</span>
            <span style={{ fontSize: 13 }}>
              {hasTimestamp(view.command.createdAt) ? formatTime(view.command.createdAt) : '-'}
            </span>
          </div>

          {view.detail && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              {view.detail}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
