import { initializeApp, getApps } from 'firebase/app'
import {
  getDatabase,
  get,
  ref,
  onValue,
  update,
  push,
  query,
  limitToLast,
  serverTimestamp,
  runTransaction,
} from 'firebase/database'
import {
  MEDICATION_SCHEDULE_SCHEMA_VERSION,
  MEDICATION_TIMEZONE,
  normalizeMedicationScheduleDocument,
} from '../medicationSchedule'

import { getEmergencyState, isActionableEventStatus } from '../dashboardState'
import { runMemoryReset, isFirebaseKey } from '../memoryReset'

const COMMAND_HISTORY_LIMIT = 25
export const isIsolatedAuthMode = () => import.meta.env.VITE_ISOLATED_AUTH === 'true'
let authBoundary = null
export function installAuthBoundary(capture) { authBoundary = capture }

const captureAuth = (elderId, deviceId, supplied) => {
  if (!isIsolatedAuthMode()) return { current: () => true, assertCurrent() {}, track: (stop) => stop }
  if (!authBoundary) throw new Error('AUTH_NOT_READY')
  const guard = supplied ?? authBoundary(elderId, deviceId)
  guard.assertCurrent()
  if ((elderId && guard.scope.elderId !== elderId) || (deviceId && guard.scope.deviceId !== deviceId)) {
    throw new Error('AUTH_SCOPE_DENIED')
  }
  return guard
}
const watch = (target, callback, onError, guard) => {
  guard.assertCurrent()
  const stop = onValue(target, (snapshot) => {
    if (guard.current()) callback(snapshot)
  }, (error) => { if (guard.current()) onError?.(error) })
  return guard.track(stop)
}
const refuseRootMutation = () => {
  if (isIsolatedAuthMode()) throw new Error('ISOLATED_EVENT_MUTATION_DISABLED')
}

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  databaseURL: import.meta.env.VITE_FIREBASE_DATABASE_URL,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
}
export const getFirebaseProjectId = () => firebaseConfig.projectId

export function isFirebaseConfigured() {
  return Boolean(
    firebaseConfig.apiKey &&
    firebaseConfig.authDomain &&
    firebaseConfig.databaseURL &&
    firebaseConfig.projectId
  )
}

export function getMemoryResetScope(elderId) {
  if (!isFirebaseConfigured() || !isFirebaseKey(elderId)) return null
  try {
    const url = new URL(firebaseConfig.databaseURL)
    // The local recovery key identifies backend + elder, without credentials,
    // query strings, or fragments. A trailing slash does not create a new scope.
    return `${url.origin}${url.pathname.replace(/\/+$/, '')}|${elderId}`
  } catch {
    return null
  }
}

export function getFirebaseApp() {
  if (isIsolatedAuthMode()) {
    const existing = getApps().find((app) => app.name === 'doran-isolated-guardian')
    if (existing && Object.entries(firebaseConfig).some(([key, value]) => existing.options?.[key] !== value)) {
      throw new Error('AUTH_CONFIGURATION_MISMATCH')
    }
    return existing ?? initializeApp(firebaseConfig, 'doran-isolated-guardian')
  }
  return getApps().length > 0 ? getApps()[0] : initializeApp(firebaseConfig)
}
export const getFirebaseDatabase = () => getDatabase(getFirebaseApp())
const getDb = getFirebaseDatabase

export function subscribeToElder(elderId, callback, onError) {
  const guard = captureAuth(elderId)
  const db = getDb()
  const elderRef = ref(db, `users/${elderId}`)
  return watch(elderRef, (snap) => callback(snap.val()), onError, guard)
}

export function subscribeToDevice(deviceId, callback, onError) {
  const guard = captureAuth(null, deviceId)
  const db = getDb()
  const deviceRef = ref(db, `devices/${deviceId}`)
  return watch(deviceRef, (snap) => callback(snap.val()), onError, guard)
}

export function subscribeToEvents(elderId, callback, onError) {
  const guard = captureAuth(elderId)
  const db = getDb()
  const eventsRef = ref(db, `events/${elderId}`)
  return watch(
    eventsRef,
    (snap) => {
      const val = snap.val()
      const events = val
        ? Object.entries(val).map(([id, data]) => ({ id, ...data }))
        : []
      callback(events)
    },
    onError, guard
  )
}

export function subscribeToCommands(elderId, deviceId, callback, onError) {
  const guard = captureAuth(elderId, deviceId)
  const db = getDb()
  // This dashboard creates Firebase push IDs, so key order bounds the retained
  // client snapshot without requiring a new RTDB `.indexOn` rule.
  const commandsRef = query(
    ref(db, `commands/${elderId}/${deviceId}`),
    limitToLast(COMMAND_HISTORY_LIMIT)
  )
  return watch(
    commandsRef,
    (snap) => {
      const val = snap.val()
      const commands = val
        ? Object.entries(val).map(([id, data]) => ({ id, ...data }))
        : []
      callback(commands)
    },
    onError, guard
  )
}

export function subscribeToConnection(callback, onError) {
  const guard = captureAuth()
  const connectedRef = ref(getDb(), '.info/connected')
  return watch(
    connectedRef,
    (snap) => callback(snap.val() === true),
    onError, guard
  )
}

export function subscribeToMedicationSchedule(elderId, deviceId, callback, onError) {
  const guard = captureAuth(elderId, deviceId)
  const scheduleRef = ref(getDb(), `medicationSchedules/${elderId}/${deviceId}`)
  return watch(scheduleRef, (snap) => callback(snap.val()), onError, guard)
}

export async function resetConversationMemories(elderId, { operationId, deviceId, verifyOnly = false, prepareOnly = false, intent, maxWaitMs = 3000, authGuard } = {}) {
  if (!isFirebaseConfigured()) throw new Error('Firebase가 연결되어 있지 않습니다.')
  if (!isFirebaseKey(elderId)) throw new Error('Invalid elder ID')
  if (!verifyOnly && !isFirebaseKey(deviceId)) throw new Error('Invalid reset device ID')
  let guard
  try { guard = captureAuth(elderId, deviceId, authGuard) }
  catch { return { outcome: 'UNKNOWN', operationId } }
  const db = getDb()
  const operationPath = `memoryResetOperations/${elderId}`
  const preparing = prepareOnly && !verifyOnly
  // Allocation is local; the caller saves the identity before any server write.
  if (!preparing) operationId ??= push(ref(db, operationPath)).key
  if (!preparing && !isFirebaseKey(operationId)) throw new Error('Invalid reset operation ID')
  const operationRef = preparing ? null : ref(db, `${operationPath}/${operationId}`)
  const bounded = (promise) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Memory operation timeout')), 5000)
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
  const guardedRead = async (target) => {
    guard.assertCurrent()
    const snapshot = await bounded(get(target))
    guard.assertCurrent()
    return snapshot.val()
  }
  return runMemoryReset({ operationId, deviceId, verifyOnly, prepareOnly, intent, maxWaitMs, io: {
    readOperation: () => guardedRead(operationRef),
    readControl: () => guardedRead(ref(db, `memories/${elderId}/_control`)),
    readLeaderOperation: (identity) => guardedRead(ref(db, `${operationPath}/${identity}`)),
    createOperation: async (request) => {
      // Create-if-absent preserves terminal records on SDK retries and reconnect.
      // A cold null cache submits a candidate for server hash reconciliation.
      guard.assertCurrent()
      const ownedRequest = isIsolatedAuthMode() ? { ...request, requestedUid: guard.uid } : request
      const result = await bounded(runTransaction(operationRef, (current) => {
        if (!guard.current()) return undefined
        return current == null ? ownedRequest : undefined
      }, { applyLocally: false }))
      guard.assertCurrent()
      return result.snapshot?.val() ?? null
    },
    timestamp: serverTimestamp,
    now: () => performance.now(),
    sleep: async (ms) => { guard.assertCurrent(); await new Promise((resolve) => setTimeout(resolve, ms)); guard.assertCurrent() },
  } })
}
async function medicationScheduleMutation(elderId, deviceId, mutateEntries, authGuard) {
  const guard = captureAuth(elderId, deviceId, authGuard)
  const db = getDb()
  const rootPath = `medicationSchedules/${elderId}/${deviceId}`
  const revision = push(ref(db, `${rootPath}/revisionIds`)).key
  const result = await runTransaction(ref(db, rootPath), (current) => {
    if (!guard.current()) return undefined
    let entries = {}
    if (current != null) {
      const normalized = normalizeMedicationScheduleDocument(current)
      if (normalized.state !== 'ready') {
        throw new Error('Existing medication schedule is invalid')
      }
      entries = Object.fromEntries(normalized.entries.map((entry) => [entry.scheduleId, {
        enabled: entry.enabled,
        time: entry.time,
        medicationName: entry.medicationName,
        weekdays: entry.weekdays,
      }]))
    }

    mutateEntries(entries)
    const candidate = {
      schemaVersion: MEDICATION_SCHEDULE_SCHEMA_VERSION,
      timezone: MEDICATION_TIMEZONE,
      revision,
      updatedAt: serverTimestamp(),
      updatedBy: 'guardian_dashboard',
      ...(isIsolatedAuthMode() ? { updatedUid: guard.uid } : {}),
      entries,
    }
    const normalizedCandidate = normalizeMedicationScheduleDocument(candidate)
    if (normalizedCandidate.state !== 'ready') {
      throw new Error(normalizedCandidate.error || 'Medication schedule is invalid')
    }
    if (Object.keys(entries).length === 0) delete candidate.entries
    return candidate
  }, { applyLocally: false })
  guard.assertCurrent()
  if (!result.committed) throw new Error('Medication schedule transaction was not committed')
  return revision
}

export async function createMedicationSchedule(elderId, deviceId, entry, authGuard) {
  captureAuth(elderId, deviceId, authGuard)
  const db = getDb()
  const rootPath = `medicationSchedules/${elderId}/${deviceId}`
  const scheduleId = push(ref(db, `${rootPath}/entries`)).key
  await medicationScheduleMutation(elderId, deviceId, (entries) => {
    entries[scheduleId] = entry
  }, authGuard)
  return scheduleId
}

export function updateMedicationSchedule(elderId, deviceId, scheduleId, entry, authGuard) {
  return medicationScheduleMutation(elderId, deviceId, (entries) => {
    if (!Object.prototype.hasOwnProperty.call(entries, scheduleId)) {
      throw new Error('Medication schedule entry no longer exists')
    }
    entries[scheduleId] = entry
  }, authGuard)
}

export function deleteMedicationSchedule(elderId, deviceId, scheduleId, authGuard) {
  return medicationScheduleMutation(elderId, deviceId, (entries) => {
    if (!Object.prototype.hasOwnProperty.call(entries, scheduleId)) {
      throw new Error('Medication schedule entry no longer exists')
    }
    delete entries[scheduleId]
  }, authGuard)
}

const isEmergencyEvent = (event) => (
  event?.type === 'emergency_call' || event?.type === 'fall_suspected'
)

// events and devices share only the database root. Recompute from the exact
// snapshot whose hash the server checks; a multi-location update alone cannot
// protect a decision made from an earlier browser snapshot.
async function eventTransaction(mutate) {
  refuseRootMutation()
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const result = await runTransaction(ref(getDb()), mutate, { applyLocally: false })
      if (!result.committed) throw new Error('Event transaction was not committed')
      return result.snapshot.val()
    } catch (error) {
      // The SDK cancels an ancestor transaction with Error('set') when this
      // client writes a command/device/noncritical event. Retry the same intent.
      // Other-client conflicts are already retried by runTransaction itself.
      if (error.message !== 'set' || attempt === 2) throw error
    }
  }
}

function reconcileEmergencyDevice(root, event) {
  if (!isEmergencyEvent(event) || typeof event.deviceId !== 'string' || !event.deviceId) return false
  // Device IDs are global, so include this device's events in every elder bucket.
  const events = Object.values(root.events ?? {}).flatMap((bucket) => Object.values(bucket ?? {}))
  const { emergencyStatus, fallRisk } = getEmergencyState(events, event.deviceId)
  const device = root.devices?.[event.deviceId] ?? {}
  if (device.emergencyStatus === emergencyStatus && device.sensors?.radar?.fallRisk === fallRisk) {
    return false
  }
  root.devices = {
    ...root.devices,
    [event.deviceId]: {
      ...device,
      emergencyStatus,
      sensors: { ...device.sensors, radar: { ...device.sensors?.radar, fallRisk } },
    },
  }
  return true
}

// Repair legacy or externally drifted derived state once a live dashboard has
// loaded. The same root transaction boundary keeps the repair race-safe with
// concurrent SOS creation/resolution, and an already-correct projection aborts
// without writing the database.
export async function reconcileEmergencyDeviceState(deviceId) {
  refuseRootMutation()
  if (typeof deviceId !== 'string' || !deviceId) throw new Error('Device ID is required')
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const result = await runTransaction(ref(getDb()), (current) => {
        // A cold root cache must submit a candidate so the server can reject its
        // stale hash and rerun this callback with the authoritative root.
        if (current == null) return {}
        if (!current.devices?.[deviceId]) return undefined
        const next = structuredClone(current)
        const changed = reconcileEmergencyDevice(next, {
          type: 'emergency_call',
          deviceId,
        })
        return changed ? next : undefined
      }, { applyLocally: false })
      return result.committed
    } catch (error) {
      if (error.message !== 'set' || attempt === 2) throw error
    }
  }
  return false
}

// Keep the old call signature, but never trust caller type/device/clear hints.
// The persisted event identifies the target even when a guardian's view is stale.
export async function resolveEventWithDevicePatch(
  elderId,
  eventId,
  _eventType,
  _deviceId,
  _clearRelatedDeviceState
) {
  const resolvedAt = serverTimestamp()
  let identity
  const root = await eventTransaction((current) => {
    const event = current?.events?.[elderId]?.[eventId]
    if (!event || (!isActionableEventStatus(event.status) && event.status !== 'resolved')) {
      // Cold caches may initially be null/partial. An unchanged candidate lets
      // the server check the hash and supply fresh data instead of aborting early.
      return current
    }
    // A reused/reassigned ID must not turn a retry into resolving a new alert.
    identity ??= Object.fromEntries(['type', 'deviceId', 'elderId', 'createdAt'].map((key) => [key, event[key]]))
    if (!sameEventIdentity(event, identity)) return current
    const next = {
      ...current,
      events: {
        ...current.events,
        [elderId]: {
          ...current.events[elderId],
          [eventId]: event.status === 'resolved' ? event : { ...event, status: 'resolved', resolvedAt },
        },
      },
    }
    reconcileEmergencyDevice(next, event)
    return next
  })
  const saved = root?.events?.[elderId]?.[eventId]
  if (!identity || saved?.status !== 'resolved' || !sameEventIdentity(saved, identity)) {
    throw new Error('Event is missing or cannot be resolved')
  }
}

function sameEventIdentity(existing, event) {
  return ['type', 'deviceId', 'elderId', 'createdAt'].every((key) => existing?.[key] === event[key])
}

export async function pushEvent(elderId, event, devicePatch = {}, eventId) {
  refuseRootMutation()
  const db = getDb()
  // Keep one key across SDK conflict/cancellation retries. Callers retrying an
  // uncertain critical creation can supply the same key as the fourth argument.
  const id = eventId ?? push(ref(db, `events/${elderId}`)).key
  if (isEmergencyEvent(event)) {
    if (typeof event.deviceId !== 'string' || !event.deviceId) throw new Error('Emergency event requires deviceId')
    const candidate = structuredClone({ ...event, status: event.status ?? 'open' })
    const creationPatch = structuredClone(devicePatch)
    const root = await eventTransaction((current) => {
      const existing = current?.events?.[elderId]?.[id]
      if (existing && !sameEventIdentity(existing, candidate)) return current
      const next = structuredClone(current ?? {})
      if (!existing) {
        next.events ??= {}
        next.events[elderId] ??= {}
        next.events[elderId][id] = candidate
        next.devices ??= {}
        const device = next.devices[candidate.deviceId] ??= {}
        for (const [path, value] of Object.entries(creationPatch)) {
          const keys = path.split('/')
          let parent = device
          for (const key of keys.slice(0, -1)) parent = parent[key] ??= {}
          parent[keys.at(-1)] = value
        }
      }
      // Never reopen a previously resolved event or replay its sensor patch.
      reconcileEmergencyDevice(next, existing ?? candidate)
      return next
    })
    if (!sameEventIdentity(root?.events?.[elderId]?.[id], candidate)) {
      throw new Error('Event ID already belongs to a different event')
    }
    return id
  }
  const patch = { [`events/${elderId}/${id}`]: event }
  for (const [path, value] of Object.entries(devicePatch)) {
    patch[`devices/${event.deviceId}/${path}`] = value
  }
  await update(ref(db), patch)
  return id
}

export function updateDevice(deviceId, patch) {
  refuseRootMutation()
  const db = getDb()
  return update(ref(db, `devices/${deviceId}`), patch)
}

export async function pushAiConversationCommand(elderId, deviceId, authGuard) {
  if (!isFirebaseConfigured()) throw new Error('Firebase가 연결되어 있지 않습니다.')
  const guard = captureAuth(elderId, deviceId, authGuard)

  const commandRef = push(ref(getDb(), `commands/${elderId}/${deviceId}`))
  const commandId = commandRef.key
  await update(commandRef, {
    commandId,
    elderId,
    deviceId,
    type: 'start_ai_conversation',
    status: 'pending',
    createdAt: Date.now(),
    ...(isIsolatedAuthMode() ? { requestedUid: guard.uid } : {}),
  })
  guard.assertCurrent()
  return commandId
}

export function subscribeToCommand(elderId, deviceId, commandId, callback, onError) {
  const guard = captureAuth(elderId, deviceId)
  const commandRef = ref(getDb(), `commands/${elderId}/${deviceId}/${commandId}`)
  return watch(commandRef, (snap) => callback(snap.val()), onError, guard)
}
