/*
 * Project myProject
 * Author: Your Name
 * Date:
 * For comprehensive documentation and examples, please visit:
 * https://docs.particle.io/firmware/best-practices/firmware-template/
 */

// Include Particle Device OS APIs
#include "Particle.h"

// Let Device OS manage the connection to the Particle Cloud
SYSTEM_MODE(AUTOMATIC);

// Run the application and system concurrently in separate threads

// Show system, cloud connectivity, and application logs over USB
// View logs with CLI using 'particle serial monitor --follow'
SerialLogHandler logHandler(LOG_LEVEL_INFO);

const pin_t REED_PIN = D23;

// While testing remotely (flashing over Particle Cloud), full HIBERNATE
// leaves only a brief window where the device is actually reachable, and an
// OTA push can miss it or spill across several wake cycles. Keep this true
// during remote bring-up so the device stays connected and reflashes land
// instantly; flip to false for the real deployment cadence.
const bool TESTING_MODE = true;

// How long to hibernate between wake cycles. The real product only needs to
// report a few times a day; 5 minutes here is just for testing.
const unsigned long WAKE_INTERVAL_MS = 1UL * 60 * 1000; // 5 minutes (test value)
// If we can't get connected within this long on a given wake, give up for
// this cycle and try again next wake instead of draining the battery.
// Must comfortably cover known-operator attempt (up to 60s) + fallback full
// scan (up to 180s) + cloud handshake, or we cut the connection off right
// before it succeeds (see commit history).
const unsigned long MAX_CONNECT_WAIT_MS = 4UL * 60 * 1000; // 4 minutes

const pin_t VBAT_MEAS_PIN = A0;
const float ADC_REF_VOLTAGE = 3.3f;
const float ADC_MAX_COUNTS = 4095.0f;
// Divider R7=470k (VBAT to VBAT_MEAS) / R8=1000k (VBAT_MEAS to GND):
// VBAT_MEAS = VBAT * R8/(R7+R8), so VBAT = VBAT_MEAS * (R7+R8)/R8
const float VBAT_DIVIDER_RATIO = (470.0f + 1000.0f) / 1000.0f;

static float readBatteryVoltage() {
  float adcVoltage = (analogRead(VBAT_MEAS_PIN) / ADC_MAX_COUNTS) * ADC_REF_VOLTAGE;
  return adcVoltage * VBAT_DIVIDER_RATIO;
}

// Logs 10 consecutive raw reads so the actual settling/drift behavior of
// VBAT_MEAS is visible, instead of guessing at it.
void logTenBatteryReads() {
  const int NUM_READS = 10;
  int rawReadings[NUM_READS];

  for (int i = 0; i < NUM_READS; i++) {
    rawReadings[i] = analogRead(VBAT_MEAS_PIN);
  }

  for (int i = 0; i < NUM_READS; i++) {
    float vbat = (rawReadings[i] / ADC_MAX_COUNTS) * ADC_REF_VOLTAGE * VBAT_DIVIDER_RATIO;
    Log.info("vbat read %d: raw=%d vbat=%.3f", i, rawReadings[i], vbat);
  }
}

// Prints raw AT command responses as they arrive
int atCallback(int type, const char* buf, int len, void* data) {
  if (buf) {
    Log.info("%.*s", len, buf);
  }
  return WAIT;
}

// Full airtime scan: lists every operator the modem can see and whether
// this SIM is allowed on it. Empirically this is what got the modem to
// actually register after it sat at Cellular.ready=0 for a long time.
// Expensive (1-3 minutes, lots of current) - only used as a fallback when
// the known-operator shortcut below doesn't pan out.
void scanNetworks() {
  Cellular.on();
  Log.info("Scanning for available networks, this takes 1-3 minutes...");
  Cellular.command(atCallback, (void*)nullptr, 180000, "AT+COPS=?\r\n");
}

// Backup-RAM-backed storage (survives HIBERNATE - Device OS syncs retained
// variables to flash automatically on hibernate entry) so we can skip
// straight to the operator that worked last time instead of paying for a
// full scan. Zero-initialized on first-ever cold boot.
retained char g_lastOperatorNumeric[8] = {}; // MCC+MNC, e.g. "28201"

void saveOperator(const char* numeric) {
  strncpy(g_lastOperatorNumeric, numeric, sizeof(g_lastOperatorNumeric) - 1);
  g_lastOperatorNumeric[sizeof(g_lastOperatorNumeric) - 1] = '\0';
}

// Returns true and fills outNumeric if retained memory holds a plausible MCC+MNC value.
bool loadOperator(char* outNumeric, size_t outSize) {
  size_t len = strnlen(g_lastOperatorNumeric, sizeof(g_lastOperatorNumeric));
  if (len < 5 || len > 6) {
    return false;
  }
  for (size_t i = 0; i < len; i++) {
    if (!isdigit((unsigned char)g_lastOperatorNumeric[i])) {
      return false;
    }
  }
  strncpy(outNumeric, g_lastOperatorNumeric, outSize - 1);
  outNumeric[outSize - 1] = '\0';
  return true;
}

// Skip the full scan: force the modem to register directly on the operator
// that worked last time. Much faster/cheaper than searching all operators.
void tryKnownOperator(const char* numeric) {
  Cellular.on();
  Log.info("Trying known operator %s...", numeric);
  Cellular.command(atCallback, (void*)nullptr, 60000, "AT+COPS=1,2,\"%s\"\r\n", numeric);
}

// Accumulates AT command response text into a buffer for parsing, instead
// of just logging it.
char atResponseBuf[256];
size_t atResponseLen = 0;

int captureCallback(int type, const char* buf, int len, void* data) {
  if (buf && atResponseLen + len < sizeof(atResponseBuf) - 1) {
    memcpy(atResponseBuf + atResponseLen, buf, len);
    atResponseLen += len;
    atResponseBuf[atResponseLen] = '\0';
  }
  return WAIT;
}

// Parses the numeric MCC+MNC out of "+COPS: 0,2,"28201",7" so it can be saved.
bool queryCurrentOperator(char* outNumeric, size_t outSize) {
  atResponseLen = 0;
  atResponseBuf[0] = '\0';
  Cellular.command(captureCallback, (void*)nullptr, 10000, "AT+COPS?\r\n");

  const char* start = strchr(atResponseBuf, '"');
  if (!start) {
    return false;
  }
  start++;
  const char* end = strchr(start, '"');
  if (!end) {
    return false;
  }
  size_t len = end - start;
  if (len == 0 || len >= outSize) {
    return false;
  }
  memcpy(outNumeric, start, len);
  outNumeric[len] = '\0';
  return true;
}

void publishStatus(bool reedClosed, float vbat) {
  CellularSignal sig = Cellular.RSSI();
  String payload = String::format(
    "{\"reed\":%d,\"cellular_ready\":%d,\"rsrp\":%.1f,\"rsrq\":%.1f,\"vbat\":%.2f}",
    reedClosed, Cellular.ready(), sig.getStrengthValue(), sig.getQualityValue(), vbat);
  Particle.publish("reed_status", payload, PRIVATE);
  Log.info("Published: %s", payload.c_str());
}

// Full reset-based sleep: powers down until the RTC timer fires, device
// comes back up through a fresh boot (setup() runs again). This is the
// proven-working approach carried over from the msom/EG91 board, where
// STOP/ULTRA_LOW_POWER + network standby hit a platform bug. On b5som
// (nRF52840), the sleep HAL does properly handle a network wakeup source,
// so ULTRA_LOW_POWER + .network(NETWORK_INTERFACE_CELLULAR,
// SystemSleepNetworkFlag::INACTIVE_STANDBY) may work here without the full
// reconnect cost every cycle - untested on real hardware, worth trying.
void hibernateUntilNextWake() {
  SystemSleepConfiguration sleepConfig;
  sleepConfig.mode(SystemSleepMode::HIBERNATE)
             .duration(WAKE_INTERVAL_MS);
  System.sleep(sleepConfig); // Does not return: device resets on wake.
}

// In TESTING_MODE, stays connected instead of hibernating so a remote
// `particle flash` lands immediately. Otherwise sleeps for real.
void sleepOrIdle() {
  if (TESTING_MODE) {
    delay(WAKE_INTERVAL_MS);
  } else {
    hibernateUntilNextWake();
  }
}

// Captured once at wake, before the modem starts drawing current - a
// reading taken later (e.g. during a TX burst) would sag and not reflect
// the battery's actual resting voltage.
float g_vbatAtWake = 0;

// setup() runs once, when the device is first turned on
void setup() {
  pinMode(REED_PIN, INPUT_PULLUP);

  g_vbatAtWake = readBatteryVoltage();

  // No GPS antenna on this board - make sure the modem's GNSS is off.
  // GNSS defaults to off on u-blox SARA-R510, so this may return ERROR
  // ("already off") rather than OK - that's expected, not a fault.
  Cellular.on();
  Cellular.command(atCallback, (void*)nullptr, 10000, "AT+UGPS=0\r\n");
}

// loop() runs over and over again, as quickly as it can execute.
void loop() {
  static bool triedKnownOperator = false;
  static bool triedFullScan = false;
  static unsigned long wakeStart = millis();

  if (!Cellular.ready() && millis() > 5000) {
    if (!triedKnownOperator) {
      triedKnownOperator = true;
      char numeric[8];
      if (loadOperator(numeric, sizeof(numeric))) {
        tryKnownOperator(numeric);
      } else {
        // Nothing saved yet (first-ever boot) - go straight to a full scan.
        triedFullScan = true;
        scanNetworks();
      }
    } else if (!triedFullScan && (millis() - wakeStart > MAX_CONNECT_WAIT_MS / 2)) {
      // Known operator didn't pan out in time - fall back to a full scan.
      triedFullScan = true;
      scanNetworks();
    }
  }

  Log.info("Cellular.ready=%d Particle.connected=%d", Cellular.ready(), Particle.connected());

  if (Particle.connected()) {
    char numeric[8];
    if (queryCurrentOperator(numeric, sizeof(numeric))) {
      saveOperator(numeric);
    }
    publishStatus(digitalRead(REED_PIN), g_vbatAtWake);
    sleepOrIdle();
  } else if (millis() - wakeStart > MAX_CONNECT_WAIT_MS) {
    // Couldn't connect this cycle - don't drain the battery waiting, try again next wake.
    Log.info("Giving up on connecting this cycle");
    if (TESTING_MODE) {
      // No reboot to reset these, so retry the connect sequence ourselves.
      triedKnownOperator = false;
      triedFullScan = false;
      wakeStart = millis();
    }
    sleepOrIdle();
  } else {
    delay(1000);
  }
}
