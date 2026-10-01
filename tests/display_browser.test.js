"use strict";

// Executes the shipped browser script with controllable DOM/media/network edges.
// No browser package is needed; run with: node --test tests/display_browser.test.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const display = path.join(__dirname, "..", "raspberry-pi-voice-hub", "display");
const source = fs.readFileSync(path.join(display, "app.js"), "utf8");
const gameSource = fs.readFileSync(path.join(display, "memory_game.js"), "utf8");
const html = fs.readFileSync(path.join(display, "index.html"), "utf8");
const idle = (revision = 0, nonGameRevision = 0) => ({ state: "idle", busy: false,
  can_start: true, activity: null, alert: null,
  audio_focus: { blocked: false, revision, non_game_revision: nonGameRevision } });
const station = { name: "테스트 방송", url: "https://example.invalid/demo.mp3" };
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { for (let i = 0; i < 30; ++i) await Promise.resolve(); };

class Element {
  constructor(id, tag = "div") {
    this.id = id; this.tag = tag; this.dataset = {}; this.style = {}; this.attributes = {};
    this.textContent = ""; this.disabled = false; this.hidden = false; this.value = "50";
    this.events = {}; this.classList = { toggle() {} }; this.paused = true;
    this.parentElement = null; this.capturedPointers = new Set(); this.releaseCalls = [];
  }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  async emit(type, fields = {}) {
    const event = { target: this, cancelable: true, defaultPrevented: false,
      preventDefault() { if (this.cancelable) this.defaultPrevented = true; }, ...fields };
    for (const fn of this.events[type] || []) await fn(event);
    return event;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; if (name === "src") this.src = ""; }
  contains(target) {
    for (let node = target; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) {
      if (selector.split(",").some((part) => {
        const match = part.trim();
        return match.startsWith("[") ? Object.hasOwn(node.attributes, match.slice(1, -1)) : node.tag === match;
      })) return node;
    }
    return null;
  }
  focus() {}
  setPointerCapture(id) {
    if (this.failCapture) throw new Error("Pointer is no longer active");
    this.capturedPointers.add(id);
  }
  hasPointerCapture(id) { return this.capturedPointers.has(id); }
  releasePointerCapture(id) {
    assert.ok(this.hasPointerCapture(id), "Only an owned pointer capture can be released");
    this.capturedPointers.delete(id); this.releaseCalls.push(id);
  }
}

async function setup(config = { presets: [station], status: "ready" }, randomValues = [0, .4, .9]) {
  const elements = new Map(); const nodes = []; const ancestors = [];
  const voidTags = new Set(["meta", "link", "input", "br"]);
  for (const match of html.matchAll(/<(\/?)([a-z][a-z0-9]*)\b([^>]*)>/g)) {
    if (match[1]) {
      const index = ancestors.findLastIndex((node) => node.tag === match[2]);
      if (index !== -1) ancestors.length = index;
      continue;
    }
    const node = new Element("", match[2]);
    for (const attr of match[3].matchAll(/([^\s=/>]+)(?:="([^"]*)")?/g)) node.attributes[attr[1]] = attr[2] ?? "";
    node.id = node.attributes.id || "";
    for (const [key, value] of Object.entries(node.attributes)) {
      if (key.startsWith("data-")) node.dataset[key.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = value;
    }
    node.parentElement = ancestors.at(-1) || null; nodes.push(node);
    if (node.id) elements.set(node.id, node);
    if (!voidTags.has(node.tag) && !match[3].trimEnd().endsWith("/")) ancestors.push(node);
  }
  const pages = ["home", "radio", "game", "memories"].map((name) => {
    const page = elements.get(`page-${name}`); page.dataset.page = name; return page;
  });
  const dots = nodes.filter((node) => Object.hasOwn(node.dataset, "pageIndex"));
  const document = new Element("document");
  document.body = nodes.find((node) => node.tag === "body"); document.hidden = false; document.activeElement = null;
  document.getElementById = (id) => { assert.ok(elements.has(id), `Unknown shipped DOM id: ${id}`); return elements.get(id); };
  const gameCards = nodes.filter((node) => Object.hasOwn(node.dataset, "gameAction"));
  document.querySelectorAll = (selector) => selector === ".page" ? pages
    : selector === ".page-dot" ? dots : selector === ".game-action" ? gameCards : [];
  const window = new Element("window");
  const timers = new Map(); let timer = 0;
  const env = { elements, nodes, document, window, dots, state: idle(), config, requests: [], playCalls: 0, pauseCalls: 0, mediaPending: null, focusHandler: null, gameVoiceHandler: null, stateHandler: null, now: Date.UTC(2026, 8, 16) };
  document.elementFromPoint = () => env.hitTarget || elements.get(`page-${document.body.dataset.page}`);
  class MockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [env.now])); }
    static now() { return env.now; }
  }
  const audio = elements.get("radio-audio"); env.audio = audio;
  audio.pause = () => { ++env.pauseCalls; audio.paused = true; };
  audio.load = () => { audio.paused = true; };
  audio.play = () => {
    ++env.playCalls;
    if (env.mediaPending) return env.mediaPending.promise;
    audio.paused = false;
    void audio.emit("playing");
    return Promise.resolve();
  };
  const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
  const fetch = async (url, options = {}) => {
    env.requests.push({ url, ...options });
    if (url === "/api/state") return env.stateHandler ? env.stateHandler(options) : response(structuredClone(env.state));
    if (url === "/api/radio-presets") return response(env.config);
    if (url === "/api/radio-focus") {
      const playing = JSON.parse(options.body).playing;
      if (env.focusHandler) return env.focusHandler(playing, response);
      const busy = playing && env.state.audio_focus.blocked;
      return response({ status: busy ? "busy" : "accepted", audio_focus: structuredClone(env.state.audio_focus) }, busy ? 409 : 200);
    }
    if (url === "/api/conversation") { env.pausedAtConversation = audio.paused; return response({ status: "accepted" }, 202); }
    if (url === "/api/conversation-reset") {
      return env.resetHandler ? env.resetHandler(options, response) : response({ status: "accepted" }, 202);
    }
    if (url === "/api/game-voice") {
      const payload = JSON.parse(options.body);
      if (env.gameVoiceHandler) return env.gameVoiceHandler(payload, options, response);
      return response({ status: payload.action === "play" ? "played" : "idle",
        audio_focus: structuredClone(env.state.audio_focus) });
    }
    throw new Error(`Unexpected request ${url}`);
  };
  let randomIndex = 0;
  const mockMath = Object.create(Math);
  mockMath.random = () => randomValues[randomIndex++ % randomValues.length];
  const context = vm.createContext({ document, window, fetch, AbortController, URL, Intl, Math: mockMath, Date: MockDate, console,
    setTimeout: (fn, ms) => { timers.set(++timer, { fn, ms }); return timer; },
    clearTimeout: (id) => timers.delete(id), setInterval: () => 1,
  });
  vm.runInContext(gameSource, context, { filename: "display/memory_game.js" });
  vm.runInContext(source, context, { filename: "display/app.js" });
  env.click = async (id) => { const el = elements.get(id); if (!el.disabled) await el.emit("click"); await flush(); };
  env.poll = async () => {
    const entry = Array.from(timers.entries()).find(([, value]) => value.ms === 250);
    assert.ok(entry, "A completed poll schedules another state poll");
    timers.delete(entry[0]);
    await entry[1].fn();
    await flush();
  };
  env.text = (id) => elements.get(id).textContent;
  // Only advance game timers: poll/fetch timers continue to use their own helpers.
  const gameDelays = [500, 850, 350, 1400, 180];
  env.gameTimers = () => [...timers.values()].filter(({ ms }) => gameDelays.includes(ms));
  env.gameTimer = async (delay) => {
    const entries = [...timers.entries()].filter(([, { ms }]) => ms === delay);
    assert.equal(entries.length, 1, `Exactly one ${delay} ms game timer should be pending`);
    timers.delete(entries[0][0]); await entries[0][1].fn(); await flush();
  };
  env.gamePhase = () => elements.get("page-game").dataset.phase;
  env.activeActions = () => gameCards.filter((card) => card.dataset.active === "true").map((card) => card.dataset.gameAction);
  env.gameView = () => ({ phase: env.gamePhase(), active: env.activeActions(),
    status: env.text("game-status"), progress: env.text("game-progress"),
    score: env.text("game-score"), best: env.text("game-best") });
  env.showSequence = async (length) => {
    assert.equal(env.gamePhase(), "showing");
    assert.deepEqual(env.activeActions(), [], "Round begins with a neutral ready interval");
    await env.gameTimer(500);
    const sequence = [];
    for (let step = 0; step < length; step++) {
      assert.equal(env.gamePhase(), "showing");
      assert.equal(env.activeActions().length, 1, "Only the current sequence card is highlighted");
      sequence.push(env.activeActions()[0]);
      assert.ok(gameCards.every((card) => card.disabled), "Reproduction is disabled during presentation");
      await env.gameTimer(850);
      assert.deepEqual(env.activeActions(), [], "Every step has a neutral gap, including repeated labels");
      await env.gameTimer(350);
    }
    assert.equal(env.gamePhase(), "input");
    assert.deepEqual(env.activeActions(), [], "Sequence is hidden when reproduction begins");
    assert.ok(gameCards.every((card) => !card.disabled));
    return sequence;
  };
  env.touchSequence = async (sequence) => {
    for (const action of sequence) {
      await env.click(`game-${action}`);
      if (env.gamePhase() === "input") await env.gameTimer(180);
    }
  };
  env.expireStateRequest = () => {
    const entry = Array.from(timers.entries()).find(([, value]) => value.ms === 400);
    assert.ok(entry, "State requests must fail closed after 400 ms, inside the audio guard");
    timers.delete(entry[0]); entry[1].fn();
  };
  env.focusIntents = () => env.requests.filter((req) => req.url === "/api/radio-focus").map((req) => JSON.parse(req.body).playing);
  env.pointer = (type, fields = {}) => elements.get("page-viewport").emit(type, {
    target: elements.get(`page-${document.body.dataset.page}`), pointerType: "touch", isPrimary: true,
    button: 0, pointerId: 1, clientX: 300, clientY: 100, ...fields,
  });
  env.swipe = async (dx, dy = 0, { target, duration = 100, moves = [] } = {}) => {
    const down = await env.pointer("pointerdown", target ? { target } : {});
    for (const [x, y] of moves) await env.pointer("pointermove", { clientX: 300 + x, clientY: 100 + y });
    env.now += duration;
    const up = await env.pointer("pointerup", { clientX: 300 + dx, clientY: 100 + dy });
    return { down, up };
  };
  await flush();
  return env;
}

test("four real pages navigate by arrows, dots and Home; hidden pages are inert", async () => {
  const env = await setup();
  assert.equal(env.document.body.dataset.page, "home");
  await env.click("page-next"); assert.equal(env.document.body.dataset.page, "radio");
  assert.equal(env.elements.get("page-home").inert, true);
  assert.equal(env.elements.get("page-radio").inert, false);
  await env.dots[3].emit("click"); assert.equal(env.document.body.dataset.page, "memories");
  assert.equal(env.elements.get("page-next").disabled, true);
  await env.click("home-button"); assert.equal(env.document.body.dataset.page, "home");
  assert.equal(env.elements.get("page-previous").disabled, true);
});

test("conversation reset requires confirmation and keeps radio playback untouched", async () => {
  const env = await setup();
  await env.click("radio-play");
  assert.equal(env.elements.get("radio-audio").paused, false);
  await env.click("conversation-reset-button");
  assert.equal(env.elements.get("conversation-reset-confirmation").hidden, false);
  assert.equal(env.requests.filter(({ url }) => url === "/api/conversation-reset").length, 0);
  await env.click("conversation-reset-cancel");
  assert.equal(env.elements.get("conversation-reset-confirmation").hidden, true);
  await env.click("conversation-reset-confirm");
  assert.equal(env.requests.filter(({ url }) => url === "/api/conversation-reset").length, 0);
  await env.click("conversation-reset-button");
  await env.click("conversation-reset-confirm");
  const resets = env.requests.filter(({ url }) => url === "/api/conversation-reset");
  assert.equal(resets.length, 1);
  assert.equal(resets[0].headers["X-Doran-Action"], "conversation-reset");
  assert.equal(resets[0].body, "{}");
  assert.match(env.text("conversation-reset-feedback"), /저장된 기억은 그대로/);
  assert.equal(env.elements.get("radio-audio").paused, false);
});

test("conversation reset remains usable during active audio but never starts another capture", async () => {
  const env = await setup();
  for (const state of ["speaking", "listening", "thinking"]) {
    env.state = { ...idle(1, 1), state, busy: true, can_start: false,
      activity: "conversation", audio_focus: { blocked: true, revision: 1, non_game_revision: 1 } };
    await env.poll();
    assert.equal(env.elements.get("talk-button").disabled, true);
    assert.equal(env.elements.get("conversation-reset-button").disabled, false);
  }
  await env.click("conversation-reset-button");
  await env.click("conversation-reset-confirm");
  assert.equal(env.requests.filter(({ url }) => url === "/api/conversation").length, 0);
  assert.match(env.text("conversation-reset-feedback"), /말하기·듣기와 돌봄 안내가 끝나면/);
});

test("reset duplicate taps are ignored and ambiguous failures are not retried", async () => {
  const env = await setup();
  const pending = deferred();
  env.resetHandler = () => pending.promise;
  await env.click("conversation-reset-button");
  const submitted = env.click("conversation-reset-confirm");
  await flush();
  assert.equal(env.elements.get("conversation-reset-confirm").disabled, true);
  await env.click("conversation-reset-confirm");
  pending.resolve({ status: 503, ok: false, json: async () => ({ status: "unavailable" }) });
  await submitted;
  assert.equal(env.requests.filter(({ url }) => url === "/api/conversation-reset").length, 1);
  assert.match(env.text("conversation-reset-feedback"), /초기화할 수 없어요/);
  env.resetHandler = () => { throw new Error("Network failed after acceptance"); };
  await env.click("conversation-reset-button");
  await env.click("conversation-reset-confirm");
  await env.poll();
  assert.equal(env.requests.filter(({ url }) => url === "/api/conversation-reset").length, 2);
  assert.match(env.text("conversation-reset-feedback"), /결과를 확인하지 못했어요/);
});

test("offline and care states disable reset and a lost connection closes confirmation", async () => {
  const env = await setup();
  env.state = { ...idle(1, 1), state: "speaking", busy: true, can_start: false,
    activity: "medication", audio_focus: { blocked: true, revision: 1, non_game_revision: 1 } };
  await env.poll();
  assert.equal(env.elements.get("conversation-reset-button").disabled, true);
  env.state = idle(2, 2);
  await env.poll();
  await env.click("conversation-reset-button");
  env.stateHandler = () => { throw new Error("offline"); };
  await env.poll();
  assert.equal(env.elements.get("conversation-reset-button").disabled, true);
  assert.equal(env.elements.get("conversation-reset-confirmation").hidden, true);
  assert.equal(env.requests.filter(({ url }) => url === "/api/conversation-reset").length, 0);
});

test("swipes require deliberate distance, horizontal dominance and a short duration in either direction", async () => {
  const cases = [
    [0, 0, 100, "radio"], [3, 2, 100, "radio"], [-59, 0, 100, "radio"], [59, 0, 100, "radio"],
    [-60, 0, 100, "game"], [60, 0, 100, "home"], [-70, 50, 100, "radio"], [-71, 50, 100, "game"],
    [-100, 200, 100, "radio"], [-100, 0, 1199, "game"], [-100, 0, 1200, "radio"],
  ];
  for (const [dx, dy, duration, expected] of cases) {
    const env = await setup(); await env.click("page-next");
    const { down, up } = await env.swipe(dx, dy, { duration });
    assert.equal(env.document.body.dataset.page, expected, `dx=${dx}, dy=${dy}, duration=${duration}`);
    assert.equal(down.defaultPrevented, false, "Pointer down must preserve native taps");
    assert.equal(up.defaultPrevented, expected !== "radio");
    assert.equal(env.elements.get("page-viewport").capturedPointers.size, 0);
  }
});

test("vertical or diagonal starts and later vertical turns cancel the whole gesture; small jitter is tolerated", async () => {
  for (const moves of [[[0, 20], [-150, 20]], [[10, 13], [-150, 0]], [[-30, 2], [-50, 80], [-150, 0]]]) {
    const env = await setup();
    await env.swipe(-180, 0, { moves });
    assert.equal(env.document.body.dataset.page, "home", JSON.stringify(moves));
  }
  const env = await setup();
  await env.swipe(-100, 15, { moves: [[-4, 7], [-30, 3], [-90, 12]] });
  assert.equal(env.document.body.dataset.page, "radio");
});

test("one long gesture changes exactly one page on release, preserving order, bounds and indicators", async () => {
  const env = await setup(); const viewport = env.elements.get("page-viewport");
  const assertPage = (index) => {
    const names = ["home", "radio", "game", "memories"];
    assert.equal(env.document.body.dataset.page, names[index]);
    assert.equal(env.elements.get("page-track").style.transform, `translateX(-${index * 100}%)`);
    assert.equal(env.text("page-label"), ["처음", "음악 · 라디오", "인지 놀이", "추억 · 사진"][index]);
    assert.deepEqual(env.dots.map((dot) => dot.attributes["aria-current"] === "page"), names.map((_, i) => i === index));
    assert.equal(env.elements.get("page-previous").disabled, index === 0);
    assert.equal(env.elements.get("page-next").disabled, index === 3);
  };
  await env.pointer("pointerdown"); assert.equal(viewport.hasPointerCapture(1), true);
  for (const clientX of [200, 0, -300, -1000]) {
    await env.pointer("pointermove", { clientX }); assertPage(0);
  }
  await env.poll(); // Routine state rendering must not discard an in-flight swipe.
  await env.pointer("pointerup", { clientX: -1000 }); assertPage(1);
  await env.pointer("pointerup", { clientX: -1500 }); assertPage(1);
  assert.deepEqual(viewport.releaseCalls, [1]);
  await env.swipe(-100); assertPage(2);
  await env.swipe(-100); assertPage(3);
  await env.swipe(-100); assertPage(3);
  for (const index of [2, 1, 0, 0]) { await env.swipe(100); assertPage(index); }
});

test("controls, nested icons, editable text and the entire volume area never start page swipes", async () => {
  const env = await setup(); await env.click("page-next");
  const viewport = env.elements.get("page-viewport");
  const volumeLabel = env.elements.get("volume-label").parentElement;
  const targets = ["radio-volume", "volume-label", "radio-play-icon", "radio-play-label", "button-label", "page-next", "page-previous"]
    .map((id) => env.elements.get(id));
  targets.push(volumeLabel, volumeLabel.parentElement);
  for (const tag of ["select", "textarea", "label", "a", "div"]) {
    const control = new Element("", tag); control.parentElement = env.elements.get("page-radio");
    if (tag === "div") control.attributes.contenteditable = "true";
    const child = new Element("", "span"); child.parentElement = control; targets.push(child);
  }
  for (const target of targets) {
    const down = await env.pointer("pointerdown", { target });
    assert.equal(down.defaultPrevented, false); assert.equal(viewport.capturedPointers.size, 0);
    const move = await env.pointer("pointermove", { target, clientX: 180 });
    const up = await env.pointer("pointerup", { target, clientX: 120 });
    assert.equal(move.defaultPrevented, false); assert.equal(up.defaultPrevented, false);
    assert.equal(env.document.body.dataset.page, "radio", target.id || target.tag);
  }
});

test("a captured swipe entering the volume area is canceled even when its event target remains the viewport", async () => {
  for (const crossingEvent of ["pointermove", "pointerup"]) {
    const env = await setup(); await env.click("page-next");
    const viewport = env.elements.get("page-viewport");
    await env.pointer("pointerdown");
    env.hitTarget = env.elements.get("volume-label");
    await env.pointer(crossingEvent, { target: viewport, clientX: 190 });
    env.hitTarget = null;
    await env.pointer("pointerup", { target: viewport, clientX: 100 });
    assert.equal(env.document.body.dataset.page, "radio");
    assert.equal(viewport.capturedPointers.size, 0);
  }
});

test("short control taps retain native play, pause, volume, talk and arrow actions", async () => {
  const env = await setup();
  const tap = async (id, target = env.elements.get(id)) => {
    const { down, up } = await env.swipe(3, 2, { target });
    assert.equal(down.defaultPrevented, false); assert.equal(up.defaultPrevented, false);
    await env.click(id);
  };
  await tap("page-next"); assert.equal(env.document.body.dataset.page, "radio");
  await tap("radio-play", env.elements.get("radio-play-icon")); assert.equal(env.audio.paused, false);
  await tap("radio-play", env.elements.get("radio-play-label")); assert.equal(env.audio.paused, true);
  const slider = env.elements.get("radio-volume");
  const { down, up } = await env.swipe(3, 0, { target: slider });
  assert.equal(down.defaultPrevented, false); assert.equal(up.defaultPrevented, false);
  slider.value = "75"; await slider.emit("input");
  assert.equal(env.audio.volume, .75); assert.equal(env.text("volume-label"), "75%");
  assert.equal(env.document.body.dataset.page, "radio");
  await tap("page-previous"); assert.equal(env.document.body.dataset.page, "home");
  await tap("talk-button", env.elements.get("button-label"));
  assert.equal(env.requests.filter((request) => request.url === "/api/conversation").length, 1);
});

test("pointer cancellation, lost capture, a second contact and capture failure cannot complete stale swipes", async () => {
  for (const terminal of ["pointercancel", "lostpointercapture", "second-contact", "capture-failure"]) {
    const env = await setup(); const viewport = env.elements.get("page-viewport");
    if (terminal === "capture-failure") viewport.failCapture = true;
    await env.pointer("pointerdown");
    if (terminal === "second-contact") await env.pointer("pointerdown", { pointerId: 2, isPrimary: false });
    else if (terminal !== "capture-failure") {
      if (terminal === "lostpointercapture") viewport.capturedPointers.delete(1);
      await env.pointer(terminal);
    }
    await env.pointer("pointerup", { pointerId: 2, clientX: 100 });
    await env.pointer("pointerup", { clientX: 100 });
    assert.equal(env.document.body.dataset.page, "home", terminal);
    assert.equal(viewport.capturedPointers.size, 0);
    viewport.failCapture = false;
    await env.swipe(-100); assert.equal(env.document.body.dataset.page, "radio");
  }
});

test("only the active primary pointer may finish or cancel a swipe", async () => {
  const env = await setup();
  for (const fields of [{ isPrimary: false }, { button: 2 }]) {
    await env.pointer("pointerdown", fields);
    await env.pointer("pointerup", { clientX: 150 });
    assert.equal(env.document.body.dataset.page, "home");
  }
  await env.pointer("pointerdown");
  for (const type of ["pointermove", "pointerup", "pointercancel", "lostpointercapture"]) {
    await env.pointer(type, { pointerId: 2, clientX: 100, clientY: 250 });
  }
  assert.equal(env.elements.get("page-viewport").hasPointerCapture(1), true);
  await env.pointer("pointerup", { clientX: 200 });
  assert.equal(env.document.body.dataset.page, "radio");
});

test("page changes, care starting on Home, suspension and blur invalidate an in-flight swipe", async () => {
  for (const interruption of ["page", "care", "hidden", "pagehide", "blur"]) {
    const env = await setup(); await env.pointer("pointerdown");
    if (interruption === "page") await env.click("page-next");
    else if (interruption === "care") {
      env.state = { ...idle(1, 1), busy: true }; await env.poll();
      env.state = idle(2, 1); await env.poll();
    } else if (interruption === "hidden") {
      env.document.hidden = true; await env.document.emit("visibilitychange");
      env.document.hidden = false; await env.document.emit("visibilitychange"); await flush();
    } else await env.window.emit(interruption);
    await env.pointer("pointerup", { clientX: 100 });
    assert.equal(env.document.body.dataset.page, interruption === "page" ? "radio" : "home", interruption);
    assert.equal(env.elements.get("page-viewport").capturedPointers.size, 0);
  }
});

test("empty and invalid configurations have a clear empty state with no media playback", async () => {
  for (const status of ["empty", "missing", "invalid"]) {
    const env = await setup({ presets: [], status });
    assert.equal(env.elements.get("radio-player").hidden, true);
    assert.equal(env.elements.get("radio-empty").hidden, false);
    assert.match(env.text("radio-empty-title"), /등록된 방송|설정/);
    assert.equal(env.elements.get("radio-play").disabled, true);
    assert.equal(env.playCalls, 0); assert.equal(env.audio.src, "");
  }
});

test("unsafe or malformed preset data is rejected without assigning a media URL", async () => {
  for (const url of ["javascript:alert(1)", "file:///music.mp3", "https://user:secret@example.invalid/audio", "/relative.mp3"]) {
    const env = await setup({ status: "ready", presets: [{ name: "방송", url }] });
    assert.equal(env.elements.get("radio-player").hidden, true);
    assert.match(env.text("radio-empty-title"), /불러오지 못/);
    assert.equal(env.audio.src, undefined); assert.equal(env.playCalls, 0);
  }
});

test("play requires an idle server grant; pause and volume affect actual audio", async () => {
  const env = await setup();
  assert.equal(env.playCalls, 0);
  await env.click("radio-play");
  assert.equal(env.audio.paused, false); assert.equal(env.playCalls, 1);
  assert.equal(env.focusIntents().at(-1), true);
  const slider = env.elements.get("radio-volume"); slider.value = "80"; await slider.emit("input");
  assert.equal(env.audio.volume, .8); assert.equal(env.text("volume-label"), "80%");
  await env.click("radio-play"); assert.equal(env.audio.paused, true);
  assert.equal(env.focusIntents().at(-1), false);
  await env.poll(); assert.equal(env.playCalls, 1);
});

test("preset next/previous wrap, use literal labels, and never autoplay", async () => {
  const env = await setup({ status: "ready", presets: [station, { name: "<script>literal</script>", url: "http://localhost/demo.wav" }] });
  await env.click("radio-play");
  await env.click("radio-next");
  assert.equal(env.audio.paused, true); assert.equal(env.playCalls, 1);
  assert.equal(env.text("station-title"), "<script>literal</script>");
  assert.equal(env.audio.src, "http://localhost/demo.wav");
  await env.click("radio-next"); assert.equal(env.text("station-title"), station.name);
  await env.click("radio-previous"); assert.equal(env.text("station-number"), "방송 2 / 2");
  assert.equal(env.playCalls, 1);
});

test("every voice/care state pauses radio and brings Home forward; idle does not resume", async () => {
  const cases = [
    { state: "listening" }, { state: "thinking" }, { state: "speaking" }, { busy: true },
    { alert: { kind: "medication", title: "복약", message: "확인" } },
    { alert: { kind: "inactivity", title: "안부", message: "확인" } },
    { audio_focus: { blocked: true, revision: 1, non_game_revision: 1 } },
  ];
  for (const changes of cases) {
    const env = await setup(); await env.click("page-next"); await env.click("radio-play");
    env.state = { ...idle(1, 1), ...changes }; await env.poll();
    assert.equal(env.audio.paused, true); assert.equal(env.document.body.dataset.page, "home");
    assert.equal(env.elements.get("page-next").disabled, true);
    assert.equal(env.focusIntents().at(-1), false);
    env.state = idle(2, 1); await env.poll(); await env.click("page-next");
    assert.equal(env.playCalls, 1); assert.match(env.text("radio-status"), /대화 중 일시정지됨/);
    await env.click("radio-play"); assert.equal(env.playCalls, 2);
  }
});

test("an unseen non-game focus change stops playback when both polls report idle", async () => {
  const env = await setup(); await env.click("radio-play");
  env.state = idle(2, 1); await env.poll();
  assert.equal(env.audio.paused, true); assert.equal(env.playCalls, 1);
  await env.poll(); assert.equal(env.playCalls, 1);
});

test("local talk pauses synchronously before sending the existing conversation request", async () => {
  const env = await setup(); await env.click("radio-play"); await env.click("talk-button");
  assert.equal(env.pausedAtConversation, true);
  const requests = env.requests.filter((req) => req.url === "/api/conversation");
  assert.equal(requests.length, 1); assert.equal(requests[0].body, "{}");
  assert.equal(requests[0].headers["X-Doran-Action"], "conversation");
  assert.equal(env.playCalls, 1);
});

test("a late play grant cannot start audio after care or user pause", async () => {
  const env = await setup(); const grant = deferred();
  env.focusHandler = (playing, response) => playing ? grant.promise : response({ status: "accepted", audio_focus: idle(1).audio_focus });
  await env.click("radio-play"); assert.equal(env.playCalls, 0);
  env.state = { ...idle(1, 1), busy: true }; await env.poll();
  grant.resolve({ status: 200, ok: true, json: async () => ({ status: "accepted", audio_focus: idle().audio_focus }) });
  await flush();
  assert.equal(env.playCalls, 0); assert.equal(env.audio.paused, true);
  assert.equal(env.focusIntents().at(-1), false);
});

test("a late native play promise/event is stopped during care", async () => {
  const env = await setup(); env.mediaPending = deferred();
  await env.click("radio-play"); assert.equal(env.playCalls, 1);
  env.state = { ...idle(1, 1), busy: true }; await env.poll();
  env.audio.paused = false; await env.audio.emit("playing");
  env.mediaPending.resolve(); await flush();
  assert.equal(env.audio.paused, true); assert.equal(env.focusIntents().at(-1), false);
  env.state = idle(2, 1); await env.poll(); assert.equal(env.playCalls, 1);
});

test("focus intent requests stay ordered through a pending pause, play and cancel", async () => {
  const env = await setup(); await env.click("radio-play");
  const pause = deferred(); let holdPause = true;
  env.focusHandler = (playing, response) => {
    if (!playing && holdPause) { holdPause = false; return pause.promise; }
    return response({ status: "accepted", audio_focus: idle().audio_focus });
  };
  await env.click("radio-play"); // pause POST in flight
  await env.click("radio-play"); // grant queued behind pause
  await env.click("radio-play"); // cancel before queued grant runs
  pause.resolve({ status: 200, ok: true, json: async () => ({ status: "accepted", audio_focus: idle().audio_focus }) });
  await flush();
  assert.equal(env.focusIntents().at(-1), false); assert.equal(env.playCalls, 1);
  await env.click("radio-play"); assert.equal(env.playCalls, 2);
  assert.equal(env.focusIntents().at(-1), true);
});

test("busy grant rejects stale idle display without starting audio", async () => {
  const env = await setup(); env.state = { ...idle(1, 1), busy: true,
    audio_focus: { blocked: true, revision: 1, non_game_revision: 1 } };
  await env.click("radio-play");
  assert.equal(env.playCalls, 0); assert.equal(env.audio.paused, true);
  assert.equal(env.focusIntents().at(-1), false);
});

test("hidden/offline/malformed state stop media; recovery requires fresh state and manual tap", async () => {
  const env = await setup(); await env.click("radio-play");
  env.document.hidden = true; await env.document.emit("visibilitychange"); await flush();
  assert.equal(env.audio.paused, true); assert.equal(env.elements.get("radio-play").disabled, true);
  env.document.hidden = false; await env.document.emit("visibilitychange"); await flush();
  assert.equal(env.playCalls, 1);
  await env.click("radio-play"); assert.equal(env.playCalls, 2);
  await env.window.emit("offline"); await flush(); assert.equal(env.audio.paused, true);
  await env.poll(); assert.equal(env.playCalls, 2);
  await env.click("radio-play"); env.state = { ...idle(),
    audio_focus: { blocked: false, revision: "bad", non_game_revision: 0 } };
  await env.poll(); assert.equal(env.audio.paused, true); assert.equal(env.document.body.dataset.state, "offline");
  env.state = idle(); await env.poll(); assert.equal(env.playCalls, 3);
});

test("media failures and natural end release focus without automatic retry", async () => {
  for (const event of ["error", "ended"]) {
    const env = await setup(); await env.click("radio-play"); await env.audio.emit(event); await flush();
    assert.equal(env.audio.paused, true); assert.equal(env.focusIntents().at(-1), false);
    await env.poll(); assert.equal(env.playCalls, 1);
  }
});

test("a rejected native media start gives a retry message and releases focus", async () => {
  const env = await setup(); env.audio.play = () => { ++env.playCalls; return Promise.reject(Object.assign(new Error("gesture required"), { name: "NotAllowedError" })); };
  await env.click("radio-play");
  assert.match(env.text("radio-status"), /재생을 다시 눌러/); assert.equal(env.audio.paused, true);
  assert.equal(env.focusIntents().at(-1), false); assert.equal(env.playCalls, 1);
});

test("stalled state requests time out inside the care guard and pause audio", async () => {
  const env = await setup(); await env.click("radio-play");
  env.stateHandler = (options) => new Promise((_, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("state timed out")));
  });
  const polling = env.poll(); await flush();
  env.expireStateRequest(); await polling;
  assert.equal(env.audio.paused, true); assert.equal(env.document.body.dataset.state, "offline");
  assert.equal(env.focusIntents().at(-1), false); assert.equal(env.playCalls, 1);
  env.stateHandler = null; await env.poll(); assert.equal(env.playCalls, 1);
});

test("an old queued pause event cannot stop a newly started station", async () => {
  const env = await setup(); await env.click("radio-play");
  await env.click("radio-play"); await env.click("radio-play");
  await env.audio.emit("pause"); await flush();
  assert.equal(env.audio.paused, false); assert.equal(env.playCalls, 2);
  assert.equal(env.focusIntents().at(-1), true);
});

test("memory game presents two deterministic actions one at a time and waits for reproduction", async () => {
  const env = await setup();
  assert.equal(typeof env.window.DoranGame.submitGameInput, "function");
  assert.equal(env.gamePhase(), "idle");
  await env.dots[2].emit("click");
  await env.click("game-start");
  const before = env.gameView();
  assert.equal(env.window.DoranGame.submitGameInput("left", "motion"), false);
  await env.click("game-left");
  assert.deepEqual(env.gameView(), before, "Early motion and touch cannot skip the shown sequence");
  assert.deepEqual(await env.showSequence(2), ["left", "right"]);
  assert.equal(env.document.body.dataset.page, "game");
  assert.equal(env.text("game-score"), "0");
});

test("repeated memory actions have separate visual flashes and neutral gaps", async () => {
  const env = await setup(undefined, [0]);
  await env.dots[2].emit("click"); await env.click("game-start");
  assert.deepEqual(await env.showSequence(2), ["left", "left"]);
});

test("correct touch reproduction gives feedback then adds exactly one sequence step", async () => {
  const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
  const first = await env.showSequence(2);
  await env.touchSequence(first);
  assert.equal(env.gamePhase(), "correct");
  assert.deepEqual(env.activeActions(), []);
  assert.equal(env.text("game-score"), "1"); assert.equal(env.text("game-best"), "1");
  assert.ok(env.text("game-status").length > 0);
  assert.equal(env.window.DoranGame.submitGameInput("both", "motion"), false, "Feedback ignores input");
  await env.gameTimer(1400);
  const second = await env.showSequence(3);
  assert.deepEqual(second, [...first, "both"]);
});

test("incorrect reproduction shows session results and retry starts short while retaining the best", async () => {
  const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
  await env.touchSequence(await env.showSequence(2));
  await env.gameTimer(1400); const second = await env.showSequence(3);
  await env.click(`game-${second[0] === "left" ? "right" : "left"}`);
  assert.equal(env.gamePhase(), "incorrect");
  assert.equal(env.text("game-score"), "1"); assert.equal(env.text("game-best"), "1");
  assert.ok(env.text("game-status").length > 0);
  assert.equal(env.gameTimers().length, 0, "A failed round cannot advance in the background");
  assert.equal(env.window.DoranGame.submitGameInput(second[0], "motion"), false);
  await env.click("game-start");
  assert.equal(env.text("game-score"), "0"); assert.equal(env.text("game-best"), "1");
  assert.equal((await env.showSequence(2)).length, 2);
});

test("restart invalidates a pending presentation callback and begins a new short sequence", async () => {
  const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
  await env.gameTimer(500);
  const stale = env.gameTimers().map(({ fn }) => fn);
  await env.click("game-start"); const restarted = env.gameView();
  for (const fn of stale) await fn();
  assert.deepEqual(env.gameView(), restarted, "A callback queued before restart cannot light an old action");
  assert.equal(env.text("game-score"), "0");
  assert.equal((await env.showSequence(2)).length, 2);
});

test("touch cards and the documented motion hook share one action-validation path", async () => {
  const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
  const sequence = await env.showSequence(2);
  const before = env.gameView();
  for (const [action, source] of [["LEFT_ARM", "motion"], ["unknown", "touch"], ["left", "radar"], ["left", undefined], [null, "motion"]]) {
    assert.equal(env.window.DoranGame.submitGameInput(action, source), false);
    assert.deepEqual(env.gameView(), before, `${String(action)} / ${String(source)} must not consume an answer`);
  }
  await env.click(`game-${sequence[0]}`); await env.gameTimer(180);
  assert.equal(env.gamePhase(), "input");
  assert.equal(env.window.DoranGame.submitGameInput(sequence[1], "motion"), true);
  assert.equal(env.gamePhase(), "correct");
  assert.equal(env.text("game-score"), "1");
});

test("leaving the game by arrow, swipe or Home cancels timers and reentry requires a new start", async () => {
  for (const destination of ["arrow", "swipe", "home"]) {
    const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
    await env.gameTimer(500); const stale = env.gameTimers().map(({ fn }) => fn);
    if (destination === "arrow") await env.click("page-next");
    else if (destination === "swipe") await env.swipe(-100);
    else await env.click("home-button");
    assert.equal(env.document.body.dataset.page, destination === "home" ? "home" : "memories");
    assert.equal(env.gamePhase(), "idle"); assert.equal(env.gameTimers().length, 0);
    assert.equal(env.window.DoranGame.submitGameInput("left", "motion"), false);
    for (const fn of stale) await fn();
    await env.dots[2].emit("click");
    assert.equal(env.gamePhase(), "idle"); assert.deepEqual(env.activeActions(), []);
    assert.equal(env.gameTimers().length, 0);
    await env.click("game-start"); assert.equal((await env.showSequence(2)).length, 2);
  }
});

test("proactive care and local conversation cancel game presentation and reproduction", async () => {
  const takeovers = ["talk", "listening", "thinking", "speaking", "busy", "alert", "blocked"];
  for (const phase of ["showing", "input"]) {
    for (const takeover of takeovers) {
      const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
      if (phase === "input") await env.showSequence(2);
      else await env.gameTimer(500);
      const stale = env.gameTimers().map(({ fn }) => fn);
      if (takeover === "talk") await env.click("talk-button");
      else {
        const change = takeover === "busy" ? { busy: true }
          : takeover === "alert" ? { alert: { kind: "inactivity", title: "안부", message: "확인" } }
          : takeover === "blocked" ? { audio_focus: {
            blocked: true, revision: 1, non_game_revision: 1,
          } } : { state: takeover };
        env.state = { ...idle(1, 1), ...change }; await env.poll();
      }
      assert.equal(env.document.body.dataset.page, "home", `${phase} / ${takeover}`);
      assert.equal(env.gamePhase(), "idle"); assert.deepEqual(env.activeActions(), []);
      assert.equal(env.gameTimers().length, 0);
      assert.equal(env.window.DoranGame.submitGameInput("left", "motion"), false);
      for (const fn of stale) await fn();
      assert.equal(env.gamePhase(), "idle");
      env.state = idle(2, 1); await env.poll(); await env.dots[2].emit("click");
      assert.equal(env.gamePhase(), "idle", "Care completion never resumes a half-played round");
    }
  }
});

test("suspension, blur, connection loss and an unseen care revision safely reset the game", async () => {
  for (const interruption of ["hidden", "blur", "pagehide", "offline", "invalid-state", "focus-revision"]) {
    const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
    await env.gameTimer(500); const stale = env.gameTimers().map(({ fn }) => fn);
    if (interruption === "hidden") {
      env.document.hidden = true; await env.document.emit("visibilitychange");
    } else if (interruption === "focus-revision") {
      env.state = idle(2, 1); await env.poll();
    } else if (interruption === "invalid-state") {
      env.state = { ...idle(), audio_focus: {
        blocked: false, revision: "invalid", non_game_revision: 0,
      } }; await env.poll();
    } else await env.window.emit(interruption);
    assert.equal(env.gamePhase(), "idle", interruption); assert.equal(env.gameTimers().length, 0);
    assert.equal(env.window.DoranGame.submitGameInput("left", "motion"), false);
    if (interruption !== "focus-revision") {
      await env.click("game-start"); assert.equal(env.gamePhase(), "idle", "Unsafe screen cannot start a game");
    }
    for (const fn of stale) await fn();
    assert.deepEqual(env.activeActions(), []);
    env.document.hidden = false;
    env.state = idle(2, interruption === "focus-revision" ? 1 : 0);
    if (interruption === "blur") await env.window.emit("focus");
    if (interruption === "hidden") { await env.document.emit("visibilitychange"); await flush(); }
    else await env.poll();
    await env.dots[2].emit("click");
    assert.equal(env.gamePhase(), "idle");
    await env.click("game-start"); assert.equal((await env.showSequence(2)).length, 2);
  }
});

test("game voice pauses radio, announces each transition once and never auto-resumes", async () => {
  const env = await setup(); await env.click("radio-play"); await env.dots[2].emit("click");
  const focusBefore = env.focusIntents(); const pausesBefore = env.pauseCalls;
  await env.click("game-start"); await env.touchSequence(await env.showSequence(2));
  await env.gameTimer(1400); await env.showSequence(3);
  await env.click("game-start"); await env.click("home-button");
  assert.equal(env.audio.paused, true); assert.equal(env.playCalls, 1);
  assert.ok(env.pauseCalls > pausesBefore);
  assert.ok(env.focusIntents().length > focusBefore.length);
  const plays = env.requests.filter(({ url, body }) => url === "/api/game-voice"
    && JSON.parse(body).action === "play").map(({ body }) => JSON.parse(body).cue);
  assert.deepEqual(plays, ["start", "input", "correct", "input", "start"]);
});

test("game-owned thinking and speaking focus keep the game page and session alive", async () => {
  const env = await setup(); await env.dots[2].emit("click");
  const voice = deferred();
  env.gameVoiceHandler = (payload, _options, response) => payload.action === "play"
    ? voice.promise : response({ status: "accepted", audio_focus: env.state.audio_focus });
  const starting = env.click("game-start"); await flush();
  env.state = { ...idle(1), state: "thinking", busy: true, can_start: false, activity: "game",
    audio_focus: { blocked: true, revision: 1, non_game_revision: 0 } };
  await env.poll();
  assert.equal(env.document.body.dataset.page, "game"); assert.equal(env.gamePhase(), "idle");
  env.state = { ...env.state, state: "speaking" }; await env.poll();
  assert.equal(env.document.body.dataset.page, "game");
  env.state = idle(2);
  voice.resolve({ ok: true, status: 200, json: async () => ({ status: "played",
    audio_focus: env.state.audio_focus }) });
  await starting; await flush();
  assert.equal(env.document.body.dataset.page, "game"); assert.equal(env.gamePhase(), "showing");
});

test("input narration blocks both touch cards and the future motion boundary", async () => {
  const env = await setup(); await env.dots[2].emit("click");
  const inputVoice = deferred();
  env.gameVoiceHandler = (payload, _options, response) => {
    if (payload.action === "play" && payload.cue === "input") return inputVoice.promise;
    return response({ status: payload.action === "play" ? "played" : "accepted",
      audio_focus: env.state.audio_focus });
  };
  await env.click("game-start");
  await env.gameTimer(500);
  const sequence = [];
  for (let step = 0; step < 2; step++) {
    sequence.push(env.activeActions()[0]);
    await env.gameTimer(850);
    await env.gameTimer(350);
  }
  assert.equal(env.gamePhase(), "input");
  assert.ok([...env.nodes].filter((node) => Object.hasOwn(node.dataset, "gameAction"))
    .every((card) => card.disabled));
  const before = env.gameView();
  assert.equal(env.window.DoranGame.submitGameInput(sequence[0], "motion"), false);
  assert.deepEqual(env.gameView(), before);
  inputVoice.resolve({ ok: true, status: 200, json: async () => ({ status: "played",
    audio_focus: env.state.audio_focus }) });
  await flush();
  assert.equal(env.window.DoranGame.submitGameInput(sequence[0], "motion"), true);
});

test("leaving during a game cue cancels that session and late completion cannot start it", async () => {
  const env = await setup(); await env.dots[2].emit("click");
  const voice = deferred();
  env.gameVoiceHandler = (payload, _options, response) => payload.action === "play"
    ? voice.promise : response({ status: "accepted", audio_focus: env.state.audio_focus });
  const starting = env.click("game-start"); await flush();
  const play = env.requests.find(({ url, body }) => url === "/api/game-voice"
    && JSON.parse(body).action === "play");
  assert.equal(JSON.parse(play.body).cue, "start");
  await env.click("home-button");
  const cancel = env.requests.find(({ url, body }) => url === "/api/game-voice"
    && JSON.parse(body).action === "cancel");
  assert.equal(JSON.parse(cancel.body).session, JSON.parse(play.body).session);
  voice.resolve({ ok: true, status: 200, json: async () => ({ status: "played",
    audio_focus: idle(2).audio_focus }) });
  await starting; await flush();
  assert.equal(env.document.body.dataset.page, "home"); assert.equal(env.gamePhase(), "idle");
  assert.equal(env.gameTimers().length, 0);
});

test("a quick restart falls back to visual play while a cancelled old cue clears", async () => {
  const env = await setup(); await env.dots[2].emit("click");
  const oldVoice = deferred(); let plays = 0;
  env.gameVoiceHandler = (payload, _options, response) => {
    if (payload.action === "cancel") {
      return response({ status: "accepted", audio_focus: {
        blocked: true, revision: 1, non_game_revision: 0,
      } });
    }
    plays += 1;
    if (plays === 1) return oldVoice.promise;
    return response({ status: "game_busy", audio_focus: {
      blocked: true, revision: 1, non_game_revision: 0,
    } }, 409);
  };
  const oldStart = env.click("game-start"); await flush();
  await env.click("home-button"); await env.dots[2].emit("click"); await env.click("game-start");
  assert.equal(env.gamePhase(), "showing");
  env.state = idle(2); await env.poll();
  oldVoice.resolve({ ok: true, status: 409, json: async () => ({ status: "cancelled",
    audio_focus: idle(2).audio_focus }) });
  await oldStart; await flush();
  assert.equal(env.gamePhase(), "showing");
  await env.showSequence(2);
  assert.equal(plays, 2, "Visual fallback must not retry every narration phase");
});

test("voice service failure falls back to the visual game without retrying forever", async () => {
  const env = await setup(); await env.dots[2].emit("click");
  env.gameVoiceHandler = (_payload, _options, response) => response({ status: "unavailable",
    audio_focus: env.state.audio_focus }, 503);
  await env.click("game-start");
  assert.equal(env.gamePhase(), "showing");
  assert.equal(env.requests.filter(({ url }) => url === "/api/game-voice").length, 1);
});

test("finishing length six plays the completion praise exactly once", async () => {
  const env = await setup(); await env.dots[2].emit("click"); await env.click("game-start");
  for (let length = 2; length <= 6; length++) {
    await env.touchSequence(await env.showSequence(length));
    if (length < 6) await env.gameTimer(1400);
  }
  assert.equal(env.gamePhase(), "complete");
  const cues = env.requests.filter(({ url, body }) => url === "/api/game-voice"
    && JSON.parse(body).action === "play").map(({ body }) => JSON.parse(body).cue);
  assert.equal(cues.filter((cue) => cue === "complete").length, 1);
});

test("game cards and their text are swipe exclusions while ordinary page text can swipe", async () => {
  const env = await setup(); await env.dots[2].emit("click");
  for (const action of ["left", "right", "both"]) {
    const card = env.elements.get(`game-${action}`);
    const child = env.nodes.find((node) => node.parentElement === card) || card;
    for (const target of [card, child]) {
      const { down, up } = await env.swipe(-100, 0, { target });
      assert.equal(down.defaultPrevented, false); assert.equal(up.defaultPrevented, false);
      assert.equal(env.document.body.dataset.page, "game");
    }
  }
  await env.swipe(-100, 0, { target: env.elements.get("game-status") });
  assert.equal(env.document.body.dataset.page, "memories");
  await env.click("home-button");
  await env.swipe(-100, 0, { target: env.elements.get("voice-hint") });
  assert.equal(env.document.body.dataset.page, "radio");
});
