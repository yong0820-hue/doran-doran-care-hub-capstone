import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createMemoryResetFlow, RESET_ACKNOWLEDGEMENT } from '../memoryReset'

const preventRepeatedActivation = (event) => {
  if (event.repeat && (event.key === 'Enter' || event.key === ' ')) event.preventDefault()
}

export default function ResetSettings({ canReset, onMemoryReset, resetScope, isCurrent = () => true }) {
  const resetHandler = useRef(onMemoryReset)
  resetHandler.current = onMemoryReset
  const currentOwner = useRef(isCurrent)
  currentOwner.current = isCurrent
  const [flow] = useState(() => createMemoryResetFlow((request) => resetHandler.current(request), {
    scope: resetScope, prepareIntent: () => resetHandler.current({ prepareOnly: true }),
    isCurrent: () => currentOwner.current(),
  }))
  const state = useSyncExternalStore(flow.subscribe, flow.getSnapshot)
  const dialog = useRef(null)
  const openButton = useRef(null)
  const cancelButton = useRef(null)
  const acknowledgementInput = useRef(null)
  const isOpen = state.step !== 'idle'
  const pending = state.step === 'pending' || state.verifying
  const unknown = state.outcome === 'UNKNOWN'
  const waiting = state.outcome === 'WAITING_FOR_DEVICE'
  const unresolved = unknown || waiting

  useEffect(() => {
    // Reload/reconnection reconciles saved IDs; it can never enqueue a reset.
    void flow.reconcile(canReset)
  }, [flow, canReset])

  useEffect(() => {
    if (isOpen && !dialog.current.open) {
      dialog.current.showModal()
      cancelButton.current?.focus()
    } else if (!isOpen && dialog.current.open) {
      dialog.current.close()
      openButton.current?.focus()
    }
  }, [isOpen])

  useEffect(() => {
    if (state.step === 'confirm') acknowledgementInput.current?.focus()
  }, [state.step])

  return (
    <section className="card reset-settings" aria-labelledby="reset-settings-title">
      <div className="card-title" id="reset-settings-title">⚙️ 대화와 기기 관리</div>
      <div className="reset-settings-row">
        <div>
          <h3>기억 초기화</h3>
          <p>대화에서 저장한 약속, 관계, 취향 등의 기억을 비활성화합니다.</p>
          <p>복약 일정, 돌봄·응급 이력, 대화 기록, 보호자 계정과 기기 설정은 유지됩니다.</p>
        </div>
        <button
          ref={openButton}
          type="button"
          className="reset-danger-button"
          disabled={!canReset || pending}
          onClick={() => flow.open(canReset)}
        >기억 초기화</button>
      </div>
      {!canReset && <p className="reset-settings-note">Firebase에 연결된 상태에서 사용할 수 있습니다.</p>}
      {state.message && <p className="medication-action-success" role="status">{state.message}</p>}
      {unresolved && !isOpen && <p className="reset-result-unknown" role="status">{state.error}</p>}
      {state.outcome === 'FAILED' && !isOpen && <p className="medication-action-error" role="alert">{state.error}</p>}
      <div className="reset-settings-row reset-device-row">
        <div>
          <h3>기기 초기화</h3>
          <p>준비 중입니다. 기기의 임시 데이터와 개인 설정을 초기화하는 기능이 추가될 예정입니다.</p>
        </div>
        <button type="button" disabled>기기 초기화 · 준비 중</button>
      </div>

      <dialog
        ref={dialog}
        className="reset-dialog"
        aria-labelledby="memory-reset-title"
        aria-describedby="memory-reset-description"
        onCancel={(event) => { event.preventDefault(); flow.cancel() }}
      >
        <h2 id="memory-reset-title">{waiting ? '기기 초기화 완료 대기' : unknown ? '기억 초기화 결과 확인' : state.step === 'review' ? '기억 초기화 내용을 확인해 주세요' : '기억을 초기화할까요?'}</h2>
        <div id="memory-reset-description">
          <p>어르신과의 대화에서 저장한 기억이 더 이상 새 대화에 사용되지 않습니다.</p>
          <p>복약 일정, 돌봄·응급 이력, 대화 기록, 보호자 계정과 기기 설정은 유지됩니다.</p>
          <p>이미 요청한 응답은 이어질 수 있습니다. 초기화 완료 후 새 응답 요청에는 이전 저장 기억이 사용되지 않습니다.</p>
          {state.step !== 'review' && <p className="reset-final-warning">확인 후 비활성화한 기억은 이 화면에서 되돌릴 수 없습니다.</p>}
        </div>
        {state.error && <p className={unresolved ? 'reset-result-unknown' : 'medication-action-error'} role="alert">{state.error}</p>}
        {state.step === 'preparing' && <p className="reset-settings-note" role="status">초기화 상태를 확인하고 있습니다…</p>}
        {!canReset && <p className="medication-action-error" role="alert">Firebase 연결이 끊겼습니다. 다시 연결한 뒤 진행해 주세요.</p>}
        <div className="reset-dialog-actions">
          <button ref={cancelButton} type="button" disabled={pending} onClick={() => flow.cancel()}>{unresolved ? '닫기' : '취소'}</button>
          <button type="button" disabled={!canReset || state.step !== 'review'} onKeyDown={preventRepeatedActivation} onClick={() => flow.advance(canReset)}>내용 확인 · 다음</button>
        </div>
        {(state.step === 'confirm' || (pending && !unresolved)) && (
          <div className="reset-final-action">
            <label htmlFor="memory-reset-acknowledgement">확인 문구 <strong>{RESET_ACKNOWLEDGEMENT}</strong>를 입력해 주세요.</label>
            <input
              ref={acknowledgementInput}
              id="memory-reset-acknowledgement"
              type="text"
              value={state.acknowledgement}
              disabled={pending}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => flow.acknowledge(event.target.value)}
            />
            <button
              type="button"
              className="reset-danger-button"
              disabled={!canReset || pending || state.acknowledgement !== RESET_ACKNOWLEDGEMENT}
              onKeyDown={preventRepeatedActivation}
              onClick={() => flow.confirm(canReset)}
            >{pending ? '초기화 중…' : '다시 확인 · 기억 초기화'}</button>
          </div>
        )}
        {unresolved && <div className="reset-final-action"><button type="button" disabled={!canReset || pending} onClick={() => flow.verify(canReset)}>{pending ? '결과 확인 중…' : '결과 확인'}</button></div>}
      </dialog>
    </section>
  )
}
