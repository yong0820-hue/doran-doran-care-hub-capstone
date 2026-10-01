"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const enginePath = path.join(__dirname, "..", "raspberry-pi-voice-hub", "display", "memory_game.js");
const { createGame, ACTIONS, READY_MS, STEP_MS, GAP_MS, INPUT_FEEDBACK_MS,
  CORRECT_MS, START_LENGTH, MAX_LENGTH } = require(enginePath);

function harness(samples = [0, 0.4, 0.9], options = {}) {
  let now = 0;
  let nextId = 1;
  let randomCalls = 0;
  const pending = new Map();
  const allCallbacks = new Map();
  const changes = [];
  const game = createGame({
    ...options,
    random: () => samples[randomCalls++ % samples.length],
    setTimeout(callback, delay) {
      const id = nextId++;
      pending.set(id, { callback, at: now + delay });
      allCallbacks.set(id, callback);
      return id;
    },
    clearTimeout: (id) => pending.delete(id),
    onChange: (state) => changes.push(state),
  });
  function advance(ms) {
    const target = now + ms;
    for (;;) {
      const next = [...pending.entries()].filter(([, timer]) => timer.at <= target)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      pending.delete(next[0]);
      now = next[1].at;
      next[1].callback();
    }
    now = target;
  }
  function showRound() {
    assert.equal(game.getState().phase, "showing");
    const before = changes.length;
    advance(READY_MS + game.getState().sequenceLength * (STEP_MS + GAP_MS));
    assert.equal(game.getState().phase, "input");
    return changes.slice(before).filter((state) => state.phase === "showing"
      && state.activeAction !== null).map((state) => state.activeAction);
  }
  function answer(actions, source = "touch") {
    for (const action of actions) assert.equal(game.submitGameInput(action, source), true);
  }
  return { game, changes, pending, allCallbacks, advance, showRound, answer,
    get randomCalls() { return randomCalls; } };
}

test("idle state and exported motion labels are immutable and reveal no sequence", () => {
  const { game, changes } = harness();
  assert.deepEqual(ACTIONS, ["left", "right", "both"]);
  assert.ok(Object.isFrozen(ACTIONS));
  assert.deepEqual(game.getState(), { phase: "idle", activeAction: null,
    sequenceLength: 0, presentedCount: 0, inputCount: 0, score: 0, best: 0 });
  assert.ok(Object.isFrozen(game.getState()));
  assert.notEqual(game.getState(), game.getState());
  assert.equal("sequence" in game.getState(), false);
  assert.equal(changes.length, 0);
});

test("deterministic generation maps all random sample boundaries to the three actions", () => {
  const h = harness([0, 1 / 3, 2 / 3, 1 - Number.EPSILON, 1, 0]);
  h.game.start();
  assert.equal(h.game.getState().sequenceLength, START_LENGTH);
  assert.deepEqual(h.showRound(), ["left", "right"]);
  h.game.start();
  assert.deepEqual(h.showRound(), ["both", "both"]);
  h.game.start();
  assert.deepEqual(h.showRound(), ["both", "left"]);
  assert.equal(h.randomCalls, 6);
});

test("presentation shows exactly one step at a time with ready and final neutral gaps", () => {
  const h = harness();
  h.game.start();
  h.advance(READY_MS - 1);
  assert.equal(h.game.getState().activeAction, null);
  assert.equal(h.game.getState().presentedCount, 0);
  h.advance(1);
  assert.equal(h.game.getState().activeAction, "left");
  assert.equal(h.game.getState().presentedCount, 1);
  h.advance(STEP_MS - 1);
  assert.equal(h.game.getState().activeAction, "left");
  h.advance(1);
  assert.equal(h.game.getState().activeAction, null);
  h.advance(GAP_MS - 1);
  assert.equal(h.game.getState().presentedCount, 1);
  h.advance(1);
  assert.equal(h.game.getState().activeAction, "right");
  assert.equal(h.game.getState().presentedCount, 2);
  h.advance(STEP_MS);
  assert.equal(h.game.getState().activeAction, null);
  h.advance(GAP_MS - 1);
  assert.equal(h.game.getState().phase, "showing");
  h.advance(1);
  assert.equal(h.game.getState().phase, "input");
  assert.equal(h.game.getState().inputCount, 0);
  assert.equal(h.pending.size, 0);
});

test("repeated generated actions remain visually distinct and accept consecutive taps", () => {
  const h = harness([0]);
  h.game.start();
  const actions = h.showRound();
  assert.deepEqual(actions, ["left", "left"]);
  const displays = h.changes.filter((state) => state.phase === "showing")
    .map((state) => state.activeAction);
  assert.deepEqual(displays, [null, "left", null, "left", null]);
  h.answer(actions);
  assert.equal(h.game.getState().phase, "correct");
  assert.equal(h.game.getState().score, 1);
});

test("a correct answer increments score and appends one action after positive feedback", () => {
  const h = harness();
  h.game.start();
  h.answer(h.showRound());
  assert.equal(h.game.getState().phase, "correct");
  assert.equal(h.game.getState().score, 1);
  assert.equal(h.game.getState().best, 1);
  assert.equal(h.game.getState().activeAction, null);
  assert.equal(h.game.submitGameInput("left", "touch"), false);
  h.advance(CORRECT_MS - 1);
  assert.equal(h.game.getState().phase, "correct");
  assert.equal(h.randomCalls, 2);
  h.advance(1);
  assert.equal(h.game.getState().sequenceLength, 3);
  assert.equal(h.game.getState().activeAction, null);
  assert.deepEqual(h.showRound(), ["left", "right", "both"]);
  assert.equal(h.randomCalls, 3);
});

test("manual round advance waits for narration and rejects every other phase", () => {
  const h = harness(undefined, { autoAdvance: false });
  assert.equal(h.game.advance(), false);
  h.game.start();
  assert.equal(h.game.advance(), false);
  const first = h.showRound();
  h.answer(first);
  assert.equal(h.game.getState().phase, "correct");
  h.advance(CORRECT_MS * 2);
  assert.equal(h.game.getState().phase, "correct", "No timer may outrun the voice cue");
  assert.equal(h.game.advance(), true);
  assert.equal(h.game.getState().phase, "showing");
  assert.equal(h.game.getState().sequenceLength, START_LENGTH + 1);
  assert.equal(h.game.advance(), false);
});

test("the short session ends after completing length six without scheduling another round", () => {
  const h = harness();
  h.game.start();
  for (let length = START_LENGTH; length <= MAX_LENGTH; length += 1) {
    assert.equal(h.game.getState().sequenceLength, length);
    h.answer(h.showRound());
    assert.equal(h.game.getState().score, length - START_LENGTH + 1);
    if (length < MAX_LENGTH) h.advance(CORRECT_MS);
  }
  assert.equal(h.game.getState().phase, "complete");
  assert.equal(h.game.getState().score, 5);
  assert.equal(h.game.getState().best, 5);
  assert.equal(h.game.getState().activeAction, null);
  assert.equal(h.pending.size, 0);
  assert.equal(h.game.submitGameInput("left", "motion"), false);
  h.advance(60000);
  assert.equal(h.game.getState().sequenceLength, MAX_LENGTH);
});

test("incorrect answers retain completed-round score and best and stop the round", () => {
  const h = harness();
  h.game.start();
  h.answer(h.showRound());
  h.advance(CORRECT_MS);
  h.showRound();
  assert.equal(h.game.submitGameInput("left", "touch"), true);
  assert.equal(h.game.submitGameInput("both", "touch"), true);
  assert.equal(h.game.getState().phase, "incorrect");
  assert.equal(h.game.getState().inputCount, 1);
  assert.equal(h.game.getState().score, 1);
  assert.equal(h.game.getState().best, 1);
  assert.equal(h.game.getState().activeAction, null);
  assert.equal(h.pending.size, 0);
  assert.equal(h.game.submitGameInput("right", "touch"), false);
});

test("restart makes a fresh length-two sequence and clears score while retaining session best", () => {
  const h = harness();
  h.game.start();
  h.answer(h.showRound());
  h.game.start();
  assert.equal(h.game.getState().score, 0);
  assert.equal(h.game.getState().best, 1);
  assert.equal(h.game.getState().sequenceLength, START_LENGTH);
  assert.deepEqual(h.showRound(), ["both", "left"]);
  h.game.submitGameInput("right", "touch");
  h.game.start();
  assert.deepEqual(h.showRound(), ["right", "both"]);
  assert.equal(h.game.getState().best, 1);
});

test("only valid actions and sources during the input phase change state", () => {
  const h = harness();
  assert.equal(h.game.submitGameInput("left", "touch"), false);
  h.game.start();
  assert.equal(h.game.submitGameInput("left", "touch"), false);
  h.showRound();
  const before = h.game.getState();
  const count = h.changes.length;
  for (const [action, source] of [["LEFT_ARM", "motion"], ["up", "touch"],
    [null, "touch"], ["left", "radar"], ["left", undefined], ["left", null]]) {
    assert.equal(h.game.submitGameInput(action, source), false);
  }
  assert.deepEqual(h.game.getState(), before);
  assert.equal(h.changes.length, count);
});

test("touch, motion and mixed sources use the identical centralized game path", () => {
  const touch = harness();
  const motion = harness();
  for (const h of [touch, motion]) { h.game.start(); h.showRound(); }
  touch.answer(["left", "right"], "touch");
  motion.answer(["left", "right"], "motion");
  assert.deepEqual(motion.changes, touch.changes);
  motion.advance(CORRECT_MS);
  motion.showRound();
  motion.game.submitGameInput("left", "motion");
  motion.game.submitGameInput("right", "touch");
  motion.game.submitGameInput("both", "motion");
  assert.equal(motion.game.getState().score, 2);
});

test("accepted input highlights briefly and clears without requiring another action", () => {
  const h = harness();
  h.game.start();
  h.showRound();
  h.game.submitGameInput("left", "touch");
  assert.equal(h.game.getState().activeAction, "left");
  h.advance(INPUT_FEEDBACK_MS - 1);
  assert.equal(h.game.getState().activeAction, "left");
  h.advance(1);
  assert.equal(h.game.getState().activeAction, null);
  assert.equal(h.game.getState().inputCount, 1);
  assert.equal(h.game.getState().phase, "input");
});

test("rapid input replaces feedback and a stale callback cannot clear newer feedback", () => {
  const h = harness();
  h.game.start();
  h.answer(h.showRound());
  h.advance(CORRECT_MS);
  h.showRound();
  h.game.submitGameInput("left", "touch");
  const oldCallback = [...h.pending.values()][0].callback;
  h.game.submitGameInput("right", "motion");
  oldCallback();
  assert.equal(h.game.getState().activeAction, "right");
  assert.equal(h.game.getState().inputCount, 2);
  h.game.submitGameInput("both", "touch");
  assert.equal(h.game.getState().phase, "correct");
});

test("reset cancels showing callbacks including callbacks already queued for delivery", () => {
  const h = harness();
  h.game.start();
  h.advance(READY_MS);
  const stale = [...h.pending.values()].map((timer) => timer.callback);
  h.game.reset();
  const before = h.game.getState();
  const count = h.changes.length;
  for (const callback of stale) callback();
  h.advance(60000);
  assert.deepEqual(h.game.getState(), before);
  assert.equal(h.game.getState().phase, "idle");
  assert.equal(h.pending.size, 0);
  assert.equal(h.changes.length, count);
});

test("reset cancels correct feedback and input highlights and preserves session best", () => {
  for (const phase of ["correct", "input"]) {
    const h = harness();
    h.game.start();
    h.answer(h.showRound());
    if (phase === "input") {
      h.advance(CORRECT_MS);
      h.showRound();
      h.game.submitGameInput("left", "touch");
    }
    const stale = [...h.pending.values()].map((timer) => timer.callback);
    h.game.reset();
    for (const callback of stale) callback();
    h.advance(60000);
    assert.equal(h.game.getState().phase, "idle");
    assert.equal(h.game.getState().sequenceLength, 0);
    assert.equal(h.game.getState().score, 0);
    assert.equal(h.game.getState().best, 1);
    assert.equal(h.pending.size, 0);
  }
});

test("a fresh start rejects old presentation and correct-round callbacks", () => {
  for (const phase of ["showing", "correct"]) {
    const h = harness();
    h.game.start();
    if (phase === "correct") h.answer(h.showRound());
    const stale = [...h.pending.values()].map((timer) => timer.callback);
    h.game.start();
    const before = h.game.getState();
    for (const callback of stale) callback();
    assert.deepEqual(h.game.getState(), before);
    assert.deepEqual(h.showRound(), ["both", "left"]);
  }
});

test("onChange fires only for observable state changes", () => {
  const h = harness();
  h.game.reset();
  assert.equal(h.changes.length, 0);
  h.game.start();
  assert.equal(h.changes.length, 1);
  h.game.start(); // A new private sequence has the same ready-state snapshot.
  assert.equal(h.changes.length, 1);
  h.game.reset();
  assert.equal(h.changes.length, 2);
  h.game.reset();
  assert.equal(h.changes.length, 2);
});

test("the same file exports a browser global without CommonJS or DOM dependencies", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(enginePath, "utf8"), context);
  assert.equal(typeof context.DoranMemoryGame.createGame, "function");
  assert.deepEqual(Array.from(context.DoranMemoryGame.ACTIONS), ACTIONS);
});
