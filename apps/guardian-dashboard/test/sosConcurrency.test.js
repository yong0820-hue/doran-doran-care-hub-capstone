import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transformWithEsbuild } from 'vite'

const require = createRequire(import.meta.url)
const sourceRoot = fileURLToPath(new URL('../src/', import.meta.url))
const copy = (value) => structuredClone(value)

// Exercise the real service, while replacing every Firebase I/O edge. No
// Firebase app, socket, credential, timer, or production URL is initialized.
async function loadService(database) {
  const replacements = {
    'firebase/app': { getApps: () => [{}], initializeApp: () => ({}) },
    'firebase/database': database,
  }
  const cache = new Map()
  async function load(filename) {
    if (cache.has(filename)) return cache.get(filename)
    const source = await readFile(filename, 'utf8')
    const dependencies = new Map(Object.entries(replacements))
    for (const [, specifier] of source.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
      if (dependencies.has(specifier) || !specifier.startsWith('.')) continue
      const base = path.resolve(path.dirname(filename), specifier)
      let dependency
      for (const suffix of ['', '.js', '.jsx']) {
        try { dependency = await load(base + suffix); break }
        catch (error) { if (error.code !== 'ENOENT') throw error }
      }
      assert.ok(dependency, `Missing dependency: ${specifier}`)
      dependencies.set(specifier, dependency)
    }
    const transformed = await transformWithEsbuild(source, filename, {
      format: 'cjs', define: { 'import.meta.env': '{}' },
    })
    const module = { exports: {} }
    vm.runInNewContext(transformed.code, {
      module, exports: module.exports,
      require: (specifier) => dependencies.has(specifier)
        ? dependencies.get(specifier) : require(specifier),
      Date, structuredClone, console,
    }, { filename })
    cache.set(filename, module.exports)
    return module.exports
  }
  return load(path.join(sourceRoot, 'services/firebase.js'))
}

function put(root, target, value) {
  const segments = target.split('/').filter(Boolean)
  let parent = root
  for (const segment of segments.slice(0, -1)) parent = parent[segment] ??= {}
  if (value === null) delete parent[segments.at(-1)]
  else parent[segments.at(-1)] = copy(value)
}

function resolveTimestamps(value, timestamp) {
  if (value && typeof value === 'object') {
    if (value['.sv'] === 'timestamp') return timestamp
    if (Array.isArray(value)) return value.map((entry) => resolveTimestamps(entry, timestamp))
    return Object.fromEntries(Object.entries(value)
      .map(([key, entry]) => [key, resolveTimestamps(entry, timestamp)]))
  }
  return value
}

// Each client has separate SDK state but shares one versioned server. A write
// between read and commit invalidates the compare-and-set and reruns the real
// callback. A same-client update cancels its pending root transaction, matching
// the Firebase SDK's Error('set') behavior. This is a deterministic CAS model,
// not a replacement for an eventual RTDB emulator/integration check.
class LocalDatabase {
  constructor(initial) {
    this.root = copy(initial)
    this.revision = 0
    this.sequence = 0
    this.clock = 1800000000000
    this.history = []
  }

  commit(candidate) {
    this.root = resolveTimestamps(copy(candidate), ++this.clock)
    this.revision++
    this.history.push(copy(this.root))
  }

  patch(patch) {
    const next = copy(this.root ?? {})
    for (const [target, value] of Object.entries(patch)) put(next, target, value)
    this.commit(next)
  }

  async client(options = {}) {
    const server = this
    const stats = { transactions: 0, callbacks: 0, updates: 0, pushIds: 0, timestamps: 0 }
    let localUpdates = 0
    let cold = options.cold ?? false
    const failures = [...(options.failures ?? [])]
    const database = {
      getDatabase: () => ({}),
      ref: (_db, path) => ({ path: path ?? '' }),
      onValue: () => () => {},
      query: (reference) => reference,
      limitToLast: () => ({}),
      serverTimestamp: () => { stats.timestamps++; return { '.sv': 'timestamp' } },
      push: () => { stats.pushIds++; return { key: `local_event_${++server.sequence}` } },
      update: async (reference, patch) => {
        stats.updates++
        localUpdates++
        server.patch(Object.fromEntries(Object.entries(patch)
          .map(([key, value]) => [[reference.path, key].filter(Boolean).join('/'), value])))
      },
      runTransaction: async (reference, callback, transactionOptions) => {
        stats.transactions++
        assert.equal(reference.path, '', 'events and devices require the shared root boundary')
        assert.equal(transactionOptions?.applyLocally, false, 'uncommitted state must stay hidden')
        const startLocalUpdates = localUpdates
        for (let attempt = 0; attempt < 30; attempt++) {
          const revision = cold && server.root != null ? -1 : server.revision
          const input = cold ? null : copy(server.root)
          cold = false
          stats.callbacks++
          const candidate = callback(input)
          if (candidate === undefined) {
            return { committed: false, snapshot: { val: () => copy(server.root) } }
          }
          await options.beforeCommit?.({ server, database, stats, attempt, candidate: copy(candidate) })
          const failure = failures.shift()
          if (failure === 'not_committed') {
            return { committed: false, snapshot: { val: () => copy(server.root) } }
          }
          if (failure) throw failure
          if (localUpdates !== startLocalUpdates) throw new Error('set')
          if (server.revision !== revision) continue
          server.commit(candidate)
          if (options.loseAcknowledgement) {
            options.loseAcknowledgement = false
            throw new Error('simulated lost acknowledgement')
          }
          return { committed: true, snapshot: { val: () => copy(server.root) } }
        }
        throw new Error('local CAS retry limit')
      },
    }
    return { api: await loadService(database), stats, database }
  }
}

const alert = (overrides = {}) => ({
  type: 'emergency_call', status: 'open', deviceId: 'device_001',
  elderId: 'elder_001', createdAt: 1000, title: 'SOS',
  meta: { retained: 'original metadata' }, ...overrides,
})

function initial(events = { first: alert() }) {
  return {
    events: { elder_001: copy(events) },
    devices: {
      device_001: {
        name: 'original device', emergencyStatus: 'active', lastSeen: 800,
        sensors: { radar: { presence: true, fallRisk: 'low' }, pillbox: { open: false } },
      },
      device_002: { emergencyStatus: 'active', sensors: { radar: { fallRisk: 'high' } } },
    },
    users: { elder_001: { name: 'retained user' } },
    conversations: { untouched: { messages: { message_1: { text: 'retained message' } } } },
    commands: { untouched: { pending: true } },
  }
}

function assertProjection(root, deviceId = 'device_001') {
  const pending = Object.values(root.events ?? {}).flatMap((events) => Object.values(events ?? {}))
    .filter((event) => event.deviceId === deviceId && ['open', 'unread'].includes(event.status))
  const critical = pending.some((event) => ['emergency_call', 'fall_suspected'].includes(event.type))
  const falls = pending.some((event) => event.type === 'fall_suspected')
  assert.equal(root.devices[deviceId].emergencyStatus, critical ? 'active' : 'normal')
  assert.equal(root.devices[deviceId].sensors.radar.fallRisk, falls ? 'high' : 'low')
}

test('SOS arriving after resolve read invalidates the stale clear and remains active', async () => {
  const server = new LocalDatabase(initial())
  let injected = false
  const client = await server.client({
    beforeCommit: () => {
      if (injected) return
      injected = true
      // Exact ESP32 write shape: new open event and active flag are atomic.
      server.patch({
        'events/elder_001/arrived': alert({ createdAt: 2000 }),
        'devices/device_001/emergencyStatus': 'active',
        'devices/device_001/lastSeen': 2000,
      })
    },
  })
  await client.api.resolveEventWithDevicePatch('elder_001', 'first', 'emergency_call', 'device_001', true)
  assert.equal(client.stats.callbacks, 2)
  assert.equal(server.root.events.elder_001.first.status, 'resolved')
  assert.equal(server.root.events.elder_001.arrived.status, 'open')
  assert.equal(server.root.devices.device_001.lastSeen, 2000)
  for (const committed of server.history) assertProjection(committed)
})

test('two guardians resolving different SOS events converge to normal after the final resolution', async () => {
  const server = new LocalDatabase(initial({ first: alert(), second: alert({ createdAt: 1100 }) }))
  let waiting = 0
  let release
  const bothRead = new Promise((resolve) => { release = resolve })
  const beforeCommit = async ({ attempt }) => {
    if (attempt > 0) return
    if (++waiting === 2) release()
    await bothRead
  }
  const guardianA = await server.client({ beforeCommit })
  const guardianB = await server.client({ beforeCommit })
  await Promise.all([
    guardianA.api.resolveEventWithDevicePatch('elder_001', 'first', 'emergency_call', 'device_001', false),
    guardianB.api.resolveEventWithDevicePatch('elder_001', 'second', 'emergency_call', 'device_001', false),
  ])
  assert.equal(guardianA.stats.callbacks + guardianB.stats.callbacks, 3)
  assert.ok(Object.values(server.root.events.elder_001).every((event) => event.status === 'resolved'))
  assert.equal(server.root.devices.device_001.emergencyStatus, 'normal')
  for (const committed of server.history) assertProjection(committed)
})

test('new SOS overlapping both guardians resolving old alerts survives every committed transition', async () => {
  const server = new LocalDatabase(initial({ first: alert(), second: alert({ createdAt: 1100 }) }))
  let waiting = 0
  let release
  const bothRead = new Promise((resolve) => { release = resolve })
  const beforeCommit = async ({ attempt }) => {
    if (attempt > 0) return
    if (++waiting === 2) {
      // Both callbacks have already calculated candidates from the old events.
      server.patch({
        'events/elder_001/arrived': alert({ createdAt: 3000 }),
        'devices/device_001/emergencyStatus': 'active',
      })
      release()
    }
    await bothRead
  }
  const guardianA = await server.client({ beforeCommit })
  const guardianB = await server.client({ beforeCommit })
  await Promise.all([
    guardianA.api.resolveEventWithDevicePatch('elder_001', 'first'),
    guardianB.api.resolveEventWithDevicePatch('elder_001', 'second'),
  ])
  assert.ok(guardianA.stats.callbacks >= 2)
  assert.ok(guardianB.stats.callbacks >= 2)
  assert.equal(server.root.events.elder_001.first.status, 'resolved')
  assert.equal(server.root.events.elder_001.second.status, 'resolved')
  assert.equal(server.root.events.elder_001.arrived.status, 'open')
  assert.equal(server.root.devices.device_001.emergencyStatus, 'active')
  for (const committed of server.history) assertProjection(committed)
})

test('concurrent duplicate resolution is idempotent and retains the first resolvedAt', async () => {
  const server = new LocalDatabase(initial())
  const first = await server.client()
  const second = await server.client()
  await Promise.all([
    first.api.resolveEventWithDevicePatch('elder_001', 'first'),
    second.api.resolveEventWithDevicePatch('elder_001', 'first'),
  ])
  const firstResolvedAt = server.history[0].events.elder_001.first.resolvedAt
  assert.equal(typeof firstResolvedAt, 'number')
  assert.equal(server.root.events.elder_001.first.resolvedAt, firstResolvedAt)
  assert.deepEqual(server.root.events.elder_001.first.meta, { retained: 'original metadata' })
  await second.api.resolveEventWithDevicePatch('elder_001', 'first')
  assert.equal(server.root.events.elder_001.first.resolvedAt, firstResolvedAt)
  assertProjection(server.root)
})

test('mixed SOS and unread fall events keep emergency active until both are resolved', async () => {
  const server = new LocalDatabase(initial({
    first: alert(),
    fall: alert({ type: 'fall_suspected', status: 'unread', createdAt: 1200 }),
  }))
  server.root.devices.device_001.sensors.radar.fallRisk = 'high'
  const client = await server.client()
  await client.api.resolveEventWithDevicePatch('elder_001', 'first')
  assert.equal(server.root.devices.device_001.emergencyStatus, 'active')
  assert.equal(server.root.devices.device_001.sensors.radar.fallRisk, 'high')
  assertProjection(server.root)
  await client.api.resolveEventWithDevicePatch('elder_001', 'fall')
  assertProjection(server.root)
})

test('resolving one fall preserves a second fall; resolving the final fall preserves a separate SOS', async () => {
  const server = new LocalDatabase(initial({
    first: alert(), fallA: alert({ type: 'fall_suspected' }), fallB: alert({ type: 'fall_suspected' }),
  }))
  server.root.devices.device_001.sensors.radar.fallRisk = 'high'
  const client = await server.client()
  await client.api.resolveEventWithDevicePatch('elder_001', 'fallA')
  assertProjection(server.root)
  await client.api.resolveEventWithDevicePatch('elder_001', 'fallB')
  assert.equal(server.root.devices.device_001.emergencyStatus, 'active')
  assert.equal(server.root.devices.device_001.sensors.radar.fallRisk, 'low')
  assertProjection(server.root)
})

test('stored device and type override stale UI hints, with unrelated state preserved', async () => {
  const seed = initial({ first: alert(), other: alert({ deviceId: 'device_002' }) })
  const server = new LocalDatabase(seed)
  const client = await server.client()
  await client.api.resolveEventWithDevicePatch('elder_001', 'first', 'presence_detected', 'device_002', false)
  assertProjection(server.root)
  assert.deepEqual(server.root.devices.device_002, seed.devices.device_002)
  assert.deepEqual(server.root.events.elder_001.other, seed.events.elder_001.other)
  for (const key of ['users', 'conversations', 'commands']) assert.deepEqual(server.root[key], seed[key])
  assert.equal(server.root.devices.device_001.name, seed.devices.device_001.name)
  assert.equal(server.root.devices.device_001.lastSeen, 800)
  assert.equal(server.root.devices.device_001.sensors.radar.presence, true)
  assert.deepEqual(server.root.devices.device_001.sensors.pillbox, { open: false })
})

test('the same explicit device in another elder bucket also keeps emergency active', async () => {
  const seed = initial()
  seed.events.elder_002 = { elsewhere: alert({ elderId: 'elder_002', status: 'unread' }) }
  const server = new LocalDatabase(seed)
  const client = await server.client()
  await client.api.resolveEventWithDevicePatch('elder_001', 'first')
  assert.equal(server.root.devices.device_001.emergencyStatus, 'active')
  assertProjection(server.root)
  await client.api.resolveEventWithDevicePatch('elder_002', 'elsewhere')
  assertProjection(server.root)
})

test('a legacy event without deviceId resolves without borrowing the dashboard device', async () => {
  const legacy = alert()
  delete legacy.deviceId
  const seed = initial({ legacy })
  const server = new LocalDatabase(seed)
  const client = await server.client()
  await client.api.resolveEventWithDevicePatch('elder_001', 'legacy', 'emergency_call', 'device_001', true)
  assert.equal(server.root.events.elder_001.legacy.status, 'resolved')
  assert.deepEqual(server.root.devices, seed.devices)
})

test('missing and nonactionable events reject without creating phantom events or changing devices', async () => {
  for (const target of ['missing', 'unknown']) {
    const seed = initial({ unknown: alert({ status: 'processing' }) })
    const server = new LocalDatabase(seed)
    const client = await server.client()
    await assert.rejects(client.api.resolveEventWithDevicePatch('elder_001', target))
    assert.deepEqual(server.root, seed)
  }
})

test('a cold local cache retries against the existing server instead of aborting resolution', async () => {
  const server = new LocalDatabase(initial())
  const client = await server.client({ cold: true })
  await client.api.resolveEventWithDevicePatch('elder_001', 'first')
  assert.equal(client.stats.callbacks, 2)
  assert.equal(client.stats.timestamps, 1)
  assert.equal(server.root.events.elder_001.first.status, 'resolved')
  assertProjection(server.root)
})

test('an event ID replaced during resolve retry is not mistaken for the originally selected event', async () => {
  for (const changed of [
    { deviceId: 'device_002' }, { type: 'fall_suspected' },
    { elderId: 'elder_002' }, { createdAt: 9000 },
  ]) {
    const seed = initial()
    const replacement = alert(changed)
    const server = new LocalDatabase(seed)
    let replaced = false
    const client = await server.client({
      beforeCommit: () => {
        if (replaced) return
        replaced = true
        server.patch({ 'events/elder_001/first': replacement })
      },
    })
    await assert.rejects(client.api.resolveEventWithDevicePatch('elder_001', 'first'))
    assert.equal(client.stats.callbacks, 2)
    assert.deepEqual(server.root.events.elder_001.first, replacement)
    assert.deepEqual(server.root.devices, seed.devices)
  }
})

test('critical creation uses one stable ID through conflict retries and derives both SOS and fall flags', async () => {
  const server = new LocalDatabase(initial({}))
  server.root.devices.device_001.emergencyStatus = 'normal'
  let injected = false
  const client = await server.client({
    beforeCommit: () => {
      if (injected) return
      injected = true
      server.patch({ 'users/elder_001/name': 'concurrent user update' })
    },
  })
  const id = await client.api.pushEvent('elder_001', alert({ type: 'fall_suspected' }), {
    'sensors/radar/fallRisk': 'high', emergencyStatus: 'normal', lastSeen: 1000,
  })
  assert.equal(client.stats.pushIds, 1)
  assert.equal(client.stats.callbacks, 2)
  assert.equal(client.stats.updates, 0)
  assert.equal(Object.keys(server.root.events.elder_001).length, 1)
  assert.equal(server.root.events.elder_001[id].type, 'fall_suspected')
  assert.equal(server.root.users.elder_001.name, 'concurrent user update')
  assert.equal(server.root.devices.device_001.lastSeen, 1000)
  assertProjection(server.root)
})

test('creation retries keep the original event and device patch despite caller object mutation', async () => {
  const event = alert({ type: 'fall_suspected' })
  const devicePatch = { lastSeen: 1000, 'sensors/radar/presence': false, localMetadata: { value: 'captured' } }
  const expectedEvent = copy(event)
  const server = new LocalDatabase(initial({}))
  server.root.devices.device_001.emergencyStatus = 'normal'
  let mutated = false
  const client = await server.client({
    beforeCommit: () => {
      if (mutated) return
      mutated = true
      Object.assign(event, { type: 'emergency_call', deviceId: 'device_002', elderId: 'elder_002', createdAt: 9000 })
      event.meta.retained = 'changed after request'
      devicePatch.lastSeen = 9000
      devicePatch['sensors/radar/presence'] = true
      devicePatch.localMetadata.value = 'changed after request'
      server.patch({ 'users/elder_001/name': 'concurrent update' })
    },
  })
  const eventId = await client.api.pushEvent('elder_001', event, devicePatch)
  assert.equal(client.stats.callbacks, 2)
  assert.deepEqual(server.root.events.elder_001[eventId], expectedEvent)
  assert.equal(server.root.devices.device_001.lastSeen, 1000)
  assert.equal(server.root.devices.device_001.sensors.radar.presence, false)
  assert.deepEqual(server.root.devices.device_001.localMetadata, { value: 'captured' })
  assert.deepEqual(server.root.devices.device_002, initial().devices.device_002)
  assertProjection(server.root)
})

test('critical create arriving after the final resolution restores active atomically', async () => {
  const server = new LocalDatabase(initial())
  const guardian = await server.client()
  let resolved = false
  const sender = await server.client({
    beforeCommit: async () => {
      if (resolved) return
      resolved = true
      await guardian.api.resolveEventWithDevicePatch('elder_001', 'first')
    },
  })
  const eventId = await sender.api.pushEvent('elder_001', alert({ createdAt: 2000 }))
  assert.equal(sender.stats.callbacks, 2)
  assert.equal(server.root.events.elder_001.first.status, 'resolved')
  assert.equal(server.root.events.elder_001[eventId].status, 'open')
  for (const committed of server.history) assertProjection(committed)
})

test('retry after a lost create acknowledgement never reopens the subsequently resolved event or replays patches', async () => {
  const server = new LocalDatabase(initial({}))
  server.root.devices.device_001.emergencyStatus = 'normal'
  const event = alert({ createdAt: 2000 })
  const sender = await server.client({ loseAcknowledgement: true })
  await assert.rejects(sender.api.pushEvent('elder_001', event, { lastSeen: 2000 }, 'stable_sos'), /lost acknowledgement/)
  assert.equal(server.root.events.elder_001.stable_sos.status, 'open')
  const guardian = await server.client()
  await guardian.api.resolveEventWithDevicePatch('elder_001', 'stable_sos')
  const resolvedAt = server.root.events.elder_001.stable_sos.resolvedAt
  server.patch({ 'devices/device_001/lastSeen': 9000 })
  const id = await sender.api.pushEvent('elder_001', event, { lastSeen: 2000 }, 'stable_sos')
  assert.equal(id, 'stable_sos')
  assert.equal(server.root.events.elder_001.stable_sos.status, 'resolved')
  assert.equal(server.root.events.elder_001.stable_sos.resolvedAt, resolvedAt)
  assert.equal(server.root.devices.device_001.lastSeen, 9000)
  assert.equal(Object.keys(server.root.events.elder_001).length, 1)
  assertProjection(server.root)
})

test('matching critical creation retry repairs derived status without replacing existing metadata', async () => {
  const event = alert()
  const server = new LocalDatabase(initial({ stable: event }))
  server.root.devices.device_001.emergencyStatus = 'normal'
  server.root.events.elder_001.stable.meta.savedByGuardian = true
  const client = await server.client()
  await client.api.pushEvent('elder_001', event, { lastSeen: 999 }, 'stable')
  assert.equal(server.root.events.elder_001.stable.meta.savedByGuardian, true)
  assert.equal(server.root.devices.device_001.lastSeen, 800)
  assertProjection(server.root)
})

test('startup reconciliation repairs persisted drift once and preserves stored records', async () => {
  const seed = initial({
    fallA: alert({ type: 'fall_suspected' }),
    fallB: alert({ type: 'fall_suspected', createdAt: 1100 }),
  })
  seed.devices.device_001.emergencyStatus = 'normal'
  seed.devices.device_001.sensors.radar.fallRisk = 'low'
  const server = new LocalDatabase(seed)
  const client = await server.client({ cold: true })

  assert.equal(await client.api.reconcileEmergencyDeviceState('device_001'), true)
  assert.equal(client.stats.callbacks, 2)
  assertProjection(server.root)
  assert.deepEqual(server.root.events, seed.events)
  for (const key of ['users', 'conversations', 'commands']) assert.deepEqual(server.root[key], seed[key])
  assert.equal(server.root.devices.device_001.name, seed.devices.device_001.name)
  assert.equal(server.root.devices.device_001.lastSeen, seed.devices.device_001.lastSeen)

  const historyLength = server.history.length
  assert.equal(await client.api.reconcileEmergencyDeviceState('device_001'), false)
  assert.equal(server.history.length, historyLength)
})

test('reused critical event IDs with conflicting immutable identity reject without overwriting', async () => {
  for (const conflicting of [
    { type: 'fall_suspected' }, { deviceId: 'device_002' },
    { elderId: 'elder_002' }, { createdAt: 9999 },
  ]) {
    const seed = initial({ stable: alert() })
    const server = new LocalDatabase(seed)
    const client = await server.client()
    await assert.rejects(client.api.pushEvent('elder_001', alert(conflicting), { lastSeen: 999 }, 'stable'))
    assert.deepEqual(server.root, seed)
  }
})

test('same-client update cancellation retries the root transaction using the new state', async () => {
  const server = new LocalDatabase(initial())
  let updated = false
  const client = await server.client({
    beforeCommit: async ({ database }) => {
      if (updated) return
      updated = true
      await database.update({ path: '' }, { 'devices/device_001/lastSeen': 7777 })
    },
  })
  await client.api.resolveEventWithDevicePatch('elder_001', 'first')
  assert.equal(client.stats.transactions, 2)
  assert.equal(server.root.devices.device_001.lastSeen, 7777)
  assertProjection(server.root)
})

test('transaction cancellation retries are bounded and leave both records unchanged on exhaustion', async () => {
  const seed = initial()
  const server = new LocalDatabase(seed)
  const client = await server.client({ failures: Array.from({ length: 3 }, () => new Error('set')) })
  await assert.rejects(client.api.resolveEventWithDevicePatch('elder_001', 'first'))
  assert.equal(client.stats.transactions, 3)
  assert.equal(server.history.length, 0)
  assert.deepEqual(server.root, seed)
})

test('permission denial and uncommitted transactions propagate without partial resolve or creation', async () => {
  for (const operation of ['resolve', 'create']) {
    for (const failure of [new Error('PERMISSION_DENIED'), 'not_committed']) {
      const seed = initial()
      const server = new LocalDatabase(seed)
      const client = await server.client({ failures: [failure] })
      const request = operation === 'resolve'
        ? client.api.resolveEventWithDevicePatch('elder_001', 'first')
        : client.api.pushEvent('elder_001', alert({ createdAt: 2000 }), {}, 'new_sos')
      await assert.rejects(request)
      assert.equal(client.stats.transactions, 1)
      assert.equal(server.history.length, 0)
      assert.deepEqual(server.root, seed)
    }
  }
})

test('noncritical demo events retain one atomic multi-location update without touching SOS state', async () => {
  const server = new LocalDatabase(initial())
  const client = await server.client()
  await client.api.pushEvent('elder_001', alert({ type: 'presence_detected', status: 'unread' }), {
    'sensors/radar/presence': false,
  })
  assert.equal(client.stats.transactions, 0)
  assert.equal(client.stats.updates, 1)
  assert.equal(server.history.length, 1)
  assert.equal(Object.keys(server.root.events.elder_001).length, 2)
  assert.equal(server.root.devices.device_001.sensors.radar.presence, false)
  assert.equal(server.root.devices.device_001.emergencyStatus, 'active')
  assert.equal(server.root.events.elder_001.first.status, 'open')
})
