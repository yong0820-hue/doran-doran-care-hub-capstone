const isRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
export const RESET_ACKNOWLEDGEMENT = '기억 초기화'
const UNKNOWN_RESET_MESSAGE = '초기화 결과를 확인할 수 없습니다. 연결/권한을 확인한 후 다시 확인하세요.'
const WAITING_RESET_MESSAGE = '기기에 기억 초기화를 요청했습니다. 기기가 연결되어 초기화를 완료할 때까지 기다려 주세요. 결과 확인으로 진행 상태를 확인할 수 있습니다.'
const RECOVERY_STORAGE_MESSAGE = '브라우저 저장소의 초기화 복구 정보를 확인할 수 없습니다. 이전 결과 확인 전에는 새 초기화를 진행하지 않습니다.'

const createOperationId = () => Array.from(
  globalThis.crypto.getRandomValues(new Uint8Array(16)),
  (byte) => byte.toString(16).padStart(2, '0')
).join('')

export function isFirebaseKey(value) {
  return typeof value === 'string' && value.length > 0 && !/[.#$\[\]/\u0000-\u001f\u007f]/u.test(value)
}

const boundedKey = (key) => isFirebaseKey(key) && key.length <= 80
const validGeneration = (generation) => (typeof generation === 'string' && generation.length > 0 && generation.length <= 80)
  || (Number.isSafeInteger(generation) && generation >= 0 && generation < Number.MAX_SAFE_INTEGER)

const copyIntent = (intent) => {
  if (!isRecord(intent) || intent.version !== 1 || !validGeneration(intent.generation)
      || !Number.isSafeInteger(intent.epoch) || intent.epoch < 0
      || (intent.operationId != null && !boundedKey(intent.operationId))) throw new Error('Invalid reset intent')
  return Object.freeze({ version: 1, generation: intent.generation, operationId: intent.operationId ?? null, epoch: intent.epoch })
}

const resetIntent = (control, deviceId) => {
  if (control != null && !isRecord(control)) throw new Error('Invalid memory control')
  const generation = control?.generation ?? 'initial'
  if (!validGeneration(generation)) throw new Error('Invalid memory generation')
  const coordinator = control?.resetCoordinator
  if (coordinator != null && !isRecord(coordinator)) throw new Error('Invalid reset coordinator')
  const epoch = coordinator?.epoch ?? 0
  if (!Number.isSafeInteger(epoch) || epoch < 0) throw new Error('Invalid reset epoch')
  const active = coordinator?.deviceId === deviceId && ['requested', 'processing', 'committed'].includes(coordinator.state)
  if (active && (!boundedKey(coordinator.activeOperationId) || epoch === 0)) throw new Error('Invalid active reset coordinator')
  return copyIntent({ version: 1, generation, operationId: active ? coordinator.activeOperationId : null, epoch })
}

const prepareResetIntent = async (io, deviceId) => {
  const intent = resetIntent(await io.readControl(), deviceId)
  if (!intent.operationId) return intent
  // The permanent operation is authoritative for SUCCESS. Its final control
  // bookkeeping may still be retrying after the user has observed completion.
  const leader = await io.readLeaderOperation(intent.operationId)
  if (!isRecord(leader) || leader.deviceId !== deviceId
      || !['requested', 'processing', 'completed'].includes(leader.status)) throw new Error('Reset leader unavailable')
  if (leader.status !== 'completed') return intent
  return copyIntent({ ...intent, generation: leader.resultingGeneration ?? leader.generation, operationId: null })
}

// Guardian only creates an operation and observes its authoritative result.
// The Pi owns the exclusive dispatch barrier, memory mutation, cache invalidation,
// and completion publication. No browser clock or lease can authorize SUCCESS.
export async function runMemoryReset({ io, operationId, deviceId, verifyOnly = false, prepareOnly = false, intent, maxWaitMs = 3000 }) {
  const preparing = prepareOnly && !verifyOnly
  if (!preparing && !boundedKey(operationId)) throw new Error('Invalid reset operation ID')
  if (!verifyOnly && !boundedKey(deviceId)) throw new Error('Invalid reset device ID')
  const outcome = (status, fields = {}) => ({ outcome: status, operationId, ...fields })
  const resolved = (operation) => {
    if (!isRecord(operation)) return null
    if (operation.status === 'completed') {
      const generation = operation.generation ?? operation.resultingGeneration
      return outcome('SUCCESS', { generation, operationStatus: 'completed' })
    }
    if (operation.status === 'failed') return outcome('FAILED', {
      operationStatus: 'failed', reason: operation.reason ?? operation.errorCode,
    })
    return null
  }
  try {
    if (preparing) return outcome('PREPARED', { intent: await prepareResetIntent(io, deviceId) })
    // Snapshot supplied intent before the first await. A delayed operation read
    // must never change what the user explicitly acknowledged.
    const preparedIntent = !verifyOnly && intent != null ? copyIntent(intent) : null
    let operation = await io.readOperation()
    const previous = resolved(operation)
    if (previous) return previous
    if (operation == null && !verifyOnly) {
      // Bind the request once, before its create-if-absent write. The Pi can
      // coalesce simultaneous/delayed requests by generation or active leader,
      // even when the leader has already advanced the memory generation.
      const requestIntent = preparedIntent ?? resetIntent(await io.readControl(), deviceId)
      operation = await io.createOperation({
        status: 'requested', deviceId, deviceStatus: `${deviceId}|requested`, intent: requestIntent,
        requestedAt: io.timestamp(), requestedBy: 'guardian_dashboard',
      })
    }
    // Verification never recreates an absent operation, even after a denied read.
    // An absent or malformed result cannot prove that an earlier write failed.
    const deadline = io.now() + Math.max(0, Math.min(maxWaitMs, 10000))
    for (let attempt = 0; attempt <= 40; attempt++) {
      const result = resolved(operation)
      if (result) return result
      if (!isRecord(operation) || !['requested', 'processing'].includes(operation.status)) return outcome('UNKNOWN')
      const remaining = deadline - io.now()
      if (remaining <= 0 || attempt === 40) return outcome('WAITING_FOR_DEVICE', { operationStatus: operation.status })
      await io.sleep(Math.min(250, remaining))
      operation = await io.readOperation()
    }
  } catch {
    if (preparing) return outcome('UNKNOWN')
    // ACK loss may hide a completed Pi operation. All read failures (including
    // permission_denied) remain uncertainty, never evidence of reset failure.
    try {
      const result = resolved(await io.readOperation())
      if (result) return result
    } catch { /* keep the recovery identity until an authoritative terminal state */ }
    return outcome('UNKNOWN')
  }
}
// Keep the request sequence independent of rendering, so a double click,
// disconnected dashboard, or a cancelled dialog cannot dispatch a reset.
export function createMemoryResetFlow(reset, { scope, storage, prepareIntent, isCurrent = () => true } = {}) {
  let state = { step: 'idle', outcome: null, acknowledgement: '', operationId: null, error: '', message: '' }
  let preparedIntent = null
  const storagePrefix = typeof scope === 'string' && scope ? `doran.memoryReset.pending.v2:${encodeURIComponent(scope)}.` : null
  const legacyStorageKey = storagePrefix ? `doran.memoryReset.pending.v1:${encodeURIComponent(scope)}` : null
  const recovery = new Map()
  const getStorage = () => {
    if (!storagePrefix) throw new Error('Reset recovery scope is required')
    const target = storage ?? globalThis.localStorage
    if (!target || typeof target.getItem !== 'function') throw new Error('Reset recovery storage is unavailable')
    return target
  }
  const enumeratePending = () => {
    const target = getStorage()
    const add = (key, version) => {
      const raw = target.getItem(key)
      if (raw === null) return
      const pending = JSON.parse(raw)
      if (pending?.version !== version || !boundedKey(pending.operationId)
          || (version === 2 && key !== `${storagePrefix}${pending.operationId}`)) throw new Error('Invalid reset recovery record')
      recovery.set(pending.operationId, key)
    }
    // Preserve pre-upgrade unresolved operations without writing another v1
    // singleton. Every new dispatch uses its own immutable recovery identity.
    add(legacyStorageKey, 1)
    if (!Number.isSafeInteger(target.length) || typeof target.key !== 'function') throw new Error('Reset recovery enumeration unavailable')
    const keys = Array.from({ length: target.length }, (_, index) => target.key(index))
    for (const key of keys) if (key?.startsWith(storagePrefix)) add(key, 2)
    return [...recovery.keys()]
  }
  try {
    const pending = storagePrefix ? enumeratePending() : []
    if (pending.length) state = { ...state, outcome: 'UNKNOWN', operationId: pending[0], error: UNKNOWN_RESET_MESSAGE }
  } catch {
    // Corruption/access denial may hide a previously dispatched operation.
    // Keep its marker untouched and never turn uncertainty into a new reset.
    state = { ...state, outcome: 'UNKNOWN', error: RECOVERY_STORAGE_MESSAGE }
  }
  const listeners = new Set()
  const publish = (next) => {
    state = next
    for (const listener of listeners) listener()
  }
  const unknown = (operationId, error = UNKNOWN_RESET_MESSAGE) => {
    publish({ ...state, step: 'unknown', outcome: 'UNKNOWN', operationId,
      acknowledgement: '', message: '', error })
    return false
  }
  const clearResolved = (operationId) => {
    try {
      const key = recovery.get(operationId) ?? `${storagePrefix}${operationId}`
      const raw = getStorage().getItem(key)
      if (raw !== null && JSON.parse(raw)?.operationId === operationId) getStorage().removeItem(key)
      recovery.delete(operationId)
    } catch { /* keep the saved marker for verification after storage recovers */ }
  }
  const dispatch = async (verifyOnly, background = false) => {
    if (!isCurrent()) return false
    let operationId = state.operationId
    let operationIds = [operationId]
    if (verifyOnly) {
      try { operationIds = [...new Set([...enumeratePending(), operationId].filter(Boolean))] }
      catch { return unknown(operationId, RECOVERY_STORAGE_MESSAGE) }
      if (!operationIds.length) return unknown(null, RECOVERY_STORAGE_MESSAGE)
    } else {
      // A tab may have opened this dialog before another tab became UNKNOWN.
      // Recheck shared storage immediately before issuing any new operation.
      let pending
      try { pending = enumeratePending() }
      catch { return unknown(operationId, RECOVERY_STORAGE_MESSAGE) }
      if (pending.length) return unknown(pending[0])
      try {
        const key = `${storagePrefix}${operationId}`
        const marker = JSON.stringify({ version: 2, operationId })
        getStorage().setItem(key, marker)
        if (getStorage().getItem(key) !== marker) throw new Error('Reset recovery record was not saved')
        recovery.set(operationId, key)
      } catch {
        publish({ ...state, step: 'confirm', outcome: 'FAILED', acknowledgement: '', message: '',
          error: '브라우저 저장소에 복구 정보를 저장하지 못해 기억 초기화를 시작하지 않았습니다. 저장소 설정을 확인해 주세요.' })
        return false
      }
    }
    publish({ ...state, operationId, step: background ? 'idle' : 'pending', verifying: true, acknowledgement: '', error: '', message: '' })
    const results = []
    for (const id of operationIds) {
      if (!isCurrent()) { results.push({ operationId: id, outcome: 'UNKNOWN' }); break }
      let result
      try { result = await reset({ operationId: id, verifyOnly,
        ...(!verifyOnly && preparedIntent ? { intent: preparedIntent } : {}) }) }
      catch { result = { outcome: 'UNKNOWN' } }
      const outcome = isCurrent() ? result?.outcome ?? 'UNKNOWN' : 'UNKNOWN'
      if (['SUCCESS', 'FAILED'].includes(outcome)) clearResolved(id)
      results.push({ operationId: id, outcome })
    }
    const unresolved = results.find((result) => !['SUCCESS', 'FAILED', 'WAITING_FOR_DEVICE'].includes(result.outcome))
      ?? results.find((result) => result.outcome === 'WAITING_FOR_DEVICE')
    if (unresolved) {
      const waiting = unresolved.outcome === 'WAITING_FOR_DEVICE'
      publish({ ...state, step: background ? 'idle' : waiting ? 'waiting' : 'unknown', verifying: false,
        outcome: waiting ? 'WAITING_FOR_DEVICE' : 'UNKNOWN', operationId: unresolved.operationId,
        acknowledgement: '', message: '', error: waiting ? WAITING_RESET_MESSAGE : UNKNOWN_RESET_MESSAGE })
      return false
    }
    const outcome = results.find((result) => result.outcome !== 'SUCCESS')?.outcome ?? 'SUCCESS'
    if (outcome === 'SUCCESS') {
      publish({ ...state, step: 'idle', verifying: false, outcome, error: '',
        message: '대화에서 저장한 기억을 비활성화했습니다. 이후 새 응답 요청부터 반영됩니다.' })
      return true
    }
    if (outcome === 'FAILED') {
      preparedIntent = null
      publish({ ...state, step: background ? 'idle' : prepareIntent ? 'review' : 'confirm', verifying: false, outcome,
        operationId: createOperationId(), message: '',
        error: '기기에서 기억을 초기화하지 못했습니다. 기기 상태를 확인한 뒤 확인 문구를 다시 입력해 주세요.' })
      return false
    }
  }
  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    open(canReset) {
      if (!isCurrent()) return
      if (!canReset || state.step !== 'idle' || state.verifying) return
      if (['UNKNOWN', 'WAITING_FOR_DEVICE'].includes(state.outcome)) {
        publish({ ...state, step: state.outcome === 'UNKNOWN' ? 'unknown' : 'waiting' })
        return
      }
      preparedIntent = null
      publish({ step: 'review', outcome: null, acknowledgement: '',
        operationId: createOperationId(), error: '', message: '' })
    },
    async advance(canReset) {
      if (!isCurrent()) return false
      if (!canReset || state.step !== 'review') return false
      preparedIntent = null
      if (prepareIntent) {
        const preparing = { ...state, step: 'preparing', outcome: null, acknowledgement: '', error: '', message: '' }
        publish(preparing)
        try {
          const result = await prepareIntent()
          if (!isCurrent()) return false
          if (state !== preparing) return false
          if (result?.outcome !== 'PREPARED') throw new Error('Reset intent preparation unavailable')
          preparedIntent = copyIntent(result.intent)
        } catch {
          if (state !== preparing) return false
          publish({ ...state, step: 'review', outcome: null, acknowledgement: '',
            error: '초기화 상태를 확인하지 못했습니다. 연결과 권한을 확인한 뒤 다시 진행해 주세요.' })
          return false
        }
      }
      publish({ ...state, step: 'confirm', acknowledgement: '', error: '', message: '' })
      return true
    },
    acknowledge(value) {
      if (state.step !== 'confirm') return
      publish({ ...state, acknowledgement: value })
    },
    cancel() {
      if (state.step === 'pending' || state.verifying) return
      preparedIntent = null
      publish({ ...state, step: 'idle', acknowledgement: '',
        error: ['UNKNOWN', 'WAITING_FOR_DEVICE'].includes(state.outcome) ? state.error : '', message: '' })
    },
    async confirm(canReset) {
      if (!canReset || state.step !== 'confirm' || state.acknowledgement !== RESET_ACKNOWLEDGEMENT
          || (prepareIntent && !preparedIntent)) return false
      return dispatch(false)
    },
    async verify(canReset) {
      if (!canReset || !['unknown', 'waiting'].includes(state.step) || state.verifying) return false
      return dispatch(true)
    },
    async reconcile(canReset) {
      if (!canReset || state.step !== 'idle' || state.verifying || !['UNKNOWN', 'WAITING_FOR_DEVICE'].includes(state.outcome)) return false
      return dispatch(true, true)
    },
  }
}
