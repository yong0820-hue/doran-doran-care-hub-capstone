(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DoranMemoryGame = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ACTIONS = Object.freeze(["left", "right", "both"]);
  const START_LENGTH = 2;
  const MAX_LENGTH = 6;
  const READY_MS = 500;
  const STEP_MS = 850;
  const GAP_MS = 350;
  const INPUT_FEEDBACK_MS = 180;
  const CORRECT_MS = 1400;

  // Touch and a future IWR6843AOPEVM classifier enter through the same method:
  // LEFT_ARM -> left, RIGHT_ARM -> right, BOTH_ARMS -> both.
  // A future adapter must emit one discrete event per recognized gesture, then
  // call submitGameInput(action, "motion"). No sensor is connected by this file.
  // This engine owns no DOM, audio, backend state, storage or medical scoring.
  function createGame(options = {}) {
    const random = options.random || Math.random;
    const later = options.setTimeout || setTimeout;
    const cancel = options.clearTimeout || clearTimeout;
    const onChange = options.onChange || (() => {});
    const autoAdvance = options.autoAdvance !== false;
    const timers = new Set();
    let generation = 0;
    let sequence = [];
    let phase = "idle";
    let activeAction = null;
    let presentedCount = 0;
    let inputCount = 0;
    let score = 0;
    let best = 0;
    let lastPublished = null;

    function getState() {
      // Return primitives only: consumers cannot change or reveal the sequence.
      return Object.freeze({ phase, activeAction, sequenceLength: sequence.length,
        presentedCount, inputCount, score, best });
    }

    function publish() {
      const snapshot = getState();
      if (lastPublished && Object.keys(snapshot).every(
        (key) => snapshot[key] === lastPublished[key]
      )) return;
      lastPublished = snapshot;
      onChange(snapshot);
    }

    function cancelTimers() {
      generation += 1;
      for (const timer of timers) cancel(timer);
      timers.clear();
    }

    function schedule(callback, delay) {
      const scheduledGeneration = generation;
      const timer = later(() => {
        timers.delete(timer);
        // Also reject callbacks already queued when reset/start was requested.
        if (scheduledGeneration === generation) callback();
      }, delay);
      timers.add(timer);
    }

    function randomAction() {
      // Math.random is in [0, 1); clamp the upper boundary for injected sources.
      const sample = random();
      const index = Number.isFinite(sample)
        ? Math.max(0, Math.min(ACTIONS.length - 1, Math.floor(sample * ACTIONS.length)))
        : 0;
      return ACTIONS[index];
    }

    function showStep(index) {
      activeAction = sequence[index];
      presentedCount = index + 1;
      schedule(() => {
        activeAction = null;
        // Every step, including the last, has a visibly neutral interval.
        schedule(() => {
          if (index + 1 < sequence.length) showStep(index + 1);
          else {
            phase = "input";
            publish();
          }
        }, GAP_MS);
        publish();
      }, STEP_MS);
      publish();
    }

    function presentRound() {
      phase = "showing";
      activeAction = null;
      presentedCount = 0;
      inputCount = 0;
      schedule(() => showStep(0), READY_MS);
      publish();
    }

    function start() {
      cancelTimers();
      sequence = Array.from({ length: START_LENGTH }, randomAction);
      score = 0;
      presentRound();
    }

    function advance() {
      if (phase !== "correct") return false;
      cancelTimers();
      sequence.push(randomAction());
      presentRound();
      return true;
    }

    function reset() {
      cancelTimers();
      sequence = [];
      phase = "idle";
      activeAction = null;
      presentedCount = 0;
      inputCount = 0;
      score = 0;
      publish();
    }

    function submitGameInput(action, source) {
      if (phase !== "input" || !ACTIONS.includes(action)
          || !["touch", "motion"].includes(source)) return false;
      // Replace visual feedback without debouncing: repeated actions count.
      cancelTimers();
      if (action !== sequence[inputCount]) {
        phase = "incorrect";
        activeAction = null;
      } else {
        inputCount += 1;
        activeAction = action;
        if (inputCount === sequence.length) {
          score += 1;
          best = Math.max(best, score);
          activeAction = null;
          phase = sequence.length === MAX_LENGTH ? "complete" : "correct";
          if (phase === "correct" && autoAdvance) schedule(advance, CORRECT_MS);
        } else schedule(() => {
          activeAction = null;
          publish();
        }, INPUT_FEEDBACK_MS);
      }
      publish();
      return true;
    }

    // Establish the initial snapshot without sending a construction-time event.
    lastPublished = getState();
    return Object.freeze({ start, advance, reset, submitGameInput, getState });
  }

  return Object.freeze({ createGame, ACTIONS, START_LENGTH, MAX_LENGTH,
    READY_MS, STEP_MS, GAP_MS, INPUT_FEEDBACK_MS, CORRECT_MS });
});
