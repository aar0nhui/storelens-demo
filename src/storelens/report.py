"""Standalone HTML report: the same dashboard, with every default view embedded as a JSON snapshot.

Send it to a store owner as one file; no server, no network, no data leaves the page.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import load_config
from .server import WEB_DIR, build_snapshot, find_live


def build_report(db_path: str, config_path: str, out_path: str, live_dir: str | None = None) -> str:
    cfg = load_config(config_path)
    snap = build_snapshot(db_path, cfg, find_live(db_path, live_dir))
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    payload = json.dumps(snap, default=float).replace("</", "<\\/")
    html = html.replace('<link rel="stylesheet" href="static/style.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<script src="static/app.js"></script>',
                        f"<script>window.__STORELENS_SNAPSHOT__ = {payload};</script>\n<script>\n{js}\n</script>")
    Path(out_path).write_text(html, encoding="utf-8")
    return str(out_path)
