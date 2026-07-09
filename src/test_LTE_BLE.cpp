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
// How long to hibernate between wake cycles. The real product only needs to
// report a few times a day; 5 minutes here is just for testing.
const unsigned long WAKE_INTERVAL_MS = 5UL * 60 * 1000; // 5 minutes (test value)
// If we can't get connected within this long on a given wake, give up for
// this cycle and try again next wake instead of draining the battery.
const unsigned long MAX_CONNECT_WAIT_MS = 3UL * 60 * 1000; // 3 minutes

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
void scanNetworks() {
  Cellular.on();
  Log.info("Scanning for available networks, this takes 1-3 minutes...");
  Cellular.command(atCallback, (void*)nullptr, 180000, "AT+COPS=?\r\n");
}

void publishStatus(bool reedClosed, float vbat) {
  CellularSignal sig = Cellular.RSSI();
  String payload = String::format(
    "{\"reed\":%d,\"cellular_ready\":%d,\"rsrp\":%.1f,\"rsrq\":%.1f,\"vbat\":%.2f}",
    reedClosed, Cellular.ready(), sig.getStrengthValue(), sig.getQualityValue(), vbat);
  Particle.publish("reed_status", payload, PRIVATE);
  Log.info("Published: %s", payload.c_str());
}

// Deepest sleep mode this platform supports. Unlike STOP/ULTRA_LOW_POWER,
// it needs no network wakeup source (which errors out on this platform, see
// commit history) and fully powers down until the RTC timer fires - device
// comes back up through a fresh boot (setup() runs again).
void hibernateUntilNextWake() {
  SystemSleepConfiguration sleepConfig;
  sleepConfig.mode(SystemSleepMode::HIBERNATE)
             .duration(WAKE_INTERVAL_MS);
  System.sleep(sleepConfig); // Does not return: device resets on wake.
}

// setup() runs once, when the device is first turned on
void setup() {
  WiFi.off();
  pinMode(REED_PIN, INPUT_PULLUP);
}

// loop() runs over and over again, as quickly as it can execute.
void loop() {
  static bool scanned = false;
  static unsigned long wakeStart = millis();

  if (!Cellular.ready() && !scanned && millis() > 10000) {
    scanned = true;
    scanNetworks();
  }

  Log.info("Cellular.ready=%d Particle.connected=%d", Cellular.ready(), Particle.connected());

  if (Particle.connected()) {
    publishStatus(digitalRead(REED_PIN), readBatteryVoltage());
    hibernateUntilNextWake();
  } else if (millis() - wakeStart > MAX_CONNECT_WAIT_MS) {
    // Couldn't connect this cycle - don't drain the battery waiting, try again next wake.
    Log.info("Giving up on connecting this cycle");
    hibernateUntilNextWake();
  } else {
    delay(1000);
  }
}
