import {
  buildTodayMedicationRows,
  normalizeMedicationScheduleDocument,
} from '../medicationSchedule'

const MOCK_STATUS = {
  taken: { label: '복용 완료', badgeClass: 'badge-success' },
  delayed: { label: '미룸', badgeClass: 'badge-warning' },
  pending: { label: '예정', badgeClass: 'badge-info' },
}

export default function MedicationStatus({
  medication,
  events,
  deviceId,
  isLive = false,
  scheduleDocument,
  scheduleLoadState,
}) {
  const liveRows = isLive
    ? buildTodayMedicationRows(scheduleDocument, events, Date.now(), deviceId)
    : []
  const mockRows = !isLive && medication
    ? Object.values(medication).map((item, index) => ({
        key: `${item?.label ?? '복약'}-${item?.scheduledTime ?? index}`,
        time: item?.scheduledTime ?? '-',
        medicationName: item?.label ?? '복약 안내',
        ...(MOCK_STATUS[item?.status] ?? { label: '상태 확인 필요', badgeClass: 'badge-neutral' }),
        detail: '개발용 Mock 데이터',
      }))
    : []
  const rows = isLive ? liveRows : mockRows
  const normalizedSchedule = isLive
    ? normalizeMedicationScheduleDocument(scheduleDocument)
    : null

  let emptyMessage = '오늘 예정된 복약 일정이 없습니다.'
  if (isLive && scheduleLoadState === 'loading') emptyMessage = '복약 일정을 불러오는 중입니다.'
  if (isLive && scheduleLoadState === 'missing') {
    emptyMessage = '원격 일정이 없습니다. Pi가 유지 중인 실제 적용 일정을 위 설정 카드에서 확인해 주세요.'
  }
  if (isLive && scheduleLoadState === 'error') {
    emptyMessage = '복약 일정을 읽지 못했습니다. 실제 Pi 적용 상태를 확인해 주세요.'
  }
  if (isLive && normalizedSchedule?.state === 'invalid') {
    emptyMessage = '원격 일정 형식이 올바르지 않습니다. Pi가 유지 중인 마지막 정상 설정 또는 기본 일정을 확인해 주세요.'
  }

  return (
    <div className="card">
      <div className="card-title">💊 오늘의 복약 현황</div>
      {rows.length === 0 ? (
        <div className="alert-empty">
          <div className="alert-empty-icon">💊</div>
          {emptyMessage}
        </div>
      ) : (
        <div className="medication-today-list">
          {rows.map((row) => (
            <div
              key={row.key}
              className={`med-row ${['deferred', 'no_speech', 'unclear'].includes(row.state) ? 'med-row-delayed' : ''}`}
              data-medication-state={row.state ?? 'mock'}
            >
              <div>
                <div className="med-row-label">
                  {row.time} · {row.medicationName || '복약 안내'}
                </div>
                <div className="med-row-meta">{row.detail}</div>
              </div>
              <span className={`badge ${row.badgeClass}`}>{row.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
