"""Static site export: the dashboard as a folder of plain files for any static host (Cloudflare, GitHub Pages, S3 ...).

Unlike ``report.py`` (one self-contained HTML file for e-mailing), the site keeps the pieces apart so a CDN can cache
them: the page, ``static/app.js`` + ``static/style.css``, ``data/snapshot.js`` (every default view as JSON) and the
simulated live-view clips under ``media/``. There is no server and no network call at run time: all URLs are relative,
so it also works under a sub-path (``user.github.io/repo/``).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .config import load_config
from .server import WEB_DIR, build_snapshot, find_live, live_info

# Scripts and styles only from this origin; no fetches except to ourselves; no inline script. (Inline *style attributes*
# are used by the charts, hence 'unsafe-inline' for styles only.) Set as a <meta> so it also holds on hosts that
# cannot send headers (GitHub Pages).
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")

# Cloudflare Pages / Workers static assets read this file; other hosts ignore it.
HEADERS = """/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Content-Security-Policy: frame-ancestors 'none'
"""

NOT_FOUND = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found · Glance</title>
<style>@font-face{font-family:Geist;src:url("static/Geist-Variable.woff2") format("woff2");font-weight:100 900;font-display:swap}
body{font:16px/1.5 Geist,system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f6f9;color:#141a23}
a{color:#2c6bed}</style></head>
<body><p>That page does not exist. <a href="./">Back to the Glance demo</a></p></body></html>
"""

# files and folders this command owns inside --out; anything else in there is left alone
OWNED = ("index.html", "404.html", "_headers", "robots.txt", ".nojekyll", "static", "data", "media")


def _check_cta(url: str | None) -> str | None:
    if not url:
        return None
    if urlparse(url).scheme not in ("http", "https", "mailto"):
        raise ValueError("--cta-url must start with http://, https:// or mailto:")
    return url


def build_site(db_path: str | None, config_path: str | None, out_dir: str, live_dir: str | None = None,
               demo_label: str | None = None, cta_text: str | None = None, cta_url: str | None = None,
               noindex: bool = False, private: bool = False) -> dict:
    """Write the static site into ``out_dir`` and return a small summary (paths and byte sizes).

    ``private=True`` writes the app shell only, for the private cloud Worker (``cloud/``): no database, no snapshot and
    no clips are read or written. The Worker serves ``data/snapshot.js`` itself, from the latest snapshot the store's
    computer pushed, after checking the Cloudflare Access login."""
    cta_url = _check_cta(cta_url)
    if private:
        cfg, live, snap, noindex = None, None, None, True
    else:
        cfg = load_config(config_path)
        live = find_live(db_path, live_dir)
        snap = build_snapshot(db_path, cfg, None)  # clips stay separate files here, not base64 inside the snapshot
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name in OWNED:  # rebuild from scratch, but only touch what we own
        p = out / name
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
    (out / "static").mkdir()
    (out / "data").mkdir()
    for f in ("style.css", "app.js", "Geist-Variable.woff2", "Geist-LICENSE.txt"):  # the OFL travels with the font
        shutil.copy2(WEB_DIR / f, out / "static" / f)

    if live is not None:
        (out / "media").mkdir()
        info = live_info(live, "media/")
        for key in ("camera", "floorplan"):
            shutil.copy2(live / Path(info[key]).name, out / "media" / Path(info[key]).name)
        snap["/api/live"] = info

    if not private:
        demo = {k: v for k, v in {"label": demo_label, "ctaText": cta_text, "ctaUrl": cta_url}.items() if v}
        js = "window.__GLANCE_SNAPSHOT__ = " + json.dumps(snap, default=float, separators=(",", ":")) + ";\n"
        if demo:
            js += "window.__GLANCE_DEMO__ = " + json.dumps(demo) + ";\n"
        (out / "data" / "snapshot.js").write_text(js, encoding="utf-8")
    else:
        (out / "data").rmdir()

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    head_extra = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
    head_extra += '<meta name="description" content="Privacy-first in-store analytics on existing CCTV: footfall, display impressions, engagement and layout insights. Demo with simulated data.">\n'
    if noindex:
        head_extra += '<meta name="robots" content="noindex">\n'
    html = html.replace('<meta charset="utf-8">\n', '<meta charset="utf-8">\n' + head_extra, 1)
    html = html.replace('<script src="static/app.js"></script>',
                        '<script src="data/snapshot.js"></script>\n<script src="static/app.js"></script>', 1)
    assert "Content-Security-Policy" in html and "data/snapshot.js" in html
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "404.html").write_text(NOT_FOUND, encoding="utf-8")
    if not private:  # the Worker sets its own headers
        (out / "_headers").write_text(HEADERS, encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\n" + ("Disallow: /\n" if noindex else "Allow: /\n"), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: serve files as they are

    files = sorted(p for p in out.rglob("*") if p.is_file())
    return {"out": str(out), "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "largest": max(((p.stat().st_size, str(p.relative_to(out))) for p in files))}
