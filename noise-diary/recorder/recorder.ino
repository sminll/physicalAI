// 소음 일기 1단계: 보드 마이크로 1초짜리 소리를 녹음해 PC로 보내는 스케치
// esp32 보드 패키지 2.x + XIAO ESP32S3 Sense 기준
// PC에서 'r'을 보내면 1초(16000샘플)를 녹음한 뒤 "DATA\n" + 32000바이트(16bit PCM)를 보낸다.
#include <I2S.h>

#define SAMPLE_RATE 16000
#define SAMPLE_BITS 16
#define RECORD_BYTES (SAMPLE_RATE * SAMPLE_BITS / 8)  // 1초 = 32000바이트
#define GAIN 16  // 소리 키우는 배율 (수집 시작 후에는 바꾸지 않기)

uint8_t *buf;

void setup() {
  Serial.begin(115200);
  while (!Serial);
  I2S.setAllPins(-1, 42, 41, -1, -1);  // CLK=42, DATA=41
  if (!I2S.begin(PDM_MONO_MODE, SAMPLE_RATE, SAMPLE_BITS)) {
    Serial.println("ERR i2s");
    while (1);
  }
  buf = (uint8_t *)malloc(RECORD_BYTES);
  if (!buf) {
    Serial.println("ERR malloc");
    while (1);
  }
  // 마이크가 켜진 직후의 튀는 값을 버린다
  size_t n;
  esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, buf, RECORD_BYTES, &n, portMAX_DELAY);
  Serial.println("READY");
}

void loop() {
  if (Serial.available() && Serial.read() == 'r') {
    size_t n = 0;
    esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, buf, RECORD_BYTES, &n, portMAX_DELAY);

    // 기준값(평균)을 0으로 맞춘 뒤 GAIN배로 키우고, 범위를 넘으면 자른다
    int16_t *s = (int16_t *)buf;
    int count = RECORD_BYTES / 2;
    int32_t sum = 0;
    for (int i = 0; i < count; i++) sum += s[i];
    int32_t mean = sum / count;
    for (int i = 0; i < count; i++) {
      int32_t v = ((int32_t)s[i] - mean) * GAIN;
      if (v > 32767) v = 32767;
      if (v < -32768) v = -32768;
      s[i] = (int16_t)v;
    }

    Serial.print("DATA\n");
    Serial.write(buf, RECORD_BYTES);
    Serial.flush();
  }
}
