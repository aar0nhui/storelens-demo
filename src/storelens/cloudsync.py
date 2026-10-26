"""Send the dashboard's pre-computed answers from the store's computer to the private cloud app.

Direction of flow: the edge box PUSHES a JSON snapshot (aggregates only, the same data the dashboard shows) to the
Worker in ``cloud/``; the cloud never reaches into the store network and never receives frames, tracks or visit keys.
The box authenticates with a Cloudflare Access service token (``CF-Access-Client-Id`` / ``CF-Access-Client-Secret``).

The payload is checked here before it leaves, and again in the Worker: a bug that put an image, an embedding or a visit
key into the snapshot is stopped at the box.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import StoreConfig

SCHEMA_VERSION = 1
MAX_BYTES = 8 * 1024 * 1024  # the Worker refuses more than this
# Exact key names that mean "something per person or per image". Keep in sync with cloud/src/guard.js.
FORBIDDEN_KEYS = {
    "embedding", "embeddings", "face", "faces", "face_crop", "crop", "crops", "thumbnail", "thumbnails", "landmark",
    "landmarks", "descriptor", "descriptors", "biometric", "biometrics", "image", "images", "frame", "frames",
    "jpeg", "png", "trajectory", "trajectories", "track_id", "visit_key", "visit_keys", "uid"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
MAX_STRING = 4000  # a longer string is more likely an encoded blob than a label


def scan_payload(obj, path: str = "$", depth: int = 0, problems: list[str] | None = None) -> list[str]:
    problems = [] if problems is None else problems
    if len(problems) >= 20 or depth > 14:
        return problems
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                problems.append(f"{path}.{k}: key name suggests per-person or image data")
            scan_payload(v, f"{path}.{k}", depth + 1, problems)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5000]):
            scan_payload(v, f"{path}[{i}]", depth + 1, problems)
    elif isinstance(obj, str):
        if obj.startswith("data:image") or obj.startswith("data:video") or len(obj) > MAX_STRING:
            problems.append(f"{path}: {len(obj)}-character string / embedded media")
    return problems


def make_envelope(db_path: str, cfg: StoreConfig, store_id: str, now: datetime | None = None) -> dict:
    from .server import build_snapshot
    if not ID_RE.match(store_id):
        raise ValueError("store id must be 2-63 characters: lowercase letters, digits, '-' or '_'")
    now = now or datetime.now(timezone.utc)
    snap = build_snapshot(db_path, cfg, None)
    snap.pop("/api/live", None)
    env = {"schema_version": SCHEMA_VERSION, "store_id": store_id,
           "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
           "layout_version": str(cfg.layout_version), "snapshot": snap}
    return env


def encode(env: dict) -> bytes:
    problems = scan_payload(env)
    if problems:
        raise ValueError("refusing to send: " + "; ".join(problems[:5]))
    body = json.dumps(env, default=float, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError(f"snapshot is {len(body) / 1e6:.1f} MB, over the {MAX_BYTES / 1e6:.0f} MB limit")
    return body


def push(url: str, body: bytes, client_id: str | None = None, client_secret: str | None = None, timeout: float = 60.0,
         retries: int = 3, sleep=time.sleep) -> dict:
    """POST the snapshot. Retries connection errors and 5xx with backoff; a 4xx is final (fix the cause)."""
    client_id = client_id or os.environ.get("CF_ACCESS_CLIENT_ID")
    client_secret = client_secret or os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise ValueError("set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET (the Access service token)")
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        raise ValueError("the ingest URL must be https:// (plain http only for localhost tests)")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret,
        "User-Agent": "storelens-push/1"})
    last = ""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return {"status": r.status, "body": json.loads(r.read().decode() or "{}"), "attempts": attempt + 1}
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")[:300]
            if e.code < 500:
                raise RuntimeError(f"ingest refused the snapshot: HTTP {e.code} {text}") from None
            last = f"HTTP {e.code} {text}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(e)
        sleep(2 ** attempt)
    raise RuntimeError(f"could not reach the ingest endpoint after {retries} attempts: {last}")
