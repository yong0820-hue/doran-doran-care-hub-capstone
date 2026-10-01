> 제출본 안내(2026-10-01): 이 코드는 초기 24GHz/GPIO 기반 ESP32 센서 참고 구현입니다. 현재 제품의 레이더 기준은 **60GHz TI IWR6843AOPEVM**이며, 이 보드 연동·레이더 기반 인지게임은 아직 개발 예정입니다. 아래 초기 배선 설명을 60GHz 보드 연결법으로 사용하지 마세요. 실제 config.h는 제외했습니다. 센서 펌웨어의 운영 인증/Rules 호환성과 실물 작동은 별도 통합 검증이 필요합니다.

# ESP32 Sensor Node -- Doran-Doran Care Hub

Firmware for the ESP32 that reads a 24 GHz radar presence sensor and an
emergency call button, then pushes events to Firebase Realtime Database.

---

## 1. Arduino IDE Setup

1. Install **Arduino IDE 2.x** from https://www.arduino.cc/en/software
2. Open **File > Preferences** and add this URL to *Additional boards manager URLs*:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Open **Tools > Board > Boards Manager**, search for `esp32` (by Espressif), and install it.
4. No additional libraries are needed -- `WiFi.h` and `HTTPClient.h` are included in the ESP32 board package.

---

## 2. config.h Setup

```bash
# In this sketch folder:
cp config.example.h config.h
```

Open `config.h` and fill in:

| Define | Example value | Notes |
|---|---|---|
| `WIFI_SSID` | `"MyHomeWiFi"` | 2.4 GHz only (ESP32 limitation) |
| `WIFI_PASSWORD` | `"secret"` | Never commit this file |
| `FIREBASE_DATABASE_URL` | `"https://my-app-default-rtdb.firebaseio.com"` | No trailing slash |
| `ELDER_ID` | `"elder_001"` | Must match guardian dashboard |
| `DEVICE_ID` | `"device_001"` | Must match guardian dashboard |
| `LOCATION_NAME` | `"거실"` | Shown in event messages |

> **Security:** `config.h` is listed in `.gitignore`. Never commit real credentials.

---

## 3. Wiring

```
ESP32 DevKit          Component
-----------------     --------------------------------
GPIO25   ----------- 24 GHz radar OUT pin
GND      ----------- 24 GHz radar GND
3.3V/5V  ----------- 24 GHz radar VCC  (check module datasheet)

GPIO27   ----------- Emergency button terminal A
GND      ----------- Emergency button terminal B
                      (INPUT_PULLUP -- no external resistor needed)

GPIO26               Reserved / not connected
```

**Emergency button circuit:**
```
3.3V (internal pull-up via INPUT_PULLUP)
      |
    GPIO27 ----[button]---- GND

Button open  -> GPIO27 = HIGH  (not pressed)
Button closed -> GPIO27 = LOW  (pressed)
```

---

## 4. Upload Steps

1. Open **`esp32_sensors/esp32_sensors.ino`** in Arduino IDE.
2. Select the board: **Tools > Board > esp32 > ESP32 Dev Module**
3. Select the port: **Tools > Port > COMx** (the port that appears when you plug in the ESP32)
4. Upload speed: keep the default (921600 or 115200 -- both work)
5. Click **Upload** (arrow button).
6. If upload fails with a port error, hold the **BOOT** button on the ESP32 while clicking Upload,
   then release after "Connecting..." appears.

---

## 5. Serial Monitor Test

1. After upload, open **Tools > Serial Monitor** (or Ctrl+Shift+M).
2. Set baud rate to **115200**.
3. Expected boot output:

```
[Boot] Doran-Doran Care Hub -- ESP32 Sensor Node
[Boot] Firmware: May 25 2026 12:00:00
[WiFi] Connecting to "MyHomeWiFi"......
[WiFi] Connected -- IP 192.168.1.42
[NTP]  Syncing....
[NTP]  Synced -> 2026-05-25 12:00:05 UTC
[Boot] Ready.
```

4. Every 2 seconds you will see the radar status:

```
[Radar] pin=1 (ABSENT)  stable=2000ms
```
(With RADAR_ACTIVE_HIGH = false, pin=1/HIGH = absent. pin=0/LOW = present.)

5. When presence is stable for 5 seconds:

```
[Radar] Raw change -- pin=0  interpreted=PRESENT
[Radar] Presence stable 5012ms -- confirmed PRESENT
[Firebase] Sending presence_detected -- eventId=evt_1748000005000
[Firebase] PATCH success (200)
```

6. Pressing the emergency button once:

```
[Emergency] Button pressed -- sending emergency_call
[Firebase] Sending emergency_call -- eventId=evt_1748000010000
[Firebase] PATCH success (200)
```

7. Pressing the button again within 3 seconds (cooldown active):

```
[Emergency] Cooldown active (1842ms left) -- ignored
```

---

## 6. Firebase Paths to Check

Open your Firebase Realtime Database console and verify:

### Events written by the sensor:
```
events/
  elder_001/
    evt_1748000005000/
      deviceId:    "device_001"
      elderId:     "elder_001"
      type:        "presence_detected"
      severity:    "info"
      title:       "Presence Detected"
      message:     "Presence detected in 거실"
      status:      "unread"
      createdAt:   1748000005000
      location:    "거실"

    evt_1748000010000/
      deviceId:    "device_001"
      elderId:     "elder_001"
      type:        "emergency_call"
      severity:    "critical"
      title:       "응급 호출"
      message:     "어르신이 응급 호출 버튼을 눌렀습니다."
      status:      "open"
      createdAt:   1748000010000
      location:    "거실"
```

### Device state updated by the sensor:
```
devices/
  device_001/
    lastSeen:      1748000005000
    lastSignal:    1748000005000
    sensors/
      radar/
        presence:      false        <- boolean, not string
        lastUpdated:   1748000005000
    emergencyStatus:   "active"     <- only after emergency button press
```

---

## 7. Radar Active HIGH / LOW Troubleshooting

The 24 GHz radar OUT pin polarity depends on the module model and firmware.

**Current setting in firmware:**
```cpp
const bool RADAR_ACTIVE_HIGH = false;  // LOW = presence (active-LOW)
```

### How to verify polarity

1. Open Serial Monitor at 115200 baud.
2. Stand in front of the radar so it should detect you.
3. Watch the log:
   ```
   [Radar] pin=0 (PRESENT)  stable=500ms
   ```
   If `pin=0` shows **PRESENT** when you are there -> `RADAR_ACTIVE_HIGH = false` is correct.

4. If `pin=1` shows **PRESENT** when you are there -> your module is active-HIGH.
   Change the constant:
   ```cpp
   const bool RADAR_ACTIVE_HIGH = true;
   ```
   Re-upload and test again.

### Common 24 GHz module behavior

| Module | Default OUT polarity |
|---|---|
| HLK-LD2410 (firmware default) | Active-LOW (LOW = presence) |
| HLK-LD2410B | Active-LOW |
| HLK-LD2410C | Active-LOW |
| Some custom firmware variants | May be active-HIGH -- check with Serial Monitor |

### Presence/absence timing reference

| Event | Stability required |
|---|---|
| `presence_detected` | 5 seconds continuous PRESENT signal |
| `absence_detected` | 20 seconds continuous ABSENT signal |

To adjust thresholds, edit `esp32_sensors.ino`:

```cpp
const unsigned long PRESENCE_STABLE_MS =  5000;  // increase to reduce false positives
const unsigned long ABSENCE_STABLE_MS  = 20000;  // increase to avoid flapping
```

---

## 8. Emergency Button Behavior

| Condition | Result |
|---|---|
| Button pressed (first press) | `emergency_call` event sent to Firebase |
| Button held down | No repeat -- falling-edge detection only |
| Button pressed again within 3 s | Cooldown active -- ignored, logged to Serial Monitor |
| Button pressed after 3 s | New `emergency_call` event sent |

The 3-second cooldown (`EMERGENCY_COOLDOWN_MS`) prevents accidental repeated triggers.
To change the cooldown duration, edit `esp32_sensors.ino`:

```cpp
const unsigned long EMERGENCY_COOLDOWN_MS = 3000;  // milliseconds
```
