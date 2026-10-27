"""The Python uploader against the real Worker code (run in Node with a fake Access in front)."""
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from storelens import cloudsync
from storelens.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
pytestmark = pytest.mark.skipif(
    not (shutil.which("node") and (CLOUD / "node_modules" / "jose").exists()),
    reason="needs node and `npm install` in cloud/")


@pytest.fixture()
def harness():
    p = subprocess.Popen(["node", str(CLOUD / "test" / "harness.mjs")], stdout=subprocess.PIPE, text=True, cwd=CLOUD)
    info = json.loads(p.stdout.readline())
    yield info
    p.terminate()
    p.wait(timeout=10)


def _get(info, path, user=None):
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", headers={"X-Test-User": user} if user else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture()
def envelope(tmp_path):
    cfg_path = ROOT / "demo_data" / "store.yaml"
    db = ROOT / "demo_data" / "demo.db"
    return cloudsync.make_envelope(str(db), load_config(cfg_path), "demo-store")


def test_real_snapshot_passes_the_worker_guard_and_round_trips(harness, envelope):
    body = cloudsync.encode(envelope)
    url = f"http://127.0.0.1:{harness['port']}/ingest"
    r = cloudsync.push(url, body, harness["client_id"], harness["client_secret"], retries=1)
    assert r["status"] == 200 and r["body"]["latest_updated"] is True
    status, js = _get(harness, "/data/snapshot.js", user="owner@example.com")
    assert status == 200 and js.startswith("window.__STORELENS_SNAPSHOT__ = ")
    got = json.loads(js[len("window.__STORELENS_SNAPSHOT__ = "):].rstrip().rstrip(";"))
    assert set(got) == set(envelope["snapshot"])


def test_unauthenticated_and_wrong_credentials_are_refused(harness, envelope):
    assert _get(harness, "/data/snapshot.js")[0] == 403
    assert _get(harness, "/data/snapshot.js", user="stranger@example.com")[0] == 403
    url = f"http://127.0.0.1:{harness['port']}/ingest"
    with pytest.raises(RuntimeError, match="403"):
        cloudsync.push(url, cloudsync.encode(envelope), "wrong.access", "nope", retries=1)


def test_wrong_store_is_refused_by_the_worker(harness, envelope):
    envelope = {**envelope, "store_id": "someone-else"}
    url = f"http://127.0.0.1:{harness['port']}/ingest"
    with pytest.raises(RuntimeError, match="403"):
        cloudsync.push(url, cloudsync.encode(envelope), harness["client_id"], harness["client_secret"], retries=1)


def test_client_side_guard_stops_per_person_data_before_it_leaves(envelope):
    envelope["snapshot"]["/api/zones"] = [{"visit_key": "abc123", "dwell": 4}]
    with pytest.raises(ValueError, match="visit_key"):
        cloudsync.encode(envelope)
    assert cloudsync.scan_payload({"image_size": [640, 480]}) == []


def test_push_refuses_plain_http_to_the_internet_and_missing_credentials():
    with pytest.raises(ValueError, match="https"):
        cloudsync.push("http://example.com/ingest", b"{}", "a", "b")
    with pytest.raises(ValueError, match="CF_ACCESS"):
        cloudsync.push("https://example.com/ingest", b"{}", None, None)


def test_push_retries_server_errors_then_gives_up(monkeypatch):
    calls = []

    def boom(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        cloudsync.push("https://example.com/ingest", b"{}", "a", "b", sleep=lambda s: None)
    assert len(calls) == 3
