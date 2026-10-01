> 제출본 안내: 초기 설계 참고 문서입니다. 과거 24GHz·대화 모델·단계별 계획은 최신 구현 상태와 다를 수 있습니다. 현재 범위는 루트 README_SUBMISSION.md와 docs/ROADMAP.md를 우선해 주세요.

# Review Checklist — Doran-Doran Care Hub

Use this checklist before marking any phase complete and before moving to the next phase.

---

## General (every phase)

- [ ] No API keys, tokens, or credentials hardcoded in any source file
- [ ] `.env` file is listed in `.gitignore`
- [ ] Build command runs without errors
- [ ] No existing working code was deleted without approval
- [ ] Relevant documentation updated if the schema or architecture changed

---

## Phase 1 — Guardian Dashboard Mock UI

- [ ] `npm run dev` starts the dev server without errors
- [ ] Event list renders with mock data
- [ ] Event cards show: device name, event type, timestamp, location
- [ ] Layout is usable on a standard laptop browser (no horizontal scroll)
- [ ] No real Firebase calls yet (purely static data)

---

## Phase 2 — Firebase Integration

- [ ] Firebase config loaded from environment variables only (`VITE_FIREBASE_*`)
- [ ] `onValue` listener attached to `/events` path
- [ ] Dashboard updates in realtime when a new event is written to Firebase (no page reload)
- [ ] Console shows no Firebase permission errors
- [ ] Fallback UI shown if Firebase is unreachable

---

## Phase 3 — Mock Sensor Event Generator

- [ ] `node scripts/generate-event.js` writes a valid event to Firebase
- [ ] Event appears on the dashboard within 3 seconds
- [ ] Event schema matches `docs/DB_SCHEMA.md` exactly
- [ ] Script reads Firebase credentials from environment variables, not hardcoded

---

## Phase 4 — Demo Data Seeding

- [ ] `node scripts/seed.js` runs without errors on a fresh Firebase project
- [ ] Seeds at least 2 devices, 1 senior, and 10 historical events
- [ ] Dashboard renders seed data correctly
- [ ] Running seed twice does not duplicate devices or seniors (idempotent)

---

## MVP Sign-Off (Phases 1–4 complete)

- [ ] End-to-end demo rehearsed: seed → open dashboard → run generator → event appears live
- [ ] Demo completes in under 3 seconds from event trigger to dashboard update
- [ ] No manual page refresh required
- [ ] Demo works on a fresh browser tab (no cached state required)

---

## Phase 5 — ESP32 Button Firmware

- [ ] Firmware compiles without errors (PlatformIO or Arduino IDE)
- [ ] Button press writes `button_press` event to Firebase via REST
- [ ] Event schema matches `docs/DB_SCHEMA.md`
- [ ] WiFi credentials loaded from firmware config, not hardcoded in shared source
- [ ] Dashboard shows the hardware event identically to mock events

---

## Phase 6 — Pillbox Reed Switch

- [ ] `pillbox_open` and `pillbox_close` events fire correctly
- [ ] Dashboard differentiates pillbox events from button events visually
- [ ] No false events when lid is held mid-position

---

## Phase 7 — Radar Presence Detection

- [ ] `presence_detected` and `presence_lost` events fire reliably
- [ ] Dashboard shows current presence status (present / not present) per device/room
- [ ] No excessive event spam (debounce confirmed)

---

## Phase 8 — AI Voice Conversation

- [ ] Claude API key loaded from environment variable only
- [ ] Wake word detection working
- [ ] Basic conversation loop functional
- [ ] Conversation does not expose raw Firebase data to the senior

---

## Phase 9 — Memories-Based AI Grandchild

- [ ] Senior profile and memories readable from Firebase
- [ ] Claude prompt includes relevant memories without exceeding context limits
- [ ] Persona remains consistent across sessions
- [ ] Conversation history written back to Firebase after each session
