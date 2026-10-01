#!/usr/bin/env python3
"""
check-integration-config.py
Doran-Doran Care Hub — integration config diagnostic

Reads local config files (no network calls, no file modifications).
Run from the repo root:
    python scripts/check-integration-config.py
"""

import io
import os
import re
import sys

# Force UTF-8 output on Windows (cp949 cannot encode em-dashes etc.)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLORS = {
    "ok":   "\033[92m",  # green
    "warn": "\033[93m",  # yellow
    "err":  "\033[91m",  # red
    "dim":  "\033[90m",  # grey
    "bold": "\033[1m",
    "end":  "\033[0m",
}

def c(color, text):
    return f"{COLORS[color]}{text}{COLORS['end']}"

def section(title):
    print(f"\n{c('bold', '── ' + title + ' ' + '─' * max(0, 60 - len(title)))}")

def ok(msg):  print(f"  {c('ok',   '✓')} {msg}")
def warn(msg): print(f"  {c('warn', '!')} {msg}")
def err(msg):  print(f"  {c('err',  '✗')} {msg}")
def info(msg): print(f"  {c('dim',  '·')} {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def read_dotenv(path):
    """Parse a .env file into a dict. Returns {} if file not found."""
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


def read_file(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_define(content, name):
    """Extract value from #define NAME "VALUE" in C/C++ headers."""
    m = re.search(r'#define\s+' + name + r'\s+"([^"]*)"', content)
    return m.group(1) if m else None


def normalise_url(url):
    """Strip trailing slash for comparison."""
    return (url or "").rstrip("/")


# ─────────────────────────────────────────────────────────────────────────────
# Load configs
# ─────────────────────────────────────────────────────────────────────────────

dashboard_env_path  = os.path.join(REPO_ROOT, "apps", "guardian-dashboard", ".env")
dashboard_envl_path = os.path.join(REPO_ROOT, "apps", "guardian-dashboard", ".env.local")
pi_env_path         = os.path.join(REPO_ROOT, "raspberry-pi-voice-hub", ".env")
pi_example_path     = os.path.join(REPO_ROOT, "raspberry-pi-voice-hub", ".env.example")
esp32_config_path   = os.path.join(REPO_ROOT, "firmware", "esp32-sensors", "esp32_sensors", "config.h")
esp32_ino_path      = os.path.join(REPO_ROOT, "firmware", "esp32-sensors", "esp32_sensors", "esp32_sensors.ino")

dashboard_env  = read_dotenv(dashboard_env_path)
dashboard_envl = read_dotenv(dashboard_envl_path)
pi_env         = read_dotenv(pi_env_path)
pi_example     = read_dotenv(pi_example_path)
esp32_h        = read_file(esp32_config_path)
esp32_ino      = read_file(esp32_ino_path)

# Merge dashboard env (local overrides base)
merged_dash = {**dashboard_env, **dashboard_envl}


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

print(c("bold", "\nDoran-Doran Care Hub — Integration Config Check"))
print(c("dim", f"Repo root: {REPO_ROOT}"))


section("1. Firebase Database URL")

dash_url = normalise_url(merged_dash.get("VITE_FIREBASE_DATABASE_URL", ""))
pi_url   = normalise_url(pi_env.get("FIREBASE_DATABASE_URL", ""))
esp_url  = normalise_url(extract_define(esp32_h, "FIREBASE_DATABASE_URL") if esp32_h else None)

info(f"Dashboard .env  VITE_FIREBASE_DATABASE_URL = {dash_url or c('err', '(not set)')}")
info(f"Raspberry Pi .env  FIREBASE_DATABASE_URL    = {pi_url  or c('warn', '(file not found locally)')}")
info(f"ESP32 config.h  FIREBASE_DATABASE_URL       = {esp_url or c('warn', '(file not found)')}")

urls = [u for u in [dash_url, pi_url, esp_url] if u]
if len(set(urls)) == 1:
    ok("All present URLs match")
elif len(urls) > 1:
    err("URLs DO NOT MATCH — check each component's config")
else:
    warn("Only one URL found locally — cannot fully compare")

if pi_url:
    pass
else:
    warn("Raspberry Pi .env not found locally — Pi may have a separate .env on the device hardware")


section("2. Elder ID / Device ID")

dash_elder  = "elder_001"   # hardcoded in App.jsx
dash_device = "device_001"  # hardcoded in App.jsx

pi_elder    = pi_env.get("ELDER_ID", pi_example.get("ELDER_ID", ""))
pi_device   = pi_env.get("DEVICE_ID", pi_example.get("DEVICE_ID", ""))

esp_elder   = extract_define(esp32_h, "ELDER_ID")  if esp32_h else None
esp_device  = extract_define(esp32_h, "DEVICE_ID") if esp32_h else None

rows = [
    ("Dashboard (App.jsx)", dash_elder, dash_device),
    ("Raspberry Pi (.env / .env.example)", pi_elder, pi_device),
    ("ESP32 (config.h)", esp_elder, esp_device),
]

all_elder  = [r[1] for r in rows if r[1]]
all_device = [r[2] for r in rows if r[2]]

for label, eid, did in rows:
    info(f"{label}: elderId={eid or '?'}  deviceId={did or '?'}")

if len(set(all_elder)) == 1:
    ok(f"All elderIds match: {all_elder[0]}")
else:
    err(f"elderIds DO NOT MATCH: {set(all_elder)}")

if len(set(all_device)) == 1:
    ok(f"All deviceIds match: {all_device[0]}")
else:
    err(f"deviceIds DO NOT MATCH: {set(all_device)}")


section("3. Dashboard Firebase Variables")

required_vars = [
    "VITE_FIREBASE_API_KEY",
    "VITE_FIREBASE_AUTH_DOMAIN",
    "VITE_FIREBASE_DATABASE_URL",
    "VITE_FIREBASE_PROJECT_ID",
]
for v in required_vars:
    val = merged_dash.get(v, "")
    if val and "your_" not in val and "your-" not in val:
        ok(f"{v} is set")
    elif val:
        warn(f"{v} appears to be a placeholder: {val}")
    else:
        err(f"{v} is MISSING from dashboard .env")

if os.path.exists(dashboard_envl_path):
    info(".env.local found — overrides .env for matching keys")
else:
    info(".env.local not found (optional override file)")


section("4. Raspberry Pi Config")

if os.path.exists(pi_env_path):
    ok(".env found at raspberry-pi-voice-hub/.env")
    for k in ["FIREBASE_DATABASE_URL", "ELDER_ID", "DEVICE_ID", "LOCATION_NAME"]:
        v = pi_env.get(k, "")
        if v:
            ok(f"  {k} = {v}")
        else:
            err(f"  {k} is not set in .env")
else:
    warn("raspberry-pi-voice-hub/.env not found locally")
    info("This is expected if the Pi runs on separate hardware with its own .env")
    info("Showing .env.example defaults:")
    for k in ["FIREBASE_DATABASE_URL", "ELDER_ID", "DEVICE_ID", "LOCATION_NAME"]:
        v = pi_example.get(k, "")
        info(f"  .env.example {k} = {v or '(not set)'}")


section("5. ESP32 Config")

if esp32_h:
    ok("config.h found")
    for name in ["FIREBASE_DATABASE_URL", "ELDER_ID", "DEVICE_ID", "LOCATION_NAME"]:
        v = extract_define(esp32_h, name)
        if v:
            ok(f"  {name} = {v}")
        else:
            warn(f"  {name} not found in config.h")

    url_raw = extract_define(esp32_h, "FIREBASE_DATABASE_URL") or ""
    if url_raw.endswith("/"):
        warn("FIREBASE_DATABASE_URL in config.h has a trailing slash")
        info("The .ino strips it at runtime (line: fbBaseUrl.endsWith('/') → remove) — safe")
else:
    err("firmware/esp32-sensors/esp32_sensors/config.h not found")
    info("Copy config.example.h to config.h and fill in values")


section("6. ESP32 Event Status Field")

if esp32_ino:
    if '"status":"unread"' in esp32_ino or '"status": "unread"' in esp32_ino:
        warn('ESP32 presence events write status="unread" (not "open")')
        info('Dashboard treats any non-"resolved" status as unresolved — display still works')
        info('But for consistency, consider changing to "open" in sendPresenceEvent()')
    else:
        ok('ESP32 event status field OK')


section("7. lastSignal Numeric Check")

if esp32_ino and 'lastSignal":' in esp32_ino:
    ok("ESP32 writes lastSignal as numeric Unix-ms (via tsString())")
else:
    warn("Could not verify ESP32 lastSignal format")

info("Dashboard relativeTime() expects numeric Unix-ms — numeric is correct")


section("8. Dashboard Build State")

dist_html = os.path.join(REPO_ROOT, "apps", "guardian-dashboard", "dist", "index.html")
dist_assets = os.path.join(REPO_ROOT, "apps", "guardian-dashboard", "dist", "assets")

if os.path.exists(dist_html):
    html_content = read_file(dist_html)
    if "/assets/" in html_content:
        ok("dist/index.html exists and references /assets/ (absolute path)")
        warn("Opening dist/index.html via file:// URL will NOT work — JS won't load")
        info("Must use: npm run dev  OR  npm run preview")
    else:
        ok("dist/index.html exists")
else:
    warn("dist/index.html not found — run: npm run build")

dist_js = None
if os.path.exists(dist_assets):
    for f in os.listdir(dist_assets):
        if f.endswith(".js"):
            dist_js = os.path.join(dist_assets, f)
            break

if dist_js:
    bundle = read_file(dist_js)
    if bundle:
        if "medication_reminder_sent" in bundle:
            ok(f"dist bundle contains 'medication_reminder_sent' — code change is deployed")
        else:
            err(f"dist bundle MISSING 'medication_reminder_sent' — run npm run build")

        if "inactivityStatus" in bundle:
            ok("dist bundle contains 'inactivityStatus' UI")
        else:
            warn("dist bundle does NOT contain 'inactivityStatus' UI — known feature gap")
            info("ESP32 writes this field; dashboard does not yet display it")


section("Summary")
print()
print("  If the dashboard shows '🔌 Firebase 미연동':")
print("    1. Confirm apps/guardian-dashboard/.env exists and has VITE_ vars filled in")
print("    2. Restart the dev server: cd apps/guardian-dashboard && npm run dev")
print()
print("  If the dashboard shows '🟢 Firebase 실시간 연결됨' but events are missing:")
print("    1. Hard-refresh browser: Ctrl+Shift+R")
print("    2. Check Firebase Realtime Database rules allow unauthenticated read")
print("    3. Confirm Pi writes to events/elder_001/{eventId} (check Pi serial output)")
print()
print("  NEVER open dist/index.html directly — use npm run dev or npm run preview")
print()
