"""Static-site export: a plain folder of files that any static host can serve."""
import functools
import http.server
import json
import re
import threading

import pytest

from storelens.site import build_site
from tests.test_server import chromium, client, live_dir  # noqa: F401  (fixtures shared with the API tests)

LIMIT_FILE = 25 * 1024 * 1024  # Cloudflare Pages / Workers static assets: max size of one file
LIMIT_FILES = 20_000           # ... and files per deployment on the free plans


def _snapshot(site):
    js = (site / "data" / "snapshot.js").read_text()
    m = re.match(r"window\.__STORELENS_SNAPSHOT__ = (.*);\n", js)
    return json.loads(m.group(1)), js


def test_site_layout_relative_urls_and_snapshot(client, live_dir, tmp_path):
    out = tmp_path / "site"
    r = build_site(client.db_path, client.cfg_path, str(out), str(live_dir), demo_label="Demo · simulated data",
                   cta_text="Request a pilot", cta_url="https://example.org/form")
    assert r["files"] >= 9
    for f in ("index.html", "404.html", "_headers", "robots.txt", ".nojekyll", "static/app.js", "static/style.css",
              "data/snapshot.js", "media/camera.mp4", "media/floorplan.mp4"):
        assert (out / f).is_file(), f
    html = (out / "index.html").read_text()
    assert not re.search(r'(?:href|src)="/', html), "root-relative URLs break hosting under a sub-path"
    assert html.index("data/snapshot.js") < html.index("static/app.js")  # snapshot defined before the app runs
    assert "Content-Security-Policy" in html and "script-src 'self'" in html and "media-src 'self'" in html
    snap, js = _snapshot(out)
    assert {"/api/config", "/api/runs", "/api/summary", "/api/zones", "/api/timeseries", "/api/insights"} <= set(snap)
    assert snap["/api/live"]["camera"] == "media/camera.mp4"  # relative path to a separate file, not base64
    assert "data:video" not in js
    demo = json.loads(re.search(r"window\.__STORELENS_DEMO__ = (.*);\n", js).group(1))  # ASCII-escaped, so charset-proof
    assert demo == {"label": "Demo · simulated data", "ctaText": "Request a pilot", "ctaUrl": "https://example.org/form"}
    assert "nosniff" in (out / "_headers").read_text()
    assert (out / "robots.txt").read_text().strip().endswith("Allow: /")


def test_site_without_live_clips_has_no_media(client, tmp_path):
    out = tmp_path / "s"
    build_site(client.db_path, client.cfg_path, str(out))
    assert not (out / "media").exists() and "/api/live" not in _snapshot(out)[0]
    assert "__STORELENS_DEMO__" not in _snapshot(out)[1]


def test_rebuild_replaces_what_it_owns_and_keeps_the_rest(client, tmp_path):
    out = tmp_path / "s"
    out.mkdir()
    (out / "CNAME").write_text("demo.example.org")
    (out / "static").mkdir()
    (out / "static" / "stale.js").write_text("old")
    build_site(client.db_path, client.cfg_path, str(out))
    assert (out / "CNAME").read_text() == "demo.example.org" and not (out / "static" / "stale.js").exists()


def test_noindex_and_cta_validation(client, tmp_path):
    out = tmp_path / "s"
    build_site(client.db_path, client.cfg_path, str(out), noindex=True)
    assert 'name="robots" content="noindex"' in (out / "index.html").read_text()
    assert "Disallow: /" in (out / "robots.txt").read_text()
    with pytest.raises(ValueError):
        build_site(client.db_path, client.cfg_path, str(tmp_path / "x"), cta_url="javascript:alert(1)")


def test_site_fits_free_hosting_limits(client, live_dir, tmp_path):
    out = tmp_path / "s"
    build_site(client.db_path, client.cfg_path, str(out), str(live_dir))
    files = [p for p in out.rglob("*") if p.is_file()]
    assert len(files) < LIMIT_FILES and max(p.stat().st_size for p in files) < LIMIT_FILE


def test_site_works_in_a_browser_under_a_sub_path_with_its_csp(client, chromium, tmp_path):  # noqa: F811
    """Serve it from /some/sub/path/ over HTTP (as a CDN would) and click through: relative URLs resolve, the CSP
    meta blocks nothing the app needs, nothing is requested from another origin."""
    root = tmp_path / "www"
    build_site(client.db_path, client.cfg_path, str(root / "some" / "sub" / "path"), demo_label="Demo · simulated data",
               cta_text="Request a pilot", cta_url="mailto:team@example.org")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    page = chromium.new_page(viewport={"width": 1280, "height": 860})
    problems, requested = [], []
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("request", lambda r: requested.append(r.url))
    try:
        page.goto(base + "/some/sub/path/")
        page.wait_for_selector("#kpis .kpi")
        assert page.inner_text("#demoChip") == "Demo · simulated data"
        assert page.get_attribute("#ctaLink", "href") == "mailto:team@example.org" and page.is_visible("#ctaLink")
        for t in ("floor", "activity", "audience", "recommend", "insights", "data"):
            page.click(f"#nav-{t}")
            page.wait_for_function(f"location.hash === '#/{t}'")
            page.wait_for_timeout(400)
            text = page.inner_text("main") if page.query_selector("main") else page.inner_text("body")
            for bad in ("null", "undefined", "NaN", "[object"):  # a child that was null/undefined once rendered as text
                assert bad not in text, f"'{bad}' is showing on the {t} tab"
        page.goto(base + "/some/sub/path/#/floor")
        page.wait_for_selector("#zoneTable tr.zrow")
    finally:
        page.close()
        srv.shutdown()
    assert all(u.startswith(base) or u.startswith("data:") for u in requested), [u for u in requested if not u.startswith(base)]
    assert not problems, problems


def test_dark_mode_toggle(client, chromium, tmp_path):  # noqa: F811
    """Light mode is the default look. The toggle switches to dark, remembers the choice across a reload, redraws the
    floor-plan canvas in the new palette, and switching back restores the light look exactly. Served over HTTP with the
    site's own CSP, which forbids inline script: the theme has to be applied by static/app.js."""
    root = tmp_path / "www"
    build_site(client.db_path, client.cfg_path, str(root))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    page = chromium.new_page(viewport={"width": 1280, "height": 860})
    problems = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    theme = lambda: page.evaluate("document.documentElement.getAttribute('data-theme')")  # noqa: E731
    bg = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")  # noqa: E731
    try:
        page.goto(base + "/#/floor")
        page.wait_for_selector("#zoneTable tr.zrow")
        # nothing stored yet: no override, so the stylesheet follows the operating system (headless chromium = light)
        assert theme() is None
        light_bg, light_floor = bg(), page.evaluate("document.querySelector('canvas.floor').toDataURL()")
        assert page.get_attribute("#themeBtn", "aria-label") == "Switch to dark mode"

        page.click("#themeBtn")
        assert theme() == "dark" and bg() != light_bg
        assert page.get_attribute("#themeBtn", "aria-label") == "Switch to light mode"
        dark_floor = page.evaluate("document.querySelector('canvas.floor').toDataURL()")
        assert dark_floor != light_floor, "the floor plan kept its light-mode colours"

        page.reload()  # the choice sticks
        page.wait_for_selector("#zoneTable tr.zrow")
        assert theme() == "dark"

        page.click("#themeBtn")  # ... and back to exactly the light look
        assert theme() == "light" and bg() == light_bg
        assert page.evaluate("document.querySelector('canvas.floor').toDataURL()") == light_floor
    finally:
        page.close()
        srv.shutdown()
    assert not problems, problems
