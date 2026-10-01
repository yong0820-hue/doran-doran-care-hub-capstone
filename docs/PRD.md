> 제출본 안내: 초기 설계 참고 문서입니다. 과거 24GHz·대화 모델·단계별 계획은 최신 구현 상태와 다를 수 있습니다. 현재 범위는 루트 README_SUBMISSION.md와 docs/ROADMAP.md를 우선해 주세요.

# Product Requirements Document — Doran-Doran Care Hub

## Problem Statement

Elderly individuals living alone are at risk from undetected falls, missed medications, and social isolation. Guardians (family members, caregivers) need real-time situational awareness without being physically present.

## Users

| Persona | Description |
|---|---|
| **Senior (elderly user)** | Lives alone or semi-independently. Non-tech-savvy. Interacts via voice or physical button press — never a screen. |
| **Guardian** | Family member or caregiver. Monitors via the web dashboard. Wants instant alerts and event history. |

## MVP Scope (Phase 1–4)

The MVP proves one end-to-end flow:

> **Sensor event → Firebase Realtime Database → Guardian dashboard realtime update**

MVP is considered complete when a guardian can open the dashboard and see a live sensor event appear within seconds of it being triggered — no manual refresh required.

### MVP Features

- Guardian dashboard (web, read-only) showing live sensor events and event history
- Firebase Realtime Database as the single source of truth
- Mock sensor event generator (script that writes events to Firebase)
- Demo data seeding script for the dashboard

### Out of Scope for MVP

- Authentication / multi-user accounts
- Push notifications / SMS alerts
- AI voice features
- Physical hardware (deferred to Phase 5+)
- Settings or configuration UI

## Post-MVP Phases

| Phase | Feature |
|---|---|
| 5 | ESP32 button-based mock sensor (real hardware, same event schema) |
| 6 | Pillbox reed switch firmware |
| 7 | 24GHz radar presence detection |
| 8 | AI voice conversation |
| 9 | Memories-based AI grandchild conversation |

## Success Criteria

- **MVP demo:** Guardian opens the dashboard; a sensor event fires; the dashboard updates in under 3 seconds without a page reload.
- **Phase 9 demo:** Elderly user speaks naturally to the device; it responds as a familiar AI grandchild using stored memories.

## Non-Functional Requirements

- No API keys or secrets in source code (env vars only).
- Dashboard must be usable on a laptop browser without installation.
- Sensor event schema must be consistent across mock scripts and real hardware.
