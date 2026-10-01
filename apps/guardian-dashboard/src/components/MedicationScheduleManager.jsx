import { useMemo, useState } from 'react'
import {
  WEEKDAYS,
  getMedicationApplyView,
  normalizeMedicationScheduleDocument,
  toMedicationSchedulePayload,
  validateMedicationScheduleEntry,
} from '../medicationSchedule'

const EMPTY_FORM = {
  enabled: true,
  time: '08:00',
  medicationName: '',
  weekdays: [1, 2, 3, 4, 5, 6, 7],
}

const REASON_LABELS = {
  missing: 'Firebase 원격 일정이 없습니다. 아래 Pi 적용 상태와 사용 시간을 확인해 주세요.',
  unavailable: 'Firebase 일정을 읽지 못해 안전 설정을 유지합니다.',
  invalid: 'Firebase 일정 형식이 잘못되어 안전 설정을 유지합니다.',
  pending: '원격 일정 확인 전 안전 설정을 준비 중입니다.',
}

function weekdaySummary(days) {
  if (days.length === 7) return '매일'
  return WEEKDAYS.filter((day) => days.includes(day.value)).map((day) => day.label).join('·')
}

export default function MedicationScheduleManager({
  scheduleDocument,
  scheduleLoadState,
  scheduleError,
  deviceStatus,
  canEdit,
  onCreate,
  onUpdate,
  onDelete,
}) {
  const schedule = useMemo(
    () => normalizeMedicationScheduleDocument(scheduleDocument),
    [scheduleDocument]
  )
  const applyView = getMedicationApplyView(deviceStatus, scheduleDocument)
  const [editingId, setEditingId] = useState(null)
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [actionError, setActionError] = useState('')

  const entries = schedule.state === 'ready' ? schedule.entries : []
  const editDisabled = !canEdit || saving || scheduleLoadState === 'loading' ||
    scheduleLoadState === 'error' || schedule.state === 'invalid'
  const activeTimes = Array.isArray(deviceStatus?.activeTimes)
    ? deviceStatus.activeTimes.filter((value) => typeof value === 'string')
    : []

  const beginAdd = () => {
    setEditingId(null)
    setForm({ ...EMPTY_FORM, weekdays: [...EMPTY_FORM.weekdays] })
    setMessage('')
    setActionError('')
  }

  const beginEdit = (entry) => {
    setEditingId(entry.scheduleId)
    setForm({
      enabled: entry.enabled,
      time: entry.time,
      medicationName: entry.medicationName,
      weekdays: [...entry.weekdays],
    })
    setMessage('')
    setActionError('')
  }

  const cancelEdit = () => {
    setEditingId(null)
    setForm(null)
    setActionError('')
  }

  const save = async (event) => {
    event.preventDefault()
    const errors = validateMedicationScheduleEntry(form, entries, editingId)
    if (errors.length > 0) {
      setActionError(errors.join(' '))
      return
    }
    setSaving(true)
    setActionError('')
    setMessage('')
    try {
      const payload = toMedicationSchedulePayload(form)
      if (editingId) await onUpdate(editingId, payload)
      else await onCreate(payload)
      setMessage('Firebase에 저장했습니다. Pi 반영 상태를 확인해 주세요.')
      setEditingId(null)
      setForm(null)
    } catch (error) {
      console.error('[MedicationSchedule] save failed', error)
      setActionError('저장하지 못했습니다. Firebase 연결과 쓰기 권한을 확인해 주세요.')
    } finally {
      setSaving(false)
    }
  }

  const toggleEnabled = async (entry) => {
    const next = { ...entry, enabled: !entry.enabled }
    const errors = validateMedicationScheduleEntry(next, entries, entry.scheduleId)
    if (errors.length > 0) {
      setActionError(errors.join(' '))
      return
    }
    setSaving(true)
    setActionError('')
    try {
      await onUpdate(entry.scheduleId, toMedicationSchedulePayload(next))
      setMessage(next.enabled ? '일정을 활성화했습니다.' : '일정을 비활성화했습니다.')
    } catch (error) {
      console.error('[MedicationSchedule] toggle failed', error)
      setActionError('활성 상태를 변경하지 못했습니다.')
    } finally {
      setSaving(false)
    }
  }

  const remove = async (entry) => {
    if (!window.confirm(`${entry.time} 복약 일정을 삭제할까요?`)) return
    setSaving(true)
    setActionError('')
    try {
      await onDelete(entry.scheduleId)
      setMessage('일정을 삭제했습니다. 오늘의 실제 안내 기록은 그대로 유지됩니다.')
      if (editingId === entry.scheduleId) cancelEdit()
    } catch (error) {
      console.error('[MedicationSchedule] delete failed', error)
      setActionError('일정을 삭제하지 못했습니다.')
    } finally {
      setSaving(false)
    }
  }

  const toggleWeekday = (day) => {
    setForm((current) => ({
      ...current,
      weekdays: current.weekdays.includes(day)
        ? current.weekdays.filter((value) => value !== day)
        : [...current.weekdays, day].sort((left, right) => left - right),
    }))
  }

  return (
    <section className="card medication-schedule-card">
      <div className="medication-schedule-header">
        <div>
          <div className="card-title">⚙️ 보호자 복약 일정 설정</div>
          <div className="medication-schedule-subtitle">
            저장 후 Pi가 보통 5초 안에 새 일정을 확인합니다.
          </div>
        </div>
        <div className="medication-apply-status">
          <span className={`badge ${applyView.badgeClass}`}>{applyView.label}</span>
          {deviceStatus?.reason && (
            <span>{REASON_LABELS[deviceStatus.reason] ?? 'Pi의 일정 상태를 확인해 주세요.'}</span>
          )}
          {activeTimes.length > 0 && <span>현재 사용 시간: {activeTimes.join(' / ')}</span>}
          {Array.isArray(deviceStatus?.activeTimes) && activeTimes.length === 0 && (
            <span>현재 사용 시간: 알림 없음</span>
          )}
        </div>
      </div>

      {scheduleLoadState === 'loading' && (
        <div className="medication-schedule-notice">원격 복약 일정을 불러오는 중입니다.</div>
      )}
      {(scheduleLoadState === 'error' || scheduleError) && (
        <div className="firebase-error-notice medication-inline-notice" role="alert">
          {scheduleError || '원격 복약 일정을 불러오지 못했습니다.'}
        </div>
      )}
      {schedule.state === 'missing' && scheduleLoadState !== 'loading' && (
        <div className="medication-schedule-notice medication-schedule-warning">
          원격 설정이 아직 없습니다. 이 상태는 반영 성공으로 표시하지 않으며, Pi는 마지막 정상 설정이 있으면 유지하고 없을 때만 환경변수 기본 일정을 사용합니다.
        </div>
      )}
      {schedule.state === 'invalid' && (
        <div className="firebase-error-notice medication-inline-notice" role="alert">
          {schedule.error} Pi는 이 값을 적용하지 않고 마지막 정상 설정 또는 기본 일정을 사용합니다.
        </div>
      )}

      <div className="medication-schedule-list">
        {entries.map((entry) => (
          <div className={`medication-schedule-row ${entry.enabled ? '' : 'is-disabled'}`} key={entry.scheduleId}>
            <button
              type="button"
              className={`medication-toggle ${entry.enabled ? 'is-on' : ''}`}
              aria-label={`${entry.time} 일정 ${entry.enabled ? '비활성화' : '활성화'}`}
              aria-pressed={entry.enabled}
              disabled={editDisabled}
              onClick={() => toggleEnabled(entry)}
            >
              <span />
            </button>
            <div className="medication-schedule-main">
              <strong>{entry.time}</strong>
              <span>{entry.medicationName || '복약 안내'}</span>
              <small>{weekdaySummary(entry.weekdays)}</small>
            </div>
            <div className="medication-schedule-actions">
              <button type="button" disabled={editDisabled} onClick={() => beginEdit(entry)}>수정</button>
              <button type="button" disabled={editDisabled} onClick={() => remove(entry)}>삭제</button>
            </div>
          </div>
        ))}
        {schedule.state === 'ready' && entries.length === 0 && (
          <div className="alert-empty medication-empty">
            활성 여부와 관계없이 저장된 일정이 없습니다. Pi에는 “알림 없음” 설정이 명시적으로 적용됩니다.
          </div>
        )}
      </div>

      {!form && (
        <button type="button" className="medication-add-button" disabled={editDisabled} onClick={beginAdd}>
          + 복약 일정 추가
        </button>
      )}

      {form && (
        <form className="medication-schedule-form" onSubmit={save}>
          <div className="medication-form-grid">
            <label>
              <span>시간</span>
              <input
                type="time"
                required
                value={form.time}
                onChange={(event) => setForm({ ...form, time: event.target.value })}
              />
            </label>
            <label>
              <span>약 이름 (선택)</span>
              <input
                type="text"
                maxLength={80}
                placeholder="예: 혈압약"
                value={form.medicationName}
                onChange={(event) => setForm({ ...form, medicationName: event.target.value })}
              />
            </label>
            <label className="medication-enabled-field">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
              />
              <span>저장 후 바로 활성화</span>
            </label>
          </div>
          <fieldset>
            <legend>요일</legend>
            <div className="weekday-buttons">
              {WEEKDAYS.map((day) => (
                <button
                  type="button"
                  key={day.value}
                  className={form.weekdays.includes(day.value) ? 'is-selected' : ''}
                  aria-pressed={form.weekdays.includes(day.value)}
                  onClick={() => toggleWeekday(day.value)}
                >
                  {day.label}
                </button>
              ))}
            </div>
          </fieldset>
          <div className="medication-form-actions">
            <button type="button" onClick={cancelEdit} disabled={saving}>취소</button>
            <button type="submit" className="primary" disabled={saving || !canEdit}>
              {saving ? '저장 중…' : editingId ? '수정 저장' : '일정 저장'}
            </button>
          </div>
        </form>
      )}

      {actionError && <div className="medication-action-error" role="alert">{actionError}</div>}
      {message && <div className="medication-action-success" role="status">{message}</div>}
    </section>
  )
}
