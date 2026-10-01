import { useEffect, useState, useSyncExternalStore } from 'react'
import Header from './components/Header'
import ElderProfileCard from './components/ElderProfileCard'
import PresenceStatus from './components/PresenceStatus'
import DeviceStatusCard from './components/DeviceStatusCard'
import MedicationStatus from './components/MedicationStatus'
import MedicationScheduleManager from './components/MedicationScheduleManager'
import ResetSettings from './components/ResetSettings'
import AiConversationStatus from './components/AiConversationStatus'
import CriticalAlerts from './components/CriticalAlerts'
import EventTimeline from './components/EventTimeline'
import DemoControls from './components/DemoControls'
import DebugPanel from './components/DebugPanel'
import GuardianSignIn from './components/GuardianSignIn'
import { guardianAuth } from './services/guardianAuth'
import { mockMedication } from './data/mockData'
import { useDashboardData } from './hooks/useDashboardData'
import {
  createMedicationSchedule,
  deleteMedicationSchedule,
  getMemoryResetScope,
  isIsolatedAuthMode,
  pushAiConversationCommand,
  resetConversationMemories,
  updateMedicationSchedule,
} from './services/firebase'
import { isActionableEventStatus, selectLatestAiConversationCommand } from './dashboardState'

const ELDER_ID = 'elder_001'
const DEVICE_ID = 'device_001'
const MEMORY_RESET_SCOPE = getMemoryResetScope(ELDER_ID)

export default function App() {
  if (isIsolatedAuthMode()) return <AuthenticatedDashboard />
  return <Dashboard />
}

function AuthenticatedDashboard() {
  const auth = useSyncExternalStore(guardianAuth.subscribe, guardianAuth.getSnapshot)
  useEffect(() => { guardianAuth.start() }, [])
  if (auth.status !== 'ready') return <div className="dashboard"><Header />
    <GuardianSignIn state={auth} onLogin={guardianAuth.login} onLogout={guardianAuth.logout} />
  </div>
  let guard
  try { guard = guardianAuth.capture(auth.scope.elderId, auth.scope.deviceId) }
  catch { return <div className="dashboard"><Header /><p role="status">인증 상태를 확인하고 있습니다…</p></div> }
  return <Dashboard key={`${auth.uid}:${auth.generation}`} authGuard={guard} />
}

function Dashboard({ authGuard = null }) {
  const elderId = authGuard?.scope.elderId ?? ELDER_ID
  const deviceId = authGuard?.scope.deviceId ?? DEVICE_ID
  const isolated = Boolean(authGuard)
  const [, setRelativeTimeTick] = useState(0)
  const {
    elder, device, events, commands,
    medicationSchedule, medicationScheduleState, medicationScheduleError,
    isLive, loading, error, connectionState,
    resolveError, clearResolveError,
    handleResolve, handleSendEvent,
  } = useDashboardData(elderId, deviceId, { isolated })

  useEffect(() => {
    const timer = setInterval(() => setRelativeTimeTick((tick) => tick + 1), 60_000)
    return () => clearInterval(timer)
  }, [])

  const handleStartAiConversation = async () => {
    if (!isLive || connectionState !== 'connected') {
      throw new Error('Firebase가 연결되어 있지 않습니다.')
    }
    return pushAiConversationCommand(elderId, deviceId, authGuard)
  }

  const ensureScheduleWritable = () => {
    if (!isLive || connectionState !== 'connected') {
      throw new Error('Firebase가 연결되어 있지 않습니다.')
    }
  }

  const handleCreateMedicationSchedule = (entry) => {
    ensureScheduleWritable()
    return createMedicationSchedule(elderId, deviceId, entry, authGuard)
  }

  const handleUpdateMedicationSchedule = (scheduleId, entry) => {
    ensureScheduleWritable()
    return updateMedicationSchedule(elderId, deviceId, scheduleId, entry, authGuard)
  }

  const handleDeleteMedicationSchedule = (scheduleId) => {
    ensureScheduleWritable()
    return deleteMedicationSchedule(elderId, deviceId, scheduleId, authGuard)
  }

  const handleMemoryReset = (request) => {
    if (!isLive || connectionState !== 'connected') {
      return Promise.resolve({ outcome: request.verifyOnly ? 'UNKNOWN' : 'FAILED' })
    }
    return resetConversationMemories(elderId, { ...request, deviceId, authGuard })
  }

  const criticalEvents = events.filter(
    (e) => e.severity === 'critical' && isActionableEventStatus(e.status)
  )
  const latestAiCommand = selectLatestAiConversationCommand(commands)

  const connectionMessage = {
    connected: `🟢 Firebase 실시간 연결됨 · ${elderId}`,
    connecting: '🔄 Firebase 연결 중...',
    disconnected: '🟠 Firebase 연결이 끊겼습니다 · 자동으로 다시 연결합니다',
    error: '🔴 Firebase 데이터를 불러오지 못했습니다',
    mock: '🔌 Firebase 미연동 · 개발용 Mock 데이터 표시 중',
  }[connectionState] ?? 'Firebase 연결 상태 확인 중...'

  const connectionClass = connectionState === 'connected'
    ? 'connection-banner-live'
    : connectionState === 'error'
      ? 'connection-banner-error'
      : 'connection-banner-mock'

  return (
    <div className="dashboard">
      <Header />
      {isolated && <div className="connection-banner connection-banner-live">
        보호자 로그인 · {elderId}
        <button type="button" className="resolve-btn" onClick={guardianAuth.logout} style={{ marginLeft: 16 }}>로그아웃</button>
      </div>}

      <div className={`connection-banner ${connectionClass}`}>
        {connectionMessage}
      </div>
      {isolated && <p className="firebase-error-notice" role="status">
        격리 Memory 검증 모드: SOS·낙상 모의 이벤트, 이벤트 확인 완료 처리와 응급 상태 자동 동기화는 비활성화되어 있습니다. 현재 알림을 해결된 것으로 바꾸지 않습니다.
      </p>}

      {error && (
        <div className="firebase-error-notice">
          ⚠️ Firebase 오류: {error}
        </div>
      )}

      {resolveError && (
        <div className="firebase-error-notice" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>⚠️ {resolveError}</span>
          <button
            onClick={clearResolveError}
            style={{ marginLeft: 12, background: 'none', border: 'none', cursor: 'pointer', fontSize: 16, fontWeight: 700, color: 'inherit' }}
          >
            ✕
          </button>
        </div>
      )}

      {loading ? (
        <div className="loading-screen">
          <div className="loading-spinner" />
          Firebase 데이터 불러오는 중...
        </div>
      ) : (
        <main className="main-content">
          {/* Row 1: Profile · Presence · Device */}
          <div className="grid-top">
            <ElderProfileCard elder={elder} />
            <PresenceStatus device={device} />
            <DeviceStatusCard device={device} />
          </div>

          <MedicationScheduleManager
            scheduleDocument={medicationSchedule}
            scheduleLoadState={medicationScheduleState}
            scheduleError={medicationScheduleError}
            deviceStatus={device?.medicationScheduleStatus}
            canEdit={isLive && connectionState === 'connected'}
            onCreate={handleCreateMedicationSchedule}
            onUpdate={handleUpdateMedicationSchedule}
            onDelete={handleDeleteMedicationSchedule}
          />

          <ResetSettings
            resetScope={isolated ? getMemoryResetScope(elderId) : MEMORY_RESET_SCOPE}
            isCurrent={authGuard?.current}
            canReset={isLive && connectionState === 'connected'}
            onMemoryReset={handleMemoryReset}
          />

          {/* Row 2: Medication · Critical alerts */}
          <div className="grid-middle">
            <MedicationStatus
              medication={isLive ? null : mockMedication}
              events={events}
              deviceId={deviceId}
              isLive={isLive}
              scheduleDocument={medicationSchedule}
              scheduleLoadState={medicationScheduleState}
            />
            <AiConversationStatus command={latestAiCommand} />
            <CriticalAlerts events={criticalEvents} />
          </div>

          {/* Row 3: Event timeline */}
          <EventTimeline events={events} onResolve={isolated ? undefined : handleResolve} />

          {/* Row 4: Demo controls */}
          <DemoControls
            isLive={isLive}
            connectionState={connectionState}
            commands={commands}
            latestAiCommand={latestAiCommand}
            onSendEvent={handleSendEvent}
            onStartAiConversation={handleStartAiConversation}
            eventWritesEnabled={!isolated}
          />

          {/* Row 5: Temporary debug panel */}
          <DebugPanel
            isLive={isLive}
            elderId={elderId}
            deviceId={deviceId}
            events={events}
          />
        </main>
      )}
    </div>
  )
}
