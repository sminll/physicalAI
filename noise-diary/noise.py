"""소음 일기 프로젝트 CLI

서브커맨드
  record : 보드 마이크로 1초짜리 wav를 녹음해 학습 데이터 만들기
  serve  : 보드가 보내는 1분 요약을 받아 SQLite DB에 저장하고 리포트 페이지 제공
  fake   : 보드 없이 가짜 1분 요약을 서버로 보내 테스트

사용 예
  python noise.py record --port COM3 --label speech --count 60
  python noise.py serve
  python noise.py fake --minutes 30

외부 패키지는 record에만 필요해요(pip install pyserial). serve와 fake는 파이썬 기본 기능만 써요.
"""
import argparse
import json
import random
import sqlite3
import time
import urllib.request
import wave
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LABELS = ["silence", "speech", "music", "typing"]

# ───────────────────────── record ─────────────────────────
SAMPLE_RATE = 16000
RECORD_BYTES = SAMPLE_RATE * 2  # 16bit mono, 1초


def read_until(ser, token: bytes, timeout: float) -> bool:
    """token이 나올 때까지 읽는다. 다른 출력(READY 등)은 버린다."""
    end = time.time() + timeout
    got = b""
    while time.time() < end:
        c = ser.read(1)
        if not c:
            continue
        got = (got + c)[-len(token):]
        if got == token:
            return True
    return False


def record_one(ser) -> bytes:
    ser.reset_input_buffer()
    ser.write(b"r")
    if not read_until(ser, b"DATA\n", timeout=5):
        raise RuntimeError("보드 응답이 없어요. 포트와 스케치 업로드를 확인하세요.")
    data = b""
    end = time.time() + 5
    while len(data) < RECORD_BYTES and time.time() < end:
        data += ser.read(RECORD_BYTES - len(data))
    if len(data) != RECORD_BYTES:
        raise RuntimeError(f"데이터가 덜 왔어요 ({len(data)}/{RECORD_BYTES}바이트)")
    return data


def save_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def cmd_record(args):
    import serial  # pip install pyserial

    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)
    start = len(list(out.glob(f"{args.label}.*.wav")))  # 이어서 녹음하면 번호를 이어 붙임

    with serial.Serial(args.port, 115200, timeout=0.5) as ser:
        time.sleep(2)  # 포트를 열 때 보드가 리셋될 수 있어 잠깐 기다림
        print(f"[{args.label}] {args.count}개 녹음 시작 (Ctrl+C로 중단)")
        for i in range(start, start + args.count):
            if args.gap > 0:
                time.sleep(args.gap)
            pcm = None
            for attempt in range(3):  # USB로 오다가 바이트가 빠지면 버리고 다시 녹음
                try:
                    pcm = record_one(ser)
                    break
                except RuntimeError as e:
                    print(f"  [다시 시도 {attempt + 1}/3] {e}")
                    time.sleep(0.5)  # 늦게 도착하는 나머지 바이트가 들어오길 기다렸다 버림
            if pcm is None:
                raise RuntimeError("3번 연속 실패했어요. 보드를 RESET하고 다시 실행하세요.")
            path = out / f"{args.label}.{i:04d}.wav"
            save_wav(path, pcm)
            print(f"  저장: {path}")
    print("완료")


# ───────────────────────── DB ─────────────────────────
def db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS minute_log (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT NOT NULL,          -- 서버가 받은 시각 (YYYY-MM-DD HH:MM:SS)
               device TEXT NOT NULL,
               silence INTEGER NOT NULL,  -- 1분 중 각 소리로 분류된 횟수(초)
               speech INTEGER NOT NULL,
               music INTEGER NOT NULL,
               typing INTEGER NOT NULL,
               top TEXT NOT NULL          -- 그 1분 동안 가장 많았던 소리
           )"""
    )
    conn.commit()
    return conn


def validate_payload(data) -> dict:
    """보드가 보낸 JSON을 검사해 {device, counts}로 돌려준다. 문제가 있으면 ValueError."""
    if not isinstance(data, dict):
        raise ValueError("JSON 객체여야 해요")
    device = data.get("device", "xiao")
    if not isinstance(device, str) or not device or len(device) > 40:
        raise ValueError("device 값이 이상해요")
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("counts가 없어요")
    clean = {}
    for label in LABELS:
        v = counts.get(label, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 120:
            raise ValueError(f"{label} 값이 이상해요: {v!r}")
        clean[label] = v
    if sum(clean.values()) == 0:
        raise ValueError("모든 값이 0이에요")
    return {"device": device, "counts": clean}


def insert_minute(conn, device: str, counts: dict, ts: datetime | None = None) -> int:
    ts = ts or datetime.now()
    top = max(LABELS, key=lambda k: counts[k])
    cur = conn.execute(
        "INSERT INTO minute_log (ts, device, silence, speech, music, typing, top) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts.strftime("%Y-%m-%d %H:%M:%S"), device, *(counts[k] for k in LABELS), top),
    )
    conn.commit()
    return cur.lastrowid


def summary(conn, date: str) -> dict:
    """하루치 기록: 클래스별 총 초, 분 단위 목록."""
    rows = conn.execute(
        "SELECT ts, silence, speech, music, typing, top FROM minute_log "
        "WHERE substr(ts, 1, 10) = ? ORDER BY ts",
        (date,),
    ).fetchall()
    totals = {k: 0 for k in LABELS}
    minutes = []
    for ts, *vals, top in rows:
        item = {"ts": ts, "top": top}
        for k, v in zip(LABELS, vals):
            totals[k] += v
            item[k] = v
        minutes.append(item)
    return {"date": date, "labels": LABELS, "minutes_logged": len(rows),
            "totals": totals, "minutes": minutes}


# ───────────────────────── serve ─────────────────────────
PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>소음 일기</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}
 h1{margin-bottom:4px} .sub{color:#666;margin-top:0}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
 .card{flex:1;min-width:140px;border:1px solid #ddd;border-radius:10px;padding:12px}
 .card b{display:block;font-size:26px} canvas{margin-top:12px}
</style></head><body>
<h1>소음 일기</h1>
<p class="sub"><input type="date" id="d"> · 기록된 시간 <span id="n">0</span>분 · 10초마다 자동 새로고침</p>
<div class="cards" id="cards"></div>
<canvas id="timeline" height="120"></canvas>
<script>
const COLORS={silence:'#9aa5b1',speech:'#2f9e44',music:'#1c7ed6',typing:'#e8590c'};
const NAMES={silence:'조용함',speech:'말소리',music:'음악',typing:'타이핑'};
const d=document.getElementById('d'); d.value=new Date().toLocaleDateString('sv-SE');
let chart;
async function load(){
  const r=await fetch('/api/summary?date='+d.value); const s=await r.json();
  document.getElementById('n').textContent=s.minutes_logged;
  const total=Object.values(s.totals).reduce((a,b)=>a+b,0)||1;
  document.getElementById('cards').innerHTML=s.labels.map(k=>
    `<div class="card" style="border-left:6px solid ${COLORS[k]}">${NAMES[k]}<b>${Math.round(s.totals[k]/total*100)}%</b>${s.totals[k]}초</div>`).join('');
  const data={labels:s.minutes.map(m=>m.ts.slice(11,16)),
    datasets:s.labels.map(k=>({label:NAMES[k],data:s.minutes.map(m=>m[k]),backgroundColor:COLORS[k]}))};
  if(chart){chart.data=data;chart.update();return;}
  chart=new Chart(document.getElementById('timeline'),{type:'bar',data,
    options:{animation:false,scales:{x:{stacked:true},y:{stacked:true,max:60,title:{display:true,text:'초'}}}}});
}
d.onchange=load; load(); setInterval(load,10000);
</script></body></html>"""


def make_handler(conn):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/summary":
                date = parse_qs(u.query).get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
                self._send(200, summary(conn, date))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/log":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                p = validate_payload(json.loads(self.rfile.read(length) or b"null"))
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            row_id = insert_minute(conn, p["device"], p["counts"])
            print(f"  기록 #{row_id} {p['device']} {p['counts']}")
            self._send(201, {"ok": True, "id": row_id})

        def log_message(self, *a):  # 기본 접속 로그는 숨김
            pass

    return Handler


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(db_connect(db_path)))


def cmd_serve(args):
    srv = make_server(args.host, args.port, args.db)
    print(f"서버 시작: http://localhost:{args.port}  (DB: {args.db}, 종료: Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


# ───────────────────────── fake ─────────────────────────
def post_json(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def fake_counts(rng: random.Random) -> dict:
    main = rng.choice(LABELS)
    counts = {k: 0 for k in LABELS}
    counts[main] = rng.randint(35, 55)
    for _ in range(60 - counts[main]):
        counts[rng.choice(LABELS)] += 1
    return counts


def cmd_fake(args):
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + "/api/log"
    for i in range(args.minutes):
        res = post_json(url, {"device": "fake", "counts": fake_counts(rng)})
        print(f"  보냄 {i + 1}/{args.minutes} → {res}")
        if args.interval > 0:
            time.sleep(args.interval)
    print("완료")


# ───────────────────────── main ─────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(description="소음 일기 프로젝트 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="보드 마이크로 1초짜리 wav를 여러 개 녹음")
    r.add_argument("--port", required=True, help="예: COM3")
    r.add_argument("--label", required=True, help="예: silence, speech, music, typing")
    r.add_argument("--count", type=int, default=30)
    r.add_argument("--gap", type=float, default=0.3, help="녹음 사이 쉬는 시간(초)")
    r.add_argument("--out", default="data")
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("serve", help="1분 요약을 받아 DB에 저장하고 리포트 페이지 제공")
    s.add_argument("--host", default="0.0.0.0", help="0.0.0.0이면 보드가 와이파이로 접속 가능")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default="noise.db")
    s.set_defaults(func=cmd_serve)

    f = sub.add_parser("fake", help="보드 없이 가짜 1분 요약을 서버로 보내 테스트")
    f.add_argument("--url", default="http://localhost:8000")
    f.add_argument("--minutes", type=int, default=10)
    f.add_argument("--interval", type=float, default=0.0, help="보내는 간격(초)")
    f.add_argument("--seed", type=int, default=None)
    f.set_defaults(func=cmd_fake)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
