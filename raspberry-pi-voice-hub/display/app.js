"use strict";

(() => {
  const POLL_INTERVAL_MS = 250;
  const STATE_TIMEOUT_MS = 400;
  const REQUEST_TIMEOUT_MS = 2000;
  const GAME_VOICE_TIMEOUT_MS = 30000;
  const states = {
    idle: ["곁에 있어요", "아래 버튼을 누르면 대화할 수 있어요"],
    listening: ["듣고 있어요", "편하게 말씀해 주세요"],
    thinking: ["생각하고 있어요", "잠시만 기다려 주세요"],
    speaking: ["말하고 있어요", "도란이의 이야기를 들어 주세요"],
  };
  const pageNames = ["처음", "음악 · 라디오", "인지 놀이", "추억 · 사진"];
  const element = (id) => document.getElementById(id);
  const button = element("talk-button");
  const audio = element("radio-audio");
  const pages = Array.from(document.querySelectorAll(".page"));
  const dots = Array.from(document.querySelectorAll(".page-dot"));
  let current = null;
  let pending = false;
  let resetPending = false;
  let connected = false;
  let pollTimer = null;
  let pollInFlight = false;
  let connectionFailed = false;
  let pageIndex = 0;
  let swipe = null;
  let presets = [];
  let presetIndex = 0;
  let presetsLoading = false;
  let wantsAudio = false;
  let playPending = false;
  let playToken = 0;
  let nonGameRevision = null;
  let focusQueue = Promise.resolve();
  let pauseAckPending = null;
  let radioMessage = "재생 버튼을 눌러 주세요";
  let lifecycle = 0;
  let needsFreshState = true;
  let gameHasFocus = true;
  let gameNotice = "";
  let gameGeneration = 0;
  let gameSessionCounter = 0;
  let gameVoiceSession = "";
  let gameVoiceController = null;
  let gameVoiceInFlight = false;
  let gameVoiceEnabled = false;
  let gameFlowBusy = false;
  let gameNarrationKeys = new Set();
  const gameFlowTimers = new Set();
  const gameActions = Array.from(document.querySelectorAll(".game-action"));
  const game = DoranMemoryGame.createGame({ onChange: handleGameChange, autoAdvance: false });

  // Only replace changed text: polling must not repeatedly announce live regions.
  function setText(id, value) {
    const target = element(id);
    if (target.textContent !== value) target.textContent = value;
  }

  function updateClock() {
    const now = new Date();
    const hour = now.getHours();
    const minute = String(now.getMinutes()).padStart(2, "0");
    const period = hour < 12 ? "오전" : "오후";
    setText("date", new Intl.DateTimeFormat("ko-KR", { month: "long", day: "numeric", weekday: "long" }).format(now));
    setText("period", period);
    setText("clock", `${hour % 12 || 12}:${minute}`);
    setText("accessible-clock", `${period} ${hour % 12 || 12}시 ${now.getMinutes()}분`);
    element("accessible-clock").dateTime = now.toISOString();
  }

  function setFeedback(message) {
    setText("action-feedback", message);
    element("action-feedback").classList.toggle("sr-only", !message);
  }

  function careActive() {
    return pending || Boolean(connected && current && current.activity !== "game"
      && (current.busy || current.state !== "idle" || current.alert || current.audio_focus.blocked));
  }

  function safeForAudio() {
    return connected && current && !needsFreshState && !document.hidden
      && !current.audio_focus.blocked && !careActive();
  }

  function safeForGame() {
    return pageIndex === 2 && connected && current && !needsFreshState
      && !document.hidden && gameHasFocus && !careActive()
      && (!current.audio_focus.blocked || current.activity === "game");
  }

  function renderGame(state = game.getState()) {
    const available = safeForGame() && !gameFlowBusy;
    element("page-game").dataset.phase = state.phase;
    let status = "반짝이는 순서를 기억해요";
    let progress = "순서를 보고, 같은 순서로 눌러 주세요";
    if (state.phase === "idle" && gameNotice) status = gameNotice;
    if (state.phase === "showing") {
      status = "순서를 잘 보세요";
      progress = `${state.sequenceLength}개 기억하기 · ${state.presentedCount} / ${state.sequenceLength}`;
    } else if (state.phase === "input") {
      status = "이제 같은 순서로 눌러 주세요";
      progress = `${state.inputCount} / ${state.sequenceLength}개 입력 · 천천히 해도 괜찮아요`;
    } else if (state.phase === "correct") {
      status = "잘하셨어요!";
      progress = "이번에는 하나 더 기억해 볼까요?";
    } else if (state.phase === "incorrect") {
      status = "괜찮아요, 다시 해볼까요?";
      progress = `${state.sequenceLength}개 순서에 도전했어요`;
    } else if (state.phase === "complete") {
      status = "멋져요! 오늘 놀이를 마쳤어요";
      progress = "6개 순서까지 성공했어요 · 잠깐 쉬어도 좋아요";
    }
    if (!connected || needsFreshState) {
      status = "화면 연결 후 시작할 수 있어요";
      progress = "잠시만 기다려 주세요";
    }
    setText("game-status", status);
    setText("game-progress", progress);
    setText("game-score", String(state.score));
    setText("game-best", String(state.best));
    for (const card of gameActions) {
      card.disabled = !available || state.phase !== "input";
      card.dataset.active = String(state.activeAction === card.dataset.gameAction);
    }
    element("game-start").disabled = !available
      || Boolean(current && current.audio_focus.blocked);
    setText("game-start", ["incorrect", "complete"].includes(state.phase) ? "다시 하기"
      : state.phase === "idle" ? "놀이 시작" : "처음부터");
  }

  function gameSessionId() {
    gameSessionCounter += 1;
    return `game-${Date.now().toString(36)}-${gameSessionCounter}`;
  }

  function clearGameFlowTimers() {
    for (const timer of gameFlowTimers) clearTimeout(timer);
    gameFlowTimers.clear();
  }

  async function sendGameCancel(session) {
    if (!session) return;
    try {
      await request("/api/game-voice", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Doran-Action": "game" },
        body: JSON.stringify({ action: "cancel", session }), keepalive: true,
      });
    } catch (_) { /* A page exit remains safe even if the local server is gone. */ }
  }

  function cancelGameFlow() {
    const session = gameVoiceSession;
    const shouldCancel = gameVoiceInFlight;
    gameGeneration += 1;
    clearGameFlowTimers();
    gameNarrationKeys = new Set();
    gameFlowBusy = false;
    gameVoiceInFlight = false;
    gameVoiceEnabled = false;
    gameVoiceSession = "";
    if (gameVoiceController) gameVoiceController.abort();
    gameVoiceController = null;
    if (shouldCancel) void sendGameCancel(session);
  }

  function resetGame(message = "놀이 시작을 눌러 다시 해요") {
    cancelGameFlow();
    gameNotice = message;
    game.reset();
    renderGame();
  }

  async function playGameCue(cue, token) {
    if (token !== gameGeneration || !gameVoiceSession || !safeForGame()) return false;
    if (!gameVoiceEnabled) return true;
    const session = gameVoiceSession;
    const controller = new AbortController();
    gameVoiceController = controller;
    gameVoiceInFlight = true;
    renderGame();
    const timeout = setTimeout(() => controller.abort(), GAME_VOICE_TIMEOUT_MS);
    let outcome = "failed";
    try {
      const response = await fetch("/api/game-voice", {
        method: "POST", cache: "no-store", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Doran-Action": "game" },
        body: JSON.stringify({ action: "play", cue, session }), signal: controller.signal,
      });
      const data = await response.json();
      if (![200, 409, 503].includes(response.status)
          || !["played", "cancelled", "game_busy", "interrupted",
            "unavailable", "failed"].includes(data.status)) {
        throw new Error("Game voice unavailable");
      }
      outcome = data.status;
    } catch (_) {
      if (token === gameGeneration) void sendGameCancel(session);
    } finally {
      clearTimeout(timeout);
      if (gameVoiceController === controller) {
        gameVoiceController = null;
        gameVoiceInFlight = false;
        renderGame();
      }
    }
    if (token !== gameGeneration || gameVoiceSession !== session) return false;
    if (["unavailable", "failed", "game_busy"].includes(outcome)) {
      // Keep the already-tested visual game usable, but do not retry every
      // phase while a stale synthesis or unavailable voice service clears.
      gameVoiceEnabled = false;
    }
    if (["cancelled", "interrupted"].includes(outcome)) {
      resetGame("다른 안내가 시작되어 놀이를 멈췄어요");
      return false;
    }
    return safeForGame();
  }

  function narrateGamePhase(cue, state) {
    const key = `${gameGeneration}:${cue}:${state.sequenceLength}:${state.score}`;
    if (gameNarrationKeys.has(key)) return;
    gameNarrationKeys.add(key);
    const token = gameGeneration;
    gameFlowBusy = true;
    renderGame(state);
    if (cue === "correct") {
      const timer = setTimeout(async () => {
        gameFlowTimers.delete(timer);
        if (token !== gameGeneration) return;
        const currentSession = await playGameCue(cue, token);
        if (!currentSession) return;
        gameFlowBusy = false;
        if (!game.advance()) renderGame();
      }, DoranMemoryGame.CORRECT_MS);
      gameFlowTimers.add(timer);
      return;
    }
    void (async () => {
      await playGameCue(cue, token);
      if (token !== gameGeneration) return;
      gameFlowBusy = false;
      renderGame();
    })();
  }

  function handleGameChange(state) {
    renderGame(state);
    if (["input", "correct", "incorrect", "complete"].includes(state.phase)) {
      narrateGamePhase(state.phase, state);
    }
  }

  async function startGame() {
    if (!safeForGame() || gameFlowBusy) return;
    cancelGameFlow();
    game.reset();
    gameVoiceSession = gameSessionId();
    gameVoiceEnabled = true;
    const token = gameGeneration;
    gameNotice = "놀이 방법을 알려드릴게요";
    gameFlowBusy = true;
    pauseRadio("인지 놀이 안내 중이에요 · 재생을 눌러 다시 들어요");
    renderGame();
    const currentSession = await playGameCue("start", token);
    if (!currentSession) return;
    gameFlowBusy = false;
    gameNotice = "";
    game.start();
  }

  // Single input boundary for touch and a future IWR6843AOPEVM adapter.
  // Classifier labels: LEFT_ARM -> left, RIGHT_ARM -> right, BOTH_ARMS -> both.
  // A future adapter must emit once per completed gesture, re-arm/deduplicate,
  // and discard delayed events across rounds/resets. No sensor transport exists yet.
  function submitGameInput(action, source) {
    if (!safeForGame() || gameFlowBusy) return false;
    return game.submitGameInput(action, source);
  }
  window.DoranGame = Object.freeze({ submitGameInput });
  gameActions.forEach((card) => card.addEventListener("click", () => {
    submitGameInput(card.dataset.gameAction, "touch");
  }));
  element("game-start").addEventListener("click", startGame);

  function showPage(index) {
    const next = Math.max(0, Math.min(pages.length - 1, index));
    if (next !== 0 && careActive()) return;
    if (next !== pageIndex || careActive()) cancelSwipe();
    if (pageIndex === 2 && next !== pageIndex) resetGame();
    const oldPage = pages[pageIndex];
    if (pageIndex !== next && oldPage.contains(document.activeElement)) element("home-button").focus({ preventScroll: true });
    pageIndex = next;
    element("page-track").style.transform = `translateX(-${next * 100}%)`;
    document.body.dataset.page = pages[next].dataset.page;
    pages.forEach((page, i) => {
      page.setAttribute("aria-hidden", String(i !== next));
      page.inert = i !== next;
    });
    dots.forEach((dot, i) => {
      if (i === next) dot.setAttribute("aria-current", "page");
      else dot.removeAttribute("aria-current");
      dot.disabled = careActive() && i !== 0;
    });
    setText("page-label", pageNames[next]);
    element("page-previous").disabled = next === 0 || careActive();
    element("page-next").disabled = next === pages.length - 1 || careActive();
    renderGame();
  }

  function renderRadio() {
    const active = wantsAudio || playPending;
    element("radio-play").disabled = !presets.length || (!active && !safeForAudio());
    element("radio-play").setAttribute("aria-pressed", String(active));
    element("radio-play").setAttribute("aria-busy", String(playPending));
    setText("radio-play-icon", active ? "Ⅱ" : "▶");
    setText("radio-play-label", active ? "멈춤" : "재생");
    element("radio-previous").disabled = presets.length < 2;
    element("radio-next").disabled = presets.length < 2;
    let message = radioMessage;
    if (!connected || needsFreshState) message = "화면 연결 후 재생할 수 있어요";
    else if (careActive()) message = "대화·돌봄 안내 중에는 잠시 멈춰요";
    setText("radio-status", message);
  }

  function render() {
    const state = connected && current ? current.state : "offline";
    document.body.dataset.state = state;
    setText("connection", connected ? "화면 연결됨" : connectionFailed ? "화면 연결 끊김" : "화면 연결 중");
    let status;
    let hint;
    if (state === "offline") {
      status = connectionFailed ? "화면 연결을 확인해 주세요" : "화면 연결 중이에요";
      hint = "화면을 다시 연결하고 있어요";
    } else {
      [status, hint] = states[state];
      if (state === "idle" && (current.busy || !current.can_start)) hint = current.busy ? "음성 안내를 준비하고 있어요" : "지금은 대화를 시작할 수 없어요";
    }
    setText("voice-status", status);
    setText("voice-hint", hint);
    const alert = connected && current ? current.alert : null;
    const card = element("alert-card");
    if (alert) {
      card.dataset.kind = alert.kind;
      setText("alert-title", alert.title);
      setText("alert-message", alert.message);
      card.hidden = false;
    } else {
      card.hidden = true;
      card.removeAttribute("data-kind");
      setText("alert-title", "");
      setText("alert-message", "");
    }
    button.disabled = !connected || pending || resetPending || !current || current.busy || !current.can_start;
    setText("button-label", pending ? "대화를 시작하고 있어요" : "도란이와 대화하기");
    button.setAttribute("aria-busy", String(pending));
    const resetUnavailable = !connected || pending || resetPending || !current
      || (!current.can_start && current.activity !== "conversation");
    element("conversation-reset-button").disabled = resetUnavailable;
    element("conversation-reset-confirm").disabled = resetUnavailable;
    element("conversation-reset-button").setAttribute("aria-busy", String(resetPending));
    if (!connected) closeResetConfirmation();
    showPage(careActive() ? 0 : pageIndex);
    renderRadio();
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    // 250 ms polling + 400 ms timeout stays inside the runtime's 750 ms audio guard.
    const timeout = setTimeout(() => controller.abort(), path === "/api/state" ? STATE_TIMEOUT_MS : REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options, signal: controller.signal });
      const data = await response.json();
      return { response, data };
    } finally { clearTimeout(timeout); }
  }

  function validFocus(focus) {
    return focus && typeof focus.blocked === "boolean"
      && Number.isSafeInteger(focus.revision) && focus.revision >= 0
      && Number.isSafeInteger(focus.non_game_revision) && focus.non_game_revision >= 0;
  }

  function validState(data) {
    return data && Object.prototype.hasOwnProperty.call(states, data.state)
      && typeof data.busy === "boolean" && typeof data.can_start === "boolean"
      && [null, "conversation", "inactivity", "medication", "game"].includes(data.activity)
      && validFocus(data.audio_focus)
      && (data.alert === null || (data.alert && typeof data.alert.kind === "string"
        && typeof data.alert.title === "string" && typeof data.alert.message === "string"));
  }

  // Keep intent/ack POSTs ordered: an old pause must never clear a newer play grant.
  function reportFocus(playing, token) {
    if (playing) pauseAckPending = null;
    const operation = focusQueue.catch(() => {}).then(() => {
      if (playing && (token !== playToken || !safeForAudio())) return null;
      return request("/api/radio-focus", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Doran-Action": "radio" },
        body: JSON.stringify({ playing }), keepalive: !playing,
      });
    });
    focusQueue = operation.catch(() => {});
    return operation;
  }

  function acknowledgePause() {
    // At most one queued pause acknowledgement, including on a slow/offline server.
    if (pauseAckPending) return pauseAckPending;
    const acknowledgement = reportFocus(false).catch(() => {}).finally(() => {
      if (pauseAckPending === acknowledgement) pauseAckPending = null;
    });
    pauseAckPending = acknowledgement;
    return pauseAckPending;
  }

  function pauseRadio(message = "일시정지됨 · 재생을 눌러 다시 들어요") {
    const active = wantsAudio || playPending || !audio.paused;
    ++playToken;
    wantsAudio = false;
    playPending = false;
    // Synchronous pause comes before any fetch or local conversation request.
    audio.pause();
    if (active) radioMessage = message;
    void acknowledgePause();
    renderRadio();
  }

  function loseConnection() {
    connected = false;
    connectionFailed = true;
    current = null;
    needsFreshState = true;
    resetGame();
    pauseRadio("화면 연결이 끊겨 멈췄어요 · 재생을 눌러 다시 들어요");
    render();
  }

  async function poll() {
    if (pollInFlight) return;
    clearTimeout(pollTimer);
    pollInFlight = true;
    const startedLifecycle = lifecycle;
    try {
      const { response, data } = await request("/api/state");
      if (!response.ok || !validState(data)) throw new Error("Display state unavailable");
      if (startedLifecycle !== lifecycle) return;
      const nonGameChanged = nonGameRevision !== null
        && nonGameRevision !== data.audio_focus.non_game_revision;
      nonGameRevision = data.audio_focus.non_game_revision;
      current = data;
      connected = true;
      needsFreshState = document.hidden;
      if (connectionFailed) setFeedback("");
      connectionFailed = false;
      // This counter excludes game narration, so even care that starts and ends
      // between two polls safely invalidates the current game session.
      const interrupted = careActive() || nonGameChanged || document.hidden;
      if (interrupted) resetGame();
      if (data.audio_focus.blocked || interrupted) pauseRadio(data.activity === "game"
        ? "인지 놀이 안내 중이에요 · 재생을 눌러 다시 들어요"
        : "대화 중 일시정지됨 · 재생을 눌러 다시 들어요");
      render();
    } catch (_) { loseConnection(); }
    finally {
      pollInFlight = false;
      // Schedule after completion so slow connections never overlap state requests.
      pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
    }
  }

  function validPresets(data) {
    return data && ["ready", "empty", "invalid", "missing"].includes(data.status)
      && Array.isArray(data.presets) && data.presets.length <= 50
      && (data.status === "ready" ? data.presets.length > 0 : data.presets.length === 0)
      && data.presets.every((preset) => {
        if (!preset || typeof preset.name !== "string" || !preset.name.trim() || typeof preset.url !== "string") return false;
        try {
          const url = new URL(preset.url);
          return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password && Boolean(url.hostname);
        } catch (_) { return false; }
      });
  }

  function selectPreset(index) {
    if (!presets.length) return;
    pauseRadio();
    presetIndex = (index + presets.length) % presets.length;
    const preset = presets[presetIndex];
    audio.src = preset.url;
    audio.load();
    setText("station-title", preset.name);
    setText("station-number", `방송 ${presetIndex + 1} / ${presets.length}`);
    radioMessage = "재생 버튼을 눌러 주세요";
    renderRadio();
  }

  async function loadPresets() {
    if (presetsLoading) return;
    presetsLoading = true;
    element("radio-reload").disabled = true;
    try {
      const { response, data } = await request("/api/radio-presets");
      if (!response.ok || !validPresets(data)) throw new Error("Radio presets unavailable");
      presets = data.presets;
      element("radio-player").hidden = !presets.length;
      element("radio-empty").hidden = Boolean(presets.length);
      if (presets.length) selectPreset(0);
      else {
        pauseRadio();
        audio.removeAttribute("src");
        audio.load();
        setText("radio-empty-title", data.status === "invalid" ? "라디오 설정을 확인해 주세요" : "아직 등록된 방송이 없어요");
        setText("radio-empty-hint", "방송을 등록하면 여기서 들을 수 있어요.");
      }
    } catch (_) {
      presets = [];
      pauseRadio();
      element("radio-player").hidden = true;
      element("radio-empty").hidden = false;
      setText("radio-empty-title", "방송 목록을 불러오지 못했어요");
      setText("radio-empty-hint", "잠시 후 다시 확인해 주세요.");
    } finally {
      presetsLoading = false;
      element("radio-reload").disabled = false;
      renderRadio();
    }
  }

  async function startRadio() {
    if (!presets.length || !safeForAudio()) return;
    const token = ++playToken;
    wantsAudio = true;
    playPending = true;
    radioMessage = "방송에 연결하고 있어요";
    renderRadio();
    try {
      const result = await reportFocus(true, token);
      if (token !== playToken) return;
      if (!result || result.response.status !== 200 || result.data.status !== "accepted"
          || !validFocus(result.data.audio_focus) || result.data.audio_focus.blocked || !safeForAudio()) {
        pauseRadio("음성 안내가 끝난 뒤 재생을 눌러 주세요");
        void poll();
        return;
      }
      // Every start requires a fresh user tap and an idle grant from the same display API.
      await audio.play();
      if (token !== playToken || !safeForAudio()) {
        // A newer user tap owns playback; an older promise must not stop that attempt.
        if (!wantsAudio || !safeForAudio()) audio.pause();
        return;
      }
      playPending = false;
      radioMessage = "재생 중";
      renderRadio();
    } catch (error) {
      if (token !== playToken) return;
      pauseRadio(error && error.name === "NotAllowedError"
        ? "재생을 다시 눌러 주세요" : "방송에 연결할 수 없어요 · 다른 방송을 선택해 주세요");
    }
  }

  element("radio-play").addEventListener("click", () => {
    if (wantsAudio || playPending) pauseRadio();
    else void startRadio();
  });
  element("radio-previous").addEventListener("click", () => selectPreset(presetIndex - 1));
  element("radio-next").addEventListener("click", () => selectPreset(presetIndex + 1));
  element("radio-reload").addEventListener("click", () => { void loadPresets(); });
  element("radio-volume").addEventListener("input", (event) => {
    const volume = Math.max(0, Math.min(100, Number(event.target.value) || 0));
    audio.volume = volume / 100;
    setText("volume-label", `${volume}%`);
    event.target.setAttribute("aria-valuetext", `${volume}%`);
  });
  audio.volume = .5;
  audio.addEventListener("playing", () => {
    if (!wantsAudio || !safeForAudio()) { pauseRadio("대화 중 일시정지됨 · 재생을 눌러 다시 들어요"); return; }
    playPending = false;
    radioMessage = "재생 중";
    renderRadio();
  });
  for (const event of ["waiting", "stalled"]) audio.addEventListener(event, () => {
    if (wantsAudio && safeForAudio()) { radioMessage = "방송을 기다리고 있어요"; renderRadio(); }
  });
  audio.addEventListener("error", () => {
    if (wantsAudio || playPending) pauseRadio("방송에 연결할 수 없어요 · 다른 방송을 선택해 주세요");
  });
  audio.addEventListener("ended", () => { pauseRadio("방송이 끝났어요 · 재생을 눌러 다시 들어요"); });
  audio.addEventListener("pause", () => {
    if (audio.paused && wantsAudio && !playPending) pauseRadio();
  });

  button.addEventListener("click", async () => {
    if (button.disabled || pending) return;
    pauseRadio("대화 중 일시정지됨 · 재생을 눌러 다시 들어요");
    pending = true;
    setFeedback("");
    render();
    try {
      const { response, data } = await request("/api/conversation", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Doran-Action": "conversation" }, body: "{}",
      });
      if (response.status === 202 && data.status === "accepted") {
        // Accepting a request is not listening; phases still come from the voice runtime.
        if (current) current = { ...current, busy: true, can_start: false };
      } else if (response.status === 409 && data.status === "busy") setFeedback("음성 안내가 끝난 뒤 다시 눌러 주세요");
      else if (response.status === 503 && data.status === "unavailable") setFeedback("지금은 대화를 시작할 수 없어요. 잠시 후 다시 눌러 주세요");
      else throw new Error("Conversation request unavailable");
    } catch (_) {
      loseConnection();
      setFeedback("화면 연결을 확인하고 다시 눌러 주세요");
    } finally {
      pending = false;
      render();
      // Never retry a conversation request: it may already have been accepted.
      void poll();
    }
  });

  function closeResetConfirmation() {
    element("conversation-reset-confirmation").hidden = true;
    element("conversation-reset-button").setAttribute("aria-expanded", "false");
  }

  element("conversation-reset-button").addEventListener("click", () => {
    if (element("conversation-reset-button").disabled) return;
    element("conversation-reset-confirmation").hidden = false;
    element("conversation-reset-button").setAttribute("aria-expanded", "true");
    setText("conversation-reset-feedback", "");
    element("conversation-reset-cancel").focus();
  });
  element("conversation-reset-cancel").addEventListener("click", () => {
    closeResetConfirmation();
    element("conversation-reset-button").focus();
  });
  element("conversation-reset-confirm").addEventListener("click", async () => {
    if (element("conversation-reset-confirm").disabled || resetPending
        || element("conversation-reset-confirmation").hidden) return;
    resetPending = true;
    closeResetConfirmation();
    render();
    try {
      const { response, data } = await request("/api/conversation-reset", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Doran-Action": "conversation-reset" }, body: "{}",
      });
      if (response.status === 202 && data.status === "accepted") {
        setText("conversation-reset-feedback", "초기화를 요청했어요. 말하기·듣기와 돌봄 안내가 끝나면 새 대화를 준비해요. 저장된 기억은 그대로예요.");
      } else if (response.status === 503 && data.status === "unavailable") {
        setText("conversation-reset-feedback", "지금은 대화를 초기화할 수 없어요. 잠시 후 다시 눌러 주세요.");
      } else throw new Error("Conversation reset unavailable");
    } catch (_) {
      // The request may have arrived. Do not automatically submit it again.
      setText("conversation-reset-feedback", "초기화 결과를 확인하지 못했어요. 화면 연결과 대화 상태를 확인해 주세요.");
    } finally {
      resetPending = false;
      render();
      void poll();
    }
  });

  element("home-button").addEventListener("click", () => showPage(0));
  element("page-previous").addEventListener("click", () => showPage(pageIndex - 1));
  element("page-next").addEventListener("click", () => showPage(pageIndex + 1));
  dots.forEach((dot) => dot.addEventListener("click", () => showPage(Number(dot.dataset.pageIndex))));
  const viewport = element("page-viewport");
  const interactive = (target) => target.closest("button, input, select, textarea, label, a, [contenteditable], [data-no-swipe]");
  const SWIPE_DISTANCE = 60;
  const SWIPE_SLOP = 12;
  const SWIPE_DIRECTION_RATIO = 1.4;
  const SWIPE_TIMEOUT_MS = 1200;

  function cancelSwipe() {
    const previous = swipe;
    swipe = null;
    if (previous && viewport.hasPointerCapture?.(previous.id)) viewport.releasePointerCapture(previous.id);
  }

  function trackSwipe(event) {
    if (!swipe || swipe.id !== event.pointerId) return false;
    // Capture retargets events to the viewport; hit-test to protect the whole
    // volume region even when a finger starts outside it and crosses into it.
    const target = document.elementFromPoint(event.clientX, event.clientY);
    if (careActive() || document.hidden || pageIndex !== swipe.page || target?.closest("[data-no-swipe]")) {
      cancelSwipe();
      return false;
    }
    const dx = Math.abs(event.clientX - swipe.x);
    const dy = Math.abs(event.clientY - swipe.y);
    // Reject scroll/diagonal intent for the rest of this gesture, even if the
    // finger later turns sideways. Small touch jitter still counts as a tap.
    if (Math.max(dx, dy) >= SWIPE_SLOP && dx <= dy * SWIPE_DIRECTION_RATIO) {
      cancelSwipe();
      return false;
    }
    return true;
  }

  viewport.addEventListener("pointerdown", (event) => {
    // A second contact cancels the first; it must never replace its start point.
    if (swipe) { cancelSwipe(); return; }
    if (!event.isPrimary || event.button !== 0 || interactive(event.target) || careActive()) return;
    swipe = { x: event.clientX, y: event.clientY, id: event.pointerId, time: Date.now(), page: pageIndex };
    try { viewport.setPointerCapture(event.pointerId); }
    catch (_) { cancelSwipe(); }
  });
  viewport.addEventListener("pointermove", trackSwipe);
  viewport.addEventListener("pointerup", (event) => {
    if (!trackSwipe(event)) return;
    const dx = event.clientX - swipe.x;
    const elapsed = Date.now() - swipe.time;
    const startPage = swipe.page;
    // Commit only on release, after clearing state: one finger gesture can
    // advance at most one page, regardless of distance or move event count.
    cancelSwipe();
    if (elapsed < SWIPE_TIMEOUT_MS && Math.abs(dx) >= SWIPE_DISTANCE) {
      event.preventDefault();
      showPage(startPage + (dx < 0 ? 1 : -1));
    }
  });
  for (const type of ["pointercancel", "lostpointercapture"]) {
    viewport.addEventListener(type, (event) => { if (swipe?.id === event.pointerId) cancelSwipe(); });
  }
  window.addEventListener("blur", () => {
    gameHasFocus = false;
    cancelSwipe();
    resetGame();
  });
  window.addEventListener("focus", () => { gameHasFocus = true; renderGame(); });
  document.addEventListener("keydown", (event) => {
    if (interactive(event.target) || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight" || event.key === "Home") {
      event.preventDefault();
      showPage(event.key === "Home" ? 0 : pageIndex + (event.key === "ArrowRight" ? 1 : -1));
    }
  });

  function suspendRadio() {
    cancelSwipe();
    resetGame();
    ++lifecycle;
    needsFreshState = true;
    pauseRadio("화면이 쉬는 동안 멈췄어요 · 재생을 눌러 다시 들어요");
    renderRadio();
  }
  document.addEventListener("visibilitychange", () => {
    suspendRadio();
    if (!document.hidden) { updateClock(); void poll(); }
  });
  window.addEventListener("pagehide", suspendRadio);
  window.addEventListener("pageshow", () => { needsFreshState = true; void poll(); });
  window.addEventListener("offline", () => { ++lifecycle; loseConnection(); });
  window.addEventListener("online", () => { void poll(); });

  updateClock();
  showPage(0);
  renderRadio();
  setInterval(updateClock, 1000);
  void poll();
  void loadPresets();
})();
