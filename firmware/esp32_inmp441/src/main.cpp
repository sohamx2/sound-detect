#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <driver/i2s.h>
#include <time.h>
#include <math.h>

#include "secrets.h"

#ifndef SOUNDDETECT_I2S_BCLK_PIN
#define SOUNDDETECT_I2S_BCLK_PIN 4
#endif
#ifndef SOUNDDETECT_I2S_WS_PIN
#define SOUNDDETECT_I2S_WS_PIN 5
#endif
#ifndef SOUNDDETECT_I2S_DATA_PIN
#define SOUNDDETECT_I2S_DATA_PIN 6
#endif
static constexpr int PIN_I2S_BCLK = SOUNDDETECT_I2S_BCLK_PIN;
static constexpr int PIN_I2S_WS = SOUNDDETECT_I2S_WS_PIN;
static constexpr int PIN_I2S_DATA = SOUNDDETECT_I2S_DATA_PIN;

static constexpr i2s_port_t I2S_PORT = I2S_NUM_0;
static constexpr uint32_t SAMPLE_RATE = 16000;
static constexpr size_t FRAME_SAMPLES = 320;
static constexpr size_t PREROLL_SAMPLES = SAMPLE_RATE / 5;
static constexpr size_t MAX_CLIP_SAMPLES = SAMPLE_RATE * 2;
static constexpr size_t POST_TRIGGER_SAMPLES = SAMPLE_RATE;
static constexpr float DEFAULT_THRESHOLD_DBFS = -28.0f;
static constexpr uint32_t CONFIG_REFRESH_MS = 8000;
static constexpr uint32_t COOLDOWN_MS = 1800;

static int32_t i2sRaw[FRAME_SAMPLES * 2];
static int16_t frame[FRAME_SAMPLES];
static int16_t preroll[PREROLL_SAMPLES];
static int16_t clipSamples[MAX_CLIP_SAMPLES];
static size_t prerollWrite = 0;
static size_t prerollCount = 0;
static size_t clipCount = 0;
static size_t clipTarget = 0;
static bool recording = false;
static uint8_t hotFrames = 0;
static volatile float thresholdDbfs = DEFAULT_THRESHOLD_DBFS;
static uint32_t lastWiFiAttemptAt = 0;
static uint32_t lastClockAttemptAt = 0;
static uint32_t lastLevelPrintAt = 0;
static uint32_t cooldownUntil = 0;
static uint32_t successfulUploads = 0;
static volatile bool clockSynced = false;
static int activeSlot = -1;
static float dcX1 = 0.0f, dcY1 = 0.0f;

static const char BASE64_TABLE[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void configureTls(WiFiClientSecure &client) {
#if ALLOW_INSECURE_TLS_FOR_LAB
  client.setInsecure();
#else
  client.setCACert(SOUNDDETECT_CA);
#endif
}

static bool wifiConnected() {
  return WiFi.status() == WL_CONNECTED;
}

static void startWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWiFiAttemptAt = millis();
  Serial.printf("Connecting to Wi-Fi '%s'", WIFI_SSID);
  const uint32_t start = millis();
  while (!wifiConnected() && millis() - start < 20000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (wifiConnected()) {
    Serial.printf("Wi-Fi connected; ESP32 address: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("Wi-Fi not connected yet; reconnecting in the background.");
  }
}

static void maintainWiFi() {
  if (wifiConnected()) return;
  if (millis() - lastWiFiAttemptAt >= 8000) {
    Serial.println("Wi-Fi disconnected; retrying.");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWiFiAttemptAt = millis();
  }
}

static void requestClockSync() {
  if (!wifiConnected()) return;
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  lastClockAttemptAt = millis();
  Serial.println("Requested UTC clock sync for HTTPS certificate validation.");
}

static String urlFor(const String &path) {
  return String(GATEWAY_BASE_URL) + path;
}

static bool fetchSensorConfig() {
  if (!wifiConnected()) return false;
  WiFiClientSecure client;
  configureTls(client);
  HTTPClient http;
  http.setTimeout(5000);
  String url = urlFor("/api/sensor-config?sid=");
  url += SENSOR_ID;
  url += "&token=";
  url += SENSOR_TOKEN;
  if (!http.begin(client, url)) {
    Serial.println("Could not open the sensor config URL.");
    return false;
  }
  http.addHeader("X-Sensor-Token", SENSOR_TOKEN);
  http.addHeader("X-Sensor-Active", "true");
  const int statusCode = http.GET();
  if (statusCode != HTTP_CODE_OK) {
    Serial.printf("Sensor config request failed: HTTP %d\n", statusCode);
    http.end();
    return false;
  }
  StaticJsonDocument<1024> doc;
  const DeserializationError error = deserializeJson(doc, http.getStream());
  http.end();
  if (error) {
    Serial.printf("Could not parse sensor config: %s\n", error.c_str());
    return false;
  }
  thresholdDbfs = doc["thresholdDbfs"] | DEFAULT_THRESHOLD_DBFS;
  Serial.printf("Sensor '%s' ready; threshold %.1f dBFS\n",
                doc["name"] | "unnamed", thresholdDbfs);
  return true;
}

static void clockSyncTask(void *) {
  for (;;) {
    if (time(nullptr) >= 1700000000) clockSynced = true;
    if (!clockSynced && wifiConnected() && millis() - lastClockAttemptAt >= 30000) {
      requestClockSync();
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

static void sensorConfigTask(void *) {
  for (;;) {
    if (wifiConnected()) fetchSensorConfig();
    vTaskDelay(pdMS_TO_TICKS(CONFIG_REFRESH_MS));
  }
}

static void appendPreroll(const int16_t *samples, size_t count) {
  for (size_t i = 0; i < count; ++i) {
    preroll[prerollWrite] = samples[i];
    prerollWrite = (prerollWrite + 1) % PREROLL_SAMPLES;
    if (prerollCount < PREROLL_SAMPLES) ++prerollCount;
  }
}

static void appendClip(const int16_t *samples, size_t count) {
  const size_t room = MAX_CLIP_SAMPLES - clipCount;
  const size_t copyCount = count < room ? count : room;
  memcpy(clipSamples + clipCount, samples, copyCount * sizeof(int16_t));
  clipCount += copyCount;
}

static void beginClip(const int16_t *triggerFrame) {
  clipCount = 0;
  const size_t first = (prerollWrite + PREROLL_SAMPLES - prerollCount) % PREROLL_SAMPLES;
  for (size_t i = 0; i < prerollCount; ++i) {
    clipSamples[clipCount++] = preroll[(first + i) % PREROLL_SAMPLES];
  }
  clipTarget = clipCount + POST_TRIGGER_SAMPLES;
  if (clipTarget > MAX_CLIP_SAMPLES) clipTarget = MAX_CLIP_SAMPLES;
  appendClip(triggerFrame, FRAME_SAMPLES);
  recording = true;
  hotFrames = 0;
  Serial.println("Threshold crossed; capturing clip.");
}

static float frameDbfs(size_t sampleCount) {
  if (!sampleCount) return -120.0f;
  double sum = 0.0;
  double sumSquares = 0.0;
  for (size_t i = 0; i < sampleCount; ++i) {
    const double value = frame[i];
    sum += value;
    sumSquares += value * value;
  }
  const double mean = sum / sampleCount;
  const double variance = fmax(0.0, sumSquares / sampleCount - mean * mean);
  const double rms = sqrt(variance);
  if (rms < 1.0) return -120.0f;
  return (float)(20.0 * log10(rms / 32768.0));
}

static bool readFrame(float &dbfs) {
  size_t bytesRead = 0;
  const esp_err_t result = i2s_read(I2S_PORT, i2sRaw, sizeof(i2sRaw), &bytesRead, portMAX_DELAY);
  if (result != ESP_OK || bytesRead < 2 * sizeof(int32_t)) return false;
  const size_t pairs = bytesRead / (2 * sizeof(int32_t));

  if (activeSlot < 0) {
    double e0 = 0, e1 = 0;
    for (size_t i = 0; i < pairs; ++i) {
      e0 += fabs((double)(i2sRaw[2 * i] >> 8));
      e1 += fabs((double)(i2sRaw[2 * i + 1] >> 8));
    }
    if (e0 + e1 > 0) {
      activeSlot = e0 >= e1 ? 0 : 1;
      Serial.printf("Microphone detected on I2S slot %d.\n", activeSlot);
    }
  }
  const int slot = activeSlot < 0 ? 0 : activeSlot;

  // Noise Cleaning
  for (size_t i = 0; i < pairs; ++i) {
    const float x = (float)(i2sRaw[2 * i + slot] >> 8);
    const float y = x - dcX1 + 0.996f * dcY1;
    dcX1 = x;
    dcY1 = y;
    float scaled = y / 256.0f;
    if (scaled > 32767.0f) scaled = 32767.0f;
    if (scaled < -32768.0f) scaled = -32768.0f;
    frame[i] = (int16_t)lrintf(scaled);
  }
  dbfs = frameDbfs(pairs);
  if (pairs != FRAME_SAMPLES) {
    Serial.printf("Short I2S frame (%u samples); ignoring.\n", (unsigned)pairs);
    return false;
  }
  return true;
}

static uint8_t wavByteAt(const uint8_t *header, size_t index) {
  if (index < 44) return header[index];
  const size_t sampleByte = index - 44;
  const uint16_t sample = (uint16_t)clipSamples[sampleByte / 2];
  return (sampleByte & 1) ? (uint8_t)(sample >> 8) : (uint8_t)(sample & 0xff);
}

static void putLe16(uint8_t *buffer, size_t offset, uint16_t value) {
  buffer[offset] = (uint8_t)(value & 0xff);
  buffer[offset + 1] = (uint8_t)(value >> 8);
}

static void putLe32(uint8_t *buffer, size_t offset, uint32_t value) {
  buffer[offset] = (uint8_t)(value & 0xff);
  buffer[offset + 1] = (uint8_t)((value >> 8) & 0xff);
  buffer[offset + 2] = (uint8_t)((value >> 16) & 0xff);
  buffer[offset + 3] = (uint8_t)(value >> 24);
}

static size_t base64WavLength(size_t sampleCount) {
  const size_t byteLength = 44 + sampleCount * sizeof(int16_t);
  return ((byteLength + 2) / 3) * 4;
}

static void appendBase64Wav(String &output, size_t sampleCount) {
  uint8_t header[44] = {};
  memcpy(header, "RIFF", 4);
  memcpy(header + 8, "WAVE", 4);
  memcpy(header + 12, "fmt ", 4);
  memcpy(header + 36, "data", 4);
  const uint32_t pcmBytes = (uint32_t)(sampleCount * sizeof(int16_t));
  putLe32(header, 4, 36 + pcmBytes);
  putLe32(header, 16, 16);
  putLe16(header, 20, 1); // PCM
  putLe16(header, 22, 1); // mono
  putLe32(header, 24, SAMPLE_RATE);
  putLe32(header, 28, SAMPLE_RATE * 2);
  putLe16(header, 32, 2);
  putLe16(header, 34, 16);
  putLe32(header, 40, pcmBytes);

  const size_t byteLength = 44 + pcmBytes;
  for (size_t i = 0; i < byteLength; i += 3) {
    const uint32_t a = wavByteAt(header, i);
    const bool hasB = i + 1 < byteLength;
    const bool hasC = i + 2 < byteLength;
    const uint32_t b = hasB ? wavByteAt(header, i + 1) : 0;
    const uint32_t c = hasC ? wavByteAt(header, i + 2) : 0;
    output += BASE64_TABLE[(a >> 2) & 0x3f];
    output += BASE64_TABLE[((a & 0x03) << 4) | ((b >> 4) & 0x0f)];
    output += hasB ? BASE64_TABLE[((b & 0x0f) << 2) | ((c >> 6) & 0x03)] : '=';
    output += hasC ? BASE64_TABLE[c & 0x3f] : '=';
  }
}

static String capturedAtUtc() {
  const time_t now = time(nullptr);
  if (now < 1700000000) return "1970-01-01T00:00:00.000Z";
  struct tm utc;
  gmtime_r(&now, &utc);
  char text[32];
  strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%S.000Z", &utc);
  return String(text);
}

static float clipPeakDbfs = -120.0f;

static void uploadClip() {
  if (!clipCount) return;
  const size_t encodedLength = base64WavLength(clipCount);
  const size_t estimatedPayloadSize = encodedLength + 512;
  if (!wifiConnected()) {
    Serial.println("Clip dropped: Wi-Fi is offline.");
    return;
  }
  if (ESP.getFreeHeap() < estimatedPayloadSize + 70000) {
    Serial.printf("Clip dropped: not enough free heap (%u bytes).\n", (unsigned)ESP.getFreeHeap());
    return;
  }

  String payload;
  if (!payload.reserve(estimatedPayloadSize)) {
    Serial.println("Clip dropped: could not allocate upload buffer.");
    return;
  }
  payload += F("{\"sensorId\":\"");
  payload += SENSOR_ID;
  payload += F("\",\"capturedAt\":\"");
  payload += capturedAtUtc();
  payload += F("\",\"peakDbfs\":");
  payload += String(clipPeakDbfs, 1);
  payload += F(",\"duration\":");
  payload += String((double)clipCount / SAMPLE_RATE, 3);
  payload += F(",\"sampleRate\":16000,\"firmware\":\"esp32-inmp441-v2\",\"audioBase64\":\"");
  appendBase64Wav(payload, clipCount);
  payload += F("\"}");

  WiFiClientSecure client;
  configureTls(client);
  HTTPClient http;
  http.setTimeout(12000);
  if (!http.begin(client, urlFor("/api/observations"))) {
    Serial.println("Could not open the observation upload URL.");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Sensor-Token", SENSOR_TOKEN);
  http.addHeader("X-Sensor-Active", "true");
  const int statusCode = http.POST((uint8_t *)payload.c_str(), payload.length());
  const String response = statusCode > 0 ? http.getString() : http.errorToString(statusCode);
  http.end();
  if (statusCode == HTTP_CODE_ACCEPTED) {
    ++successfulUploads;
    Serial.printf("Sound clip uploaded (%u samples, %.2f s).\n",
                  (unsigned)clipCount, (double)clipCount / SAMPLE_RATE);
  } else {
    Serial.printf("Upload failed: HTTP %d, %s\n", statusCode, response.c_str());
  }
}

static bool installI2s() {
  i2s_config_t config = {};
  config.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  config.sample_rate = SAMPLE_RATE;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  config.dma_buf_count = 8;
  config.dma_buf_len = FRAME_SAMPLES;
  config.use_apll = false;
  config.tx_desc_auto_clear = false;
  config.fixed_mclk = 0;

  esp_err_t result = i2s_driver_install(I2S_PORT, &config, 0, nullptr);
  if (result != ESP_OK) {
    Serial.printf("i2s_driver_install failed: %d\n", result);
    return false;
  }
  i2s_pin_config_t pins = {};
  pins.bck_io_num = PIN_I2S_BCLK;
  pins.ws_io_num = PIN_I2S_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_I2S_DATA;
  result = i2s_set_pin(I2S_PORT, &pins);
  if (result != ESP_OK) {
    Serial.printf("i2s_set_pin failed: %d\n", result);
    return false;
  }
  i2s_zero_dma_buffer(I2S_PORT);
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("SoundDetect ESP32 + INMP441 sensor starting.");
  Serial.printf("Free heap: %u bytes\n", (unsigned)ESP.getFreeHeap());
  startWiFi();
  if (wifiConnected()) requestClockSync();
  if (!installI2s()) {
    Serial.println("I2S setup failed. Check the wiring and pin assignments.");
    while (true) delay(1000);
  }
  xTaskCreatePinnedToCore(clockSyncTask, "sd-clock", 4096, nullptr, 1, nullptr, 0);
  xTaskCreatePinnedToCore(sensorConfigTask, "sd-config", 8192, nullptr, 1, nullptr, 0);
  Serial.println("Listening. Use the dashboard to tune this sensor's dBFS threshold.");
}

void loop() {
  maintainWiFi();

  float dbfs = -120.0f;
  if (!readFrame(dbfs)) return;
  if (millis() - lastLevelPrintAt >= 1000) {
    Serial.printf("Level %.1f dBFS | threshold %.1f dBFS | uploads %u\n",
                  dbfs, thresholdDbfs, (unsigned)successfulUploads);
    lastLevelPrintAt = millis();
  }

  if (recording) {
    appendClip(frame, FRAME_SAMPLES);
    if (dbfs > clipPeakDbfs) clipPeakDbfs = dbfs;
    if (clipCount >= clipTarget) {
      recording = false;
      cooldownUntil = millis() + COOLDOWN_MS;
      uploadClip();
      clipPeakDbfs = -120.0f;
    }
    return;
  }

  if (dbfs >= thresholdDbfs) {
    if (hotFrames < 255) ++hotFrames;
  } else {
    hotFrames = 0;
  }
  if (hotFrames >= 2 && (int32_t)(millis() - cooldownUntil) >= 0) {
    clipPeakDbfs = dbfs;
    beginClip(frame);
    return;
  }
  appendPreroll(frame, FRAME_SAMPLES);
}
