// 소음 일기 3단계: 1초마다 소리를 분류하고, REPORT_EVERY초마다 요약을 서버(noise.py serve)로 보내는 스케치
// esp32 보드 패키지 2.x + XIAO ESP32S3 Sense 기준
// 녹음 스케치(recorder.ino)와 똑같이 평균 빼기 + GAIN 16을 적용해야 학습 때와 같은 소리가 된다.
#include <noise-diary_inferencing.h>
#include <I2S.h>
#include <WiFi.h>
#include <HTTPClient.h>

// 와이파이 이름·비밀번호·서버 주소는 같은 폴더의 secrets.h에 둔다 (깃허브에 안 올라감)
#include "secrets.h"
const char *WIFI_SSID = WIFI_SSID_VALUE;
const char *WIFI_PASS = WIFI_PASS_VALUE;
const char *SERVER_URL = SERVER_URL_VALUE;

#define REPORT_EVERY 60  // 몇 초(=몇 번 분류)마다 서버로 보낼지

#define SAMPLE_RATE 16000
#define SAMPLE_BITS 16
#define GAIN 16  // recorder.ino와 반드시 같은 값

#if EI_CLASSIFIER_FREQUENCY != SAMPLE_RATE
#error "Edge Impulse 모델의 주파수가 16000Hz가 아니에요"
#endif

static int counts[EI_CLASSIFIER_LABEL_COUNT];  // 이번 구간에서 클래스별로 1등 한 횟수
static int counted = 0;
static int16_t *samples;  // 1초 = EI_CLASSIFIER_RAW_SAMPLE_COUNT(16000)개

// Edge Impulse가 데이터를 꺼내 갈 때 호출하는 함수
static int get_audio(size_t offset, size_t length, float *out) {
  for (size_t i = 0; i < length; i++) out[i] = (float)samples[offset + i];
  return 0;
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.disconnect(true);   // 이전 연결 정보 지우기
  delay(200);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);    // 절전 끄기 (연결 안정성)
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi 연결 중");
  for (int i = 0; i < 80 && WiFi.status() != WL_CONNECTED; i++) {  // 최대 20초 (이 공유기는 11초 정도 걸림)
    delay(250);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" OK (보드 IP %s)\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.printf(" 실패 (상태 %d)\n", WiFi.status());
  }
}

// counts를 {"device":"xiao","counts":{"music":3,...}} 형태로 서버에 보낸다
void sendReport(ei_impulse_result_t &result) {
  String body = "{\"device\":\"xiao\",\"counts\":{";
  for (int i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    if (i) body += ",";
    body += "\"";
    body += result.classification[i].label;
    body += "\":";
    body += counts[i];
  }
  body += "}}";

  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("전송 건너뜀: WiFi 연결 안 됨");
    return;
  }
  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(3000);
  int code = http.POST(body);
  Serial.printf("전송 결과 %d  %s\n", code, body.c_str());  // 201이면 성공
  http.end();
}

void setup() {
  Serial.begin(115200);
  while (!Serial);
  connectWiFi();
  I2S.setAllPins(-1, 42, 41, -1, -1);  // CLK=42, DATA=41
  if (!I2S.begin(PDM_MONO_MODE, SAMPLE_RATE, SAMPLE_BITS)) {
    Serial.println("ERR i2s");
    while (1);
  }
  samples = (int16_t *)malloc(EI_CLASSIFIER_RAW_SAMPLE_COUNT * sizeof(int16_t));
  if (!samples) {
    Serial.println("ERR malloc");
    while (1);
  }
  // 마이크가 켜진 직후의 튀는 값을 버린다
  size_t n;
  esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, samples,
                    EI_CLASSIFIER_RAW_SAMPLE_COUNT * sizeof(int16_t), &n, portMAX_DELAY);
  Serial.println("READY");
}

void loop() {
  // 1) 1초 녹음
  size_t n = 0;
  esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, samples,
                    EI_CLASSIFIER_RAW_SAMPLE_COUNT * sizeof(int16_t), &n, portMAX_DELAY);

  // 2) 녹음 때와 똑같은 전처리: 평균 빼기 + GAIN배, 범위 넘으면 자르기
  int count = EI_CLASSIFIER_RAW_SAMPLE_COUNT;
  int32_t sum = 0;
  for (int i = 0; i < count; i++) sum += samples[i];
  int32_t mean = sum / count;
  for (int i = 0; i < count; i++) {
    int32_t v = ((int32_t)samples[i] - mean) * GAIN;
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    samples[i] = (int16_t)v;
  }

  // 3) 분류
  signal_t signal;
  signal.total_length = EI_CLASSIFIER_RAW_SAMPLE_COUNT;
  signal.get_data = &get_audio;
  ei_impulse_result_t result = {0};
  EI_IMPULSE_ERROR err = run_classifier(&signal, &result, false);
  if (err != EI_IMPULSE_OK) {
    Serial.printf("ERR classifier %d\n", err);
    return;
  }

  // 4) 가장 높은 클래스와 전체 점수 출력
  int best = 0;
  for (int i = 1; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    if (result.classification[i].value > result.classification[best].value) best = i;
  }
  Serial.printf("%-8s %.2f  |", result.classification[best].label,
                result.classification[best].value);
  for (int i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    Serial.printf(" %s=%.2f", result.classification[i].label, result.classification[i].value);
  }
  Serial.printf("  (%d ms)\n", result.timing.dsp + result.timing.classification);

  // 5) 구간 집계 후 REPORT_EVERY번마다 서버로 전송
  counts[best]++;
  counted++;
  if (counted >= REPORT_EVERY) {
    sendReport(result);
    memset(counts, 0, sizeof(counts));
    counted = 0;
  }
}
