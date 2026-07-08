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
const unsigned long DEBOUNCE_MS = 50;
const unsigned long HEARTBEAT_INTERVAL_MS = 5000;

// Debounced reed switch read: only reports a new state once it has been
// stable for DEBOUNCE_MS, to filter out mechanical contact bounce.
bool readReedDebounced() {
  static bool stableState = digitalRead(REED_PIN);
  static bool lastRaw = stableState;
  static unsigned long lastChangeTime = 0;

  bool raw = digitalRead(REED_PIN);
  if (raw != lastRaw) {
    lastRaw = raw;
    lastChangeTime = millis();
  }
  if (raw == lastRaw && (millis() - lastChangeTime) > DEBOUNCE_MS) {
    stableState = lastRaw;
  }
  return stableState;
}

void publishStatus(bool reedClosed) {
  CellularSignal sig = Cellular.RSSI();
  String payload = String::format(
    "{\"reed\":%d,\"cellular_ready\":%d,\"rsrp\":%.1f,\"rsrq\":%.1f}",
    reedClosed, Cellular.ready(), sig.getStrengthValue(), sig.getQualityValue());
  Particle.publish("reed_status", payload, PRIVATE);
  Log.info("Published: %s", payload.c_str());
}

// setup() runs once, when the device is first turned on
void setup() {
  WiFi.off();
  pinMode(REED_PIN, INPUT_PULLUP);
}

// loop() runs over and over again, as quickly as it can execute.
void loop() {
  static bool lastReedState = readReedDebounced();
  static unsigned long lastPublish = 0;

  bool reedState = readReedDebounced();
  bool changed = (reedState != lastReedState);
  bool dueForHeartbeat = (millis() - lastPublish >= HEARTBEAT_INTERVAL_MS);

  if ((changed || dueForHeartbeat) && Particle.connected()) {
    publishStatus(reedState);
    lastReedState = reedState;
    lastPublish = millis();
  }
}
