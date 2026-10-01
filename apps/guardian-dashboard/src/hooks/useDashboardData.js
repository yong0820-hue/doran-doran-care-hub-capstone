import { useState, useEffect, useRef } from 'react'
import {
  isFirebaseConfigured,
  subscribeToElder,
  subscribeToDevice,
  subscribeToEvents,
  subscribeToCommands,
  subscribeToConnection,
  subscribeToMedicationSchedule,
  reconcileEmergencyDeviceState,
  resolveEventWithDevicePatch,
  pushEvent,
} from '../services/firebase'
import { mockElder, mockDevice, mockEvents } from '../data/mockData'
import { getEmergencyState, isActionableEventStatus } from '../dashboardState'

const LIVE = isFirebaseConfigured()
const OFFLINE_LOADING_GRACE_MS = 3000

// Device state side-effects triggered by certain event types.
const DEVICE_PATCHES = {
  presence_detected: { 'sensors/radar/presence': true },
  absence_detected:  { 'sensors/radar/presence': false },
  fall_suspected:    { 'sensors/radar/fallRisk': 'high', emergencyStatus: 'active' },
  emergency_call:    { emergencyStatus: 'active' },
}

export function useDashboardData(elderId, deviceId, { isolated = false } = {}) {
  const reportError = (message, error) => {
    // SDK errors are opaque here: isolated authentication never logs their
    // payload, which can contain request or account details.
    if (isolated) console.error(message)
    else console.error(message, error)
  }
  const mockSequence = useRef(0)
  const [elder, setElder] = useState(LIVE ? null : mockElder)
  const [device, setDevice] = useState(LIVE ? null : mockDevice)
  const [events, setEvents] = useState(LIVE ? [] : mockEvents)
  const [commands, setCommands] = useState([])
  const [medicationSchedule, setMedicationSchedule] = useState(null)
  const [medicationScheduleState, setMedicationScheduleState] = useState(
    LIVE ? 'loading' : 'mock'
  )
  const [medicationScheduleError, setMedicationScheduleError] = useState(null)
  const [loading, setLoading] = useState(LIVE)
  const [error, setError] = useState(null)
  const [connectionState, setConnectionState] = useState(LIVE ? 'connecting' : 'mock')
  const [resolveError, setResolveError] = useState(null)

  useEffect(() => {
    if (!LIVE) return

    let active = true
    let subscriptionError = false
    let firebaseConnected = false
    let emergencyReconcileInFlight = false
    let emergencyReconciled = false
    let offlineLoadingTimer = null
    const readySources = new Set()
    const unsubscribers = []
    const requiredSources = ['elder', 'device', 'events', 'commands', 'medicationSchedule']

    setElder(null)
    setDevice(null)
    setEvents([])
    setCommands([])
    setMedicationSchedule(null)
    setMedicationScheduleState('loading')
    setMedicationScheduleError(null)
    setLoading(true)
    setError(null)
    setConnectionState('connecting')

    const clearOfflineLoadingTimer = () => {
      if (offlineLoadingTimer !== null) {
        clearTimeout(offlineLoadingTimer)
        offlineLoadingTimer = null
      }
    }

    const unsubscribeAll = () => {
      clearOfflineLoadingTimer()
      while (unsubscribers.length > 0) {
        const unsubscribe = unsubscribers.pop()
        try {
          unsubscribe?.()
        } catch (err) {
          reportError('[Firebase] unsubscribe failed', err)
        }
      }
    }

    const maybeReconcileEmergencyState = () => {
      if (isolated) return
      if (!active || !firebaseConnected || emergencyReconcileInFlight || emergencyReconciled) return
      if (!requiredSources.every((item) => readySources.has(item))) return
      emergencyReconcileInFlight = true
      reconcileEmergencyDeviceState(deviceId)
        .then(() => { emergencyReconciled = true })
        .catch((err) => {
          reportError('[Firebase] emergency state reconciliation failed', err)
          if (active) {
            setResolveError('응급 상태 동기화 실패: Firebase 권한 또는 네트워크를 확인하세요.')
          }
        })
        .finally(() => { emergencyReconcileInFlight = false })
    }

    const markReady = (source) => {
      if (!active) return
      readySources.add(source)
      if (requiredSources.every((item) => readySources.has(item))) {
        clearOfflineLoadingTimer()
        setLoading(false)
        maybeReconcileEmergencyState()
      }
    }

    const handleError = (source) => (err) => {
      if (!active) return
      reportError(`[Firebase] ${source}`, err)
      subscriptionError = true
      setError(
        source === 'connection'
          ? 'Firebase 연결 상태를 확인하지 못했습니다.'
          : '실시간 데이터를 불러오지 못했습니다. 네트워크와 Firebase 권한을 확인해 주세요.'
      )
      setConnectionState('error')
      clearOfflineLoadingTimer()
      setLoading(false)
      if (requiredSources.includes(source)) markReady(source)
    }

    try {
      unsubscribers.push(subscribeToElder(
        elderId,
        (data) => {
          if (!active) return
          setElder(data ? { id: elderId, ...data } : null)
          markReady('elder')
        },
        handleError('elder')
      ))

      unsubscribers.push(subscribeToDevice(
        deviceId,
        (data) => {
          if (!active) return
          setDevice(data ? { id: deviceId, ...data } : null)
          markReady('device')
        },
        handleError('device')
      ))

      unsubscribers.push(subscribeToEvents(
        elderId,
        (data) => {
          if (!active) return
          setEvents(data)
          markReady('events')
        },
        handleError('events')
      ))

      unsubscribers.push(subscribeToCommands(
        elderId,
        deviceId,
        (data) => {
          if (!active) return
          setCommands(data)
          markReady('commands')
        },
        handleError('commands')
      ))

      unsubscribers.push(subscribeToMedicationSchedule(
        elderId,
        deviceId,
        (data) => {
          if (!active) return
          setMedicationSchedule(data)
          setMedicationScheduleState(data == null ? 'missing' : 'ready')
          setMedicationScheduleError(null)
          markReady('medicationSchedule')
        },
        (err) => {
          if (!active) return
          setMedicationScheduleState('error')
          setMedicationScheduleError(
            '복약 일정 데이터를 읽지 못했습니다. Firebase 권한과 연결을 확인해 주세요.'
          )
          handleError('medicationSchedule')(err)
        }
      ))

      unsubscribers.push(subscribeToConnection(
        (connected) => {
          if (!active || subscriptionError) return
          firebaseConnected = connected
          setConnectionState(connected ? 'connected' : 'disconnected')
          clearOfflineLoadingTimer()
          if (connected) maybeReconcileEmergencyState()
          if (!connected && !requiredSources.every((item) => readySources.has(item))) {
            offlineLoadingTimer = setTimeout(() => {
              if (active && !subscriptionError) setLoading(false)
            }, OFFLINE_LOADING_GRACE_MS)
          }
        },
        handleError('connection')
      ))
    } catch (err) {
      if (active) {
        reportError('[Firebase] subscription setup', err)
        setError('Firebase 실시간 연결을 시작하지 못했습니다.')
        setConnectionState('error')
        setLoading(false)
      }
      active = false
      unsubscribeAll()
    }

    return () => {
      active = false
      unsubscribeAll()
    }
  }, [elderId, deviceId, isolated])

  useEffect(() => {
    if (LIVE) return
    const { emergencyStatus, fallRisk } = getEmergencyState(events, deviceId)
    setDevice((prev) => ({
      ...prev,
      emergencyStatus,
      sensors: { ...prev?.sensors, radar: { ...prev?.sensors?.radar, fallRisk } },
    }))
  }, [events, deviceId])

  const handleResolve = (eventId) => {
    if (isolated) return
    const evt = events.find((e) => e.id === eventId)
    if (!evt || !isActionableEventStatus(evt.status)) return

    if (LIVE) {
      setResolveError(null)
      resolveEventWithDevicePatch(
        elderId,
        eventId,
        evt.type,
        evt.deviceId
      )
        .catch((err) => {
          reportError('[Firebase] resolve failed', err)
          setResolveError('확인 완료 처리 실패: Firebase 권한 또는 네트워크를 확인하세요.')
        })
    } else {
      // Derive the mock device from the resulting event list as well. The
      // effect below runs after queued updates, including same-render actions.
      setEvents((prev) => prev.map((event) => (
        event.id === eventId && isActionableEventStatus(event.status)
          ? { ...event, status: 'resolved', resolvedAt: Date.now() }
          : event
      )))
    }
  }

  const handleSendEvent = async (template) => {
    if (isolated) throw new Error('ISOLATED_EVENT_MUTATION_DISABLED')
    if (LIVE && connectionState !== 'connected') {
      throw new Error('Firebase 연결을 확인해 주세요.')
    }
    const now = Date.now()
    const event = {
      type: template.type,
      severity: template.severity,
      title: `[모의] ${template.title}`,
      message: `시뮬레이션 기록: ${template.message}`,
      meta: { source: 'guardian_dashboard_demo' },
      status: 'open',
      deviceId,
      deviceName: device?.name ?? '도란도란 거실 라디오',
      elderId,
      location: device?.location ?? '거실',
      createdAt: now,
    }

    const patch = template.type === 'medication_taken'
      ? { 'sensors/pillbox/lastOpenedAt': now }
      : DEVICE_PATCHES[template.type] ?? {}
    if (LIVE) {
      await pushEvent(elderId, event, patch)
    } else {
      const id = `mock_${now}_${++mockSequence.current}`
      setEvents((prev) => [{ ...event, id }, ...prev])
      setDevice((prev) => {
        const next = structuredClone(prev ?? {})
        for (const [path, value] of Object.entries(patch)) {
          const keys = path.split('/')
          let parent = next
          for (const key of keys.slice(0, -1)) parent = parent[key] ??= {}
          parent[keys.at(-1)] = value
        }
        return next
      })
    }
  }

  return {
    elder, device, events, commands,
    medicationSchedule, medicationScheduleState, medicationScheduleError,
    isLive: LIVE, loading, error, connectionState,
    resolveError, clearResolveError: () => setResolveError(null),
    handleResolve, handleSendEvent,
  }
}
