#!/usr/bin/env node
// Real Auth + RTDB Emulator tests. No firebase-tools/rules-unit-testing imports.
// Start the loopback emulators with firebase.emulator.json, then pass --python.
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '../..')
const PROJECT = 'demo-doran-memory-local'
const NAMESPACE = PROJECT + '-default-rtdb'
const AUTH = 'http://127.0.0.1:9099'
const DB = 'http://127.0.0.1:9000'
const ELDER = 'elder_test_a'
const DEVICE = 'device_test_a'
const STAMP = { '.sv': 'timestamp' }
const pythonAt = process.argv.indexOf('--python')
const python = pythonAt >= 0 ? process.argv[pythonAt + 1] : null
const { runMemoryReset } = await import(pathToFileURL(resolve(ROOT, 'apps/guardian-dashboard/src/memoryReset.js')))
const { toMedicationSchedulePayload } = await import(pathToFileURL(resolve(ROOT, 'apps/guardian-dashboard/src/medicationSchedule.js')))
let current = 'preflight'
let passed = 0
const results = []

function check(value, code) { if (!value) throw new Error(code) }
async function localFetch(url, options = {}) {
  const parsed = new URL(url)
  check([AUTH, DB].includes(parsed.origin) && !parsed.username && !parsed.password,
    'NONLOCAL_ENDPOINT_BLOCKED')
  return fetch(url, { ...options, redirect: 'error', signal: AbortSignal.timeout(10000) })
}
async function authCall(action, body) {
  const response = await localFetch(`${AUTH}/identitytoolkit.googleapis.com/v1/${action}?key=local-emulator-only`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  return { status: response.status, data: await response.json() }
}
async function account(label) {
  const email = `${label}-${Date.now()}-${Math.random().toString(16).slice(2)}@local.invalid`
  const password = 'Local-fixture-only_123!'
  const created = await authCall('accounts:signUp', { email, password, returnSecureToken: true })
  check(created.status === 200, 'LOCAL_ACCOUNT_CREATE_FAILED')
  const login = await authCall('accounts:signInWithPassword', { email, password, returnSecureToken: true })
  check(login.status === 200 && login.data.localId === created.data.localId, 'LOCAL_LOGIN_FAILED')
  const claims = JSON.parse(Buffer.from(login.data.idToken.split('.')[1], 'base64url').toString())
  check(claims.aud === PROJECT && claims.iss === `https://securetoken.google.com/${PROJECT}`, 'TOKEN_PROJECT_MISMATCH')
  return { uid: login.data.localId, token: login.data.idToken, email, password }
}
async function db(method, path, body, actor, options = {}) {
  const url = new URL(`${DB}/${path ? path.replace(/^\//u, '') : ''}.json`)
  url.searchParams.set('ns', NAMESPACE)
  if (actor?.token) url.searchParams.set('auth', actor.token)
  for (const [key, value] of Object.entries(options.query ?? {})) url.searchParams.set(key, String(value))
  const headers = { 'Content-Type': 'application/json', ...options.headers }
  if (actor === 'admin') headers.Authorization = 'Bearer owner'
  const response = await localFetch(url, { method, headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
  const text = await response.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { throw new Error('NON_JSON_EMULATOR_RESPONSE') }
  return { status: response.status, data, etag: response.headers.get('etag') }
}
async function allow(method, path, body, actor, options) {
  const response = await db(method, path, body, actor, options)
  check(response.status >= 200 && response.status < 300, `ALLOW_EXPECTED_HTTP_${response.status}`)
  return response
}
async function deny(method, path, body, actor) {
  const response = await db(method, path, body, actor)
  check([401, 403].includes(response.status), `DENY_EXPECTED_HTTP_${response.status}`)
}
async function test(name, run) {
  current = name
  await run()
  passed++
  results.push({ name, result: 'PASS' })
  console.log(`PASS ${name}`)
}
async function read(path, actor) { return (await allow('GET', path, undefined, actor)).data }
async function worker(action, actor, extra = {}) {
  const result = await new Promise((resolveResult, reject) => {
    const child = spawn(python, ['-B', resolve(HERE, 'firebase_emulator_worker.py'),
      '--code-dir', resolve(ROOT, 'raspberry-pi-voice-hub')], {
      cwd: ROOT, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    })
    let output = ''
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', chunk => { output += chunk })
    // Worker logs have no tokens; raw stderr is nevertheless never forwarded.
    child.stderr.resume()
    child.on('error', () => reject(new Error('LOCAL_WORKER_START_FAILED')))
    child.on('close', code => {
      const line = output.trim().split(/\r?\n/u).at(-1)
      let decoded
      try { decoded = JSON.parse(line) } catch { return reject(new Error('LOCAL_WORKER_RESULT_INVALID')) }
      if (code !== 0 || !decoded.success) {
        const failed = (decoded.trace ?? []).filter(item => item.status >= 400)
        if (failed.length) console.error(JSON.stringify({ protocolFailures: failed }))
        return reject(new Error('LOCAL_PROTOCOL_FAILED'))
      }
      resolveResult(decoded)
    })
    child.stdin.end(JSON.stringify({ project: PROJECT, elder: ELDER, device: DEVICE,
      idToken: actor.token, action, ...extra }))
  })
  return result
}
function ioFor(actor, operationId, overrides = {}) {
  const path = `memoryResetOperations/${ELDER}/${operationId}`
  return {
    readControl: () => read(`memories/${ELDER}/_control`, actor),
    readOperation: () => read(path, actor),
    readLeaderOperation: id => read(`memoryResetOperations/${ELDER}/${id}`, actor),
    createOperation: async request => {
      const before = await allow('GET', path, undefined, actor,
        { headers: { 'X-Firebase-ETag': 'true' } })
      if (before.data != null) return before.data
      const created = await db('PUT', path, { ...request, requestedUid: actor.uid }, actor,
        { headers: { 'if-match': before.etag } })
      check([200, 412].includes(created.status), 'REQUEST_CREATE_FAILED')
      return read(path, actor)
    },
    timestamp: () => STAMP,
    now: () => performance.now(),
    sleep: milliseconds => new Promise(resolveSleep => setTimeout(resolveSleep, milliseconds)),
    ...overrides,
  }
}
function request(operationId, actor, extra = {}) {
  return runMemoryReset({ io: ioFor(actor, operationId), operationId, deviceId: DEVICE, maxWaitMs: 0, ...extra })
}
function requested(uid, intent = { version: 1, generation: 'initial', epoch: 0 }) {
  return { status: 'requested', deviceId: DEVICE, deviceStatus: `${DEVICE}|requested`,
    requestedAt: STAMP, requestedBy: 'guardian_dashboard', requestedUid: uid, intent }
}

try {
  check(python && process.argv.length === 4, 'USAGE_NODE_TESTS_PYTHON_ABSOLUTE_EXECUTABLE')
  for (const [name, expected] of [['FIREBASE_AUTH_EMULATOR_HOST', '127.0.0.1:9099'],
    ['FIREBASE_DATABASE_EMULATOR_HOST', '127.0.0.1:9000'], ['GCLOUD_PROJECT', PROJECT]]) {
    check(!process.env[name] || process.env[name] === expected, 'EMULATOR_ENVIRONMENT_MISMATCH')
  }
  const rules = JSON.parse(await readFile(resolve(HERE, 'firebase-isolated.rules.json'), 'utf8'))
  await allow('PUT', '.settings/rules', rules, 'admin')
  // Only the fixed local emulator namespace is reset; cloud URLs cannot enter db().
  await allow('PUT', '', null, 'admin')
  const guardian = await account('guardian')
  const device = await account('device')
  const stranger = await account('stranger')
  const other = await account('other-scope')
  const foreignGuardian = await account('foreign-guardian')
  const wrongDevice = await account('wrong-device')
  const disabled = await account('disabled-binding')
  const disabledLogin = await account('disabled-login')
  const bindings = {
    [guardian.uid]: { role: 'guardian', elderId: ELDER, deviceId: DEVICE, enabled: true },
    [device.uid]: { role: 'device', elderId: ELDER, deviceId: DEVICE, enabled: true },
    [other.uid]: { role: 'device', elderId: 'elder_test_b', deviceId: 'device_test_b', enabled: true },
    [foreignGuardian.uid]: { role: 'guardian', elderId: 'elder_test_b', deviceId: 'device_test_b', enabled: true },
    [wrongDevice.uid]: { role: 'device', elderId: ELDER, deviceId: 'device_wrong', enabled: true },
    [disabled.uid]: { role: 'guardian', elderId: ELDER, deviceId: DEVICE, enabled: false },
    [disabledLogin.uid]: { role: 'device', elderId: ELDER, deviceId: DEVICE, enabled: true },
  }
  await allow('PATCH', '', { accessBindings: bindings,
    accessScopes: { [ELDER]: { deviceId: DEVICE, projectId: PROJECT },
      elder_test_b: { deviceId: 'device_test_b', projectId: PROJECT } },
    [`users/${ELDER}`]: { name: 'Local fixture' },
    [`devices/${DEVICE}`]: { elderId: ELDER, name: 'Local fixture device' },
  }, 'admin')

  await test('Auth real email/password login; wrong password rejected', async () => {
    check((await authCall('accounts:signInWithPassword', {
      email: guardian.email, password: 'definitely-wrong', returnSecureToken: true,
    })).status === 400, 'WRONG_PASSWORD_ACCEPTED')
  })
  await test('Actual Pi credential loader/login/binding/refresh/singleflight/no anonymous fallback', async () => {
    const result = await worker('auth', device, { email: device.email, password: device.password, uid: device.uid })
    check(result.loginCount === 1 && result.successfulRefreshes === 2 && result.concurrentCalls === 6,
      'ACTUAL_PI_AUTH_COUNTS')
  })
  await test('Actual Pi rejects wrong password and unauthorized Guardian device role', async () => {
    for (const [actor, password] of [[device, 'wrong-fixture-password'], [guardian, guardian.password]]) {
      const result = await worker('auth', actor, { email: actor.email, password, uid: actor.uid, expectedReject: true })
      check(result.rejected && result.anonymousFallback === false, 'ACTUAL_PI_AUTH_REJECTION_FAILED')
    }
  })
  await test('Auth emulator disabled account rejected by actual Pi provider', async () => {
    const response = await localFetch(`${AUTH}/identitytoolkit.googleapis.com/v1/accounts:update?key=local-emulator-only`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer owner' },
      body: JSON.stringify({ localId: disabledLogin.uid, disableUser: true }),
    })
    check(response.status === 200, 'EMULATOR_ACCOUNT_DISABLE_FAILED')
    const result = await worker('auth', disabledLogin, { email: disabledLogin.email,
      password: disabledLogin.password, uid: disabledLogin.uid, expectedReject: true })
    check(result.rejected && result.anonymousFallback === false, 'DISABLED_ACCOUNT_NOT_REJECTED')
  })
  await test('Unauthenticated denied on root and every runtime collection', async () => {
    for (const path of ['', `users/${ELDER}`, `devices/${DEVICE}`, `events/${ELDER}`,
      `commands/${ELDER}/${DEVICE}`, `medicationSchedules/${ELDER}/${DEVICE}`,
      `conversations/${ELDER}`, `memories/${ELDER}/_control`, `memoryResetOperations/${ELDER}`]) {
      await deny('GET', path, undefined, null)
    }
  })
  await test('Authenticated unregistered and disabled binding denied', async () => {
    for (const actor of [stranger, disabled]) await deny('GET', `users/${ELDER}`, undefined, actor)
  })
  await test('Owner binding/scope readable; authority assignment and listing denied', async () => {
    for (const actor of [guardian, device]) {
      await allow('GET', `accessBindings/${actor.uid}`, undefined, actor)
      await allow('GET', `accessScopes/${ELDER}`, undefined, actor)
      await deny('GET', 'accessBindings', undefined, actor)
      await deny('GET', `accessBindings/${stranger.uid}`, undefined, actor)
      await deny('PUT', `accessBindings/${actor.uid}`, { ...bindings[actor.uid], role: 'admin' }, actor)
      await deny('DELETE', `accessBindings/${actor.uid}`, undefined, actor)
      await deny('PUT', `accessScopes/${ELDER}`, { deviceId: 'device_wrong' }, actor)
      await deny('DELETE', `accessScopes/${ELDER}`, undefined, actor)
    }
  })
  await test('Guardian and Pi own subscriptions/read paths allowed', async () => {
    for (const actor of [guardian, device]) for (const path of [`users/${ELDER}`, `devices/${DEVICE}`,
      `events/${ELDER}`, `commands/${ELDER}/${DEVICE}`, `medicationSchedules/${ELDER}/${DEVICE}`,
      `conversations/${ELDER}`, `memories/${ELDER}/_control`]) await allow('GET', path, undefined, actor)
  })
  await test('Root reads/PUT transactions denied; memory elder root read denied', async () => {
    for (const actor of [guardian, device]) {
      await deny('GET', '', undefined, actor)
      await deny('PUT', '', { users: {} }, actor)
      await deny('GET', `memories/${ELDER}`, undefined, actor)
    }
  })
  await test('Cross elder/device and misbound same-elder device denied', async () => {
    for (const path of [`users/${ELDER}`, `devices/${DEVICE}`, `memories/${ELDER}/_control`,
      `memoryResetOperations/${ELDER}`, `events/${ELDER}`, `commands/${ELDER}/${DEVICE}`]) {
      for (const actor of [other, wrongDevice]) await deny('GET', path, undefined, actor)
    }
    for (const path of [`commands/${ELDER}/device_wrong`, `medicationSchedules/${ELDER}/device_wrong`,
      'devices/device_wrong', 'users/elder_test_b']) await deny('GET', path, undefined, guardian)
  })
  await test('Foreign project ID token rejected', async () => {
    const parts = guardian.token.split('.')
    const claims = JSON.parse(Buffer.from(parts[1], 'base64url').toString())
    claims.aud = 'demo-other-project'
    claims.iss = 'https://securetoken.google.com/demo-other-project'
    parts[1] = Buffer.from(JSON.stringify(claims)).toString('base64url')
    await deny('GET', `users/${ELDER}`, undefined, { token: parts.join('.') })
    await deny('GET', `accessBindings/${guardian.uid}`, undefined, { token: parts.join('.') })
  })
  await test('Guardian cannot write device/care/profile/conversation truth', async () => {
    for (const [path, value] of [[`devices/${DEVICE}/lastSeen`, 100], [`users/${ELDER}/name`, 'fake'],
      [`events/${ELDER}/fake`, { type: 'long_inactivity', elderId: ELDER, deviceId: DEVICE,
        createdAt: 100, status: 'open', meta: { source: 'guardian_dashboard_demo' } }],
      [`conversations/${ELDER}/fake`, { elderId: ELDER, deviceId: DEVICE, createdAt: 1, updatedAt: 2 }]]) {
      await deny('PUT', path, value, guardian)
    }
  })
  await test('Guardian own schedule write/read allowed; device schedule write denied', async () => {
    const schedule = { schemaVersion: 1, timezone: 'Asia/Seoul', revision: 'fixture_revision',
      updatedAt: STAMP, updatedBy: 'guardian_dashboard', updatedUid: guardian.uid,
      entries: { fixture_schedule: { enabled: true, time: '09:00', medicationName: 'local fixture', weekdays: [1] } } }
    await allow('PUT', `medicationSchedules/${ELDER}/${DEVICE}`, schedule, guardian)
    await read(`medicationSchedules/${ELDER}/${DEVICE}`, device)
    await deny('PUT', `medicationSchedules/${ELDER}/${DEVICE}`, schedule, device)
    await deny('PUT', `medicationSchedules/${ELDER}/${DEVICE}`, { ...schedule, updatedUid: stranger.uid }, guardian)
    await deny('PUT', `medicationSchedules/${ELDER}/device_wrong`, schedule, guardian)
  })
  await test('Guardian ISO weekdays pass whole-document conditional schedule writes', async () => {
    const schedulePath = `medicationSchedules/${ELDER}/${DEVICE}`
    for (const [label, days] of [
      ['every', [1, 2, 3, 4, 5, 6, 7]],
      ['monday', [1]],
      ['sunday', [7]],
      ['weekdays', [1, 2, 3, 4, 5]],
    ]) {
      const payload = toMedicationSchedulePayload({ enabled: true, time: '09:00',
        medicationName: 'local fixture', weekdays: days })
      check(JSON.stringify(payload.weekdays) === JSON.stringify(days), 'GUARDIAN_WEEKDAY_PAYLOAD_CHANGED')
      const document = { schemaVersion: 1, timezone: 'Asia/Seoul', revision: `fixture_${label}`,
        updatedAt: STAMP, updatedBy: 'guardian_dashboard', updatedUid: guardian.uid,
        entries: { fixture_schedule: payload } }
      const before = await allow('GET', schedulePath, undefined, guardian,
        { headers: { 'X-Firebase-ETag': 'true' } })
      check(typeof before.etag === 'string' && before.etag.length > 0, 'SCHEDULE_ETAG_MISSING')
      await allow('PUT', schedulePath, document, guardian,
        { headers: { 'If-Match': before.etag } })
      const stored = await read(schedulePath, guardian)
      check(JSON.stringify(stored.entries.fixture_schedule.weekdays) === JSON.stringify(days),
        'SCHEDULE_WEEKDAYS_NOT_STORED')
    }
  })
  await test('Schedule weekday types, bounds, and guardian scope stay restricted', async () => {
    const schedulePath = `medicationSchedules/${ELDER}/${DEVICE}`
    const valid = await read(schedulePath, guardian)
    for (const days of [[0], [8], ['7'], [true], [1.5]]) {
      await deny('PUT', schedulePath, { ...valid, entries: {
        fixture_schedule: { ...valid.entries.fixture_schedule, weekdays: days },
      } }, guardian)
    }
    for (const actor of [device, stranger, other, foreignGuardian, wrongDevice, disabled, null]) {
      await deny('PUT', schedulePath, valid, actor)
    }
    await deny('PUT', `medicationSchedules/${ELDER}/device_wrong`, valid, guardian)
    await deny('PUT', `medicationSchedules/elder_test_b/${DEVICE}`, valid, guardian)
  })
  await test('Actual ConversationStore root multipath PATCH allowed at owned leaves', async () => {
    await worker('conversation', device)
    const session = await read(`conversations/${ELDER}/auth_fixture_session`, guardian)
    check(session.messages.auth_fixture_message.role === 'user', 'CONVERSATION_NOT_STORED')
    await deny('PATCH', '', { [`devices/device_wrong/lastSeen`]: 100 }, device)
    await deny('PATCH', '', { [`devices/${DEVICE}/lastSeen`]: 100,
      [`accessBindings/${device.uid}/role`]: 'guardian' }, device)
  })
  await test('Guardian command create; actual device ETag claim/complete', async () => {
    const command = { commandId: 'auth_command', elderId: ELDER, deviceId: DEVICE,
      type: 'start_ai_conversation', status: 'pending', createdAt: 100, requestedUid: guardian.uid }
    await allow('PUT', `commands/${ELDER}/${DEVICE}/auth_command`, command, guardian)
    await deny('PATCH', `commands/${ELDER}/${DEVICE}/auth_command`, { status: 'processed' }, guardian)
    await worker('command', device)
    check((await read(`commands/${ELDER}/${DEVICE}/auth_command`, guardian)).status === 'processed', 'COMMAND_NOT_COMPLETED')
    await deny('DELETE', `commands/${ELDER}/${DEVICE}/auth_command`, undefined, device)
  })
  await test('Actual MemoryStore immutable body save and control CAS', async () => {
    await worker('save', device)
    const control = await read(`memories/${ELDER}/_control`, guardian)
    check(Object.keys(control.activeIds ?? {}).length === 1 && control.generation === 'initial', 'MEMORY_SAVE_MISSING')
    const id = Object.keys(control.activeIds)[0]
    const body = await read(`memories/${ELDER}/active/${id}`, guardian)
    await deny('PUT', `memories/${ELDER}/active/${id}`, { ...body, content: 'forged' }, guardian)
    await deny('DELETE', `memories/${ELDER}/active/${id}`, undefined, device)
    await deny('PATCH', `memories/${ELDER}/_control`, { generation: 999 }, guardian)
    await deny('PATCH', `memories/${ELDER}/_control`, { generation: 999 }, device)
  })
  await test('ETag stale write returns 412 without generation mutation', async () => {
    const path = `memories/${ELDER}/_control`
    const old = await allow('GET', path, undefined, device, { headers: { 'X-Firebase-ETag': 'true' } })
    check(typeof old.etag === 'string', 'ETAG_MISSING')
    await allow('PUT', path, { ...old.data, maintenanceRevision: 'first' }, device, { headers: { 'if-match': old.etag } })
    const conflict = await db('PUT', path, { ...old.data, maintenanceRevision: 'stale' }, device, { headers: { 'if-match': old.etag } })
    check(conflict.status === 412, 'STALE_ETAG_NOT_412')
    check((await read(path, device)).maintenanceRevision === 'first', 'ETAG_CONFLICT_MUTATED')
  })
  await test('Guardian reset field/UID/device spoof and device creation rejected', async () => {
    const body = requested(guardian.uid)
    for (const [suffix, patch] of [['completed', { status: 'completed', deviceStatus: `${DEVICE}|completed` }],
      ['generation', { generation: 42 }], ['role', { role: 'device' }],
      ['uid', { requestedUid: stranger.uid }], ['device', { deviceId: 'device_wrong', deviceStatus: 'device_wrong|requested' }]]) {
      await deny('PUT', `memoryResetOperations/${ELDER}/bad_${suffix}`, { ...body, ...patch }, guardian)
    }
    await deny('PUT', `memoryResetOperations/${ELDER}/device_created`, body, device)
  })
  await test('Real Guardian protocol requests two coalesced operations; no premature SUCCESS', async () => {
    const [first, second] = await Promise.all([request('reset_A', guardian), request('reset_B', guardian)])
    check(first.outcome === 'WAITING_FOR_DEVICE', 'PREMATURE_SUCCESS_A')
    check(second.outcome === 'WAITING_FOR_DEVICE', 'PREMATURE_SUCCESS_B')
    await deny('PATCH', `memoryResetOperations/${ELDER}/reset_A`, { status: 'completed', generation: 1 }, guardian)
    await deny('PATCH', `memories/${ELDER}/_control`, { resetCoordinator: { state: 'completed' } }, guardian)
    await deny('DELETE', `memoryResetOperations/${ELDER}/reset_A`, undefined, guardian)
    const immutable = await read(`memoryResetOperations/${ELDER}/reset_A`, device)
    const processing = { ...immutable, status: 'processing', deviceStatus: `${DEVICE}|processing`,
      processingAt: STAMP, baseGeneration: 'initial' }
    await deny('PUT', `memoryResetOperations/${ELDER}/reset_A`, {
      ...processing, intent: { ...immutable.intent, generation: 999 },
    }, device)
    await deny('PUT', `memoryResetOperations/${ELDER}/reset_A`, {
      ...processing, requestedAt: 42,
    }, device)
    await deny('PUT', `memoryResetOperations/${ELDER}/reset_A`, {
      ...immutable, status: 'completed', deviceStatus: `${DEVICE}|completed`, generation: 1,
    }, device)
  })
  await test('Real Pi reset, callback, archive and concurrent/coalesced intent', async () => {
    const done = await worker('reset', device)
    check(done.generation === 1 && done.activeCount === 0, 'RESET_GENERATION_OR_ACTIVE_INVALID')
    for (const id of ['reset_A', 'reset_B']) {
      const result = await request(id, guardian, { verifyOnly: true })
      check(result.outcome === 'SUCCESS' && result.generation === 1, 'RESET_NOT_AUTHORITATIVELY_COMPLETED')
    }
    const control = await read(`memories/${ELDER}/_control`, guardian)
    check(Object.keys(control.archiveIds ?? {}).length === 1, 'ARCHIVE_MISSING')
    const operations = await Promise.all(['reset_A', 'reset_B'].map(id => read(`memoryResetOperations/${ELDER}/${id}`, guardian)))
    check(operations.filter(operation => operation.coalesced === true).length === 1, 'FOLLOWER_NOT_COALESCED')
  })
  await test('UNKNOWN keeps same operation; verify never creates absent ID', async () => {
    const unknown = await runMemoryReset({ io: ioFor(guardian, 'reset_A', {
      readOperation: async () => { throw new Error('LOCAL_LOST_READ') },
    }), operationId: 'reset_A', deviceId: DEVICE, verifyOnly: true, maxWaitMs: 0 })
    check(unknown.outcome === 'UNKNOWN' && unknown.operationId === 'reset_A', 'UNKNOWN_ID_CHANGED')
    const absent = await request('never_create_on_verify', guardian, { verifyOnly: true })
    check(absent.outcome === 'UNKNOWN', 'ABSENT_VERIFY_NOT_UNKNOWN')
    check(await read(`memoryResetOperations/${ELDER}/never_create_on_verify`, guardian) === null, 'VERIFY_CREATED_OPERATION')
    check((await request('reset_A', guardian, { verifyOnly: true })).outcome === 'SUCCESS', 'UNKNOWN_RECONCILE_FAILED')
  })
  await test('ACK loss after actual completion recovers same ID without second reset', async () => {
    await worker('save', device)
    check((await request('reset_C', guardian)).outcome === 'WAITING_FOR_DEVICE', 'REQUEST_C_NOT_WAITING')
    const done = await worker('reset', device, { loseCompletion: true })
    check(done.lostCompletion && done.generation === 2, 'ACK_LOSS_RECOVERY_INVALID')
    check((await request('reset_C', guardian, { verifyOnly: true })).outcome === 'SUCCESS', 'ACK_RECONCILE_FAILED')
  })
  await test('Permanent leader lookup survives bounded receipt trimming', async () => {
    // Trim only old receipt with emulator-admin fixture, leaving the permanent
    // leader operation and current authoritative coordinator intact.
    const firstOps = await Promise.all(['reset_A', 'reset_B'].map(id => read(`memoryResetOperations/${ELDER}/${id}`, guardian)))
    const firstLeader = ['reset_A', 'reset_B'][firstOps.findIndex(operation => operation.coalesced === false)]
    check(firstLeader, 'FIRST_LEADER_MISSING')
    await allow('DELETE', `memories/${ELDER}/_control/resetOperations/${firstLeader}`, undefined, 'admin')
    await allow('PUT', `memoryResetOperations/${ELDER}/delayed_old_intent`,
      requested(guardian.uid, { version: 1, generation: 'initial', epoch: 0 }), guardian)
    const done = await worker('reset', device)
    check(done.generation === 2, 'DELAYED_INTENT_RESET_NEW_MEMORY')
    const old = await read(`memoryResetOperations/${ELDER}/delayed_old_intent`, guardian)
    check(old.status === 'completed' && old.coalesced === true && old.generation === 1,
      'PERMANENT_LEADER_NOT_USED')
  })
  await test('Terminal operation identity and trusted binding remain immutable', async () => {
    for (const actor of [guardian, device]) {
      await deny('PATCH', `memoryResetOperations/${ELDER}/reset_A`, { status: 'requested' }, actor)
      await deny('PATCH', `memoryResetOperations/${ELDER}/reset_A`, { requestedUid: stranger.uid }, actor)
      await deny('DELETE', `memoryResetOperations/${ELDER}/reset_A`, undefined, actor)
    }
  })
  await test('Actual Guardian Web SDK sign-in/binding/root denial/sign-out', async () => {
    const require = createRequire(resolve(ROOT, 'apps/guardian-dashboard/package.json'))
    const { initializeApp, deleteApp } = require('firebase/app')
    const { initializeAuth, inMemoryPersistence, connectAuthEmulator,
      signInWithEmailAndPassword, signOut, onAuthStateChanged } = require('firebase/auth')
    const { getDatabase, connectDatabaseEmulator, ref, get, runTransaction, goOffline } = require('firebase/database')
    const app = initializeApp({ projectId: PROJECT, apiKey: 'AIza' + 'a'.repeat(35),
      databaseURL: `https://${NAMESPACE}.firebaseio.com` }, `local-rules-sdk-${Date.now()}`)
    const auth = initializeAuth(app, { persistence: inMemoryPersistence })
    connectAuthEmulator(auth, AUTH, { disableWarnings: true })
    const database = getDatabase(app)
    connectDatabaseEmulator(database, '127.0.0.1', 9000)
    const seen = []
    const unsubscribe = onAuthStateChanged(auth, user => seen.push(user?.uid ?? null))
    const observe = expected => new Promise((resolveState, rejectState) => {
      let stop = () => {}
      const timeout = setTimeout(() => { stop(); rejectState(new Error('SDK_AUTH_CALLBACK_TIMEOUT')) }, 5000)
      stop = onAuthStateChanged(auth, user => {
        if ((user?.uid ?? null) === expected) {
          clearTimeout(timeout)
          stop()
          resolveState()
        }
      })
    })
    try {
      let wrongRejected = false
      try { await signInWithEmailAndPassword(auth, guardian.email, 'wrong-local-fixture-password') }
      catch (error) { wrongRejected = ['auth/wrong-password', 'auth/invalid-credential'].includes(error?.code) }
      check(wrongRejected && auth.currentUser === null, 'SDK_WRONG_PASSWORD_ACCEPTED')
      await signInWithEmailAndPassword(auth, guardian.email, guardian.password)
      await observe(guardian.uid)
      check(auth.currentUser?.uid === guardian.uid, 'SDK_SIGN_IN_IDENTITY_MISMATCH')
      const binding = (await get(ref(database, `accessBindings/${guardian.uid}`))).val()
      const scope = (await get(ref(database, `accessScopes/${ELDER}`))).val()
      check(binding?.role === 'guardian' && binding.elderId === ELDER
        && binding.deviceId === DEVICE && scope.projectId === PROJECT, 'SDK_BINDING_SCOPE_READ_FAILED')
      let rootDenied = false
      try { await get(ref(database)) }
      catch (error) { rootDenied = error?.code === 'PERMISSION_DENIED'
        || (typeof error?.message === 'string' && /^permission denied$/iu.test(error.message)) }
      check(rootDenied, 'SDK_ROOT_READ_ALLOWED')
      const scheduleRef = ref(database, `medicationSchedules/${ELDER}/${DEVICE}`)
      const transaction = await runTransaction(scheduleRef, current => ({
        ...current, revision: 'sdk_every_day', updatedAt: Date.now(),
        entries: { fixture_schedule: toMedicationSchedulePayload({ enabled: true,
          time: '09:00', medicationName: 'local fixture', weekdays: [1, 2, 3, 4, 5, 6, 7] }) },
      }), { applyLocally: false })
      check(transaction.committed && transaction.snapshot.val()?.entries?.fixture_schedule?.weekdays?.[6] === 7,
        'SDK_SCHEDULE_TRANSACTION_FAILED')
      await signOut(auth)
      await observe(null)
      check(auth.currentUser === null && seen.includes(guardian.uid) && seen.at(-1) === null,
        'SDK_SIGN_OUT_CALLBACK_FAILED')
    } finally {
      unsubscribe()
      goOffline(database)
      await deleteApp(app)
    }
  })
  console.log(JSON.stringify({ project: PROJECT, backend: 'loopback Auth + RTDB Emulator',
    passed, failed: 0, skipped: 0, results }, null, 2))
} catch (error) {
  // Only our short fixed diagnostic messages are safe to print.
  const code = typeof error?.message === 'string' && /^[A-Z0-9_]+$/u.test(error.message)
    ? error.message : 'LOCAL_EMULATOR_TEST_FAILURE'
  console.error(`FAIL ${current}: ${code}`)
  console.error(JSON.stringify({ passed, failed: 1, skipped: 0, failedTest: current, code }))
  process.exitCode = 1
}
