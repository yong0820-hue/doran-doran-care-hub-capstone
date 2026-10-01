import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transformWithEsbuild } from 'vite'
import { summarizeEvents, isKnownEventStatus } from '../src/dashboardState.js'
import { EVENT_TEMPLATES } from '../src/data/eventTemplates.js'

const require = createRequire(import.meta.url)
const root = fileURLToPath(new URL('../src/', import.meta.url))

// Load the actual hook/components with deterministic React state and I/O adapters.
// Vite is already a project dependency; no browser or Firebase connection is used.
async function loadSource(relative, replacements = {}, cache = new Map()) {
  const filename = path.resolve(root, relative)
  if (cache.has(filename)) return cache.get(filename)
  const source = await readFile(filename, 'utf8')
  const dependencies = new Map(Object.entries(replacements))
  for (const [, specifier] of source.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
    if (dependencies.has(specifier) || !specifier.startsWith('.')) continue
    const base = path.resolve(path.dirname(filename), specifier)
    let dependency
    for (const suffix of ['', '.js', '.jsx']) {
      try { dependency = await loadSource(base + suffix, replacements, cache); break }
      catch (error) { if (error.code !== 'ENOENT') throw error }
    }
    assert.ok(dependency, `Missing dependency: ${specifier}`)
    dependencies.set(specifier, dependency)
  }
  const transformed = await transformWithEsbuild(source, filename, {
    format: 'cjs', jsx: 'automatic', define: { 'import.meta.env': '{}' },
  })
  const module = { exports: {} }
  vm.runInNewContext(transformed.code, {
    module, exports: module.exports,
    require: (specifier) => dependencies.has(specifier) ? dependencies.get(specifier) : require(specifier),
    console: { ...console, error: () => {} }, Date, structuredClone,
    setTimeout: () => 1, clearTimeout: () => {},
  }, { filename })
  cache.set(filename, module.exports)
  return module.exports
}

function hookState() {
  const values = []
  let cursor = 0
  let effects = []
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in values)) values[index] = typeof initial === 'function' ? initial() : initial
      return [values[index], (next) => { values[index] = typeof next === 'function' ? next(values[index]) : next }]
    },
    useRef(initial) {
      const [value] = hooks.useState(() => ({ current: initial }))
      return value
    },
    useEffect(effect, dependencies) {
      const index = cursor++
      if (!(index in values) || dependencies.some((value, i) => value !== values[index][i])) {
        values[index] = dependencies
        effects.push(effect)
      }
    },
  }
  return {
    hooks,
    render(fn) { cursor = 0; return fn() },
    flushEffects() { const pending = effects; effects = []; pending.forEach((effect) => effect()) },
  }
}

function flatten(node) {
  if (node == null || typeof node === 'boolean') return []
  if (Array.isArray(node)) return node.flatMap(flatten)
  return [node, ...flatten(node?.props?.children)]
}

function textOf(tree) { return flatten(tree).filter((node) => typeof node === 'string').join(' ') }
function buttons(tree) { return flatten(tree).filter((node) => node?.type === 'button') }

async function dashboardHarness({ live = false, events = [], device = { emergencyStatus: 'active' } } = {}) {
  const state = hookState()
  const calls = []
  const reconcileCalls = []
  const services = {
    isFirebaseConfigured: () => live,
    subscribeToElder: (_, callback) => { callback({}); return () => {} },
    subscribeToDevice: (_, callback) => { callback(device); return () => {} },
    subscribeToEvents: (_, callback) => { callback(events); return () => {} },
    subscribeToCommands: (_elder, _device, callback) => { callback([]); return () => {} },
    subscribeToMedicationSchedule: (_elder, _device, callback) => { callback(null); return () => {} },
    subscribeToConnection: (callback) => { callback(true); return () => {} },
    reconcileEmergencyDeviceState: async (...args) => { reconcileCalls.push(args) },
    resolveEventWithDevicePatch: async (...args) => { calls.push(args) },
    pushEvent: async (...args) => { calls.push(args) },
  }
  const { useDashboardData } = await loadSource('hooks/useDashboardData.js', {
    react: state.hooks, '../services/firebase': services,
  })
  const render = () => {
    state.render(() => useDashboardData('elder_001', 'device_001'))
    state.flushEffects()
    return state.render(() => useDashboardData('elder_001', 'device_001'))
  }
  render()
  state.flushEffects()
  return { render, calls, reconcileCalls, services }
}

test('open and firmware unread remain actionable; unknown states do not inflate counts', () => {
  const summary = summarizeEvents([
    { id: 'sos', status: 'open' }, { id: 'presence', status: 'unread' },
    { id: 'done', status: 'resolved' }, { id: 'bad', status: 'processing' },
  ])
  assert.equal(summary.openCount, 1)
  assert.equal(summary.unreadCount, 1)
  assert.equal(summary.unresolvedCount, 2)
  assert.equal(summary.unknownCount, 1)
  assert.equal(isKnownEventStatus('unread'), true)
})

test('SOS acknowledgement updates its own device and preserves other open SOS events', async () => {
  const other = { id: 'other', type: 'emergency_call', status: 'open', deviceId: 'device_002' }
  const current = { id: 'current', type: 'emergency_call', status: 'open', deviceId: 'device_001' }
  const second = { ...current, id: 'second' }
  const presence = { id: 'presence', type: 'presence_detected', status: 'unread', deviceId: 'device_001' }
  const harness = await dashboardHarness({ live: true, events: [other, current, second, presence] })
  const dashboard = harness.render()
  dashboard.handleResolve('other')
  dashboard.handleResolve('current')
  dashboard.handleResolve('presence')
  dashboard.handleResolve('unknown')
  assert.equal(harness.calls.length, 3)
  assert.deepEqual(Array.from(harness.calls[0]), ['elder_001', 'other', 'emergency_call', 'device_002'])
  assert.equal(harness.calls[1][3], 'device_001')
  assert.equal(harness.calls[2][1], 'presence')
})

test('live startup requests one transactional emergency-state reconciliation', async () => {
  const fall = { id: 'fall', type: 'fall_suspected', status: 'open', deviceId: 'device_001' }
  const harness = await dashboardHarness({
    live: true,
    events: [fall],
    device: { emergencyStatus: 'normal', sensors: { radar: { fallRisk: 'high' } } },
  })
  await new Promise((resolve) => setImmediate(resolve))
  harness.render()
  assert.deepEqual(harness.reconcileCalls.map((args) => Array.from(args)), [['device_001']])
})

test('same-timestamp mock SOS events have distinct IDs and clear state only after the last resolution', async () => {
  const harness = await dashboardHarness()
  const sos = EVENT_TEMPLATES.find((event) => event.type === 'emergency_call')
  const originalNow = Date.now
  Date.now = () => 1234567
  try {
    await harness.render().handleSendEvent(sos)
    await harness.render().handleSendEvent(sos)
  } finally { Date.now = originalNow }
  let dashboard = harness.render()
  const generated = dashboard.events.filter((event) => event.meta?.source === 'guardian_dashboard_demo')
  assert.equal(generated.length, 2)
  assert.notEqual(generated[0].id, generated[1].id)
  assert.match(generated[0].title, /모의/)
  assert.equal(dashboard.device.emergencyStatus, 'active')
  dashboard.handleResolve(generated[0].id)
  dashboard = harness.render()
  assert.equal(dashboard.device.emergencyStatus, 'active')
  dashboard.handleResolve(generated[1].id)
  // The initial mock data still contains an open fall for this device.
  assert.equal(harness.render().device.emergencyStatus, 'active')
  harness.render().handleResolve('evt_002')
  assert.equal(harness.render().device.emergencyStatus, 'normal')
})

test('long inactivity demo creates a unique open warning event with worker-eligible metadata', async () => {
  const template = EVENT_TEMPLATES.find((event) => event.type === 'long_inactivity')
  assert.deepEqual(template, {
    icon: '🕰️',
    type: 'long_inactivity',
    severity: 'warning',
    title: '장시간 미활동',
    message: '대시보드 데모 및 Pi 실물 검증을 위한 장시간 미활동 이벤트입니다.',
  })

  const harness = await dashboardHarness({
    live: true,
    device: { name: '도란도란 거실 라디오', location: '거실' },
  })
  const originalNow = Date.now
  Date.now = () => 1_800_000_000_123
  try {
    await harness.render().handleSendEvent(template)
  } finally { Date.now = originalNow }

  assert.equal(harness.calls.length, 1)
  const [elderId, event, devicePatch] = harness.calls[0]
  assert.equal(elderId, 'elder_001')
  assert.deepEqual({ ...event, meta: { ...event.meta } }, {
    type: 'long_inactivity',
    severity: 'warning',
    title: '[모의] 장시간 미활동',
    message: '시뮬레이션 기록: 대시보드 데모 및 Pi 실물 검증을 위한 장시간 미활동 이벤트입니다.',
    meta: { source: 'guardian_dashboard_demo' },
    status: 'open',
    deviceId: 'device_001',
    deviceName: '도란도란 거실 라디오',
    elderId: 'elder_001',
    location: '거실',
    createdAt: 1_800_000_000_123,
  })
  assert.equal('voiceCheck' in event, false)
  assert.equal('voiceCheck' in event.meta, false)
  assert.deepEqual(Object.keys(devicePatch), [])
})

test('noncritical Firebase event pushes allocate a fresh key without overwriting earlier events', async () => {
  const updates = []
  const keys = ['demo_inactivity_1', 'demo_inactivity_2']
  const db = {}
  const { pushEvent } = await loadSource('services/firebase.js', {
    'firebase/app': { getApps: () => [{}], initializeApp: () => ({}) },
    'firebase/database': {
      getDatabase: () => db,
      ref: (_db, path) => ({ path }),
      push: () => ({ key: keys.shift() }),
      update: async (ref, patch) => { updates.push({ ref, patch }) },
      runTransaction: () => { throw new Error('Warning events must not use the emergency transaction') },
    },
  })
  const event = {
    type: 'long_inactivity', severity: 'warning', status: 'open',
    elderId: 'elder_001', deviceId: 'device_001', createdAt: 1234,
  }

  const firstId = await pushEvent('elder_001', event)
  const secondId = await pushEvent('elder_001', event)

  assert.equal(firstId, 'demo_inactivity_1')
  assert.equal(secondId, 'demo_inactivity_2')
  assert.notEqual(firstId, secondId)
  assert.deepEqual(Object.keys(updates[0].patch), ['events/elder_001/demo_inactivity_1'])
  assert.deepEqual(Object.keys(updates[1].patch), ['events/elder_001/demo_inactivity_2'])
})

test('demo writer stores SOS and matching device state in one root transaction', async () => {
  const transactions = []
  let stored = null
  const db = {}
  const { pushEvent } = await loadSource('services/firebase.js', {
    'firebase/app': { getApps: () => [{}], initializeApp: () => ({}) },
    'firebase/database': {
      getDatabase: () => db, ref: (_db, path) => ({ path }),
      push: () => ({ key: 'demo_1' }),
      runTransaction: async (ref, mutate, options) => {
        transactions.push({ ref, options })
        stored = mutate(stored)
        return { committed: true, snapshot: { val: () => stored } }
      },
      update: () => { throw new Error('SOS must use a transaction') },
    },
  })
  const event = { type: 'emergency_call', status: 'open', deviceId: 'device_002' }
  await pushEvent('elder_001', event, { emergencyStatus: 'active' })
  assert.equal(transactions.length, 1)
  assert.equal(transactions[0].ref.path, undefined)
  assert.equal(transactions[0].options.applyLocally, false)
  assert.deepEqual(stored.events.elder_001.demo_1, event)
  assert.equal(stored.devices.device_002.emergencyStatus, 'active')
})

test('same-render mock resolutions and a new SOS preserve the remaining alert', async () => {
  const harness = await dashboardHarness()
  const sos = EVENT_TEMPLATES.find((event) => event.type === 'emergency_call')
  await harness.render().handleSendEvent(sos)
  await harness.render().handleSendEvent(sos)
  const dashboard = harness.render()
  const generated = dashboard.events.filter((event) => event.meta?.source === 'guardian_dashboard_demo')
  dashboard.handleResolve('evt_002')
  generated.forEach((event) => dashboard.handleResolve(event.id))
  await dashboard.handleSendEvent(sos)
  let current = harness.render()
  assert.equal(current.device.emergencyStatus, 'active')
  const remaining = current.events.filter((event) => event.type === 'emergency_call' && event.status === 'open')
  assert.equal(remaining.length, 1)
  current.handleResolve(remaining[0].id)
  current = harness.render()
  assert.equal(current.device.emergencyStatus, 'normal')
  assert.equal(current.device.sensors.radar.fallRisk, 'low')
})

test('live resolve failure remains visible and never clears local device state', async () => {
  const event = { id: 'sos', type: 'emergency_call', status: 'open', deviceId: 'device_001' }
  const harness = await dashboardHarness({ live: true, events: [event] })
  harness.services.resolveEventWithDevicePatch = async () => { throw new Error('permission_denied') }
  harness.render().handleResolve('sos')
  await new Promise((resolve) => setImmediate(resolve))
  const dashboard = harness.render()
  assert.match(dashboard.resolveError, /확인 완료 처리 실패/)
  assert.equal(dashboard.device.emergencyStatus, 'active')
  assert.equal(dashboard.events[0].status, 'open')
})

test('demo controls distinguish actual voice and simulated events, block duplicate sends, and show failures', async () => {
  const state = hookState()
  const { default: DemoControls } = await loadSource('components/DemoControls.jsx', { react: state.hooks })
  let rejectRequest
  let count = 0
  const props = {
    isLive: true, connectionState: 'connected',
    onSendEvent: () => { count++; return new Promise((_resolve, reject) => { rejectRequest = reject }) },
  }
  const render = () => state.render(() => DemoControls(props))
  let tree = render()
  assert.match(textOf(tree), /실제 Pi 음성 대화 시작/)
  assert.match(textOf(tree), /측정이나 음성 안내를 실행하지 않고/)
  const send = buttons(tree).find((button) => textOf(button).includes('장시간 미활동'))
  assert.ok(send)
  const pending = send.props.onClick()
  await send.props.onClick()
  assert.equal(count, 1)
  tree = render()
  assert.ok(buttons(tree).filter((button) => textOf(button).includes('모의 ·')).every((button) => button.props.disabled))
  assert.doesNotMatch(textOf(tree), /저장됨/)
  rejectRequest(new Error('offline write failure'))
  await pending
  assert.match(textOf(render()), /저장에 실패/)
  assert.doesNotMatch(textOf(render()), /\[모의\] 장시간 미활동 저장됨/)
  props.onSendEvent = async () => {}
  await buttons(render()).find((button) => textOf(button).includes('장시간 미활동')).props.onClick()
  assert.match(textOf(render()), /✓\s+\[모의\]\s+장시간 미활동\s+저장됨/)
  assert.doesNotMatch(textOf(render()), /저장에 실패/)
  props.connectionState = 'disconnected'
  assert.ok(buttons(render()).every((button) => button.props.disabled))
})

test('rendered timeline gives unread a completion button and counts only actionable events', async () => {
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const { default: EventTimeline } = await loadSource('components/EventTimeline.jsx')
  const html = renderToStaticMarkup(React.createElement(EventTimeline, {
    events: [
      { id: 'presence', type: 'presence_detected', status: 'unread', createdAt: 1000 },
      { id: 'unknown', type: 'long_inactivity', status: 'processing', createdAt: 500 },
    ], onResolve: () => {},
  }))
  assert.match(html, /<strong>1건<\/strong>/)
  assert.equal((html.match(/class="resolve-btn"/g) ?? []).length, 1)
  assert.match(html, /data-status="unread"/)
})
