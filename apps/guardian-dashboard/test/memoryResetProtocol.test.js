import test from 'node:test'
import assert from 'node:assert/strict'
import * as memory from '../src/memoryReset.js'

const operationFixture = (initial = null) => {
  let operation = initial
  let now = 0
  const calls = []
  const io = {
    readOperation: async () => { calls.push('read'); return structuredClone(operation) },
    readControl: async () => { calls.push('control'); return { generation: 7 } },
    createOperation: async (request) => { calls.push('create'); operation ??= request; return structuredClone(operation) },
    timestamp: () => 10000, now: () => now, sleep: async (ms) => { now += ms },
  }
  return { io, calls, setOperation: (value) => { operation = value } }
}
const reset = (fixture, options = {}) => memory.runMemoryReset({
  io: fixture.io, operationId: 'reset-a', deviceId: 'device_001', maxWaitMs: 100, ...options,
})

test('completed receipt is authoritative even after a later reset and verification never recreates it', async () => {
  const fixture = operationFixture({ status: 'completed', generation: 4 })
  assert.deepEqual(await reset(fixture, { verifyOnly: true }), {
    outcome: 'SUCCESS', operationId: 'reset-a', generation: 4, operationStatus: 'completed',
  })
  assert.deepEqual(fixture.calls, ['read'])
})

test('legacy generation and lease expiration cannot authorize Guardian success', async () => {
  for (const operation of [null, {}, { generation: 100, lastResetOperationId: 'reset-a' },
    { status: 'pending' }, { status: 'coalesced' }, { status: 'requested', expiresAt: 0 }]) {
    const fixture = operationFixture(operation)
    const result = await reset(fixture, { verifyOnly: true })
    assert.notEqual(result.outcome, 'SUCCESS')
    assert.equal(fixture.calls.includes('create'), false)
  }
})

test('ACK loss reconciles completed operation without retrying its creation', async () => {
  const fixture = operationFixture()
  fixture.io.createOperation = async () => {
    fixture.calls.push('create')
    fixture.setOperation({ status: 'completed', resultingGeneration: 8 })
    throw new Error('ACK lost')
  }
  assert.equal((await reset(fixture)).outcome, 'SUCCESS')
  assert.equal(fixture.calls.filter((call) => call === 'create').length, 1)
})

test('every verification read failure keeps UNKNOWN, including permission/network/timeout/unavailable', async () => {
  for (const error of ['permission_denied', 'network_error', 'timeout', 'unavailable']) {
    const fixture = operationFixture({ status: 'completed', generation: 1 })
    fixture.io.readOperation = async () => { throw new Error(error) }
    assert.equal((await reset(fixture, { verifyOnly: true })).outcome, 'UNKNOWN', error)
    assert.equal(fixture.calls.includes('create'), false)
  }
})

test('processing operation can complete during bounded observation and no second operation is created', async () => {
  const fixture = operationFixture({ status: 'processing' })
  fixture.io.sleep = async () => fixture.setOperation({ status: 'completed', generation: 2 })
  assert.equal((await reset(fixture)).outcome, 'SUCCESS')
  assert.equal(fixture.calls.includes('create'), false)
})

test('malformed operation and invalid keys cannot mutate a different Firebase path', async () => {
  for (const operation of [false, [], 'text', { status: 'mystery' }]) {
    const fixture = operationFixture(operation)
    assert.equal((await reset(fixture)).outcome, 'UNKNOWN')
    assert.equal(fixture.calls.includes('create'), false)
  }
  for (const key of ['', '../bad', 'bad/key', 'bad.key', 'x'.repeat(81)]) {
    await assert.rejects(reset(operationFixture(), { operationId: key }), /operation ID/)
  }
})
test('M5 deterministic two-tab marker interleaving retains UNKNOWN tab and reload reconciles every operation', async () => {
  const entries = new Map()
  const storage = {
    get length() { return entries.size }, key: (i) => [...entries.keys()][i] ?? null,
    getItem: (key) => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value), removeItem: (key) => entries.delete(key),
  }
  const options = { scope: 'backend|elder', storage }
  let completeA
  const a = memory.createMemoryResetFlow(() => new Promise((resolve) => { completeA = resolve }), options)
  a.open(true); a.advance(true); a.acknowledge(memory.RESET_ACKNOWLEDGEMENT)
  const pendingA = a.confirm(true)
  // Simulate a truly concurrent tab that persisted before observing tab A's
  // marker. Each identity must survive the other tab's successful completion.
  const secondId = 'independent-tab-b'
  const prefix = [...entries.keys()][0].slice(0, [...entries.keys()][0].lastIndexOf('.') + 1)
  entries.set(`${prefix}${secondId}`, JSON.stringify({ version: 2, operationId: secondId }))
  assert.equal(entries.size, 2, 'each operation must have its own storage key')
  completeA({ outcome: 'SUCCESS' })
  await pendingA
  assert.equal(entries.size, 1)
  assert.equal(JSON.parse([...entries.values()][0]).operationId, secondId)
  const extraId = 'independent-unknown'
  entries.set(`${prefix}${extraId}`, JSON.stringify({ version: 2, operationId: extraId }))
  const verified = []
  const reloaded = memory.createMemoryResetFlow(async ({ operationId, verifyOnly }) => {
    assert.equal(verifyOnly, true); verified.push(operationId)
    return { outcome: operationId === extraId ? 'UNKNOWN' : 'SUCCESS' }
  }, options)
  reloaded.open(true)
  assert.equal(await reloaded.verify(true), false)
  assert.equal(verified.length, 2)
  assert.equal(entries.size, 1)
  assert.equal(JSON.parse([...entries.values()][0]).operationId, extraId)
})
