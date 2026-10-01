> 제출본 안내: 초기 설계 참고 문서입니다. 과거 24GHz·대화 모델·단계별 계획은 최신 구현 상태와 다를 수 있습니다. 현재 범위는 루트 README_SUBMISSION.md와 docs/ROADMAP.md를 우선해 주세요.

# Architecture — Doran-Doran Care Hub

## System Overview

```
[Sensors / Mock Generator]
        |
        | writes events
        v
[Firebase Realtime Database]
        |
        | realtime listener
        v
[Guardian Dashboard (Web)]
```

In later phases, an AI Voice module sits alongside the sensor layer and reads from the same Firebase database.

## Components

### 1. Sensor Layer (Phase 1–7)

| Phase | Source |
|---|---|
| 1–4 | Mock event generator script (Node.js or Python) |
| 5 | ESP32 + physical button |
| 6 | ESP32 + reed switch (pillbox) |
| 7 | ESP32 + 24GHz radar module |

All sources write to the same Firebase event schema — the dashboard is source-agnostic.

### 2. Firebase Realtime Database

- Single source of truth for all sensor events and device state.
- Chosen for: zero-config realtime push to web clients, free tier suitable for demo, simple JSON tree.
- No server required — sensors and the dashboard communicate directly with Firebase via SDK or REST.

### 3. Guardian Dashboard (Web)

- Framework: React (Vite) — lightweight, fast dev server.
- Reads from Firebase using the Firebase JS SDK (`onValue` listener for realtime updates).
- No backend server. All data comes from Firebase directly.
- Deployed as a static site (Firebase Hosting or local `vite preview`).

### 4. AI Voice Module (Phase 8–9, deferred)

- Runs on a local device near the senior (e.g., Raspberry Pi or similar).
- Uses Claude API for conversation.
- Reads senior profile and memory from Firebase.
- Out of scope until Phase 8.

## Tech Stack

| Layer | Technology |
|---|---|
| Database | Firebase Realtime Database |
| Dashboard | React + Vite + Firebase JS SDK |
| Mock generator | Node.js script |
| Firmware | Arduino / ESP32 (C++) |
| AI (future) | Claude API (Anthropic) |
| Hosting | Firebase Hosting (or local for demo) |

## Data Flow (MVP)

1. Mock generator script runs, writes a new event node under `/events/{elderId}/{eventId}`.
2. Firebase propagates the change to all connected clients in ~1 second.
3. Dashboard's `onValue` listener fires, React state updates, UI re-renders.
4. Guardian sees the new event card appear without refreshing the page.

## Environment Variables

All credentials are provided via environment variables. No secrets in source files.

| Variable | Used by |
|---|---|
| `VITE_FIREBASE_API_KEY` | Dashboard |
| `VITE_FIREBASE_AUTH_DOMAIN` | Dashboard |
| `VITE_FIREBASE_DATABASE_URL` | Dashboard |
| `VITE_FIREBASE_PROJECT_ID` | Dashboard |
| `FIREBASE_DATABASE_URL` | Mock generator (Node.js) |
| `FIREBASE_SERVICE_ACCOUNT_KEY` | Mock generator / seed script |
