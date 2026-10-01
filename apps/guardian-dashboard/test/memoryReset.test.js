import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transformWithEsbuild } from 'vite'
import * as memoryReset from '../src/memoryReset.js'

const memoryStorage = () => {
  const values = new Map()
  return {
    values,
    get length() { return values.size },
    key: (index) => [...values.keys()][index] ?? null,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  }
}
const createMemoryResetFlow = (reset, options = {}) => memoryReset.createMemoryResetFlow(reset, {
  storage: memoryStorage(), ...options, scope: options.scope ?? 'https://test.firebaseio.com|elder_001',
})
const preparedResult = () => ({ outcome: 'PREPARED', intent: { version: 1, generation: 7, operationId: null, epoch: 0 } })




test('confirmation requires both stages, cancellation never writes, and offline mode cannot reset', async () => {
  let calls = 0
  const flow = createMemoryResetFlow(async () => { calls++; return { outcome: 'SUCCESS' } })
  assert.equal(await flow.confirm(true), false)
  flow.open(false)
  assert.equal(flow.getSnapshot().step, 'idle')
  flow.open(true)
  assert.equal(flow.getSnapshot().step, 'review')
  assert.equal(await flow.confirm(true), false)
  flow.cancel()
  flow.open(true)
  flow.advance(true)
  flow.cancel()
  assert.equal(calls, 0)
  flow.open(true)
  flow.advance(false)
  assert.equal(flow.getSnapshot().step, 'review')
  flow.advance(true)
  assert.equal(await flow.confirm(false), false)
  assert.equal(calls, 0)
  flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), true)
  assert.equal(calls, 1)
  assert.equal(flow.getSnapshot().step, 'idle')
  assert.match(flow.getSnapshot().message, /비활성화/)
})

test('pending reset disables cancellation and duplicate dispatch; success waits for the write', async () => {
  let complete
  let calls = 0
  const flow = createMemoryResetFlow(() => { calls++; return new Promise((resolve) => { complete = resolve }) })
  const observed = []
  const unsubscribe = flow.subscribe(() => observed.push(flow.getSnapshot().step))
  flow.open(true)
  flow.advance(true)
  flow.acknowledge('기억 초기화')
  const pending = flow.confirm(true)
  assert.equal(flow.getSnapshot().step, 'pending')
  assert.equal(flow.getSnapshot().message, '')
  flow.cancel()
  flow.open(true)
  assert.equal(await flow.confirm(true), false)
  assert.equal(flow.getSnapshot().step, 'pending')
  assert.equal(calls, 1)
  complete({ outcome: 'SUCCESS' })
  assert.equal(await pending, true)
  assert.deepEqual(observed, ['review', 'confirm', 'confirm', 'pending', 'idle'])
  unsubscribe()
})

test('definitive failed operation requires a fresh acknowledgement and a new ID for explicit retry', async () => {
  let calls = 0
  const operationIds = []
  const flow = createMemoryResetFlow(async ({ operationId }) => {
    operationIds.push(operationId)
    return { outcome: ++calls === 1 ? 'FAILED' : 'SUCCESS' }
  })
  flow.open(true)
  flow.advance(true)
  flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), false)
  assert.equal(flow.getSnapshot().step, 'confirm')
  assert.match(flow.getSnapshot().error, /초기화하지 못했습니다/)
  assert.equal(flow.getSnapshot().message, '')
  assert.equal(flow.getSnapshot().outcome, 'FAILED')
  assert.equal(await flow.confirm(true), false)
  flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), true)
  assert.equal(calls, 2)
  assert.notEqual(operationIds[0], operationIds[1])
})

const flatten = (node) => node == null || typeof node === 'boolean' ? []
  : Array.isArray(node) ? node.flatMap(flatten) : [node, ...flatten(node?.props?.children)]
const textOf = (node) => flatten(node).filter((entry) => typeof entry === 'string').join(' ')
const button = (tree, label) => flatten(tree).find((node) => node?.type === 'button' && textOf(node) === label)

async function resetComponentHarness(initialProps, recoveryOptions = {}) {
  const values = []
  let cursor = 0
  let effects = []
  let props = initialProps
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in values)) values[index] = typeof initial === 'function' ? initial() : initial
      return [values[index], (next) => { values[index] = next }]
    },
    useRef(initial) {
      return hooks.useState(() => ({ current: initial }))[0]
    },
    useSyncExternalStore(_subscribe, getSnapshot) { return getSnapshot() },
    useEffect(effect, dependencies) {
      const index = cursor++
      if (!(index in values) || dependencies.some((value, i) => value !== values[index][i])) {
        values[index] = dependencies
        effects.push(effect)
      }
    },
  }
  const filename = new URL('../src/components/ResetSettings.jsx', import.meta.url)
  const transformed = await transformWithEsbuild(await readFile(filename, 'utf8'), filename.pathname, {
    format: 'cjs', jsx: 'automatic',
  })
  const require = createRequire(import.meta.url)
  const dependencies = { react: hooks, '../memoryReset': {
    ...memoryReset,
    createMemoryResetFlow: (reset, options) => createMemoryResetFlow(reset, { ...options, ...recoveryOptions }),
  } }
  const module = { exports: {} }
  vm.runInNewContext(transformed.code, {
    module, exports: module.exports,
    require: (specifier) => dependencies[specifier] ?? require(specifier),
  })
  return {
    render(nextProps = {}) {
      props = { ...props, ...nextProps }
      cursor = 0
      const tree = module.exports.default(props)
      for (const node of flatten(tree)) {
        if (!node?.ref || node.ref.current) continue
        node.ref.current = {
          open: false, focused: false,
          showModal() { this.open = true }, close() { this.open = false },
          focus() { this.focused = true },
        }
      }
      const pending = effects
      effects = []
      pending.forEach((effect) => effect())
      return tree
    },
  }
}

test('rendered reset dialog requires both confirmations, locks pending actions and returns focus', async () => {
  let calls = 0
  let finish
  const harness = await resetComponentHarness({
    canReset: true, onMemoryReset: ({ prepareOnly }) => {
      if (prepareOnly) return preparedResult()
      calls++; return new Promise((resolve) => { finish = resolve })
    },
  })
  let tree = harness.render()
  assert.equal(button(tree, '기기 초기화 · 준비 중').props.disabled, true)
  assert.equal(button(tree, '기기 초기화 · 준비 중').props.onClick, undefined)
  button(tree, '기억 초기화').props.onClick()
  tree = harness.render()
  const modal = flatten(tree).find((node) => node?.type === 'dialog')
  assert.equal(modal.ref.current.open, true)
  assert.equal(button(tree, '취소').ref.current.focused, true)
  assert.equal(calls, 0)
  await button(tree, '내용 확인 · 다음').props.onClick()
  tree = harness.render()
  assert.equal(calls, 0)
  flatten(tree).find((node) => node?.type === 'input').props.onChange({ target: { value: '기억 초기화' } })
  tree = harness.render()
  const writing = button(tree, '다시 확인 · 기억 초기화').props.onClick()
  tree = harness.render()
  assert.equal(calls, 1)
  assert.equal(button(tree, '초기화 중…').props.disabled, true)
  assert.equal(button(tree, '취소').props.disabled, true)
  let prevented = false
  modal.props.onCancel({ preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  assert.equal(modal.ref.current.open, true)
  finish({ outcome: 'SUCCESS' })
  await writing
  tree = harness.render()
  assert.equal(modal.ref.current.open, false)
  assert.equal(button(tree, '기억 초기화').ref.current.focused, true)
  assert.match(textOf(tree), /비활성화했습니다/)
})

test('rendered reset cancellation and connection loss never call the writer', async () => {
  let calls = 0
  const harness = await resetComponentHarness({ canReset: false, onMemoryReset: async ({ prepareOnly }) => {
    if (prepareOnly) return preparedResult()
    calls++
  } })
  let tree = harness.render()
  assert.equal(button(tree, '기억 초기화').props.disabled, true)
  button(tree, '기억 초기화').props.onClick()
  tree = harness.render({ canReset: true })
  button(tree, '기억 초기화').props.onClick()
  tree = harness.render()
  button(tree, '취소').props.onClick()
  tree = harness.render()
  assert.equal(flatten(tree).find((node) => node?.type === 'dialog').ref.current.open, false)
  button(tree, '기억 초기화').props.onClick()
  tree = harness.render()
  await button(tree, '내용 확인 · 다음').props.onClick()
  tree = harness.render({ canReset: false })
  assert.equal(button(tree, '다시 확인 · 기억 초기화').props.disabled, true)
  await button(tree, '다시 확인 · 기억 초기화').props.onClick()
  assert.equal(calls, 0)
  assert.match(textOf(tree), /연결이 끊겼습니다/)
})

async function loadFirebaseService(database, configured = true, envOverrides = {}, globals = {}) {
  const filename = new URL('../src/services/firebase.js', import.meta.url)
  const source = await readFile(filename, 'utf8')
  const env = configured ? {
    VITE_FIREBASE_API_KEY: 'test', VITE_FIREBASE_AUTH_DOMAIN: 'test',
    VITE_FIREBASE_DATABASE_URL: 'test', VITE_FIREBASE_PROJECT_ID: 'test',
    ...envOverrides,
  } : {}
  const transformed = await transformWithEsbuild(source, filename.pathname, {
    format: 'cjs', define: { 'import.meta.env': JSON.stringify(env) },
  })
  const require = createRequire(import.meta.url)
  const dependencies = {
    'firebase/app': { getApps: () => [{}] },
    'firebase/database': database,
    '../memoryReset': memoryReset,
    '../medicationSchedule': await import('../src/medicationSchedule.js'),
    '../dashboardState': await import('../src/dashboardState.js'),
  }
  const module = { exports: {} }
  vm.runInNewContext(transformed.code, {
    module, exports: module.exports, Date, performance, setTimeout, clearTimeout, console, URL,
    require: (specifier) => dependencies[specifier] ?? require(specifier),
    ...globals,
  }, { filename: filename.pathname })
  return module.exports
}

const firebaseMemoryFixture = () => {
  const bucket = {
    _control: { generation: 7, activeIds: { old: true } },
    active: { old: { id: 'old', source: 'conversation', status: 'active', content: '영수와 점심' } },
    archive: {}, legacy: { content: 'x'.repeat(2 * 1024 * 1024) },
  }
  const root = { memories: { elder_001: bucket }, events: { emergency: 'keep' }, devices: { keep: true } }
  const operations = {}
  const calls = []
  const operationId = (path) => {
    assert.match(path, /^memoryResetOperations\/elder_001\/[^/]+$/)
    return path.split('/').at(-1)
  }
  const database = {
    getDatabase: () => 'test-db', ref: (_db, path = '') => path,
    push: () => ({ key: 'generated-operation' }), serverTimestamp: () => 10000,
    get: async (path) => {
      calls.push(['get', path])
      if (path === 'memories/elder_001/_control') return { val: () => structuredClone(bucket._control) }
      return { val: () => structuredClone(operations[operationId(path)] ?? null) }
    },
    runTransaction: async (path, mutate, options) => {
      assert.equal(options.applyLocally, false)
      calls.push(['transaction', path])
      // The SDK reconciles a cold-cache candidate against the server hash.
      mutate(null)
      const id = operationId(path)
      const next = mutate(structuredClone(operations[id] ?? null))
      if (next !== undefined) operations[id] = structuredClone(next)
      return { committed: next !== undefined, snapshot: { val: () => structuredClone(operations[id] ?? null) } }
    },
    update: async () => { assert.fail('Guardian may not update any memory body') },
  }
  return { root, bucket, operations, calls, database }
}
const request = { deviceId: 'device_001', maxWaitMs: 0 }

test('Firebase adapter only creates a requested operation; offline Pi cannot change memory or imply success', async () => {
  const fixture = firebaseMemoryFixture()
  const original = structuredClone(fixture.root)
  const service = await loadFirebaseService(fixture.database)
  const result = await service.resetConversationMemories('elder_001', request)
  assert.equal(result.outcome, 'WAITING_FOR_DEVICE')
  assert.equal(result.operationId, 'generated-operation')
  assert.deepEqual(fixture.operations['generated-operation'], {
    status: 'requested', deviceId: 'device_001', deviceStatus: 'device_001|requested',
    intent: { version: 1, generation: 7, operationId: null, epoch: 0 },
    requestedAt: 10000, requestedBy: 'guardian_dashboard',
  })
  assert.deepEqual(fixture.root, original)
  assert.deepEqual(fixture.calls, [
    ['get', 'memoryResetOperations/elder_001/generated-operation'],
    ['get', 'memories/elder_001/_control'],
    ['transaction', 'memoryResetOperations/elder_001/generated-operation'],
  ])
})

test('Firebase adapter validates target and keeps denied/network writes UNKNOWN until an operation proves the result', async () => {
  const offline = await loadFirebaseService({}, false)
  await assert.rejects(offline.resetConversationMemories('elder_001', request), /Firebase/)
  const invalid = await loadFirebaseService({})
  await assert.rejects(invalid.resetConversationMemories('../devices', request), /elder ID/)
  await assert.rejects(invalid.resetConversationMemories('elder_001'), /device ID/)
  for (const message of ['permission_denied', 'network']) {
    const fixture = firebaseMemoryFixture()
    fixture.database.runTransaction = async () => { throw new Error(message) }
    const service = await loadFirebaseService(fixture.database)
    assert.equal((await service.resetConversationMemories('elder_001', request)).outcome, 'UNKNOWN')
    assert.equal(fixture.bucket._control.generation, 7)
  }
  for (const bad of ['', '../other', 'bad/key', 'bad.key', '\u0000', 123]) assert.equal(memoryReset.isFirebaseKey(bad), false)
})

test('M5 Firebase adapter reads bounded control only when creating intent and denied control preserves recovery', async () => {
  const fixture = firebaseMemoryFixture()
  const get = fixture.database.get
  fixture.database.get = async (path) => {
    if (path === 'memories/elder_001/_control') {
      fixture.calls.push(['denied-control', path])
      throw new Error('permission_denied')
    }
    return get(path)
  }
  const storage = memoryStorage()
  const service = await loadFirebaseService(fixture.database)
  const flow = createMemoryResetFlow((operation) => service.resetConversationMemories('elder_001', { ...request, ...operation }), { storage })
  flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), false)
  assert.equal(flow.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(storage.values.size, 1)
  assert.equal(await flow.verify(true), false)
  assert.equal(fixture.calls.filter(([kind]) => kind === 'denied-control').length, 1)
  assert.equal(fixture.calls.filter(([kind]) => kind === 'transaction').length, 0)
  assert.deepEqual(fixture.operations, {})
  assert.equal(storage.values.size, 1)
})

test('M5 Firebase preparation is read-only and observes permanent completion before final coordinator CAS', async () => {
  const fixture = firebaseMemoryFixture()
  fixture.bucket._control.resetCoordinator = { state: 'committed', deviceId: 'device_001', activeOperationId: 'leader-a', epoch: 1 }
  fixture.operations['leader-a'] = { status: 'completed', deviceId: 'device_001', resultingGeneration: 8 }
  fixture.database.push = () => assert.fail('preparation must not allocate a new operation')
  const service = await loadFirebaseService(fixture.database)
  const prepared = await service.resetConversationMemories('elder_001', { ...request, prepareOnly: true })
  assert.equal(prepared.outcome, 'PREPARED')
  assert.deepEqual(prepared.intent, { version: 1, generation: 8, operationId: null, epoch: 1 })
  assert.deepEqual(fixture.calls, [['get', 'memories/elder_001/_control'], ['get', 'memoryResetOperations/elder_001/leader-a']])
})

test('rendered preparation blocks final acknowledgement and remains cancellable with no recovery marker', async () => {
  const storage = memoryStorage()
  let finishPreparation
  let creates = 0
  const harness = await resetComponentHarness({ canReset: true,
    onMemoryReset: ({ prepareOnly }) => {
      if (prepareOnly) return new Promise(resolve => { finishPreparation = resolve })
      creates++; return { outcome: 'SUCCESS' }
    },
  }, { storage })
  let tree = harness.render()
  button(tree, '기억 초기화').props.onClick(); tree = harness.render()
  const pending = button(tree, '내용 확인 · 다음').props.onClick(); tree = harness.render()
  assert.match(textOf(tree), /초기화 상태를 확인하고 있습니다/)
  assert.equal(button(tree, '내용 확인 · 다음').props.disabled, true)
  assert.equal(button(tree, '다시 확인 · 기억 초기화'), undefined)
  assert.equal(flatten(tree).some(node => node?.type === 'input'), false)
  assert.equal(Boolean(button(tree, '취소').props.disabled), false)
  assert.equal(storage.values.size, 0)
  button(tree, '취소').props.onClick(); tree = harness.render()
  finishPreparation(preparedResult())
  await pending
  tree = harness.render()
  assert.equal(flatten(tree).find(node => node?.type === 'dialog').ref.current.open, false)
  assert.equal(creates, 0)
  assert.equal(storage.values.size, 0)
})

test('M5 operation creation ACK loss reconciles Pi completion without any Guardian memory mutation', async () => {
  const fixture = firebaseMemoryFixture()
  const transaction = fixture.database.runTransaction
  fixture.database.runTransaction = async (...args) => {
    await transaction(...args)
    const id = args[0].split('/').at(-1)
    fixture.operations[id] = { ...fixture.operations[id], status: 'completed', generation: 8 }
    throw new Error('ACK lost')
  }
  const service = await loadFirebaseService(fixture.database)
  const flow = createMemoryResetFlow((operation) => service.resetConversationMemories('elder_001', { ...request, ...operation }))
  flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), true)
  assert.equal(flow.getSnapshot().outcome, 'SUCCESS')
  assert.equal(fixture.calls.filter(([kind]) => kind === 'transaction').length, 1)
  assert.equal(fixture.bucket._control.generation, 7, 'only a real Pi worker may mutate memories')
})

test('M5 verification permission_denied remains UNKNOWN and restored permission verifies the same completed operation', async () => {
  const fixture = firebaseMemoryFixture()
  const storage = memoryStorage()
  const service = await loadFirebaseService(fixture.database)
  const flow = createMemoryResetFlow((operation) => service.resetConversationMemories('elder_001', { ...request, ...operation }), { storage })
  flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화')
  await flow.confirm(true)
  const id = flow.getSnapshot().operationId
  fixture.operations[id] = { ...fixture.operations[id], status: 'completed', generation: 8 }
  const get = fixture.database.get
  fixture.database.get = async () => { throw new Error('permission_denied') }
  await flow.verify(true)
  assert.equal(flow.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(storage.values.size, 1)
  flow.cancel(); flow.open(true); flow.advance(true); flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), false)
  fixture.database.get = get
  assert.equal(await flow.verify(true), true)
  assert.equal(storage.values.size, 0)
  assert.equal(fixture.calls.filter(([kind]) => kind === 'transaction').length, 1)
})

test('rendered offline Pi stays waiting, retains recovery marker and offers only result verification', async () => {
  const fixture = firebaseMemoryFixture()
  const storage = memoryStorage()
  const service = await loadFirebaseService(fixture.database)
  const harness = await resetComponentHarness({ canReset: true,
    onMemoryReset: (operation) => service.resetConversationMemories('elder_001', { ...request, ...operation }),
  }, { storage })
  let tree = harness.render()
  button(tree, '기억 초기화').props.onClick(); tree = harness.render()
  await button(tree, '내용 확인 · 다음').props.onClick(); tree = harness.render()
  flatten(tree).find((node) => node?.type === 'input').props.onChange({ target: { value: '기억 초기화' } }); tree = harness.render()
  await button(tree, '다시 확인 · 기억 초기화').props.onClick(); tree = harness.render()
  assert.match(textOf(tree), /기기가 연결되어 초기화를 완료할 때까지/)
  assert.equal(button(tree, '다시 확인 · 기억 초기화'), undefined)
  assert.equal(storage.values.size, 1)
  const id = Object.keys(fixture.operations)[0]
  fixture.operations[id] = { ...fixture.operations[id], status: 'completed', generation: 8 }
  await button(tree, '결과 확인').props.onClick(); tree = harness.render()
  assert.match(textOf(tree), /비활성화했습니다/)
  assert.equal(storage.values.size, 0)
  assert.equal(fixture.calls.filter(([kind]) => kind === 'transaction').length, 1)
})
test('regression M4: rapid first/final activation cannot reset without a fresh typed acknowledgement', async () => {
  let calls = 0
  const flow = createMemoryResetFlow(async () => { calls++; return { outcome: 'SUCCESS' } })
  flow.open(true)
  flow.advance(true)
  flow.advance(true)
  await flow.confirm(true)
  await flow.confirm(true)
  assert.equal(calls, 0)
  flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), true)
  assert.equal(calls, 1)
})

test('regression M4: rendered second click and repeated Enter/Space never dispatch the reset', async () => {
  let calls = 0
  const harness = await resetComponentHarness({ canReset: true, onMemoryReset: async ({ prepareOnly }) => {
    if (prepareOnly) return preparedResult()
    calls++; return { outcome: 'SUCCESS' }
  } })
  let tree = harness.render()
  button(tree, '기억 초기화').props.onClick()
  tree = harness.render()
  await button(tree, '내용 확인 · 다음').props.onClick()
  tree = harness.render()
  const final = button(tree, '다시 확인 · 기억 초기화')
  assert.equal(final.props.disabled, true)
  await final.props.onClick()
  assert.equal(calls, 0)
  const acknowledgement = flatten(tree).find((node) => node?.type === 'input')
  assert.equal(acknowledgement.props.id, 'memory-reset-acknowledgement')
  acknowledgement.props.onChange({ target: { value: '기억 초기화' } })
  tree = harness.render()
  for (const key of ['Enter', ' ']) {
    let prevented = false
    button(tree, '다시 확인 · 기억 초기화').props.onKeyDown({ key, repeat: true, preventDefault() { prevented = true } })
    assert.equal(prevented, true)
  }
  assert.equal(calls, 0)
  await button(tree, '다시 확인 · 기억 초기화').props.onClick()
  assert.equal(calls, 1, 'normal keyboard/button activation remains accessible')
})






test('a successful read-back without this operation receipt remains UNKNOWN', async () => {
  const service = await loadFirebaseService({
    getDatabase: () => 'test-db', ref: (_db, target) => target,
    serverTimestamp: () => 1000,
    runTransaction: async () => { throw new Error('network') },
    get: async () => ({ val: () => ({ generation: 9, lastResetOperationId: 'other-reset' }) }),
  })
  const result = await service.resetConversationMemories('elder_001', { operationId: 'this-reset', deviceId: 'device_001' })
  assert.equal(result.outcome, 'UNKNOWN')
})

test('regression M5 reload: pending operation survives remount and only verifies, preserving newly stored memories', async () => {
  const storage = memoryStorage()
  const options = { scope: 'https://test.firebaseio.com|elder_001', storage }
  let bucket = null
  let writes = 0
  let available = false
  let firstOperationId
  const reset = async ({ operationId, verifyOnly }) => {
    if (verifyOnly) {
      assert.equal(operationId, firstOperationId)
      return available ? { outcome: 'SUCCESS' } : { outcome: 'UNKNOWN' }
    }
    firstOperationId = operationId
    writes++
    bucket = { _control: { generation: 1, resetOperations: { [operationId]: { status: 'completed', generation: 1 } } } }
    return { outcome: 'UNKNOWN' }
  }
  const first = createMemoryResetFlow(reset, options)
  first.open(true)
  first.advance(true)
  first.acknowledge('기억 초기화')
  await first.confirm(true)
  bucket.fresh = { source: 'conversation', status: 'active' }

  const reloaded = createMemoryResetFlow(reset, options)
  assert.equal(reloaded.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(reloaded.getSnapshot().operationId, firstOperationId)
  reloaded.open(true)
  reloaded.advance(true)
  reloaded.acknowledge('기억 초기화')
  assert.equal(await reloaded.confirm(true), false)
  assert.equal(storage.values.size, 1)
  available = true
  assert.equal(await reloaded.verify(true), true)
  assert.equal(storage.values.size, 0)
  assert.equal(writes, 1)
  assert.equal(bucket._control.generation, 1)
  assert.equal(bucket.fresh.status, 'active')
})

test('regression M5 recovery: operation is persisted before dispatch and isolated by backend plus elder', async () => {
  const storage = memoryStorage()
  const scope = 'https://one.firebaseio.com|elder_001'
  let persistedBeforeDispatch = false
  const flow = createMemoryResetFlow(async ({ operationId }) => {
    persistedBeforeDispatch = storage.values.size === 1 && JSON.parse([...storage.values.values()][0]).operationId === operationId
    return { outcome: 'UNKNOWN' }
  }, { scope, storage })
  flow.open(true)
  flow.advance(true)
  flow.acknowledge('기억 초기화')
  await flow.confirm(true)
  assert.equal(persistedBeforeDispatch, true, 'recovery identity must exist before any server mutation')
  for (const other of ['https://one.firebaseio.com|elder_002', 'https://two.firebaseio.com|elder_001']) {
    assert.equal(createMemoryResetFlow(async () => {}, { scope: other, storage }).getSnapshot().outcome, null)
  }
  assert.equal(createMemoryResetFlow(async () => {}, { scope, storage }).getSnapshot().outcome, 'UNKNOWN')
})

test('regression M5 storage: unavailable, corrupt, or unwritable recovery storage prevents destructive dispatch', async () => {
  for (const [storage, outcome] of [
    [{ getItem() { throw new Error('storage denied') } }, 'UNKNOWN'],
    [{ getItem: () => 'malformed json' }, 'UNKNOWN'],
    [{ getItem: () => '{"version":1}' }, 'UNKNOWN'],
    [{ length: 0, key: () => null, getItem: () => null, setItem() { throw new Error('quota exceeded') } }, 'FAILED'],
  ]) {
    let writes = 0
    const flow = createMemoryResetFlow(async () => { writes++; return { outcome: 'SUCCESS' } }, { storage })
    flow.open(true)
    flow.advance(true)
    flow.acknowledge('기억 초기화')
    assert.equal(await flow.confirm(true), false)
    assert.equal(writes, 0)
    assert.equal(flow.getSnapshot().outcome, outcome)
    assert.match(flow.getSnapshot().error, /저장소/)
  }
})

test('rendered reload automatically reconciles UNKNOWN through the same operation without destructive confirmation', async () => {
  const storage = memoryStorage()
  const props = { canReset: true, resetScope: 'https://test.firebaseio.com|elder_001' }
  let writes = 0
  let verifications = 0
  const onMemoryReset = async ({ verifyOnly, prepareOnly }) => {
    if (prepareOnly) return preparedResult()
    if (verifyOnly) { verifications++; return { outcome: 'SUCCESS' } }
    writes++
    return { outcome: 'UNKNOWN' }
  }
  const beforeReload = await resetComponentHarness({ ...props, onMemoryReset }, { storage })
  let tree = beforeReload.render()
  button(tree, '기억 초기화').props.onClick()
  tree = beforeReload.render()
  await button(tree, '내용 확인 · 다음').props.onClick()
  tree = beforeReload.render()
  flatten(tree).find((node) => node?.type === 'input').props.onChange({ target: { value: '기억 초기화' } })
  tree = beforeReload.render()
  await button(tree, '다시 확인 · 기억 초기화').props.onClick()

  const afterReload = await resetComponentHarness({ ...props, onMemoryReset }, { storage })
  tree = afterReload.render()
  assert.match(textOf(tree), /결과를 확인/)
  assert.equal(button(tree, '다시 확인 · 기억 초기화'), undefined)
  assert.equal(button(tree, '내용 확인 · 다음').props.disabled, true)
  await Promise.resolve()
  tree = afterReload.render()
  assert.match(textOf(tree), /비활성화했습니다/)
  assert.equal(writes, 1)
  assert.equal(verifications, 1)
  assert.equal(storage.values.size, 0)
})

test('rendered reload shows definitive device failure after background reconciliation', async () => {
  const storage = memoryStorage()
  const scope = 'https://test.firebaseio.com|elder_001'
  storage.setItem(`doran.memoryReset.pending.v2:${encodeURIComponent(scope)}.failed-operation`,
    JSON.stringify({ version: 2, operationId: 'failed-operation' }))
  const harness = await resetComponentHarness({ canReset: true, resetScope: scope,
    onMemoryReset: async ({ verifyOnly }) => {
      assert.equal(verifyOnly, true)
      return { outcome: 'FAILED' }
    },
  }, { storage })
  harness.render()
  await Promise.resolve()
  const tree = harness.render()
  const outsideDialog = tree.props.children.filter((node) => node?.type !== 'dialog')
  assert.match(textOf(outsideDialog), /기기에서 기억을 초기화하지 못했습니다/)
  assert.equal(storage.values.size, 0)
})

test('recovery scope uses normalized backend plus elder without URL credentials or query secrets', async () => {
  const service = await loadFirebaseService({}, true, {
    VITE_FIREBASE_DATABASE_URL: 'https://username:secret@EXAMPLE.firebaseio.com/?auth=token#fragment',
  })
  assert.equal(service.getMemoryResetScope('elder_001'), 'https://example.firebaseio.com|elder_001')
  assert.equal(service.getMemoryResetScope('elder_002'), 'https://example.firebaseio.com|elder_002')
  const noTrailingSlash = await loadFirebaseService({}, true, {
    VITE_FIREBASE_DATABASE_URL: 'https://example.firebaseio.com',
  })
  assert.equal(noTrailingSlash.getMemoryResetScope('elder_001'), service.getMemoryResetScope('elder_001'))
  assert.equal(service.getMemoryResetScope('../other'), null)
})

test('an in-flight marker survives immediate remount, and losing its storage entry does not enable retry', async () => {
  const storage = memoryStorage()
  let complete
  let writes = 0
  const reset = async ({ verifyOnly }) => {
    if (verifyOnly) return { outcome: 'UNKNOWN' }
    writes++
    return new Promise((resolve) => { complete = resolve })
  }
  const first = createMemoryResetFlow(reset, { storage })
  first.open(true)
  first.advance(true)
  first.acknowledge('기억 초기화')
  const pending = first.confirm(true)
  const reloaded = createMemoryResetFlow(reset, { storage })
  assert.equal(reloaded.getSnapshot().outcome, 'UNKNOWN')
  storage.values.clear()
  reloaded.open(true)
  await reloaded.verify(true)
  reloaded.cancel()
  reloaded.open(true)
  reloaded.advance(true)
  reloaded.acknowledge('기억 초기화')
  assert.equal(await reloaded.confirm(true), false)
  assert.equal(writes, 1)
  complete({ outcome: 'UNKNOWN' })
  await pending
})

test('an unconfigured reset scope starts idle without a false recovery alarm and cannot dispatch', async () => {
  let writes = 0
  const flow = memoryReset.createMemoryResetFlow(async () => { writes++; return { outcome: 'SUCCESS' } }, {
    scope: null, storage: { getItem() { throw new Error('must not read storage without a scope') } },
  })
  assert.equal(flow.getSnapshot().outcome, null)
  assert.equal(flow.getSnapshot().error, '')
  flow.open(true)
  flow.advance(true)
  flow.acknowledge('기억 초기화')
  assert.equal(await flow.confirm(true), false)
  assert.equal(writes, 0)
})
