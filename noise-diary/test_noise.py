"""noise.py 서버·DB 테스트 (CI에서 pytest로 실행)"""
import json
import threading
import urllib.error
import urllib.request

import pytest

import noise


def test_validate_ok():
    p = noise.validate_payload({"device": "xiao", "counts": {"speech": 40, "silence": 20}})
    assert p["counts"] == {"silence": 20, "speech": 40, "music": 0, "typing": 0}


@pytest.mark.parametrize("bad", [
    None,
    {"counts": "x"},
    {"counts": {"speech": -1}},
    {"counts": {"speech": 1.5}},
    {"counts": {"speech": 999}},
    {"counts": {}},
    {"device": "", "counts": {"speech": 10}},
])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        noise.validate_payload(bad)


def test_insert_and_summary(tmp_path):
    conn = noise.db_connect(str(tmp_path / "t.db"))
    noise.insert_minute(conn, "xiao", {"silence": 10, "speech": 50, "music": 0, "typing": 0})
    noise.insert_minute(conn, "xiao", {"silence": 5, "speech": 0, "music": 45, "typing": 10})
    from datetime import datetime
    s = noise.summary(conn, datetime.now().strftime("%Y-%m-%d"))
    assert s["minutes_logged"] == 2
    assert s["totals"] == {"silence": 15, "speech": 50, "music": 45, "typing": 10}
    assert [m["top"] for m in s["minutes"]] == ["speech", "music"]


def test_http_end_to_end(tmp_path):
    srv = noise.make_server("127.0.0.1", 0, str(tmp_path / "t.db"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        res = noise.post_json(base + "/api/log", {"device": "xiao", "counts": {"typing": 60}})
        assert res["ok"] is True

        with pytest.raises(urllib.error.HTTPError) as e:
            noise.post_json(base + "/api/log", {"counts": {"typing": -5}})
        assert e.value.code == 400

        with urllib.request.urlopen(base + "/api/summary") as r:
            s = json.loads(r.read())
        assert s["totals"]["typing"] == 60

        with urllib.request.urlopen(base + "/") as r:
            assert "소음 일기" in r.read().decode()
    finally:
        srv.shutdown()
