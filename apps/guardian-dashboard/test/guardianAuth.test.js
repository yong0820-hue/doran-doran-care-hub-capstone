import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transformWithEsbuild } from 'vite'
import { createGuardianAuthState, guardianBinding } from '../src/guardianAuthState.js'
import * as memoryReset from '../src/memoryReset.js'
import * as medicationSchedule from '../src/medicationSchedule.js'
import * as dashboardState from '../src/dashboardState.js'

const require = createRequire(import.meta.url)

const binding = { enabled: true, role: 'guardian', elderId: 'elder_001', deviceId: 'device_001' }
const defer = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
function authFixture() {
  let uid = null, observer, authError
  const bindings = []
  const io = {
    currentUid: () => uid,
    watchAuth(next, error) { observer = next; authError = error; return () => {} },
    watchBinding(user, next, error) {
      const item = { user, next, error, stopped: false }
      bindings.push(item)
      return () => { item.stopped = true }
    },
    async signIn(_email, _password) { uid = 'guardian-a'; observer({ uid }); return { user: { uid } } },
    async signOut() { uid = null; observer(null) },
  }
  const auth = createGuardianAuthState(io)
  auth.start()
  return { auth, io, bindings,
    user(value) { uid = value; observer(value ? { uid: value } : null) },
    sdkUid(value) { uid = value },
    authError() { authError(new Error('secret-token')) },
    ready(user = 'guardian-a') { uid = user; observer({ uid }); bindings.at(-1).next(binding); return auth.capture() },
  }
}

test('isolated auth initializes, signs in with SDK credentials, and waits for administrator binding', async () => {
  const f = authFixture()
  assert.equal(f.auth.getSnapshot().status, 'initializing')
  f.user(null)
  assert.equal(f.auth.getSnapshot().status, 'signed_out')
  let credentials
  f.io.signIn = async (...args) => { credentials = args; f.sdkUid('guardian-a'); return { user: { uid: 'guardian-a' } } }
  assert.equal(await f.auth.login('test@example.invalid', 'fixture-password'), true)
  assert.deepEqual(credentials, ['test@example.invalid', 'fixture-password'])
  assert.equal(f.auth.getSnapshot().status, 'authorizing')
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  f.bindings.at(-1).next(binding)
  assert.equal(f.auth.getSnapshot().status, 'ready')
  assert.deepEqual(f.auth.getSnapshot().scope, { elderId: 'elder_001', deviceId: 'device_001' })
  assert.doesNotMatch(JSON.stringify(f.auth.getSnapshot()), /fixture-password/)
})

test('missing, disabled, device-role and malformed bindings deny access instead of falling back to mock', () => {
  for (const value of [null, {}, { ...binding, enabled: false }, { ...binding, role: 'device' },
    { ...binding, elderId: 'other/elder' }, { ...binding, deviceId: '' }]) {
    assert.equal(guardianBinding(value), null)
    const f = authFixture(); f.user('unregistered'); f.bindings.at(-1).next(value)
    assert.equal(f.auth.getSnapshot().status, 'no_access')
    assert.equal(f.auth.getSnapshot().scope, null)
    assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  }
})

test('wrong password and disabled account errors are generic and contain no SDK secret details', async () => {
  for (const code of ['auth/invalid-credential', 'auth/user-disabled']) {
    const f = authFixture(); f.user(null)
    f.io.signIn = async () => { throw Object.assign(new Error('password=private refresh=secret-token'), { code }) }
    assert.equal(await f.auth.login('test@example.invalid', 'private'), false)
    assert.equal(f.auth.getSnapshot().status, 'signed_out')
    assert.doesNotMatch(JSON.stringify(f.auth.getSnapshot()), /private|secret-token|refresh=/)
  }
})

test('UID replacement immediately unsubscribes resources and rejects old binding/data callbacks', () => {
  const f = authFixture(), old = f.ready()
  const previousBinding = f.bindings.at(-1)
  let stopped = 0
  old.track(() => stopped++)
  f.user('guardian-b')
  assert.equal(stopped, 1)
  assert.equal(previousBinding.stopped, true)
  previousBinding.next(binding)
  assert.equal(f.auth.getSnapshot().status, 'authorizing')
  assert.equal(old.current(), false)
  f.bindings.at(-1).next(binding)
  assert.equal(f.auth.capture().uid, 'guardian-b')
  assert.equal(old.current(), false)
})

test('SDK current user is checked even before its observer callback is delivered', () => {
  const f = authFixture(), old = f.ready()
  f.sdkUid('guardian-b')
  assert.equal(old.current(), false)
  assert.throws(old.assertCurrent, /AUTH_SCOPE_CHANGED/)
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
})

test('logout invalidates in-flight work before signOut resolves, and failed logout remains closed', async () => {
  const f = authFixture(), guard = f.ready(), pending = defer()
  f.io.signOut = () => pending.promise
  const result = f.auth.logout()
  assert.equal(guard.current(), false)
  assert.equal(f.auth.getSnapshot().status, 'signing_out')
  pending.reject(new Error('secret-token'))
  assert.equal(await result, false)
  assert.equal(f.auth.getSnapshot().status, 'error')
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  assert.doesNotMatch(JSON.stringify(f.auth.getSnapshot()), /secret-token/)
})

test('late sign-in completion after logout cannot reauthorize the old account', async () => {
  const f = authFixture(), pending = defer()
  f.user(null)
  f.io.signIn = () => pending.promise
  const login = f.auth.login('a@example.invalid', 'fake')
  await f.auth.logout()
  f.user('guardian-a')
  pending.resolve({ user: { uid: 'guardian-a' } })
  assert.equal(await login, false)
  assert.equal(f.auth.getSnapshot().status, 'signed_out')
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
})

test('a retired sign-in must settle before another account can begin a login', async () => {
  const f = authFixture(), pending = defer()
  f.user(null)
  let attempts = 0
  f.io.signIn = () => { attempts++; return pending.promise }
  const first = f.auth.login('a@example.invalid', 'fake')
  await f.auth.logout()
  assert.equal(await f.auth.login('b@example.invalid', 'fake'), false)
  assert.equal(attempts, 1)
  f.user('guardian-a')
  pending.resolve({ user: { uid: 'guardian-a' } })
  assert.equal(await first, false)
  assert.equal(f.auth.getSnapshot().status, 'signed_out')
  f.io.signIn = async () => { f.user('guardian-b'); return { user: { uid: 'guardian-b' } } }
  assert.equal(await f.auth.login('b@example.invalid', 'fake'), true)
  f.bindings.at(-1).next(binding)
  assert.equal(f.auth.capture().uid, 'guardian-b')
})

test('retired sign-in clears the revived SDK session and fences new login until delayed cleanup completes', async () => {
  const f = authFixture(), signIn = defer(), cleanup = defer()
  f.user(null)
  f.io.signIn = () => signIn.promise
  const pending = f.auth.login('a@example.invalid', 'fake')
  await f.auth.logout()
  let cleanupCalls = 0
  f.io.signOut = async () => { cleanupCalls++; await cleanup.promise; f.user(null) }
  f.user('guardian-a')
  signIn.resolve({ user: { uid: 'guardian-a' } })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(cleanupCalls, 1)
  assert.equal(f.auth.getSnapshot().status, 'signing_out')
  assert.equal(await f.auth.login('b@example.invalid', 'fake'), false)
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  cleanup.resolve()
  assert.equal(await pending, false)
  assert.equal(f.io.currentUid(), null, 'SDK restoration/persistence is cleared, not only hidden in UI')
  assert.equal(f.auth.getSnapshot().status, 'signed_out')
})

test('retired sign-in cleanup failure closes access and exposes a logout retry instead of a success state', async () => {
  const f = authFixture(), signIn = defer()
  f.user(null); f.io.signIn = () => signIn.promise
  const pending = f.auth.login('a@example.invalid', 'fake')
  await f.auth.logout()
  f.io.signOut = async () => { throw new Error('secret SDK failure') }
  f.user('guardian-a')
  signIn.resolve({ user: { uid: 'guardian-a' } })
  assert.equal(await pending, false)
  assert.equal(f.auth.getSnapshot().status, 'error')
  assert.match(f.auth.getSnapshot().error, /다시 로그아웃/)
  assert.doesNotMatch(f.auth.getSnapshot().error, /secret/)
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  f.io.signOut = async () => { f.user(null) }
  assert.equal(await f.auth.logout(), true)
  assert.equal(f.io.currentUid(), null)
  assert.equal(f.auth.getSnapshot().status, 'signed_out')
})

test('binding read denial, revocation and reassignment retire existing scope before reuse', () => {
  const f = authFixture(), first = f.ready()
  f.bindings.at(-1).next(undefined)
  assert.equal(first.current(), false)
  assert.equal(f.auth.getSnapshot().status, 'authorizing')
  f.bindings.at(-1).error()
  assert.equal(f.auth.getSnapshot().status, 'no_access')
  f.user('guardian-a'); f.bindings.at(-1).next(binding)
  const second = f.auth.capture()
  f.bindings.at(-1).next({ ...binding, enabled: false })
  assert.equal(second.current(), false)
  f.bindings.at(-1).next({ ...binding, enabled: false })
  assert.equal(f.auth.getSnapshot().status, 'no_access')
})

async function serviceFixture(database = {}, isolated = true, appOverrides = {}) {
  const path = new URL('../src/services/firebase.js', import.meta.url)
  const code = await transformWithEsbuild(await readFile(path, 'utf8'), path.pathname, {
    format: 'cjs', define: { 'import.meta.env': JSON.stringify({
      VITE_ISOLATED_AUTH: isolated ? 'true' : 'false', VITE_FIREBASE_API_KEY: 'fake',
      VITE_FIREBASE_AUTH_DOMAIN: 'demo-local.firebaseapp.com',
      VITE_FIREBASE_DATABASE_URL: 'https://demo-local.firebaseio.com', VITE_FIREBASE_PROJECT_ID: 'demo-local',
    }) },
  })
  const calls = [], subscriptions = []
  const db = {
    getDatabase: () => 'fake-db', ref: (_db, path = '') => path,
    query: (target) => target, limitToLast: (n) => n, serverTimestamp: () => 100,
    push: (target) => ({ key: 'one-operation', target }),
    onValue(target, next, error) {
      const item = { target, next, error, stopped: false }; subscriptions.push(item)
      return () => { item.stopped = true }
    },
    get: async (target) => { calls.push(['get', target]); return { val: () => null } },
    update: async (target, value) => { calls.push(['update', target, value]) },
    runTransaction: async (target, mutate) => {
      const value = mutate(null); calls.push(['transaction', target, value])
      return { committed: value !== undefined, snapshot: { val: () => value } }
    }, ...database,
  }
  const dependencies = { 'firebase/app': { getApps: () => [{ name: 'doran-isolated-guardian', options: {
    apiKey: 'fake', authDomain: 'demo-local.firebaseapp.com',
    databaseURL: 'https://demo-local.firebaseio.com', projectId: 'demo-local',
  } }], ...appOverrides },
    'firebase/database': db, '../memoryReset': memoryReset,
    '../medicationSchedule': medicationSchedule, '../dashboardState': dashboardState }
  const module = { exports: {} }
  vm.runInNewContext(code.code, { module, exports: module.exports, Date, performance, URL, setTimeout, clearTimeout,
    console: { error() { throw new Error('Unexpected logging') } },
    require: (name) => { assert.ok(dependencies[name], name); return dependencies[name] } })
  return { service: module.exports, calls, subscriptions, db }
}

test('Firebase subscriptions require ready scope, unsubscribe on UID switch and suppress queued old callbacks', async () => {
  const f = authFixture(), { service, subscriptions } = await serviceFixture()
  service.installAuthBoundary((elder, device) => f.auth.capture(elder, device))
  assert.throws(() => service.subscribeToEvents('elder_001', () => {}), /AUTH_SCOPE_CHANGED/)
  f.ready()
  assert.throws(() => service.subscribeToEvents('other-elder', () => {}), /AUTH_SCOPE_CHANGED/)
  assert.throws(() => service.subscribeToDevice('other-device', () => {}), /AUTH_SCOPE_CHANGED/)
  const received = []
  service.subscribeToEvents('elder_001', (rows) => received.push(rows))
  const original = subscriptions[0]
  original.next({ val: () => ({ before: { type: 'mock_test' } }) })
  assert.equal(received.length, 1)
  f.user('guardian-b')
  assert.equal(original.stopped, true)
  original.next({ val: () => ({ stale: { type: 'mock_test' } }) })
  original.error(new Error('late'))
  assert.equal(received.length, 1)
})

test('all root transaction and demo/device mutations are explicitly blocked in isolated mode', async () => {
  const { service, calls } = await serviceFixture()
  for (const operation of [
    () => service.reconcileEmergencyDeviceState('device_001'),
    () => service.resolveEventWithDevicePatch('elder_001', 'event', 'emergency_call', 'device_001'),
    () => service.pushEvent('elder_001', { type: 'emergency_call', deviceId: 'device_001' }),
    () => service.pushEvent('elder_001', { type: 'mock_test', deviceId: 'device_001' }),
  ]) await assert.rejects(operation, /ISOLATED_EVENT_MUTATION_DISABLED/)
  assert.throws(() => service.updateDevice('device_001', {}), /ISOLATED_EVENT_MUTATION_DISABLED/)
  assert.deepEqual(calls, [])
})

test('reset read delayed across UID switch does not continue to control or operation write', async () => {
  const f = authFixture(), delayed = defer(), paths = []
  const { service } = await serviceFixture({ get: (target) => { paths.push(target); return delayed.promise } })
  const guard = f.ready()
  service.installAuthBoundary((elder, device) => f.auth.capture(elder, device))
  const request = service.resetConversationMemories('elder_001', {
    deviceId: 'device_001', operationId: 'stable-operation', authGuard: guard, maxWaitMs: 0,
  })
  f.user('guardian-b'); f.bindings.at(-1).next(binding)
  delayed.resolve({ val: () => null })
  const result = await request
  assert.equal(result.outcome, 'UNKNOWN')
  assert.equal(result.operationId, 'stable-operation')
  assert.deepEqual(paths, ['memoryResetOperations/elder_001/stable-operation'])
})

test('reset transaction retries cannot create for a new UID, and ambiguous ACK remains UNKNOWN', async () => {
  const f = authFixture(), written = [], guard = f.ready()
  const { service } = await serviceFixture({
    runTransaction: async (target, mutate) => {
      written.push(mutate(null))
      f.user('guardian-b'); f.bindings.at(-1).next(binding)
      assert.equal(mutate(null), undefined)
      return { committed: true, snapshot: { val: () => ({ status: 'completed', generation: 4 }) } }
    },
  })
  service.installAuthBoundary((elder, device) => f.auth.capture(elder, device))
  const result = await service.resetConversationMemories('elder_001', {
    deviceId: 'device_001', operationId: 'same-id', authGuard: guard, maxWaitMs: 0,
  })
  assert.equal(written.length, 1)
  assert.equal(written[0].requestedUid, 'guardian-a')
  assert.equal(written[0].requestedBy, 'guardian_dashboard')
  assert.equal(result.operationId, 'same-id')
  assert.equal(result.outcome, 'UNKNOWN')
})

test('normal isolated reset creation preserves intent and same operation, command captures owner UID', async () => {
  const f = authFixture(), guard = f.ready(), { service, calls } = await serviceFixture()
  service.installAuthBoundary((elder, device) => f.auth.capture(elder, device))
  const result = await service.resetConversationMemories('elder_001', {
    deviceId: 'device_001', operationId: 'same-id', maxWaitMs: 0, authGuard: guard,
    intent: { version: 1, generation: 'initial', epoch: 0, operationId: null },
  })
  assert.equal(result.outcome, 'WAITING_FOR_DEVICE')
  assert.equal(calls.filter(([method]) => method === 'transaction').length, 1)
  assert.equal(calls.find(([method]) => method === 'transaction')[2].requestedUid, 'guardian-a')
  await service.pushAiConversationCommand('elder_001', 'device_001', guard)
  assert.equal(calls.at(-1)[2].requestedUid, 'guardian-a')
})

test('reset flow keeps backend+elder recovery identity when old UID receives late success', async () => {
  const values = new Map(), pending = defer()
  const storage = { get length() { return values.size }, key: (i) => [...values.keys()][i],
    getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }
  let current = true
  const scope = 'https://demo-local.firebaseio.com|elder_001'
  const flow = memoryReset.createMemoryResetFlow(() => pending.promise, { scope, storage, isCurrent: () => current })
  flow.open(true); await flow.advance(true); flow.acknowledge('기억 초기화')
  const result = flow.confirm(true), id = flow.getSnapshot().operationId
  current = false
  pending.resolve({ outcome: 'SUCCESS' })
  assert.equal(await result, false)
  assert.equal(flow.getSnapshot().outcome, 'UNKNOWN')
  assert.equal(values.size, 1)
  let verified
  const next = memoryReset.createMemoryResetFlow(async (request) => {
    verified = request; return { outcome: 'SUCCESS' }
  }, { scope, storage })
  assert.equal(await next.reconcile(true), true)
  assert.equal(verified.operationId, id)
  assert.equal(verified.verifyOnly, true)
  assert.equal(values.size, 0)
})

test('non-isolated service keeps its existing unauthenticated/mock-compatible API behavior', async () => {
  const { service, calls } = await serviceFixture({}, false)
  await service.pushAiConversationCommand('elder_001', 'device_001')
  assert.equal(calls[0][2].requestedUid, undefined)
  assert.equal(service.isIsolatedAuthMode(), false)
})

test('isolated app is separate from default Firebase and refuses a conflicting named app', async () => {
  const initialized = []
  const { service } = await serviceFixture({}, true, {
    getApps: () => [{ name: '[DEFAULT]', options: { projectId: 'other-project' } }],
    initializeApp: (options, name) => { initialized.push({ options, name }); return { options, name } },
  })
  assert.equal(service.getFirebaseApp().name, 'doran-isolated-guardian')
  assert.equal(initialized[0].options.projectId, 'demo-local')
  for (const options of [{}, { projectId: 'other-project' }]) {
    const conflicting = await serviceFixture({}, true, {
      getApps: () => [{ name: 'doran-isolated-guardian', options }],
      initializeApp: () => assert.fail('must not create after conflict'),
    })
    assert.throws(conflicting.service.getFirebaseApp, /AUTH_CONFIGURATION_MISMATCH/)
  }
})

test('schedule writes capture the UID and abort SDK transaction retries after account change', async () => {
  const f = authFixture(), guard = f.ready(), { service, calls, db } = await serviceFixture()
  service.installAuthBoundary((elder, device) => f.auth.capture(elder, device))
  const entry = { enabled: true, time: '08:30', medicationName: '테스트 약', weekdays: [1, 2, 3, 4, 5] }
  await service.createMedicationSchedule('elder_001', 'device_001', entry, guard)
  assert.equal(calls.at(-1)[2].updatedUid, 'guardian-a')
  const pending = defer()
  let retry
  db.runTransaction = (_target, mutate) => { retry = mutate; return pending.promise }
  const request = service.createMedicationSchedule('elder_001', 'device_001', entry, guard)
  assert.equal(retry(null).updatedUid, 'guardian-a')
  f.user('guardian-b'); f.bindings.at(-1).next(binding)
  assert.equal(retry(null), undefined)
  pending.resolve({ committed: true, snapshot: { val: () => null } })
  await assert.rejects(request, /AUTH_SCOPE_CHANGED/)
  await assert.rejects(service.pushAiConversationCommand('elder_001', 'device_001', guard), /AUTH_SCOPE_CHANGED/)
})

async function sourceModule(relative, dependencies, globals = {}) {
  const filename = new URL(`../src/${relative}`, import.meta.url)
  const source = await readFile(filename, 'utf8')
  const transformed = await transformWithEsbuild(source, filename.pathname, { format: 'cjs', jsx: 'automatic' })
  const module = { exports: {} }
  vm.runInNewContext(transformed.code, { module, exports: module.exports,
    require: (name) => {
      if (Object.hasOwn(dependencies, name)) return dependencies[name]
      if (name === 'react/jsx-runtime') return require(name)
      throw new Error(`Unmocked dependency: ${name}`)
    }, console: { error() { throw new Error('Unexpected error logging') } },
    ...globals,
  })
  return module.exports
}

async function authAdapterFixture() {
  let observer
  const sdk = { currentUser: null }
  const listeners = []
  const writes = []
  const { guardianAuth: auth } = await sourceModule('services/guardianAuth.js', {
    'firebase/auth': {
      getAuth: () => sdk,
      onAuthStateChanged: (_auth, next) => { observer = next; return () => {} },
      signInWithEmailAndPassword: async () => assert.fail('unused sign in'),
      signOut: async () => { sdk.currentUser = null; observer(null) },
    },
    'firebase/database': {
      ref: (_db, path) => path,
      onValue(target, next, error) {
        const item = { target, next, error, stopped: false }; listeners.push(item)
        return () => { item.stopped = true }
      },
      update: (...args) => writes.push(args), set: (...args) => writes.push(args),
    },
    '../guardianAuthState': { createGuardianAuthState, guardianBinding },
    './firebase': { getFirebaseApp: () => ({}), getFirebaseDatabase: () => ({}),
      getFirebaseProjectId: () => 'demo-local', isFirebaseConfigured: () => true, installAuthBoundary: () => {} },
  })
  return { auth, listeners, writes,
    user(uid) { sdk.currentUser = uid ? { uid } : null; observer(sdk.currentUser) },
    emit(target, value) { listeners.findLast((item) => item.target === target && !item.stopped).next({ val: () => value }) },
  }
}

test('actual auth adapter only reads own binding and matching administrator scope before allowing data', async () => {
  const f = await authAdapterFixture()
  assert.equal(f.listeners.length, 0)
  f.auth.start(); assert.equal(f.listeners.length, 0)
  f.user('guardian-a')
  assert.deepEqual(f.listeners.map((item) => item.target), ['accessBindings/guardian-a'])
  f.emit('accessBindings/guardian-a', binding)
  assert.equal(f.auth.getSnapshot().status, 'authorizing')
  assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  f.emit('accessScopes/elder_001', { deviceId: 'other-device', projectId: 'demo-local' })
  assert.equal(f.auth.getSnapshot().status, 'no_access')
  for (const projectId of [undefined, 'foreign-project']) {
    f.emit('accessScopes/elder_001', { deviceId: 'device_001', projectId })
    assert.equal(f.auth.getSnapshot().status, 'no_access')
    assert.throws(() => f.auth.capture(), /AUTH_SCOPE_CHANGED/)
  }
  f.emit('accessScopes/elder_001', { deviceId: 'device_001', projectId: 'demo-local' })
  assert.equal(f.auth.getSnapshot().status, 'ready')
  assert.equal(f.auth.capture().uid, 'guardian-a')
  assert.deepEqual(f.writes, [])
})

test('actual auth adapter fences late binding/scope callbacks and revocation unsubscribes active resources', async () => {
  const f = await authAdapterFixture(); f.auth.start(); f.user('guardian-a')
  f.emit('accessBindings/guardian-a', binding)
  const firstBinding = f.listeners[0], firstScope = f.listeners[1]
  f.emit('accessScopes/elder_001', { deviceId: 'device_001', projectId: 'demo-local' })
  const guard = f.auth.capture(); let stopped = 0
  guard.track(() => stopped++)
  f.emit('accessBindings/guardian-a', { ...binding, deviceId: 'other-device' })
  assert.equal(guard.current(), false)
  assert.equal(stopped, 1)
  assert.equal(firstBinding.stopped, true)
  assert.equal(firstScope.stopped, true)
  firstScope.next({ val: () => ({ deviceId: 'device_001', projectId: 'demo-local' }) })
  assert.equal(f.auth.getSnapshot().status, 'authorizing')
  await f.auth.logout()
  firstBinding.next({ val: () => binding })
  firstScope.error(new Error('secret'))
  assert.equal(f.auth.getSnapshot().status, 'signed_out')
  assert.equal(f.listeners.every((item) => item.stopped), true)
})

test('actual auth adapter rejects denied scope read and does not provision a relationship', async () => {
  const f = await authAdapterFixture(); f.auth.start(); f.user('guardian-a')
  f.emit('accessBindings/guardian-a', binding)
  f.listeners.at(-1).error(new Error('permission_denied token=secret'))
  assert.equal(f.auth.getSnapshot().status, 'no_access')
  assert.equal(f.listeners.every((item) => item.stopped), true)
  assert.doesNotMatch(JSON.stringify(f.auth.getSnapshot()), /secret/)
  assert.deepEqual(f.writes, [])
})

function hookState() {
  const values = [], cleanups = []
  let cursor = 0, effects = []
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in values)) values[index] = typeof initial === 'function' ? initial() : initial
      return [values[index], (next) => { values[index] = typeof next === 'function' ? next(values[index]) : next }]
    },
    useRef(initial) { const [value] = hooks.useState(() => ({ current: initial })); return value },
    useEffect(effect, deps) {
      const index = cursor++
      if (!(index in values) || deps.some((value, i) => value !== values[index][i])) {
        values[index] = deps; effects.push(effect)
      }
    },
    useSyncExternalStore(_subscribe, snapshot) { return snapshot() },
  }
  return { hooks, render(fn) { cursor = 0; return fn() },
    effects() { const pending = effects; effects = []; pending.forEach((effect) => cleanups.push(effect())) },
    unmount() { cleanups.forEach((cleanup) => cleanup?.()) },
  }
}
const flatten = (node) => node == null || typeof node === 'boolean' ? []
  : Array.isArray(node) ? node.flatMap(flatten) : [node, ...flatten(node?.props?.children)]
const textOf = (tree) => flatten(tree).filter((node) => typeof node === 'string').join(' ')

test('rendered sign-in clears password, exposes progress/error/logout, and offers no signup or mock fallback', async () => {
  const state = hookState(), calls = []
  const { default: SignIn } = await sourceModule('components/GuardianSignIn.jsx', { react: state.hooks })
  let snapshot = { status: 'signed_out', uid: null, error: '' }
  const render = () => state.render(() => SignIn({ state: snapshot,
    onLogin: async (...args) => calls.push(args), onLogout: () => calls.push(['logout']) }))
  let tree = render()
  const input = (kind) => flatten(tree).find((node) => node?.type === 'input' && node.props.type === kind)
  input('email').props.onChange({ target: { value: ' test@example.invalid ' } })
  input('password').props.onChange({ target: { value: 'fixture-password' } })
  tree = render()
  await flatten(tree).find((node) => node?.type === 'form').props.onSubmit({ preventDefault() {} })
  tree = render()
  assert.equal(input('password').props.value, '')
  assert.deepEqual(calls[0], ['test@example.invalid', 'fixture-password'])
  for (const status of ['initializing', 'signing_in', 'authorizing', 'signing_out']) {
    snapshot = { status }; tree = render()
    assert.equal(flatten(tree).find((node) => node?.props?.type === 'submit').props.disabled, true)
    assert.equal(flatten(tree).some((node) => node?.props?.role === 'status'), true)
  }
  snapshot = { status: 'no_access', uid: 'guardian-a', error: '접근 권한 없음' }; tree = render()
  assert.equal(flatten(tree).some((node) => node?.type === 'form'), false)
  assert.match(textOf(tree), /접근 권한 없음/)
  flatten(tree).find((node) => node?.type === 'button' && textOf(node) === '로그아웃').props.onClick()
  assert.deepEqual(calls.at(-1), ['logout'])
  assert.doesNotMatch(textOf(tree), /회원가입|Mock|Google|비밀번호 재설정/)
})

test('isolated dashboard hook keeps subscriptions read-only and ignores callbacks after unmount', async () => {
  const state = hookState(), subscribers = [], calls = []
  const subscribe = (name) => (...args) => {
    const callback = args.find((arg) => typeof arg === 'function')
    const item = { name, callback, stopped: false }; subscribers.push(item)
    callback(name === 'Connection' ? true : ['Events', 'Commands'].includes(name) ? [] : {})
    return () => { item.stopped = true }
  }
  const services = { isFirebaseConfigured: () => true,
    reconcileEmergencyDeviceState: async () => calls.push('root-reconcile'),
    resolveEventWithDevicePatch: async () => calls.push('root-resolve'), pushEvent: async () => calls.push('event'),
  }
  for (const name of ['Elder', 'Device', 'Events', 'Commands', 'Connection', 'MedicationSchedule']) {
    services[`subscribeTo${name}`] = subscribe(name)
  }
  const { useDashboardData } = await sourceModule('hooks/useDashboardData.js', {
    react: state.hooks, '../services/firebase': services, '../dashboardState': dashboardState,
    '../data/mockData': { mockElder: { secret: 'MOCK' }, mockDevice: {}, mockEvents: [] },
  }, { setTimeout: () => 1, clearTimeout() {} })
  const render = () => state.render(() => useDashboardData('elder_001', 'device_001', { isolated: true }))
  render(); state.effects()
  const dashboard = render()
  assert.equal(subscribers.length, 6)
  assert.equal(dashboard.connectionState, 'connected')
  dashboard.handleResolve('event')
  await assert.rejects(dashboard.handleSendEvent({}), /ISOLATED_EVENT_MUTATION_DISABLED/)
  assert.deepEqual(calls, [])
  state.unmount()
  assert.equal(subscribers.every((item) => item.stopped), true)
  subscribers.find((item) => item.name === 'Elder').callback({ name: 'late-old-user' })
  assert.equal(render().elder.name, undefined)
  assert.doesNotMatch(JSON.stringify(render()), /MOCK|late-old-user/)
})

test('App auth gate renders no dashboard data before access, then keys mounted UI by UID and generation', async () => {
  const state = hookState(), f = authFixture()
  let dashboardCalls = 0, isolated = true
  const dependencies = { react: state.hooks, './services/guardianAuth': { guardianAuth: f.auth },
    './services/firebase': { isIsolatedAuthMode: () => isolated, getMemoryResetScope: () => 'test-backend|elder_001' },
    './hooks/useDashboardData': { useDashboardData() { dashboardCalls++; throw new Error('dashboard mounted') } },
    './data/mockData': { mockMedication: {} }, './dashboardState': dashboardState,
  }
  for (const name of ['Header', 'ElderProfileCard', 'PresenceStatus', 'DeviceStatusCard', 'MedicationStatus',
    'MedicationScheduleManager', 'ResetSettings', 'AiConversationStatus', 'CriticalAlerts', 'EventTimeline',
    'DemoControls', 'DebugPanel', 'GuardianSignIn']) {
    dependencies[`./components/${name}`] = { __esModule: true, default: name }
  }
  const { default: App } = await sourceModule('App.jsx', dependencies)
  const renderGate = () => { const entry = App(); return state.render(() => entry.type(entry.props)) }
  for (const transition of [() => {}, () => f.user(null), () => f.user('unbound'),
    () => f.bindings.at(-1).next(null), () => f.authError()]) {
    transition()
    assert.equal(flatten(renderGate()).some((node) => node?.type === 'GuardianSignIn'), true)
    assert.equal(dashboardCalls, 0)
  }
  f.ready('guardian-a')
  const first = renderGate()
  assert.equal(first.props.authGuard.scope.elderId, 'elder_001')
  f.ready('guardian-b')
  const second = renderGate()
  assert.notEqual(first.key, second.key)
  assert.equal(first.props.authGuard.current(), false)
  await f.auth.logout()
  assert.equal(flatten(renderGate()).some((node) => node?.type === 'GuardianSignIn'), true)
  isolated = false
  assert.equal(App().type, first.type, 'legacy path bypasses the isolated auth gate')
})
