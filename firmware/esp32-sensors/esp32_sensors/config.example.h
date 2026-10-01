/*
 * config.example.h — Doran-Doran Care Hub ESP32 Sensor Node
 *
 * SETUP:
 *   1. Copy this file:  config.example.h  →  config.h
 *   2. Fill in your real values in config.h.
 *   3. config.h is listed in .gitignore — never commit it.
 *
 * NOTE: Do not add trailing slashes to FIREBASE_DATABASE_URL.
 * NOTE: ELDER_ID and DEVICE_ID must match the values used in
 *       the guardian dashboard and Firebase rules.
 */

#ifndef CONFIG_H
#define CONFIG_H

// ─── Wi-Fi ───────────────────────────────────────────────────────────────────
#define WIFI_SSID     "your_wifi_ssid"
#define WIFI_PASSWORD "your_wifi_password"

// ─── Firebase Realtime Database ──────────────────────────────────────────────
// Newer projects:  https://your-project-default-rtdb.firebaseio.com
// Older projects:  https://your-project.firebaseio.com
#define FIREBASE_DATABASE_URL "https://your-project-default-rtdb.firebaseio.com"

// ─── Elder & Device Identifiers ──────────────────────────────────────────────
// Must match the elder key used in the guardian dashboard.
#define ELDER_ID "elder_001"

// Unique identifier for this specific ESP32 unit.
// Must match the device key used in the guardian dashboard (e.g. "device_001").
#define DEVICE_ID "device_001"

// Human-readable location shown in event messages and on the dashboard.
// Avoid quotes, backslashes, or other special JSON characters.
#define LOCATION_NAME "거실"

#endif // CONFIG_H
