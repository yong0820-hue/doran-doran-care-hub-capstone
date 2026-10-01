import test from 'node:test'
import assert from 'node:assert/strict'
import { createMemoryResetFlow, runMemoryReset, RESET_ACKNOWLEDGEMENT } from '../src/memoryReset.js'

const fixture = (initialControl = { generation: 7 }) => {
  let control = initialControl
  const operations = new Map()
  const calls = []
  const ioFor = (operationId) => ({
    readOperation: async () => { calls.push(['operation', operationId]); return structuredClone(operations.get(operationId) ?? null) },
    readControl: async () => { calls.push(['control']); return structuredClone(control) },
    readLeaderOperation: async (identity) => { calls.push(['leader', identity]); return structuredClone(operations.get(identity) ?? null) },
    createOperation: async (request) => {
      calls.push(['create', operationId])
      if (!operations.has(operationId)) operations.set(operationId, structuredClone(request))
      return structuredClone(operations.get(operationId))
    },
    timestamp: () => 10000, now: () => 0, sleep: async () => {},
  })
  return { operations, calls, ioFor, setControl: (value) => { control = value } }
}
const reset = (store, operationId, options = {}) => runMemoryReset({
  io: store.ioFor(operationId), operationId, deviceId: 'device_001', maxWaitMs: 0, ...options,
})
const storageFor = (entries = new Map()) => ({
  get length() { return entries.size }, key: (index) => [...entries.keys()][index] ?? null,
  getItem: (key) => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value),
  removeItem: (key) => entries.delete(key),
})
const confirm = (flow) => { flow.open(true); flow.advance(true); flow.acknowledge(RESET_ACKNOWLEDGEMENT) }

test('M5 request attaches its observed generation and device-scoped pending index before any operation write', async () => {
  for (const [control, generation] of [[null, 'initial'], [{ generation: 'legacy-generation' }, 'legacy-generation'], [{ generation: 7 }, 7]]) {
    const store = fixture(control)
    assert.equal((await reset(store, 'request-a')).outcome, 'WAITING_FOR_DEVICE')
    assert.deepEqual(store.operations.get('request-a'), {
      status: 'requested', deviceId: 'device_001', deviceStatus: 'device_001|requested',
      requestedAt: 10000, requestedBy: 'guardian_dashboard',
      intent: { version: 1, generation, operationId: null, epoch: 0 },
    })
    assert.deepEqual(store.calls, [['operation', 'request-a'], ['control'], ['create', 'request-a']])
  }
})

test('M5 requests observed during an active coordinator bind its operation even after destructive generation CAS', async () => {
  for (const state of ['requested', 'processing', 'committed']) {
    const store = fixture({ generation: state === 'committed' ? 8 : 7,
      resetCoordinator: { state, deviceId: 'device_001', activeOperationId: 'leader-a', epoch: 12 } })
    await reset(store, `follower-${state}`)
    assert.deepEqual(store.operations.get(`follower-${state}`).intent, {
      version: 1, generation: state === 'committed' ? 8 : 7, operationId: 'leader-a', epoch: 12,
    })
  }
})

test('M5 only a fresh intent after completed coordinator permits a distinct reset; another device is never attached', async () => {
  for (const [state, deviceId] of [['completed', 'device_001'], ['processing', 'other-device']]) {
    const store = fixture({ generation: 8,
      resetCoordinator: { state, deviceId, activeOperationId: 'leader-a', epoch: 12 } })
    await reset(store, 'fresh-request')
    assert.deepEqual(store.operations.get('fresh-request').intent, {
      version: 1, generation: 8, operationId: null, epoch: 12,
    })
  }
})

test('M5 a delayed operation create keeps the original active coordinator binding instead of adopting a later generation', async () => {
  const store = fixture({ generation: 8,
    resetCoordinator: { state: 'committed', deviceId: 'device_001', activeOperationId: 'leader-a', epoch: 12 } })
  const io = store.ioFor('delayed-b')
  const create = io.createOperation
  io.createOperation = async (request) => {
    store.setControl({ generation: 9,
      resetCoordinator: { state: 'completed', deviceId: 'device_001', activeOperationId: 'new-leader', epoch: 13 } })
    return create(request)
  }
  await reset(store, 'delayed-b', { io })
  assert.deepEqual(store.operations.get('delayed-b').intent, { version: 1, generation: 8, operationId: 'leader-a', epoch: 12 })
  await reset(store, 'delayed-b')
  assert.equal(store.calls.filter(([kind]) => kind === 'control').length, 1, 'reconnect preserves the original immutable intent')
  assert.equal(store.calls.filter(([kind]) => kind === 'create').length, 1)
})

test('M5 failed control read creates no request, keeps UNKNOWN recovery marker, and verifyOnly never reads control', async () => {
  const store = fixture()
  const entries = new Map()
  const resetWithDeniedControl = ({ operationId, verifyOnly }) => {
    const io = store.ioFor(operationId)
    io.readControl = async () => { store.calls.push(['denied-control']); throw new Error('permission_denied') }
    return reset(store, operationId, { io, verifyOnly })
  }
  const flow = createMemoryResetFlow(resetWithDeniedControl, { scope: 'backend|elder', storage: storageFor(entries) })
  confirm(flow)
  assert.equal(await flow.confirm(true), false)
  assert.equal(flow.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(entries.size, 1)
  assert.equal(store.operations.size, 0)
  assert.equal(await flow.verify(true), false)
  assert.equal(entries.size, 1)
  assert.equal(store.calls.filter(([kind]) => kind === 'denied-control').length, 1)
  assert.equal(store.calls.filter(([kind]) => kind === 'create').length, 0)
})

test('M5 malformed control cannot silently create an unbound destructive intent', async () => {
  for (const control of [false, [], { generation: false }, { generation: -1 }, { generation: '' },
    { resetCoordinator: false }, { resetCoordinator: { epoch: -1 } },
    { resetCoordinator: { state: 'processing', deviceId: 'device_001', activeOperationId: 'bad/key', epoch: 1 } },
    { resetCoordinator: { state: 'committed', deviceId: 'device_001', activeOperationId: 'leader-a' } }]) {
    const store = fixture(control)
    assert.equal((await reset(store, 'request-a')).outcome, 'UNKNOWN')
    assert.equal(store.operations.size, 0)
    assert.equal(store.calls.some(([kind]) => kind === 'create'), false)
  }
})

test('M5 two interleaved tabs retain separate markers and reconcile coalesced authoritative receipts without another request', async () => {
  const store = fixture()
  const entries = new Map()
  const storageA = storageFor(entries)
  const options = { scope: 'backend|elder' }
  const invoke = ({ operationId, verifyOnly }) => reset(store, operationId, { verifyOnly })
  const a = createMemoryResetFlow(invoke, { ...options, storage: storageA })
  const b = createMemoryResetFlow(invoke, { ...options, storage: storageFor(entries) })
  confirm(a); confirm(b)
  let pendingB
  const writeA = storageA.setItem
  storageA.setItem = (key, value) => {
    // Both tabs enumerate shared storage before either sees the other's marker.
    pendingB = b.confirm(true)
    writeA(key, value)
  }
  await a.confirm(true)
  await pendingB
  const leaderId = a.getSnapshot().operationId
  const followerId = b.getSnapshot().operationId
  assert.notEqual(leaderId, followerId)
  assert.equal(entries.size, 2)
  assert.equal(store.operations.size, 2)
  assert.deepEqual(store.operations.get(leaderId).intent, store.operations.get(followerId).intent)
  store.operations.set(leaderId, { ...store.operations.get(leaderId), status: 'completed', generation: 8 })
  store.operations.set(followerId, { ...store.operations.get(followerId), status: 'completed', generation: 8, coalescedTo: leaderId })
  assert.equal(await b.verify(true), true)
  assert.equal(entries.size, 0)
  assert.equal(await a.verify(true), true)
  assert.equal(a.getSnapshot().outcome, 'SUCCESS')
  assert.equal(b.getSnapshot().outcome, 'SUCCESS')
  assert.equal(store.calls.filter(([kind]) => kind === 'create').length, 2)
  assert.equal(store.calls.filter(([kind]) => kind === 'control').length, 2)
})

test('M5 preparation captures active leader without allocating or reading a new operation', async () => {
  const store = fixture({ generation: 7,
    resetCoordinator: { state: 'processing', deviceId: 'device_001', activeOperationId: 'A', epoch: 1 } })
  store.operations.set('A', { status: 'processing', deviceId: 'device_001' })
  const prepared = await reset(store, undefined, { prepareOnly: true })
  assert.equal(prepared.outcome, 'PREPARED')
  assert.deepEqual(prepared.intent, { version: 1, generation: 7, operationId: 'A', epoch: 1 })
  assert.deepEqual(store.calls, [['control'], ['leader', 'A']])
  assert.equal(store.operations.size, 1)
})

test('M5 preparation after permanent SUCCESS permits fresh intent while final coordinator CAS is delayed', async () => {
  const store = fixture({ generation: 8,
    resetCoordinator: { state: 'committed', deviceId: 'device_001', activeOperationId: 'A', epoch: 1 } })
  store.operations.set('A', { status: 'completed', deviceId: 'device_001', resultingGeneration: 8 })
  const prepared = await reset(store, undefined, { prepareOnly: true })
  assert.equal(prepared.outcome, 'PREPARED')
  assert.deepEqual(prepared.intent, { version: 1, generation: 8, operationId: null, epoch: 1 })
  assert.deepEqual(store.calls, [['control'], ['leader', 'A']])
})

test('M5 final acknowledgement waits for preparation even if its first control read is delayed through A completion', async () => {
  const store = fixture()
  const entries = new Map()
  const io = store.ioFor(undefined)
  let releaseControl
  io.readControl = () => new Promise(resolve => { releaseControl = resolve })
  const flow = createMemoryResetFlow((options) => reset(store, options.operationId, options), {
    scope: 'backend|elder', storage: storageFor(entries),
    prepareIntent: () => reset(store, undefined, { prepareOnly: true, io }),
  })
  flow.open(true)
  const preparation = flow.advance(true)
  assert.equal(flow.getSnapshot().step, 'preparing')
  flow.acknowledge(RESET_ACKNOWLEDGEMENT)
  assert.equal(await flow.confirm(true), false)
  assert.equal(entries.size, 0)
  assert.equal(store.operations.size, 0)
  if (releaseControl) releaseControl({ generation: 8,
    resetCoordinator: { state: 'completed', deviceId: 'device_001', activeOperationId: 'A', epoch: 1 } })
  await preparation
  assert.equal(flow.getSnapshot().step, 'confirm')
  assert.equal(flow.getSnapshot().acknowledgement, '')
  assert.equal(await flow.confirm(true), false)
  flow.acknowledge(RESET_ACKNOWLEDGEMENT)
  await flow.confirm(true)
  assert.deepEqual(store.operations.get(flow.getSnapshot().operationId).intent,
    { version: 1, generation: 8, operationId: null, epoch: 1 })
})

test('M5 acknowledged prepared intent survives an initial operation read delayed through leader completion', async () => {
  const store = fixture({ generation: 7,
    resetCoordinator: { state: 'processing', deviceId: 'device_001', activeOperationId: 'A', epoch: 1 } })
  store.operations.set('A', { status: 'processing', deviceId: 'device_001' })
  const entries = new Map()
  let releaseRead
  const flow = createMemoryResetFlow((options) => {
    const io = store.ioFor(options.operationId)
    io.readOperation = () => new Promise(resolve => { releaseRead = resolve })
    return reset(store, options.operationId, { ...options, io })
  }, { scope: 'backend|elder', storage: storageFor(entries),
    prepareIntent: () => reset(store, undefined, { prepareOnly: true }),
  })
  flow.open(true)
  await flow.advance(true)
  flow.acknowledge(RESET_ACKNOWLEDGEMENT)
  const pending = flow.confirm(true)
  assert.equal(entries.size, 1)
  store.setControl({ generation: 8,
    resetCoordinator: { state: 'completed', deviceId: 'device_001', activeOperationId: 'A', epoch: 1 } })
  store.operations.set('A', { status: 'completed', deviceId: 'device_001', resultingGeneration: 8 })
  releaseRead(null)
  await pending
  assert.deepEqual(store.operations.get(flow.getSnapshot().operationId).intent,
    { version: 1, generation: 7, operationId: 'A', epoch: 1 })
  assert.equal(store.calls.filter(([kind]) => kind === 'control').length, 1)
})

test('M5 canceled preparation never reopens the dialog or reuses its late intent', async () => {
  let release
  let creates = 0
  const entries = new Map()
  const flow = createMemoryResetFlow(async () => { creates++; return { outcome: 'SUCCESS' } }, {
    scope: 'backend|elder', storage: storageFor(entries),
    prepareIntent: () => new Promise(resolve => { release = resolve }),
  })
  flow.open(true)
  const preparation = flow.advance(true)
  assert.equal(flow.getSnapshot().step, 'preparing')
  flow.cancel()
  flow.open(true)
  if (release) release({ outcome: 'PREPARED', intent: { version: 1, generation: 7, operationId: null, epoch: 0 } })
  await preparation
  assert.equal(flow.getSnapshot().step, 'review')
  flow.acknowledge(RESET_ACKNOWLEDGEMENT)
  assert.equal(await flow.confirm(true), false)
  assert.equal(creates, 0)
  assert.equal(entries.size, 0)
})

test('M5 preparation errors stay read-only and return to review for a fresh attempt', async () => {
  const store = fixture({ generation: 7,
    resetCoordinator: { state: 'processing', deviceId: 'device_001', activeOperationId: 'A', epoch: 1 } })
  const entries = new Map()
  const io = store.ioFor(undefined)
  io.readLeaderOperation = async () => { throw new Error('permission_denied') }
  const flow = createMemoryResetFlow((options) => reset(store, options.operationId, options), {
    scope: 'backend|elder', storage: storageFor(entries),
    prepareIntent: () => reset(store, undefined, { prepareOnly: true, io }),
  })
  flow.open(true)
  assert.equal(await flow.advance(true), false)
  assert.equal(flow.getSnapshot().step, 'review')
  assert.ok(flow.getSnapshot().error)
  assert.equal(entries.size, 0)
  assert.equal(store.operations.size, 0)
  assert.equal(store.calls.some(([kind]) => ['operation', 'create'].includes(kind)), false)
})

test('M5 definitive failure discards prepared intent and requires preparation plus fresh acknowledgement', async () => {
  const entries = new Map()
  let preparations = 0
  let creates = 0
  const flow = createMemoryResetFlow(async () => { creates++; return { outcome: 'FAILED' } }, {
    scope: 'backend|elder', storage: storageFor(entries),
    prepareIntent: async () => { preparations++; return { outcome: 'PREPARED', intent: { version: 1, generation: 7, operationId: null, epoch: 0 } } },
  })
  flow.open(true)
  await flow.advance(true)
  flow.acknowledge(RESET_ACKNOWLEDGEMENT)
  await flow.confirm(true)
  assert.equal(flow.getSnapshot().step, 'review')
  flow.acknowledge(RESET_ACKNOWLEDGEMENT)
  assert.equal(await flow.confirm(true), false)
  assert.equal(preparations, 1)
  assert.equal(creates, 1)
  await flow.advance(true)
  assert.equal(preparations, 2)
  assert.equal(await flow.confirm(true), false)
  assert.equal(entries.size, 0)
})
