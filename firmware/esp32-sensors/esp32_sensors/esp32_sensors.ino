/*
 * Doran-Doran Care Hub -- ESP32 Sensor Node
 *
 * Hardware:
 *   GPIO25  24 GHz radar OUT (presence detection, active-LOW)
 *   GPIO16  LD2410C UART TX -> ESP32 RX2 (UART debug read)
 *   GPIO17  LD2410C UART RX -> ESP32 TX2 (unused; wired for future commands)
 *   GPIO27  Emergency call button (INPUT_PULLUP, active-LOW)
 *   GPIO26  Reserved / optional test input (not used)
 *
 * Before building:
 *   Copy config.example.h -> config.h and fill in your values.
 *   config.h is in .gitignore -- never commit real credentials.
 *
 * Arduino IDE board: "ESP32 Dev Module" (esp32 by Espressi-f, any recent version)
 * Required libraries: none beyond the built-in ESP32 board package
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>
#include "config.h"

// --- Pin Definitions ---------------------------------------------------------
#define RADAR_PIN      25
#define EMERGENCY_PIN  27
#define RADAR_RX_PIN   16   // LD2410C TX -> ESP32 RX2
#define RADAR_TX_PIN   17   // LD2410C RX -> ESP32 TX2 (reserved for future commands)

// --- LD2410C UART ------------------------------------------------------------
// LD2410C sends standard data frames at ~10 Hz by default.
// Frame header: F4 F3 F2 F1  |  Frame end: F8 F7 F6 F5
// Data type 0x02 + head 0xAA = standard presence/energy/distance report.
HardwareSerial radarSerial(2);                 // UART2
#define RADAR_UART_BAUD   256000
#define RADAR_BUF_SIZE    128
#define UART_PRINT_INTERVAL_MS  1000UL         // steady-state print throttle

// --- Radar Polarity ----------------------------------------------------------
// false -> LOW = person present (active-LOW module, e.g. HLK-LD2410 default OUT)
// true  -> HIGH = person present (active-HIGH modules)
const bool RADAR_ACTIVE_HIGH = false;

// --- Timing Constants --------------------------------------------------------
const unsigned long PRESENCE_STABLE_MS    =  5000;  // 5 s stable before presence_detected
const unsigned long ABSENCE_STABLE_MS     = 20000;  // 20 s stable before absence_detected
const unsigned long EMERGENCY_DEBOUNCE_MS =    50;  // 50 ms hardware debounce
const unsigned long EMERGENCY_COOLDOWN_MS =  3000;  // 3 s cooldown between emergency events
const unsigned long RADAR_LOG_INTERVAL_MS =  2000;  // 2 s periodic Serial status log

// --- Long Inactivity Detection -----------------------------------------------
// Moving energy at or below MOTION_ACTIVE_THRESHOLD while present = no motion.
// INACTIVITY_DEMO_MS: 30 s for demo. Real deployment value: 1800000 ms (30 min) or more.
const uint8_t       MOTION_ACTIVE_THRESHOLD = 15;
const unsigned long INACTIVITY_DEMO_MS      = 30000;

// --- Radar State -------------------------------------------------------------
bool          rawPresence       = false; // Current interpreted radar reading (unfiltered)
unsigned long rawChangeTime     = 0;     // millis() when raw reading last changed
int           lastSentRadar     = -1;    // -1=none sent, 0=absence sent, 1=presence sent
bool          confirmedPresence = false; // true only after presence_detected is sent
bool          dupLogged         = false; // Suppress repeated duplicate-ignored messages
unsigned long lastRadarLog      = 0;

// --- Emergency Button State --------------------------------------------------
bool          btnPrevRaw       = HIGH;
bool          btnDebounced     = HIGH;
unsigned long btnDebounceStart = 0;
unsigned long lastEmergencyMs  = 0;   // millis() when last emergency event was sent

// --- LD2410C UART State ------------------------------------------------------
uint8_t       radarBuf[RADAR_BUF_SIZE];
int           radarBufLen    = 0;
uint8_t       lastUartState  = 0xFF;           // 0xFF = no frame received yet
unsigned long lastUartPrintMs = 0;

// --- Long Inactivity State ---------------------------------------------------
unsigned long      lastMotionAtMs   = 0;     // millis() of last active-motion reading
unsigned long long lastMotionAtTs   = 0;     // nowMs()  of last active-motion reading
bool               inactivitySent   = false; // event sent for current inactive period
bool               inactivityActive = false; // currently in inactive state

// --- Firebase Base URL -------------------------------------------------------
String fbBaseUrl;


// =============================================================================
// setup / loop
// =============================================================================

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println(F("\n[Boot] Doran-Doran Care Hub -- ESP32 Sensor Node"));
  Serial.println(F("[Boot] Firmware: " __DATE__ " " __TIME__));

  pinMode(RADAR_PIN,    INPUT);
  pinMode(EMERGENCY_PIN, INPUT_PULLUP);

  radarSerial.begin(RADAR_UART_BAUD, SERIAL_8N1, RADAR_RX_PIN, RADAR_TX_PIN);
  Serial.println(F("[RadarUART] UART2 started (256000 baud, RX=GPIO16, TX=GPIO17)"));

  connectWiFi();
  syncNTP();
  lastMotionAtMs = millis();
  lastMotionAtTs = nowMs();

  fbBaseUrl = String(FIREBASE_DATABASE_URL);
  if (fbBaseUrl.endsWith("/"))
    fbBaseUrl.remove(fbBaseUrl.length() - 1);

  Serial.println(F("[Boot] Ready.\n"));
}

void loop() {
  maintainWiFi();
  handleRadar();
  handleEmergency();
  handleRadarUART();
  delay(50);
}


// =============================================================================
// Wi-Fi
// =============================================================================

void connectWiFi() {
  Serial.printf("[WiFi] Connecting to \"%s\"", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\n[WiFi] Connected -- IP %s\n", WiFi.localIP().toString().c_str());
}

void maintainWiFi() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("[WiFi] Connection lost -- reconnecting..."));
    WiFi.disconnect();
    connectWiFi();
  }
}


// =============================================================================
// NTP Time Sync
// =============================================================================

void syncNTP() {
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.print(F("[NTP]  Syncing"));
  struct tm ti;
  for (int i = 0; i < 40; i++) {
    if (getLocalTime(&ti)) break;
    delay(500);
    Serial.print(".");
  }
  if (getLocalTime(&ti)) {
    char buf[32];
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S UTC", &ti);
    Serial.printf("\n[NTP]  Synced -> %s\n", buf);
  } else {
    Serial.println(F("\n[NTP]  Sync FAILED -- timestamps will be wrong"));
  }
}


// =============================================================================
// Timestamp Helpers
// =============================================================================

unsigned long long nowMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (unsigned long long)tv.tv_sec * 1000ULL
       + (unsigned long long)tv.tv_usec / 1000ULL;
}

// Returns Unix-ms as a decimal String safe for JSON embedding.
String tsString() {
  char buf[24];
  snprintf(buf, sizeof(buf), "%llu", nowMs());
  return String(buf);
}

// Returns a unique event ID derived from the millisecond timestamp.
String makeEventId() {
  char id[32];
  snprintf(id, sizeof(id), "evt_%llu", nowMs());
  return String(id);
}


// =============================================================================
// Firebase Multi-Location PATCH
// =============================================================================

bool firebasePatch(const String& body) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("[Firebase] No WiFi -- skipping PATCH"));
    return false;
  }
  HTTPClient http;
  String url = fbBaseUrl + "/.json";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  int code = http.PATCH(body);
  bool ok = (code >= 200 && code < 300);
  if (ok) {
    Serial.printf("[Firebase] PATCH success (%d)\n", code);
  } else {
    Serial.printf("[Firebase] PATCH FAILED (%d): %s\n",
                  code, http.getString().substring(0, 120).c_str());
  }
  http.end();
  return ok;
}


// =============================================================================
// Presence / Absence Event
// =============================================================================

void sendPresenceEvent(bool present) {
  String ts      = tsString();
  String eventId = makeEventId();
  String type    = present ? "presence_detected" : "absence_detected";
  String title   = present ? "Presence Detected"  : "Absence Detected";
  String msg;

  if (present) {
    msg = String("Presence detected in ") + String(LOCATION_NAME);
  } else {
    msg = String("No presence detected in ") + String(LOCATION_NAME);
  }

  String evtPath = String("/events/")  + ELDER_ID  + "/" + eventId;
  String devPath = String("/devices/") + DEVICE_ID;

  String body = "{";
  body += "\"" + evtPath + "\":{"
    "\"deviceId\":\""  + String(DEVICE_ID)      + "\","
    "\"elderId\":\""   + String(ELDER_ID)        + "\","
    "\"type\":\""      + type                    + "\","
    "\"severity\":\"info\","
    "\"title\":\""     + title                   + "\","
    "\"message\":\""   + msg                     + "\","
    "\"status\":\"unread\","
    "\"createdAt\":"   + ts                      + ","
    "\"location\":\""  + String(LOCATION_NAME)   + "\""
  "},";
  body += "\"" + devPath + "/sensors/radar/presence\":"    + (present ? "true" : "false") + ",";
  body += "\"" + devPath + "/sensors/radar/lastUpdated\":" + ts + ",";
  body += "\"" + devPath + "/lastSeen\":"                  + ts + ",";
  body += "\"" + devPath + "/lastSignal\":"                + ts;
  body += "}";

  Serial.printf("[Firebase] Sending %s -- eventId=%s\n", type.c_str(), eventId.c_str());
  firebasePatch(body);
}


// =============================================================================
// Emergency Call Event
// =============================================================================

void sendEmergencyEvent() {
  String ts      = tsString();
  String eventId = makeEventId();

  String evtPath = String("/events/")  + ELDER_ID  + "/" + eventId;
  String devPath = String("/devices/") + DEVICE_ID;

  String body = "{";
  body += "\"" + evtPath + "\":{"
    "\"deviceId\":\""  + String(DEVICE_ID)    + "\","
    "\"elderId\":\""   + String(ELDER_ID)     + "\","
    "\"type\":\"emergency_call\","
    "\"severity\":\"critical\","
    "\"title\":\"\xec\x9d\x91\xea\xb8\x89 \xed\x98\xb8\xec\xb6\x9c\","
    "\"message\":\"\xec\x96\xb4\xeb\xa5\xb4\xec\x8b\xa0\xec\x9d\xb4 \xec\x9d\x91\xea\xb8\x89 \xed\x98\xb8\xec\xb6\x9c \xeb\xb2\x84\xed\x8a\xbc\xec\x9d\x84 \xeb\x88\x8c\xeb\xa0\x80\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4.\","
    "\"status\":\"open\","
    "\"createdAt\":"   + ts                   + ","
    "\"location\":\""  + String(LOCATION_NAME) + "\""
  "},";
  body += "\"" + devPath + "/emergencyStatus\":\"active\",";
  body += "\"" + devPath + "/lastSeen\":"   + ts + ",";
  body += "\"" + devPath + "/lastSignal\":" + ts;
  body += "}";

  Serial.printf("[Firebase] Sending emergency_call -- eventId=%s\n", eventId.c_str());
  firebasePatch(body);
}


// =============================================================================
// Radar Handler
// =============================================================================

void handleRadar() {
  int  pin = digitalRead(RADAR_PIN);
  bool raw = (pin == HIGH) == RADAR_ACTIVE_HIGH;
  unsigned long now = millis();

  // Detect raw state change and reset stability timer.
  if (raw != rawPresence) {
    rawPresence   = raw;
    rawChangeTime = now;
    dupLogged     = false;
    Serial.printf("[Radar] Raw change -- pin=%d  interpreted=%s\n",
                  pin, raw ? "PRESENT" : "ABSENT");
  }

  // Periodic status log every RADAR_LOG_INTERVAL_MS.
  if (now - lastRadarLog >= RADAR_LOG_INTERVAL_MS) {
    lastRadarLog = now;
    Serial.printf("[Radar] pin=%d (%s)  stable=%lums\n",
                  pin,
                  raw ? "PRESENT" : "ABSENT",
                  now - rawChangeTime);
  }

  unsigned long stable = now - rawChangeTime;

  // Presence confirmed after PRESENCE_STABLE_MS.
  if (raw && stable >= PRESENCE_STABLE_MS) {
    if (lastSentRadar != 1) {
      lastSentRadar     = 1;
      confirmedPresence = true;
      dupLogged         = false;
      Serial.printf("[Radar] Presence stable %lums -- confirmed PRESENT\n", stable);
      sendPresenceEvent(true);
    } else if (!dupLogged) {
      dupLogged = true;
      Serial.println(F("[Radar] presence_detected duplicate -- ignored"));
    }
  }

  // Absence confirmed after ABSENCE_STABLE_MS.
  if (!raw && stable >= ABSENCE_STABLE_MS) {
    if (lastSentRadar != 0) {
      lastSentRadar     = 0;
      confirmedPresence = false;
      dupLogged         = false;
      Serial.printf("[Radar] Absence stable %lums -- confirmed ABSENT\n", stable);
      sendPresenceEvent(false);
    } else if (!dupLogged) {
      dupLogged = true;
      Serial.println(F("[Radar] absence_detected duplicate -- ignored"));
    }
  }
}


// =============================================================================
// Emergency Button Handler
// =============================================================================

void handleEmergency() {
  bool raw = digitalRead(EMERGENCY_PIN);
  unsigned long now = millis();

  // Reset debounce timer on any raw reading change.
  if (raw != btnPrevRaw) {
    btnPrevRaw       = raw;
    btnDebounceStart = now;
  }

  // Only act after the reading has been stable for EMERGENCY_DEBOUNCE_MS.
  if (now - btnDebounceStart >= EMERGENCY_DEBOUNCE_MS) {
    // Falling edge: button newly pressed (INPUT_PULLUP -> LOW = pressed).
    if (raw == LOW && btnDebounced != LOW) {
      if (now - lastEmergencyMs < EMERGENCY_COOLDOWN_MS) {
        Serial.printf("[Emergency] Cooldown active (%lums left) -- ignored\n",
                      EMERGENCY_COOLDOWN_MS - (now - lastEmergencyMs));
      } else {
        Serial.println(F("[Emergency] Button pressed -- sending emergency_call"));
        lastEmergencyMs = now;
        sendEmergencyEvent();
      }
    }
    btnDebounced = raw;
  }
}


// =============================================================================
// LD2410C UART Debug Reader
// =============================================================================
//
// LD2410C standard data frame layout (23 bytes total):
//   [0-3]  F4 F3 F2 F1          header
//   [4-5]  0D 00                 data length = 13 (little-endian)
//   [6]    02                    data type (standard report)
//   [7]    AA                    head marker
//   [8]    target_state          0=none 1=moving 2=static 3=both
//   [9-10] moving_distance LE    cm
//   [11]   moving_energy         0-100
//   [12-13]static_distance LE    cm
//   [14]   static_energy         0-100
//   [15-16]detect_distance LE    cm
//   [17]   55                    tail
//   [18]   00                    checksum (always 0 in default mode)
//   [19-22]F8 F7 F6 F5          end marker
// =============================================================================

// Print a parsed standard data frame.
static void printParsedFrame(const uint8_t* data, uint16_t dataLen) {
  if (dataLen < 13) return;
  // data[0]=type, data[1]=head already verified by caller
  uint8_t  state      = data[2];
  uint16_t movingDist = (uint16_t)data[3]  | ((uint16_t)data[4]  << 8);
  uint8_t  movingE    = data[5];
  uint16_t staticDist = (uint16_t)data[6]  | ((uint16_t)data[7]  << 8);
  uint8_t  staticE    = data[8];
  // Run on every frame so inactivity timing is not gated by the print throttle.
  updateInactivity(confirmedPresence, movingE);

  unsigned long now         = millis();
  bool          stateChanged = (state != lastUartState);
  bool          timeToLog    = (now - lastUartPrintMs >= UART_PRINT_INTERVAL_MS);

  if (!stateChanged && !timeToLog) return;

  lastUartState   = state;
  lastUartPrintMs = now;

  const char* stateStr;
  switch (state) {
    case 0x00: stateStr = "NO_TARGET";   break;
    case 0x01: stateStr = "MOVING";      break;
    case 0x02: stateStr = "STATIC";      break;
    case 0x03: stateStr = "MOV+STATIC";  break;
    default:   stateStr = "UNKNOWN";     break;
  }

  Serial.printf("[Radar UART] state=%s moving=%ucm(e=%u) static=%ucm(e=%u)\n",
                stateStr, movingDist, movingE, staticDist, staticE);
}

// Print unknown frame data as raw hex bytes.
static void printRawFrame(const uint8_t* data, uint16_t dataLen) {
  Serial.print(F("[Radar UART RAW]"));
  uint16_t printLen = (dataLen > 24) ? 24 : dataLen;  // cap output width
  for (uint16_t i = 0; i < printLen; i++) {
    Serial.printf(" %02X", data[i]);
  }
  if (dataLen > printLen) Serial.printf(" ...+%u", dataLen - printLen);
  Serial.println();
}

// Try to extract and process one complete LD2410C frame from radarBuf.
// Returns true if a frame was consumed (call again until false).
static bool tryParseRadarFrame() {
  // Need at least 6 bytes to read header + length field.
  if (radarBufLen < 6) return false;

  // Scan for the standard data frame header: F4 F3 F2 F1
  int hPos = -1;
  for (int i = 0; i <= radarBufLen - 4; i++) {
    if (radarBuf[i]   == 0xF4 && radarBuf[i+1] == 0xF3 &&
        radarBuf[i+2] == 0xF2 && radarBuf[i+3] == 0xF1) {
      hPos = i; break;
    }
  }

  if (hPos < 0) {
    // No header -- discard all but the last 3 bytes (partial header guard).
    int keep = (radarBufLen > 3) ? 3 : radarBufLen;
    memmove(radarBuf, radarBuf + radarBufLen - keep, keep);
    radarBufLen = keep;
    return false;
  }

  // Discard garbage bytes before the header.
  if (hPos > 0) {
    memmove(radarBuf, radarBuf + hPos, radarBufLen - hPos);
    radarBufLen -= hPos;
  }

  if (radarBufLen < 6) return false;  // re-check after discard

  // Read declared data length (little-endian at bytes [4-5]).
  uint16_t dataLen  = (uint16_t)radarBuf[4] | ((uint16_t)radarBuf[5] << 8);
  uint16_t totalLen = 4 + 2 + dataLen + 4;  // header + lenField + data + end

  // Reject obviously invalid lengths before buffering more.
  if (dataLen == 0 || dataLen > 80 || totalLen > RADAR_BUF_SIZE) {
    memmove(radarBuf, radarBuf + 1, radarBufLen - 1);
    radarBufLen--;
    return radarBufLen >= 6;
  }

  if (radarBufLen < totalLen) return false;  // incomplete frame, wait for more bytes

  // Verify end marker: F8 F7 F6 F5
  uint16_t ePos = totalLen - 4;
  if (radarBuf[ePos]   != 0xF8 || radarBuf[ePos+1] != 0xF7 ||
      radarBuf[ePos+2] != 0xF6 || radarBuf[ePos+3] != 0xF5) {
    // Bad end marker -- step past the header byte and retry.
    memmove(radarBuf, radarBuf + 1, radarBufLen - 1);
    radarBufLen--;
    return radarBufLen >= 6;
  }

  // Valid frame -- parse data starting at byte 6 (after header + length field).
  const uint8_t* data = radarBuf + 6;
  if (data[0] == 0x02 && data[1] == 0xAA) {
    // Standard data report
    printParsedFrame(data, dataLen);
  } else {
    // Unknown type (engineering mode ACK, command response, etc.) -- raw dump
    printRawFrame(data, dataLen);
  }

  // Remove the consumed frame from the front of the buffer.
  memmove(radarBuf, radarBuf + totalLen, radarBufLen - totalLen);
  radarBufLen -= totalLen;
  return radarBufLen >= 6;
}

// Called every loop(): drain UART RX and process complete frames.
void handleRadarUART() {
  // Append available bytes to the buffer.
  while (radarSerial.available() && radarBufLen < RADAR_BUF_SIZE) {
    radarBuf[radarBufLen++] = (uint8_t)radarSerial.read();
  }
  // Drop overflow bytes (should not happen at normal frame rates).
  if (radarSerial.available() && radarBufLen >= RADAR_BUF_SIZE) {
    radarBufLen = 0;  // reset and resync
    while (radarSerial.available()) radarSerial.read();
    Serial.println(F("[Radar UART] Buffer overflow -- resyncing"));
    return;
  }
  // Process all complete frames currently in the buffer.
  while (tryParseRadarFrame()) {}
}


// =============================================================================
// Long Inactivity Detection
// =============================================================================

// Patch device motion state back to normal when motion resumes.
void updateDeviceMotionNormal() {
  String ts      = tsString();
  String devPath = String("/devices/") + DEVICE_ID;
  String body = "{";
  body += "\"" + devPath + "/sensors/radar/inactivityStatus\":\"normal\",";
  body += "\"" + devPath + "/sensors/radar/lastMotionAt\":"  + ts + ",";
  body += "\"" + devPath + "/sensors/radar/lastUpdated\":"   + ts + ",";
  body += "\"" + devPath + "/lastSeen\":"                    + ts + ",";
  body += "\"" + devPath + "/lastSignal\":"                  + ts;
  body += "}";
  Serial.println(F("[Inactivity] Motion resumed -- patching device inactivityStatus=normal"));
  firebasePatch(body);
}

// Send long_inactivity event and mark device as inactive.
void sendLongInactivityEvent(uint8_t movingEnergy) {
  String ts      = tsString();
  String eventId = makeEventId();

  char lastMoBuf[24];
  snprintf(lastMoBuf, sizeof(lastMoBuf), "%llu", lastMotionAtTs);
  String lastMoTs = String(lastMoBuf);

  String evtPath = String("/events/")  + ELDER_ID  + "/" + eventId;
  String devPath = String("/devices/") + DEVICE_ID;

  String body = "{";
  body += "\"" + evtPath + "\":{"
    "\"deviceId\":\""  + String(DEVICE_ID)    + "\","
    "\"elderId\":\""   + String(ELDER_ID)     + "\","
    "\"type\":\"long_inactivity\","
    "\"severity\":\"warning\","
    "\"title\":\"\xec\x9e\xa5\xec\x8b\x9c\xea\xb0\x84 \xeb\xaf\xb8\xed\x99\x9c\xeb\x8f\x99\","
    "\"message\":\"\xec\x9e\xac\xec\x8b\xa4 \xec\x83\x81\xed\x83\x9c\xec\x97\x90\xec\x84\x9c"
               " \xec\x9d\xbc\xec\xa0\x95 \xec\x8b\x9c\xea\xb0\x84 \xeb\x8f\x99\xec\x95\x88"
               " \xec\x9b\x80\xec\xa7\x81\xec\x9e\x84\xec\x9d\xb4 \xea\xb0\x90\xec\xa7\x80"
               "\xeb\x90\x98\xec\xa7\x80 \xec\x95\x8a\xec\x95\x98\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4.\","
    "\"status\":\"open\","
    "\"createdAt\":"   + ts                    + ","
    "\"location\":\""  + String(LOCATION_NAME) + "\""
  "},";
  body += "\"" + devPath + "/sensors/radar/inactivityStatus\":\"inactive\",";
  body += "\"" + devPath + "/sensors/radar/motionLevel\":"  + String(movingEnergy) + ",";
  body += "\"" + devPath + "/sensors/radar/lastMotionAt\":" + lastMoTs + ",";
  body += "\"" + devPath + "/sensors/radar/lastUpdated\":"  + ts + ",";
  body += "\"" + devPath + "/lastSeen\":"                   + ts + ",";
  body += "\"" + devPath + "/lastSignal\":"                 + ts;
  body += "}";

  Serial.printf("[Inactivity] Sending long_inactivity -- movingE=%u  eventId=%s\n",
                movingEnergy, eventId.c_str());
  firebasePatch(body);
}

// Called on every UART standard frame. Tracks moving energy to detect inactivity.
// presence = confirmedPresence: true only after presence_detected has been sent.
// Wall reflections and empty-room static readings never set confirmedPresence,
// so they fall through the !presence branch and reset the timer harmlessly.
void updateInactivity(bool presence, uint8_t movingEnergy) {
  unsigned long now = millis();

  if (!presence) {
    // No confirmed presence -- could be a wall, empty room, or pre-confirmation
    // radar warm-up. Reset the inactivity timer so it cannot fire.
    static unsigned long lastNoPLog = 0;
    if (now - lastNoPLog >= 5000UL) {
      lastNoPLog = now;
      Serial.printf("[Inactivity] confirmedPresence=false movingE=%u"
                    " -- long_inactivity ignored\n", movingEnergy);
    }
    if (inactivityActive) {
      Serial.println(F("[Inactivity] confirmedPresence=false -- resetting inactivity timer"));
    }
    lastMotionAtMs   = now;
    lastMotionAtTs   = nowMs();
    inactivitySent   = false;
    inactivityActive = false;
    return;
  }

  if (movingEnergy > MOTION_ACTIVE_THRESHOLD) {
    if (inactivityActive) {
      Serial.printf("[Inactivity] confirmedPresence=true  movingE=%u"
                    " -- motion resumed, clearing inactivity\n", movingEnergy);
      updateDeviceMotionNormal();
    }
    lastMotionAtMs   = now;
    lastMotionAtTs   = nowMs();
    inactivityActive = false;
    inactivitySent   = false;
    return;
  }

  // Confirmed present, moving energy at or below threshold.
  inactivityActive      = true;
  unsigned long elapsed = now - lastMotionAtMs;

  if (!inactivitySent && elapsed >= INACTIVITY_DEMO_MS) {
    Serial.printf("[Inactivity] confirmedPresence=true  movingE=%u  elapsed=%lums"
                  " -- sending long_inactivity\n", movingEnergy, elapsed);
    inactivitySent = true;
    sendLongInactivityEvent(movingEnergy);
  } else if (inactivitySent) {
    static unsigned long lastDupLog = 0;
    if (now - lastDupLog >= 10000UL) {
      lastDupLog = now;
      Serial.printf("[Inactivity] long_inactivity duplicate -- ignored"
                    " (movingE=%u elapsed=%lums)\n", movingEnergy, elapsed);
    }
  } else {
    static unsigned long lastWaitLog = 0;
    if (now - lastWaitLog >= 5000UL) {
      lastWaitLog = now;
      Serial.printf("[Inactivity] confirmedPresence=true  movingE=%u"
                    "  elapsed=%lums / %lums\n",
                    movingEnergy, elapsed, (unsigned long)INACTIVITY_DEMO_MS);
    }
  }
}
