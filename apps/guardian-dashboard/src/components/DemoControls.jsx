import { useState, useRef, useEffect } from 'react'
import { EVENT_TEMPLATES } from '../data/eventTemplates'

const AI_COMMAND_LABELS = {
  sending: '명령 전송 중',
  pending: '대기 중',
  processing: '원격 요청 처리 중',
  processed: '대화 요청 처리 완료',
  failed: '대화 요청 실패',
}

const ACTIVE_AI_STATUSES = ['sending', 'pending', 'processing']

const AI_OUTCOME_LABELS = {
  completed: '처리 완료',
  no_speech: '응답 없음',
  failed: '처리 실패',
}

const SEVERITY_CLASS = {
  info:     'demo-event-btn-info',
  success:  'demo-event-btn-success',
  warning:  'demo-event-btn-warning',
  critical: 'demo-event-btn-critical',
}

export default function DemoControls({
  isLive,
  connectionState,
  commands = [],
  latestAiCommand,
  onSendEvent,
  eventWritesEnabled = true,
  onStartAiConversation,
}) {
  const [lastSent, setLastSent] = useState(null)
  const [eventError, setEventError] = useState(null)
  const [eventSending, setEventSending] = useState(false)
  const eventSendingRef = useRef(false)
  const timerRef = useRef(null)
  const [requestedCommand, setRequestedCommand] = useState(null)
  const [aiCommandError, setAiCommandError] = useState(null)
  const [allowNewRequest, setAllowNewRequest] = useState(false)

  const requestedRecord = requestedCommand?.commandId
    ? commands.find((command) => command?.id === requestedCommand.commandId) ?? null
    : null
  const latestIsActive = ACTIVE_AI_STATUSES.includes(latestAiCommand?.status)
  const aiCommand = requestedCommand?.status === 'sending'
    ? requestedCommand
    : latestIsActive
      ? latestAiCommand
      : requestedRecord ?? requestedCommand ?? latestAiCommand
  const aiCommandLoading = requestedCommand?.status === 'sending'
    || (!allowNewRequest && ACTIVE_AI_STATUSES.includes(aiCommand?.status))
  const firebaseConnected = isLive && connectionState === 'connected'
  const latestCommandIdentity = latestAiCommand?.id ?? latestAiCommand?.commandId ?? null

  useEffect(() => () => {
    clearTimeout(timerRef.current)
  }, [])

  useEffect(() => {
    setAllowNewRequest(false)
  }, [latestCommandIdentity, latestAiCommand?.status, connectionState])

  const handleStartAiConversation = async () => {
    if (!firebaseConnected || aiCommandLoading) return
    setAllowNewRequest(false)
    setRequestedCommand({ status: 'sending' })
    setAiCommandError(null)

    try {
      const newCommandId = await onStartAiConversation()
      setRequestedCommand({ commandId: newCommandId, status: 'pending' })
    } catch {
      setRequestedCommand({ status: 'failed' })
      setAiCommandError('명령 전송에 실패했습니다. Firebase 연결·쓰기 권한을 확인한 뒤 다시 시도하세요.')
    }
  }

  const handleClick = async (template) => {
    if (!eventWritesEnabled) return
    if ((isLive && !firebaseConnected) || eventSendingRef.current) return
    eventSendingRef.current = true
    setEventSending(true)
    setLastSent(null)
    setEventError(null)
    if (timerRef.current) clearTimeout(timerRef.current)
    try {
      await onSendEvent(template)
      setLastSent(template.title)
      timerRef.current = setTimeout(() => setLastSent(null), 2000)
    } catch {
      setEventError('모의 이벤트 저장에 실패했습니다. 연결과 쓰기 권한을 확인해 주세요.')
    } finally {
      eventSendingRef.current = false
      setEventSending(false)
    }
  }

  return (
    <div className="demo-panel">
      <div className="demo-panel-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="demo-panel-title">🎮 데모 컨트롤</span>
          <span className="demo-panel-badge">개발자 전용</span>
        </div>
        <span className={`badge ${firebaseConnected ? 'badge-success' : isLive ? 'badge-warning' : 'badge-neutral'}`}>
          {firebaseConnected ? 'Firebase 연결됨' : isLive ? '연결 확인 필요' : 'Mock 모드'}
        </span>
      </div>

      <p className="demo-panel-desc">
        센서 모의 버튼은 측정이나 음성 안내를 실행하지 않고 [모의] 이벤트를 만듭니다.
        {isLive ? ' Firebase에 이벤트와 모의 기기 상태를 저장합니다.' : ' 현재 화면의 Mock 데이터만 변경합니다.'}
      </p>

      <div style={{ marginBottom: 16 }}>
        <button
          type="button"
          className="demo-event-btn demo-event-btn-info"
          onClick={handleStartAiConversation}
          disabled={!firebaseConnected || aiCommandLoading}
          style={{ width: '100%', opacity: !firebaseConnected || aiCommandLoading ? 0.55 : 1, cursor: !firebaseConnected || aiCommandLoading ? 'not-allowed' : 'pointer' }}
        >
          <span className="demo-event-icon">🎙️</span>
          <span className="demo-event-title">{aiCommandLoading ? '원격 대화 요청 처리 중...' : '실제 Pi 음성 대화 시작'}</span>
          <span className="demo-event-type">개발·시연용 원격 대화 트리거</span>
        </button>
        <p className="demo-panel-desc" style={{ marginTop: 8 }}>
          {firebaseConnected
            ? '연결된 Pi에서 실제 AI 음성 대화를 시작합니다. Pi가 명령을 받을 때까지 대기 상태가 유지됩니다.'
            : isLive
              ? 'Firebase 연결이 복구되면 원격 대화를 시작할 수 있습니다.'
              : '원격 대화 시작은 Firebase 연결 후 사용할 수 있습니다.'}
        </p>
        <div role="status" aria-live="polite" aria-atomic="true">
          {aiCommand && (
            <div className="demo-last-sent">
              {AI_COMMAND_LABELS[aiCommand.status] ?? '명령 상태 확인 필요'}
              {aiCommand.outcome && ` · ${AI_OUTCOME_LABELS[aiCommand.outcome] ?? '결과 확인 필요'}`}
            </div>
          )}
        </div>
        {latestIsActive && requestedCommand?.status !== 'sending' && (
          <div style={{ marginTop: 8, textAlign: 'center' }}>
            <button
              type="button"
              className="resolve-btn"
              onClick={() => setAllowNewRequest((allowed) => !allowed)}
            >
              {allowNewRequest ? '새 요청 다시 잠그기' : '기기 확인 후 새 요청 허용'}
            </button>
            <p className="demo-panel-desc" style={{ marginTop: 6, marginBottom: 0 }}>
              처리 중인 요청이 오래 멈춘 경우에만, Pi에서 대화가 끝났는지 확인한 뒤 사용하세요.
            </p>
          </div>
        )}
        {aiCommandError && (
          <div className="firebase-error-notice" role="alert" style={{ marginTop: 8 }}>
            {aiCommandError}
          </div>
        )}
      </div>

      <div className="demo-btn-grid">
        {EVENT_TEMPLATES.map((template) => (
          <button
            key={template.type}
            className={`demo-event-btn ${SEVERITY_CLASS[template.severity] ?? ''}`}
            onClick={() => handleClick(template)}
            disabled={!eventWritesEnabled || eventSending || (isLive && !firebaseConnected)}
          >
            <span className="demo-event-icon">{template.icon}</span>
            <span className="demo-event-title">모의 · {template.title}</span>
            <span className="demo-event-type">{template.type}</span>
          </button>
        ))}
      </div>

      {lastSent && (
        <div className="demo-last-sent">✓ &nbsp;[모의] {lastSent} {isLive ? '저장됨' : '화면에 추가됨'}</div>
      )}
      {eventError && <div className="firebase-error-notice" role="alert">{eventError}</div>}
    </div>
  )
}
