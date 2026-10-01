import test from 'node:test'
import assert from 'node:assert/strict'
import { createMemoryResetFlow, runMemoryReset } from '../src/memoryReset.js'

const operationStore = () => {
  let operation = null
  let elapsed = 0
  const calls = []
  const io = {
    readOperation: async () => { calls.push('read'); return structuredClone(operation) },
    readControl: async () => { calls.push('control'); return { generation: 7 } },
    createOperation: async (request) => {
      calls.push('create')
      operation ??= structuredClone(request)
      return structuredClone(operation)
    },
    timestamp: () => 10000,
    now: () => elapsed,
    sleep: async (ms) => { elapsed += ms },
  }
  return { io, calls, get operation() { return operation }, complete(status = 'completed') {
    operation = { ...operation, status, generation: 8, completedAt: 20000 }
  } }
}
const reset = (fixture, options = {}) => runMemoryReset({
  io: fixture.io, operationId: 'reset-a', deviceId: 'device_001', maxWaitMs: 100, ...options,
})

test('regression H1 C: offline Pi leaves requested operation waiting and Guardian cannot reset cloud memories', async () => {
  const fixture = operationStore()
  const result = await reset(fixture)
  assert.equal(result.outcome, 'WAITING_FOR_DEVICE')
  assert.deepEqual(fixture.operation, {
    status: 'requested', deviceId: 'device_001', deviceStatus: 'device_001|requested',
    intent: { version: 1, generation: 7, operationId: null, epoch: 0 },
    requestedAt: 10000, requestedBy: 'guardian_dashboard',
  })
  assert.equal(fixture.calls.filter((call) => call === 'create').length, 1)
  assert.ok(fixture.calls.every((call) => ['create', 'read', 'control'].includes(call)))
})

test('regression H1: only Pi completed operation authorizes success and repeated verification is read-only', async () => {
  const fixture = operationStore()
  await reset(fixture)
  fixture.complete()
  assert.equal((await reset(fixture, { verifyOnly: true })).outcome, 'SUCCESS')
  assert.equal((await reset(fixture, { verifyOnly: true })).generation, 8)
  assert.equal(fixture.calls.filter((call) => call === 'create').length, 1)
})

test('regression M5: permission errors preserve UNKNOWN marker, block new request, and same-operation recovery clears it', async () => {
  const fixture = operationStore()
  const entries = new Map([['doran.memoryReset.pending.v2:backend%7Celder.reset-a', JSON.stringify({ version: 2, operationId: 'reset-a' })]])
  const storage = {
    get length() { return entries.size }, key: (i) => [...entries.keys()][i] ?? null,
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value), removeItem: (key) => entries.delete(key),
  }
  fixture.complete()
  const read = fixture.io.readOperation
  fixture.io.readOperation = async () => { throw new Error('permission_denied') }
  // Also expose the old adapter failure to establish the original regression.
  fixture.io.readControl = fixture.io.readOperation
  const flow = createMemoryResetFlow(({ operationId, verifyOnly }) => reset(fixture, { operationId, verifyOnly }), {
    scope: 'backend|elder', storage,
  })
  flow.open(true)
  assert.equal(await flow.verify(true), false)
  assert.equal(flow.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(entries.size, 1)
  flow.cancel(); flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), false)
  assert.equal(fixture.calls.includes('create'), false)
  fixture.io.readOperation = read
  assert.equal(await flow.verify(true), true)
  assert.equal(entries.size, 0)
})

test('regression M5: definitive failed operation clears recovery but unreadable/absent operation never does', async () => {
  const fixture = operationStore()
  assert.equal((await reset(fixture, { verifyOnly: true })).outcome, 'UNKNOWN')
  assert.equal(fixture.calls.includes('create'), false)
  fixture.complete('failed')
  assert.equal((await reset(fixture, { verifyOnly: true })).outcome, 'FAILED')
})

test('regression H1: polling request is bounded and requested/processing never implies completion', async () => {
  const fixture = operationStore()
  fixture.complete('processing')
  const result = await reset(fixture, { verifyOnly: true })
  assert.equal(result.outcome, 'WAITING_FOR_DEVICE')
  assert.equal(result.operationStatus, 'processing')
  assert.ok(fixture.io.now() <= 100)
})

test('definitive Pi failure clears the marker and a later request requires fresh confirmation', async () => {
  const entries = new Map()
  const storage = {
    get length() { return entries.size }, key: (i) => [...entries.keys()][i] ?? null,
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value), removeItem: (key) => entries.delete(key),
  }
  const fixture = operationStore()
  let dispatched = 0
  const flow = createMemoryResetFlow((options) => { dispatched++; return reset(fixture, options) }, { scope: 'backend|elder', storage })
  flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화')
  await flow.confirm(true)
  const original = flow.getSnapshot().operationId
  assert.equal(entries.size, 1)
  fixture.complete('failed')
  assert.equal(await flow.verify(true), false)
  assert.equal(flow.getSnapshot().outcome, 'FAILED')
  assert.equal(entries.size, 0)
  assert.notEqual(flow.getSnapshot().operationId, original)
  assert.equal(await flow.confirm(true), false)
  assert.equal(dispatched, 2)
})

test('background reload reconciliation preserves unreadable marker and never issues a request', async () => {
  const entries = new Map([['doran.memoryReset.pending.v2:backend%7Celder.reset-a', JSON.stringify({ version: 2, operationId: 'reset-a' })]])
  const storage = {
    get length() { return entries.size }, key: (i) => [...entries.keys()][i] ?? null,
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value), removeItem: (key) => entries.delete(key),
  }
  const fixture = operationStore()
  fixture.io.readOperation = async () => { throw new Error('permission_denied') }
  const flow = createMemoryResetFlow((options) => reset(fixture, options), { scope: 'backend|elder', storage })
  assert.equal(await flow.reconcile(true), false)
  assert.equal(flow.getSnapshot().step, 'idle')
  assert.equal(flow.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(entries.size, 1)
  assert.equal(fixture.calls.includes('create'), false)
})

test('a tab opened before another tab becomes UNKNOWN must discover its marker before issuing a new request', async () => {
  const entries = new Map()
  const storage = {
    get length() { return entries.size }, key: (i) => [...entries.keys()][i] ?? null,
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value), removeItem: (key) => entries.delete(key),
  }
  let writes = 0
  const requests = []
  const options = { scope: 'backend|elder', storage }
  const reset = async (request) => {
    requests.push(request)
    if (!request.verifyOnly) writes++
    return { outcome: 'UNKNOWN' }
  }
  const a = createMemoryResetFlow(reset, options)
  const b = createMemoryResetFlow(reset, options)
  for (const flow of [a, b]) { flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화') }
  await a.confirm(true)
  await b.confirm(true)
  assert.equal(writes, 1)
  assert.equal(entries.size, 1)
  assert.equal(b.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(b.getSnapshot().operationId, a.getSnapshot().operationId)
  await b.verify(true)
  assert.equal(requests.at(-1).verifyOnly, true)
  assert.equal(requests.at(-1).operationId, a.getSnapshot().operationId)
})
