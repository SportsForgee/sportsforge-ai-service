/* ============================================================================
   FORGE INSOLE - ESP32 prototype firmware (WiFi + WebSocket)
   TechXM (Pty) Ltd | SA SportForge Platform

   Same sensing as ForgeInsole_Firmware.ino - 100 Hz pressure + IMU sampling,
   Section 8 gait metrics - but the app link is WiFi + WebSocket instead of BLE.

   Why: BLE works from a phone but a Windows host can connect and then discover
   only the standard services, which makes bench testing on a laptop painful.
   WiFi + WebSocket has none of that: any browser on the same network can
   connect, no pairing, no adapter quirks, no OS Bluetooth cache.

   Connectivity mirrors the Cultiva plant-watering ESP32 exactly:
     - WiFi STA with auto-reconnect
     - mDNS so you can use a name instead of hunting for the IP
     - HTTP  :80  GET /device-info  -> JSON identity (CORS enabled)
     - WS    :81  password auth, then live telemetry pushed at 10 Hz

   LIBRARIES (both already installed for the watering project)
     WebSockets by Markus Sattler (arduinoWebSockets)   2.7.2
     ArduinoJson by Benoit Blanchon                     7.4.3
   Everything else ships with the ESP32 board package.

   Board: "ESP32 Dev Module".  Serial: 115200.  Type '?' + Enter for commands.
   ============================================================================ */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <WebSocketsServer.h>
#include <ArduinoJson.h>

// ---------------------------------------------------------------------------
// Network - change these two lines for your network
// ---------------------------------------------------------------------------
const char *WIFI_SSID = "DANIEL";
const char *WIFI_PASS = "59+G012f";

const char *DEVICE_PASSWORD = "112233";          // app must send this to subscribe
const char *DEVICE_ID       = "FRG-2026-P01";    // P01 = left, P02 = right
const char *DEVICE_NAME     = "Forge Insole (L)";
const char *MDNS_HOST       = "forge-insole-l";  // -> http://forge-insole-l.local

#define UNIT_FOOT   "L"
#define UNIT_PAIR   "PAIR-01"
#define FW_VERSION  "1.0.1-wifi"

static const uint16_t HTTP_PORT = 80;
static const uint16_t WS_PORT   = 81;

// ---------------------------------------------------------------------------
// Pin map - ESP32_Pin_Mapping_and_Firmware_Requirements.docx
// ---------------------------------------------------------------------------
static const int PIN_SDA       = 21;
static const int PIN_SCL       = 22;
static const int PIN_FSR_HEEL  = 34;
static const int PIN_FSR_TOE   = 35;
static const int PIN_FSR_LEFT  = 32;
static const int PIN_FSR_RIGHT = 33;

// ---------------------------------------------------------------------------
// Rates
// ---------------------------------------------------------------------------
static const uint32_t SAMPLE_HZ = 100;                    // concept doc sec 12.2
static const uint32_t SAMPLE_US = 1000000UL / SAMPLE_HZ;
static const uint32_t PRINT_HZ  = 10;
static const uint32_t STREAM_HZ = 10;                     // WebSocket push rate

// ---------------------------------------------------------------------------
// Gait thresholds (sum of the four calibrated zones, ADC counts)
// ---------------------------------------------------------------------------
static uint16_t contactOnThreshold  = 300;
static uint16_t contactOffThreshold = 180;
static const uint32_t MIN_CONTACT_MS = 80;
static const uint32_t MAX_CONTACT_MS = 3000;

// ---------------------------------------------------------------------------
// MPU6050 - direct register access, no driver library (clone-friendly)
// ---------------------------------------------------------------------------
static const uint8_t MPU_ADDR_PRIMARY = 0x68;
static const uint8_t MPU_ADDR_ALT     = 0x69;
static uint8_t       mpuAddr          = MPU_ADDR_PRIMARY;

static const uint8_t REG_SMPLRT_DIV = 0x19;
static const uint8_t REG_CONFIG     = 0x1A;
static const uint8_t REG_GYRO_CFG   = 0x1B;
static const uint8_t REG_ACCEL_CFG  = 0x1C;
static const uint8_t REG_ACCEL_XOUT = 0x3B;
static const uint8_t REG_PWR_MGMT_1 = 0x6B;
static const uint8_t REG_WHO_AM_I   = 0x75;

static const float ACCEL_LSB_PER_G  = 4096.0f;   // +/-8 g
static const float GYRO_LSB_PER_DPS = 32.8f;     // +/-1000 dps

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
struct Zones { uint16_t heel, toe, left, right; };

static Zones raw      = {0, 0, 0, 0};
static Zones baseline = {0, 0, 0, 0};
static Zones zone     = {0, 0, 0, 0};
static Zones peak     = {0, 0, 0, 0};

static float ax = 0, ay = 0, az = 0;
static float gx = 0, gy = 0, gz = 0;
static float gyroBiasX = 0, gyroBiasY = 0, gyroBiasZ = 0;
static float mpuTempC = 0;

static bool     mpuOk = false;
static uint32_t mpuReadFailures = 0;

// An MPU6050 that browns out resets into SLEEP mode. It still acknowledges its
// I2C address, so the read "succeeds" and mpuOk stays true - but every register
// reads 0x00, which surfaces as a dead-flat 0.00 g on all three axes and a
// temperature of exactly 36.5 C (the offset applied to a raw zero). WiFi TX
// bursts pull ~250 mA and can sag a shared 3.3V rail enough to trigger it.
// These track that state so the firmware can re-initialise itself.
static bool     mpuFrameAllZero = false;
static uint32_t mpuZeroFrames   = 0;
static uint32_t mpuRecoveries   = 0;
static bool     mpuStalled      = false;

static bool     inContact = false;
static uint32_t contactStartMs = 0;
static uint32_t lastContactStartMs = 0;
static uint32_t stepCount = 0;
static float    avgContactMs = 0;
static float    cadenceSpm = 0;
static const char *lastStrike = "--";
static Zones    strikeSnapshot = {0, 0, 0, 0};
static bool     strikeCaptured = false;

static uint32_t seq = 0;
static uint32_t sessionStartMs = 0;
static bool     printEnabled = true;

// Network state
static WebServer        httpServer(HTTP_PORT);
static WebSocketsServer webSocket(WS_PORT);
static bool clientAuthenticated[WEBSOCKETS_SERVER_CLIENT_MAX] = {false};
static bool webSocketStarted = false;
static bool wifiConnected    = false;
static uint32_t framesSent   = 0;

static inline uint16_t subFloor(uint16_t v, uint16_t b) { return v > b ? (uint16_t)(v - b) : 0; }
static inline uint32_t zoneTotal() { return (uint32_t)zone.heel + zone.toe + zone.left + zone.right; }
static uint8_t authenticatedCount();

// ===========================================================================
// MPU6050
// ===========================================================================
static bool mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(mpuAddr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool mpuReadBytes(uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(mpuAddr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)mpuAddr, (int)len, (int)true) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

static bool mpuPresentAt(uint8_t addr) {
  uint8_t saved = mpuAddr;
  mpuAddr = addr;
  uint8_t who = 0;
  bool got = mpuReadBytes(REG_WHO_AM_I, &who, 1);
  bool ok = got && (who == 0x68 || who == 0x70 || who == 0x71 || who == 0x72 || who == 0x73);
  if (!ok) mpuAddr = saved;
  return ok;
}

static bool mpuPresent() { return mpuPresentAt(mpuAddr); }

static bool mpuFind() {
  if (mpuPresentAt(MPU_ADDR_PRIMARY)) return true;
  if (mpuPresentAt(MPU_ADDR_ALT)) {
    Serial.println(F("[i2c] MPU6050 answered on 0x69 (AD0 tied high)."));
    return true;
  }
  return false;
}

static bool mpuInit() {
  if (!mpuFind()) return false;
  mpuWrite(REG_PWR_MGMT_1, 0x80);
  delay(100);
  mpuWrite(REG_PWR_MGMT_1, 0x01);
  delay(10);
  mpuWrite(REG_CONFIG,     0x03);
  mpuWrite(REG_SMPLRT_DIV, 0x09);
  mpuWrite(REG_ACCEL_CFG,  0x10);
  mpuWrite(REG_GYRO_CFG,   0x10);
  delay(20);
  return mpuPresent();
}

static bool mpuReadAll() {
  uint8_t b[14];
  if (!mpuReadBytes(REG_ACCEL_XOUT, b, 14)) return false;
  int16_t rax = (int16_t)((b[0]  << 8) | b[1]);
  int16_t ray = (int16_t)((b[2]  << 8) | b[3]);
  int16_t raz = (int16_t)((b[4]  << 8) | b[5]);
  int16_t rt  = (int16_t)((b[6]  << 8) | b[7]);
  int16_t rgx = (int16_t)((b[8]  << 8) | b[9]);
  int16_t rgy = (int16_t)((b[10] << 8) | b[11]);
  int16_t rgz = (int16_t)((b[12] << 8) | b[13]);

  // All seven registers reading zero at once is impossible for a live MEMS part
  // - there is always noise and gravity - so it means the chip is asleep.
  mpuFrameAllZero = ((rax | ray | raz | rt | rgx | rgy | rgz) == 0);

  ax = rax / ACCEL_LSB_PER_G;
  ay = ray / ACCEL_LSB_PER_G;
  az = raz / ACCEL_LSB_PER_G;
  gx = rgx / GYRO_LSB_PER_DPS - gyroBiasX;
  gy = rgy / GYRO_LSB_PER_DPS - gyroBiasY;
  gz = rgz / GYRO_LSB_PER_DPS - gyroBiasZ;
  mpuTempC = rt / 340.0f + 36.53f;
  return true;
}

static void mpuCalibrateGyro(uint16_t samples = 200) {
  if (!mpuOk) return;
  Serial.println(F("[cal] Calibrating gyro - keep the insole completely still..."));
  gyroBiasX = gyroBiasY = gyroBiasZ = 0;
  double sx = 0, sy = 0, sz = 0;
  uint16_t got = 0;
  for (uint16_t i = 0; i < samples; i++) {
    if (mpuReadAll()) { sx += gx; sy += gy; sz += gz; got++; }
    delay(5);
  }
  if (got > 0) { gyroBiasX = sx / got; gyroBiasY = sy / got; gyroBiasZ = sz / got; }
  Serial.printf("[cal] Gyro bias: X %.2f  Y %.2f  Z %.2f deg/s (%u samples)\n",
                gyroBiasX, gyroBiasY, gyroBiasZ, got);
}

// ===========================================================================
// Pressure
// ===========================================================================
// Anything this close to baseline is reported as exactly 0. Two things leave a zone
// sitting a few tens of counts high after the load comes off: the FSR itself creeps
// for a second or two, and a high-impedance ADC input holds residual charge. Without a
// floor, both show up as phantom load that never quite returns to zero.
static const uint16_t ZONE_NOISE_FLOOR = 60;

// While a zone is unloaded, its baseline slowly follows the raw reading so thermal
// drift and slow creep are absorbed instead of surfacing as a constant offset. It only
// runs when the whole foot is off the ground (see readAllFsr), never faster than
// DRIFT_STEP_EVERY samples, and never when the raw value is far enough above baseline
// to be a real load - so a sustained press can't be "learned" as zero.
static const uint16_t DRIFT_TRACK_BAND = 150;   // counts above baseline still treated as drift
static const uint8_t  DRIFT_STEP_EVERY = 10;    // 100 Hz / 10 = one count of correction per 100 ms

static uint16_t readFsr(int pin) {
  // The ESP32 ADC shares one sample-and-hold capacitor across every channel. Switching
  // to a new pin leaves the previous pin's charge sitting on it, and an unloaded FSR is
  // a multi-megohm source that cannot pull it off in time - so the first conversion
  // after a switch reads the *previous* sensor, and pressing one zone bleeds into the
  // next one sampled. Throw that conversion away and give the input a moment to settle
  // before the samples that count.
  (void)analogRead(pin);
  delayMicroseconds(25);
  uint32_t acc = 0;
  for (uint8_t i = 0; i < 4; i++) acc += analogRead(pin);
  return (uint16_t)(acc / 4);
}

// Calibrated zone value: raw minus baseline, floored at ZONE_NOISE_FLOOR, with slow
// baseline tracking while unloaded. 'trackDrift' is only true when nothing is on the
// insole at all, so one zone can't be re-zeroed while the foot is partly loaded.
static uint16_t calibrateZone(uint16_t rawV, uint16_t &base, bool trackDrift) {
  uint16_t z = subFloor(rawV, base);
  if (z >= ZONE_NOISE_FLOOR) return z;

  if (trackDrift) {
    int32_t d = (int32_t)rawV - (int32_t)base;
    if (d > 0 && d < DRIFT_TRACK_BAND) base++;
    else if (d < 0)                    base--;
  }
  return 0;
}

static void readAllFsr() {
  raw.heel  = readFsr(PIN_FSR_HEEL);
  raw.toe   = readFsr(PIN_FSR_TOE);
  raw.left  = readFsr(PIN_FSR_LEFT);
  raw.right = readFsr(PIN_FSR_RIGHT);

  // Only let baselines drift when the previous sample showed the foot fully off the
  // ground, and only every DRIFT_STEP_EVERY samples, so the correction is gentle.
  static uint8_t driftTick = 0;
  bool track = !inContact && (++driftTick >= DRIFT_STEP_EVERY);
  if (track) driftTick = 0;

  zone.heel  = calibrateZone(raw.heel,  baseline.heel,  track);
  zone.toe   = calibrateZone(raw.toe,   baseline.toe,   track);
  zone.left  = calibrateZone(raw.left,  baseline.left,  track);
  zone.right = calibrateZone(raw.right, baseline.right, track);

  if (zone.heel  > peak.heel)  peak.heel  = zone.heel;
  if (zone.toe   > peak.toe)   peak.toe   = zone.toe;
  if (zone.left  > peak.left)  peak.left  = zone.left;
  if (zone.right > peak.right) peak.right = zone.right;
}

static void captureBaseline(uint16_t samples = 100) {
  Serial.println(F("[cal] Capturing unloaded pressure baseline - keep weight off the insole..."));
  uint32_t h = 0, t = 0, l = 0, r = 0;
  for (uint16_t i = 0; i < samples; i++) {
    h += readFsr(PIN_FSR_HEEL);
    t += readFsr(PIN_FSR_TOE);
    l += readFsr(PIN_FSR_LEFT);
    r += readFsr(PIN_FSR_RIGHT);
    delay(3);
  }
  baseline.heel  = h / samples;
  baseline.toe   = t / samples;
  baseline.left  = l / samples;
  baseline.right = r / samples;
  Serial.printf("[cal] Baseline: heel %u  toe %u  left %u  right %u\n",
                baseline.heel, baseline.toe, baseline.left, baseline.right);
}

// ===========================================================================
// Gait metrics - concept doc Section 8
// ===========================================================================
static void classifyStrike() {
  uint32_t h = strikeSnapshot.heel, t = strikeSnapshot.toe;
  if (h + t < 50)     lastStrike = "Unknown";
  else if (h > 2 * t) lastStrike = "Heel";
  else if (t > 2 * h) lastStrike = "Forefoot";
  else                lastStrike = "Midfoot";
}

static void updateGait(uint32_t nowMs) {
  uint32_t total = zoneTotal();

  if (!inContact && total >= contactOnThreshold) {
    inContact = true;
    contactStartMs = nowMs;
    strikeCaptured = false;
  } else if (inContact) {
    if (!strikeCaptured && (nowMs - contactStartMs) >= 20) {
      strikeSnapshot = zone;
      strikeCaptured = true;
      classifyStrike();
    }
    if (total <= contactOffThreshold) {
      uint32_t contactMs = nowMs - contactStartMs;
      inContact = false;
      if (contactMs >= MIN_CONTACT_MS && contactMs <= MAX_CONTACT_MS) {
        stepCount++;
        avgContactMs = (avgContactMs == 0) ? contactMs : (avgContactMs * 0.8f + contactMs * 0.2f);
        if (lastContactStartMs > 0) {
          uint32_t stride = contactStartMs - lastContactStartMs;
          if (stride > 200 && stride < 3000) {
            float spm = 60000.0f / stride;
            cadenceSpm = (cadenceSpm == 0) ? spm : (cadenceSpm * 0.7f + spm * 0.3f);
          }
        }
        lastContactStartMs = contactStartMs;
      }
    }
  }

  if (lastContactStartMs > 0 && (nowMs - lastContactStartMs) > 4000) cadenceSpm = 0;
}

static float balanceML() {
  uint32_t s = (uint32_t)zone.left + zone.right;
  if (s < 30) return 0;
  return ((float)zone.right - (float)zone.left) * 100.0f / (float)s;
}

static float balanceFR() {
  uint32_t s = (uint32_t)zone.heel + zone.toe;
  if (s < 30) return 0;
  return ((float)zone.toe - (float)zone.heel) * 100.0f / (float)s;
}

static void resetSession() {
  stepCount = 0;
  avgContactMs = 0;
  cadenceSpm = 0;
  lastStrike = "--";
  lastContactStartMs = 0;
  inContact = false;
  peak = {0, 0, 0, 0};
  sessionStartMs = millis();
  seq = 0;
  Serial.println(F("[session] Metrics reset - new capture session started."));
}

// ===========================================================================
// Telemetry payload
// ===========================================================================
// Built with snprintf rather than ArduinoJson on purpose: this runs 10x a
// second, the shape is fixed, and the field names deliberately match the BLE
// build's status JSON (concept doc Section 13.4) so the Forge Data API ingests
// either transport with no translation layer.
static void buildTelemetryJson(char *out, size_t len) {
  snprintf(out, len,
    "{\"type\":\"sensor_data\",\"deviceId\":\"%s\",\"foot\":\"%s\",\"pair\":\"%s\","
    "\"fw\":\"%s\",\"uptimeSec\":%lu,\"sessionSec\":%lu,\"sampleRateHz\":%lu,\"seq\":%lu,"
    "\"mpuOk\":%s,\"mpuStalled\":%s,\"mpuRecoveries\":%lu,\"mpuReadFailures\":%lu,"
    "\"pressure\":{\"heel\":%u,\"toe\":%u,\"left\":%u,\"right\":%u,\"total\":%lu},"
    "\"raw\":{\"heel\":%u,\"toe\":%u,\"left\":%u,\"right\":%u},"
    "\"imu\":{\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f,\"gx\":%.2f,\"gy\":%.2f,\"gz\":%.2f,\"tempC\":%.1f},"
    "\"steps\":%lu,\"cadenceSpm\":%.1f,\"avgContactMs\":%.0f,\"footStrike\":\"%s\","
    "\"peak\":{\"heel\":%u,\"toe\":%u,\"left\":%u,\"right\":%u},"
    "\"balanceMlPct\":%.1f,\"balanceFrPct\":%.1f,\"contact\":%s,\"timestamp\":%lu}",
    DEVICE_ID, UNIT_FOOT, UNIT_PAIR, FW_VERSION,
    (unsigned long)(millis() / 1000),
    (unsigned long)((millis() - sessionStartMs) / 1000),
    (unsigned long)SAMPLE_HZ, (unsigned long)seq,
    mpuOk ? "true" : "false", mpuStalled ? "true" : "false",
    (unsigned long)mpuRecoveries, (unsigned long)mpuReadFailures,
    zone.heel, zone.toe, zone.left, zone.right, (unsigned long)zoneTotal(),
    raw.heel, raw.toe, raw.left, raw.right,
    ax, ay, az, gx, gy, gz, mpuTempC,
    (unsigned long)stepCount, cadenceSpm, avgContactMs, lastStrike,
    peak.heel, peak.toe, peak.left, peak.right,
    balanceML(), balanceFR(), inContact ? "true" : "false",
    (unsigned long)millis());
}

static void sendTelemetryTo(uint8_t num) {
  if (!clientAuthenticated[num]) return;
  char buf[896];
  buildTelemetryJson(buf, sizeof(buf));
  webSocket.sendTXT(num, buf);
}

static void broadcastTelemetry() {
  if (!webSocketStarted) return;
  char buf[896];
  buildTelemetryJson(buf, sizeof(buf));
  for (uint8_t i = 0; i < WEBSOCKETS_SERVER_CLIENT_MAX; i++) {
    if (clientAuthenticated[i]) {
      webSocket.sendTXT(i, buf);
      framesSent++;
    }
  }
}

static uint8_t authenticatedCount() {
  uint8_t n = 0;
  for (uint8_t i = 0; i < WEBSOCKETS_SERVER_CLIENT_MAX; i++) if (clientAuthenticated[i]) n++;
  return n;
}

// ===========================================================================
// HTTP
// ===========================================================================
static void handleDeviceInfo() {
  JsonDocument doc;
  doc["deviceId"]     = DEVICE_ID;
  doc["deviceName"]   = DEVICE_NAME;
  doc["type"]         = "ESP32";
  doc["foot"]         = UNIT_FOOT;
  doc["pair"]         = UNIT_PAIR;
  doc["fw"]           = FW_VERSION;
  doc["wsPort"]       = WS_PORT;
  doc["requiresAuth"] = true;
  doc["sampleRateHz"] = SAMPLE_HZ;
  doc["streamHz"]     = STREAM_HZ;
  doc["mpuOk"]        = mpuOk;

  String response;
  serializeJson(doc, response);
  httpServer.sendHeader("Access-Control-Allow-Origin", "*");
  httpServer.send(200, "application/json", response);
}

static void handleCors() {
  httpServer.sendHeader("Access-Control-Allow-Origin", "*");
  httpServer.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  httpServer.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  httpServer.send(204);
}

// Plain-text landing page, so hitting the IP in a browser confirms the device
// is alive without any tooling at all.
static void handleRoot() {
  char body[512];
  snprintf(body, sizeof(body),
    "Forge Insole %s (%s foot)\nfirmware %s\nMPU6050: %s\nsteps: %lu\n\n"
    "WebSocket telemetry: ws://%s:%u  (send {\"type\":\"auth\",\"password\":\"...\"} first)\n"
    "Identity JSON:       http://%s/device-info\n",
    DEVICE_ID, UNIT_FOOT, FW_VERSION, mpuOk ? "OK" : "NOT DETECTED",
    (unsigned long)stepCount,
    WiFi.localIP().toString().c_str(), WS_PORT,
    WiFi.localIP().toString().c_str());
  httpServer.sendHeader("Access-Control-Allow-Origin", "*");
  httpServer.send(200, "text/plain", body);
}

static void startHTTPServer() {
  httpServer.on("/",            HTTP_GET,     handleRoot);
  httpServer.on("/device-info", HTTP_GET,     handleDeviceInfo);
  httpServer.on("/device-info", HTTP_OPTIONS, handleCors);
  httpServer.begin();
  Serial.printf("[http] server started on port %u\n", HTTP_PORT);
}

// ===========================================================================
// WebSocket
// ===========================================================================
static void applySettings(JsonDocument &doc) {
  if (doc["contactOn"].is<int>()) {
    contactOnThreshold = constrain(doc["contactOn"].as<int>(), 50, 4000);
  }
  if (doc["contactOff"].is<int>()) {
    contactOffThreshold = constrain(doc["contactOff"].as<int>(), 20, (int)contactOnThreshold - 20);
  }
  Serial.printf("[cfg] contact on %u / off %u\n", contactOnThreshold, contactOffThreshold);
}

static void webSocketEvent(uint8_t num, WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      clientAuthenticated[num] = false;
      Serial.printf("[ws] client %u disconnected (%u still authenticated)\n", num, authenticatedCount());
      break;

    case WStype_CONNECTED: {
      clientAuthenticated[num] = false;
      IPAddress ip = webSocket.remoteIP(num);
      Serial.printf("[ws] client %u connected from %s - awaiting auth\n", num, ip.toString().c_str());
      JsonDocument doc;
      doc["type"]    = "auth_required";
      doc["message"] = "Please authenticate";
      char buf[128];
      serializeJson(doc, buf);
      webSocket.sendTXT(num, buf);
      break;
    }

    case WStype_TEXT: {
      JsonDocument doc;
      if (deserializeJson(doc, payload, length)) break;

      const char *msgType = doc["type"];
      if (msgType == nullptr) break;

      if (strcmp(msgType, "auth") == 0) {
        const char *password = doc["password"];
        if (password != nullptr && strcmp(password, DEVICE_PASSWORD) == 0) {
          clientAuthenticated[num] = true;
          JsonDocument response;
          response["type"]       = "auth_success";
          response["deviceId"]   = DEVICE_ID;
          response["deviceName"] = DEVICE_NAME;
          response["fw"]         = FW_VERSION;
          char buf[192];
          serializeJson(response, buf);
          webSocket.sendTXT(num, buf);
          Serial.printf("[ws] client %u authenticated\n", num);
          sendTelemetryTo(num);
        } else {
          JsonDocument response;
          response["type"] = "auth_failed";
          char buf[96];
          serializeJson(response, buf);
          webSocket.sendTXT(num, buf);
          Serial.printf("[ws] client %u sent a wrong password\n", num);
        }
        break;
      }

      if (!clientAuthenticated[num]) break;

      // Same three commands the BLE build accepted, so the app logic is identical.
      if (strcmp(msgType, "command") == 0) {
        const char *cmd = doc["cmd"];
        if (cmd == nullptr) break;
        Serial.printf("[ws] command '%s' from client %u\n", cmd, num);
        if      (strcmp(cmd, "zero")  == 0) captureBaseline();
        else if (strcmp(cmd, "reset") == 0) resetSession();
        else if (strcmp(cmd, "gyro")  == 0) mpuCalibrateGyro();
        sendTelemetryTo(num);
        break;
      }

      if (strcmp(msgType, "settings") == 0) {
        applySettings(doc);
        sendTelemetryTo(num);
        break;
      }
      break;
    }

    default:
      break;
  }
}

static void startWebSocket() {
  if (webSocketStarted) return;
  webSocket.begin();
  webSocket.onEvent(webSocketEvent);
  webSocketStarted = true;
  Serial.printf("[ws] server started on port %u\n", WS_PORT);
}

static void printNetworkInfo() {
  Serial.println(F("\n-------------------------------------------"));
  Serial.printf("  WiFi     : %s\n", WiFi.SSID().c_str());
  Serial.printf("  IP       : %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("  Signal   : %d dBm\n", WiFi.RSSI());
  Serial.printf("  Dashboard: http://%s/\n", WiFi.localIP().toString().c_str());
  Serial.printf("  Identity : http://%s/device-info\n", WiFi.localIP().toString().c_str());
  Serial.printf("  Telemetry: ws://%s:%u   (password %s)\n",
                WiFi.localIP().toString().c_str(), WS_PORT, DEVICE_PASSWORD);
  Serial.printf("  mDNS     : http://%s.local/\n", MDNS_HOST);
  Serial.println(F("-------------------------------------------\n"));
}

static void connectWiFi() {
  Serial.printf("[wifi] connecting to \"%s\"", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // keeps the 10 Hz stream smooth
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000UL) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    if (MDNS.begin(MDNS_HOST)) {
      MDNS.addService("http", "tcp", HTTP_PORT);
      MDNS.addService("ws",   "tcp", WS_PORT);
    }
    startHTTPServer();
    startWebSocket();
    printNetworkInfo();
  } else {
    wifiConnected = false;
    Serial.println(F("[wifi] *** NOT CONNECTED *** - check WIFI_SSID / WIFI_PASS at the top"));
    Serial.println(F("[wifi] sensing still runs; it will keep retrying every 5 s."));
  }
}

// ===========================================================================
// Serial output and commands
// ===========================================================================
static void printHelp() {
  Serial.println(F("\n--- Commands (type the letter, then Enter) ---"));
  Serial.println(F("  p   pause / resume printing"));
  Serial.println(F("  z   re-zero the pressure baseline (keep the insole unloaded)"));
  Serial.println(F("  g   re-calibrate the gyro (keep the insole still)"));
  Serial.println(F("  r   reset session metrics"));
  Serial.println(F("  i   device / network info"));
  Serial.println(F("  s   run the sensor self-test again"));
  Serial.println(F("  to<n> / tf<n>  contact on/off threshold (e.g. to400)"));
  Serial.println(F("  ?   this help\n"));
}

static void printInfo() {
  Serial.println(F("\n--- Forge Insole (WiFi) ---"));
  Serial.printf("  Device       : %s (%s foot, %s)\n", DEVICE_ID, UNIT_FOOT, UNIT_PAIR);
  Serial.printf("  Firmware     : %s\n", FW_VERSION);
  Serial.printf("  Sample rate  : %lu Hz   Stream: %lu Hz\n",
                (unsigned long)SAMPLE_HZ, (unsigned long)STREAM_HZ);
  Serial.printf("  MPU6050      : %s (read failures %lu)\n",
                mpuOk ? "OK" : "NOT DETECTED", (unsigned long)mpuReadFailures);
  Serial.printf("  WiFi         : %s\n", WiFi.status() == WL_CONNECTED ? "connected" : "DISCONNECTED");
  if (WiFi.status() == WL_CONNECTED) printNetworkInfo();
  Serial.printf("  WS clients   : %u authenticated, %lu frames sent\n",
                authenticatedCount(), (unsigned long)framesSent);
  Serial.printf("  Thresholds   : contact on %u / off %u\n\n",
                contactOnThreshold, contactOffThreshold);
}

// IP if we are on the network, "DOWN" if not - short enough to sit on the end
// of every telemetry line, so the link state is always on screen.
static const char *netShort() {
  static char s[20];
  if (WiFi.status() == WL_CONNECTED) snprintf(s, sizeof(s), "%s", WiFi.localIP().toString().c_str());
  else                               snprintf(s, sizeof(s), "DOWN");
  return s;
}

static void printHuman() {
  Serial.printf(
    "[%7.1fs] HEEL %4u  TOE %4u  LFT %4u  RGT %4u | tot %5lu | "
    "A %6.2f %6.2f %6.2f g | G %7.1f %7.1f %7.1f d/s | "
    "%s steps %4lu  cad %5.1f spm  ct %4.0f ms  strike %-8s | ML %+6.1f  FR %+6.1f | MPU %s | net %s | WS %u\n",
    millis() / 1000.0f,
    zone.heel, zone.toe, zone.left, zone.right, (unsigned long)zoneTotal(),
    ax, ay, az, gx, gy, gz,
    inContact ? "CONTACT" : "  swing",
    (unsigned long)stepCount, cadenceSpm, avgContactMs, lastStrike,
    balanceML(), balanceFR(),
    !mpuOk ? "ERR " : (mpuStalled ? "ZERO" : "ok  "), netShort(), authenticatedCount());
}

// The boot banner with the IP scrolls off screen in about two seconds once the
// 10 Hz telemetry starts, so repeat the essentials every 5 s. This prints even
// while telemetry is paused with 'p' - it is the line you need when the
// question is "did it get on the network, and what is the address?".
static void netHeartbeat() {
  static uint32_t nextMs = 0;
  if ((int32_t)(millis() - nextMs) < 0) return;
  nextMs = millis() + 5000;

  if (WiFi.status() == WL_CONNECTED) {
    String ip = WiFi.localIP().toString();
    Serial.printf("[net] WiFi OK \"%s\"  %s  %d dBm  |  dashboard http://%s/  |  "
                  "ws://%s:%u  |  %u client(s), %lu frames sent\n",
                  WiFi.SSID().c_str(), ip.c_str(), WiFi.RSSI(), ip.c_str(),
                  ip.c_str(), WS_PORT, authenticatedCount(), (unsigned long)framesSent);
  } else {
    Serial.printf("[net] WiFi NOT CONNECTED - retrying \"%s\" (status %d). Check the SSID "
                  "and password at the top of the sketch; the ESP32 needs 2.4 GHz.\n",
                  WIFI_SSID, (int)WiFi.status());
  }
}

static void i2cScan() {
  Serial.println(F("[test] I2C bus scan (SDA 21 / SCL 22):"));
  uint8_t found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      const char *tag = (addr == MPU_ADDR_PRIMARY) ? "  <- MPU6050"
                      : (addr == MPU_ADDR_ALT)     ? "  <- MPU6050 with AD0 high" : "";
      Serial.printf("       device found at 0x%02X%s\n", addr, tag);
      found++;
    }
  }
  if (found == 0) {
    Serial.println(F("       NOTHING ON THE BUS - power or wiring, not an address problem."));
    Serial.println(F("       Check the module LED, re-seat VCC/GND, confirm SDA=21 SCL=22."));
  }
}

static void selfTest() {
  Serial.println(F("\n===== SELF TEST ====="));
  i2cScan();

  mpuOk = mpuInit();
  Serial.println(mpuOk ? F("[test] MPU6050 detected and configured (100 Hz, +-8 g, +-1000 dps).")
                       : F("[test] *** ERROR: MPU6050 NOT DETECTED ***"));

  Serial.println(F("[test] Pressure channels (raw ADC, 0-4095, unloaded):"));
  struct { const char *name; int pin; } chans[] = {
    {"heel ", PIN_FSR_HEEL}, {"toe  ", PIN_FSR_TOE},
    {"left ", PIN_FSR_LEFT}, {"right", PIN_FSR_RIGHT}
  };
  for (auto &c : chans) {
    uint16_t lo = 4095, hi = 0;
    for (uint8_t i = 0; i < 20; i++) {
      uint16_t v = readFsr(c.pin);
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      delay(2);
    }
    const char *note = "";
    if (hi > 3900)          note = "  <- near full scale unloaded: check the pull-down / wiring";
    else if (hi - lo > 400) note = "  <- very noisy: check the ground connection";
    else if (hi == 0)       note = "  <- dead channel: open circuit or wrong pin";
    Serial.printf("       %s GPIO%-2d  min %4u  max %4u%s\n", c.name, c.pin, lo, hi, note);
  }

  captureBaseline();
  if (mpuOk) mpuCalibrateGyro();
  Serial.println(F("===== SELF TEST DONE =====\n"));
}

static void handleSerial() {
  static char buf[16];
  static uint8_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n' && n < sizeof(buf) - 1) { buf[n++] = c; continue; }
    buf[n] = '\0';
    n = 0;
    if (buf[0] == '\0') continue;

    if (!strncmp(buf, "to", 2) && isdigit((unsigned char)buf[2])) {
      contactOnThreshold = atoi(buf + 2);
      Serial.printf("[cfg] contact ON threshold = %u\n", contactOnThreshold);
      continue;
    }
    if (!strncmp(buf, "tf", 2) && isdigit((unsigned char)buf[2])) {
      contactOffThreshold = atoi(buf + 2);
      Serial.printf("[cfg] contact OFF threshold = %u\n", contactOffThreshold);
      continue;
    }

    switch (buf[0]) {
      case 'p': printEnabled = !printEnabled;
                Serial.printf("[cfg] printing %s\n", printEnabled ? "resumed" : "paused"); break;
      case 'z': captureBaseline();  break;
      case 'g': mpuCalibrateGyro(); break;
      case 'r': resetSession();     break;
      case 'i': printInfo();        break;
      case 's': selfTest();         break;
      case '?': printHelp();        break;
      default:  Serial.printf("[cfg] unknown command '%s' - type ? for help\n", buf);
    }
  }
}

// ===========================================================================
// Arduino entry points
// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(400);

  Serial.println(F("\n\n==========================================="));
  Serial.println(F("  FORGE INSOLE - ESP32 firmware (WiFi)"));
  Serial.println(F("  TechXM (Pty) Ltd | SA SportForge Platform"));
  Serial.printf ("  Unit %s (%s foot, %s) | fw %s\n", DEVICE_ID, UNIT_FOOT, UNIT_PAIR, FW_VERSION);
  Serial.println(F("==========================================="));
  Serial.println(F("    MPU6050 VCC -> 3.3V     SDA -> GPIO21"));
  Serial.println(F("    MPU6050 GND -> GND      SCL -> GPIO22"));
  Serial.println(F("    Heel FSR -> GPIO34      Toe FSR   -> GPIO35"));
  Serial.println(F("    Left FSR -> GPIO32      Right FSR -> GPIO33"));
  Serial.println(F("===========================================\n"));

  analogReadResolution(12);
  analogSetPinAttenuation(PIN_FSR_HEEL,  ADC_11db);
  analogSetPinAttenuation(PIN_FSR_TOE,   ADC_11db);
  analogSetPinAttenuation(PIN_FSR_LEFT,  ADC_11db);
  analogSetPinAttenuation(PIN_FSR_RIGHT, ADC_11db);

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);

  selfTest();
  connectWiFi();
  sessionStartMs = millis();
  printHelp();
}

void loop() {
  static uint32_t nextSampleUs = 0;
  static uint32_t nextPrintMs  = 0;
  static uint32_t nextStreamMs = 0;
  static uint32_t nextRetryMs  = 0;
  static uint32_t nextWifiTry  = 0;

  // --- network servicing ---------------------------------------------------
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiConnected) {
      wifiConnected = false;
      Serial.println(F("[wifi] connection lost - retrying"));
    }
    if ((int32_t)(millis() - nextWifiTry) >= 0) {
      nextWifiTry = millis() + 5000;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
  } else {
    if (!wifiConnected) {
      wifiConnected = true;
      Serial.println(F("[wifi] reconnected"));
      if (!webSocketStarted) { startHTTPServer(); startWebSocket(); }
      printNetworkInfo();
    }
    if (webSocketStarted) webSocket.loop();
    httpServer.handleClient();
  }

  // --- 100 Hz sensing ------------------------------------------------------
  uint32_t nowUs = micros();
  if ((int32_t)(nowUs - nextSampleUs) >= 0) {
    nextSampleUs = nowUs + SAMPLE_US;
    uint32_t nowMs = millis();

    readAllFsr();

    if (mpuOk) {
      if (!mpuReadAll()) {
        mpuReadFailures++;
        if (mpuReadFailures > 20) { mpuOk = false; ax = ay = az = gx = gy = gz = 0; }
      } else {
        // Alive on the bus but returning nothing but zeros -> it browned out and
        // reset into sleep. Half a second of that and we wake it up again,
        // rather than streaming flat zeros to the app and calling it "ok".
        if (mpuFrameAllZero) mpuZeroFrames++; else mpuZeroFrames = 0;

        static uint32_t nextWakeMs = 0;
        if (mpuZeroFrames > 50) {
          mpuStalled = true;
          if ((int32_t)(nowMs - nextWakeMs) >= 0) {
            nextWakeMs = nowMs + 2000;
            Serial.println(F("[mpu] all registers reading zero - chip reset into sleep. "
                             "Re-initialising (check the 3.3V supply / add a 100uF cap)."));
            if (mpuInit()) {
              mpuRecoveries++;
              mpuZeroFrames = 0;
              mpuStalled = false;
              Serial.printf("[mpu] recovered (%lu time(s) this session).\n",
                            (unsigned long)mpuRecoveries);
              mpuCalibrateGyro();
            }
          }
        } else if (mpuZeroFrames == 0) {
          mpuStalled = false;
        }
      }
    }

    updateGait(nowMs);
    seq++;

    // MPU6050 missing -> report and keep retrying, alternating the I2C clock:
    // long dupont wires often only work at 100 kHz.
    if (!mpuOk && (int32_t)(nowMs - nextRetryMs) >= 0) {
      nextRetryMs = nowMs + 2000;
      static bool slowClock = false;
      slowClock = !slowClock;
      Wire.setClock(slowClock ? 100000 : 400000);
      Serial.printf("[ERROR] MPU6050 not detected (%u kHz). Check VCC/GND/SDA=21/SCL=22.\n",
                    slowClock ? 100 : 400);
      if (mpuInit()) {
        mpuOk = true;
        mpuReadFailures = 0;
        Serial.printf("[ok] MPU6050 recovered at 0x%02X.\n", mpuAddr);
        mpuCalibrateGyro();
      }
    }
  }

  uint32_t nowMs = millis();

  if (printEnabled && (int32_t)(nowMs - nextPrintMs) >= 0) {
    nextPrintMs = nowMs + (1000 / PRINT_HZ);
    printHuman();
  }

  if ((int32_t)(nowMs - nextStreamMs) >= 0) {
    nextStreamMs = nowMs + (1000 / STREAM_HZ);
    broadcastTelemetry();
  }

  netHeartbeat();
  handleSerial();
}
