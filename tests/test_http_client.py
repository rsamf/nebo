# tests/test_http_client.py
import json
from unittest.mock import patch

from nebo.client import _resolve_url, _get


def test_resolve_url_prefers_explicit_url():
    assert _resolve_url(url="http://example.test:1234") == "http://example.test:1234"


def test_resolve_url_uses_port_when_no_url():
    assert _resolve_url(port=9999) == "http://localhost:9999"


def test_resolve_url_reads_env(monkeypatch):
    monkeypatch.setenv("NEBO_CLI_URL", "http://daemon.local")
    assert _resolve_url() == "http://daemon.local"


def test_resolve_url_defaults(monkeypatch):
    # No args, no env (NEBO_CLI_URL/NEBO_CLI_PORT unset via monkeypatch).
    monkeypatch.delenv("NEBO_CLI_URL", raising=False)
    monkeypatch.delenv("NEBO_CLI_PORT", raising=False)
    assert _resolve_url() == "http://localhost:7861"


def test_get_sends_auth_header_when_token_set(monkeypatch):
    captured: dict = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=5.0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return FakeResp()

    with patch("nebo.client.urllib.request.urlopen", fake_urlopen):
        result = _get("/health", url="http://h", api_token="secret")

    assert result == {"ok": True}
    assert captured["url"] == "http://h/health"
    assert captured["headers"].get("X-nebo-token") == "secret"


from nebo.client import (
    get_run_history,
    get_run_status,
    get_description,
    get_graph,
    get_loggable_status,
    get_metrics,
    get_text,
    load_file,
    _post,
)


def _stub_urlopen(monkeypatch, expected_body: bytes) -> dict:
    captured: dict = {"calls": []}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return expected_body

    def fake_urlopen(req, timeout=5.0):
        captured["calls"].append({
            "url": req.full_url,
            "method": req.get_method(),
            "data": req.data,
            "headers": dict(req.header_items()),
        })
        return FakeResp()

    monkeypatch.setattr("nebo.client.urllib.request.urlopen", fake_urlopen)
    return captured


def test_get_run_history_hits_runs_endpoint(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"runs": []}')
    result = get_run_history(url="http://h")
    assert result == {"runs": []}
    assert cap["calls"][0]["url"] == "http://h/runs"


def test_get_metrics_passes_filter_query_params(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"metrics": {}}')
    get_metrics(
        "node_a",
        name="loss",
        tag="train",
        step=5,
        run_id="abc123",
        url="http://h",
    )
    qs = cap["calls"][0]["url"].split("?", 1)[1]
    parts = dict(p.split("=", 1) for p in qs.split("&"))
    assert parts["name"] == "loss"
    assert parts["tag"] == "train"
    assert parts["step"] == "5"


def test_get_text_run_scoped(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"texts": []}')
    get_text(run_id="abc", url="http://h")
    assert cap["calls"][0]["url"] == "http://h/runs/abc/text"

def test_get_text_latest_run(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"texts": []}')
    get_text(url="http://h")
    assert cap["calls"][0]["url"] == "http://h/text"


def test_post_sends_json_body(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"status": "ok"}')
    result = _post("/events", [{"type": "metric"}], url="http://h", api_token="t")
    assert result == {"status": "ok"}
    assert cap["calls"][0]["method"] == "POST"
    assert json.loads(cap["calls"][0]["data"]) == [{"type": "metric"}]
    assert cap["calls"][0]["headers"]["Content-type"] == "application/json"
    assert cap["calls"][0]["headers"]["X-nebo-token"] == "t"


def test_load_file_posts_filepath(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"status": "loaded"}')
    load_file("/tmp/x.nebo", url="http://h")
    assert cap["calls"][0]["url"] == "http://h/load"
    assert json.loads(cap["calls"][0]["data"]) == {"filepath": "/tmp/x.nebo"}


import base64
from pathlib import Path

from nebo.client import log_metric, log_text


def test_log_metric_defaults_loggable_id_to_agent(monkeypatch):
    posted: list = []

    def fake_post(path, body, **conn):
        posted.append((path, body))
        return {"status": "ok"}

    monkeypatch.setattr("nebo.client._post", fake_post)
    log_metric([{"name": "loss", "value": 0.1}], run_id="r1")
    assert posted
    _, body = posted[0]
    metric = next(e for e in body if e["type"] == "metric")
    assert metric["loggable_id"] == "__agent__"
    reg = next(e for e in body if e["type"] == "loggable_register")
    assert reg["data"]["kind"] == "agent"


def test_log_text_routes_default_to_agent(monkeypatch):
    posted: list = []
    monkeypatch.setattr(
        "nebo.client._post",
        lambda path, body, **c: posted.append((path, body)) or {"status": "ok"},
    )
    log_text([{"message": "hi"}], run_id="r1")
    text_event = next(e for e in posted[0][1] if e["type"] == "text")
    assert text_event["loggable_id"] == "__agent__"
    assert "level" not in text_event


def test_log_text_forwards_name(monkeypatch):
    posted: list = []
    monkeypatch.setattr(
        "nebo.client._post",
        lambda path, body, **c: posted.append((path, body)) or {"status": "ok"},
    )
    log_text([{"message": "hi", "name": "status"}], run_id="r1")
    text_event = next(e for e in posted[0][1] if e["type"] == "text")
    assert text_event["name"] == "status"


import pytest
from nebo.client import log_image, log_audio


def test_log_image_reads_path_and_submits_base64(monkeypatch, tmp_path):
    raw = b"\x89PNG\r\n\x1a\nfake"
    img_path: Path = tmp_path / "x.png"
    img_path.write_bytes(raw)

    posted: list = []
    monkeypatch.setattr(
        "nebo.client._post",
        lambda path, body, **c: posted.append((path, body)) or {"status": "ok"},
    )
    log_image([{"name": "img", "path": str(img_path)}], run_id="r1")
    img_evt = next(e for e in posted[0][1] if e["type"] == "image")
    assert img_evt["data"] == base64.b64encode(raw).decode("ascii")


def test_log_image_passes_url_unchanged(monkeypatch):
    posted: list = []
    monkeypatch.setattr(
        "nebo.client._post",
        lambda path, body, **c: posted.append((path, body)) or {"status": "ok"},
    )
    log_image([{"name": "img", "url": "https://example.com/x.png"}], run_id="r1")
    img_evt = next(e for e in posted[0][1] if e["type"] == "image")
    assert img_evt["url"] == "https://example.com/x.png"


def test_log_image_rejects_two_sources(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        log_image([{"name": "img", "path": str(f), "url": "https://x"}])


def test_log_audio_defaults_sr(monkeypatch, tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"RIFFfake")
    posted: list = []
    monkeypatch.setattr(
        "nebo.client._post",
        lambda path, body, **c: posted.append((path, body)) or {"status": "ok"},
    )
    log_audio([{"name": "snd", "path": str(f)}], run_id="r1")
    audio_evt = next(e for e in posted[0][1] if e["type"] == "audio")
    assert audio_evt["sr"] == 16000


from nebo.client import wait_for_alert


def test_wait_for_alert_hits_alerts_wait_endpoint(monkeypatch):
    cap = _stub_urlopen(monkeypatch, b'{"status": "timeout"}')
    result = wait_for_alert(run_id="r1", timeout=5, min_level=30, url="http://h")
    assert result == {"status": "timeout"}
    url = cap["calls"][0]["url"]
    assert "/runs/r1/alerts/wait" in url
    qs = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert qs["timeout"] == "5"
    assert qs["min_level"] == "30"
