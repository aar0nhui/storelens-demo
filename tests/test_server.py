"""HTTP API over a small hand-built store (no video, no detector needed)."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from storelens.config import dump_config
from storelens.server import build_snapshot, create_app
from tests.test_queries import FIX, T0, Builder, _populate

import numpy as np


@pytest.fixture(scope="module")
def client(tmp_path_factory, cfg):
    d = tmp_path_factory.mktemp("srv")
    dump_config(cfg, d / "store.yaml")
    b = Builder(d / "t.db")
    rng = np.random.default_rng(2)
    _populate(b, "before", T0, 400, {z: 0.25 for z in FIX}, rng)
    _populate(b, "after", T0 + 7 * 86400, 400, dict({z: 0.25 for z in FIX}, endcap_b=0.6), rng)
    b.st.add_heat(1, {(T0, 5, 5): 12.0})
    b.st.close()
    app = create_app(str(d / "t.db"), str(d / "store.yaml"))
    c = TestClient(app)
    c.db_path, c.cfg_path = str(d / "t.db"), str(d / "store.yaml")
    return c


@pytest.mark.parametrize("path", ["/api/config", "/api/runs", "/api/summary", "/api/zones", "/api/heatmap",
                                  "/api/timeseries", "/api/flows", "/api/insights", "/api/quality"])
def test_endpoints_respond(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.json() is not None


def test_run_selection_changes_numbers(client):
    both = client.get("/api/summary").json()["visitors"]
    one = client.get("/api/summary?runs=1").json()["visitors"]
    assert both == 800 and one == 400
    assert client.get("/api/zones?runs=2").json()[3]["zone_id"] == "endcap_b"


def test_compare_and_insights_agree_on_the_improvement(client):
    cmp_ = client.get("/api/compare?a=1&b=2").json()
    by = {z["zone_id"]: z for z in cmp_["zones"]}
    assert by["endcap_b"]["adjusted"]["verdict"] == "improved"
    cards = client.get("/api/insights?a=1&b=2").json()
    assert any(c["id"] == "lift:endcap_b" for c in cards)


def test_compare_zone_subset(client):
    res = client.get("/api/compare?a=1&b=2&zones=endcap_b,wall_a").json()
    assert [z["zone_id"] for z in res["zones"]] == ["endcap_b", "wall_a"]


def test_bad_input_is_a_clean_4xx(client):
    assert client.get("/api/summary?runs=abc").status_code in (400, 422)
    assert client.get("/api/summary?t0=notatime").status_code == 400
    assert client.get("/api/compare?a=1").status_code == 422


def test_dashboard_and_static_assets_are_served(client):
    html = client.get("/").text
    assert "<title>" in html and "app.js" in html
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/style.css").status_code == 200


def test_dashboard_has_no_external_requests(client):
    """Offline-first and privacy-friendly: the UI must not pull scripts, fonts or trackers from the internet."""
    for f in ("/", "/static/app.js", "/static/style.css"):
        body = client.get(f).text
        assert "http://" not in body.replace("http://www.w3.org", "") and "https://" not in body, f


def test_dashboard_layout_sidebar_navigation_and_sections(client):
    """Sidebar navigation with one entry per section, a logo that returns to Overview, and a pane per section;
    every element the script looks up must exist."""
    html, js = client.get("/").text, client.get("/static/app.js").text
    for needle in ('id="side"', 'id="sideToggle"', 'id="nav"', 'id="kpis"', 'id="period"', 'id="liveCard"', 'id="pageTitle"'):
        assert needle in html, needle
    assert re.search(r'id="brand" href="#/overview"', html)  # logo + name go back to Overview
    tabs = re.findall(r'data-tab="(\w+)"', html)
    assert tabs == ["overview", "floor", "activity", "insights", "data"]
    assert re.search(r"const TABS = \[([^\]]*)\]", js).group(1).replace("'", "").replace(" ", "").split(",") == tabs
    for t in tabs:
        assert f'id="pane-{t}"' in html and f'href="#/{t}"' in html and f'id="nav-{t}"' in html
    # Overview = key numbers + live view only; the floor plan lives in its own section
    overview = html[html.index('id="pane-overview"'):html.index('id="pane-floor"')]
    assert 'id="kpis"' in overview and 'id="liveCard"' in overview and "<canvas" not in overview
    assert "<canvas" in html[html.index('id="pane-floor"'):html.index('id="pane-activity"')]
    ids = set(re.findall(r'id="([\w-]+)"', html))
    wanted = {m for m in re.findall(r"\$\('#([\w-]+)'\)", js) if not m.endswith("-")} - {"loadErr"}  # skip '#pane-' + name, and the error banner JS creates
    assert wanted <= ids, sorted(wanted - ids)
    assert "storelens.sidebar" in js and "hashchange" in js
    assert 'href="static/' in html or 'src="static/' in html  # relative URLs: also works when hosted under a sub-path


@pytest.fixture(scope="module")
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binaries not installed
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def test_dashboard_renders_every_section_without_errors(client, chromium, live_dir, tmp_path):
    """Open the standalone report in a real browser: each section fills in, the sidebar jumps between them, the logo
    returns to Overview, a display can be drilled into, the sidebar collapses and remembers it, and nothing throws."""
    from storelens.report import build_report
    out = build_report(client.db_path, client.cfg_path, str(tmp_path / "r.html"), str(live_dir))
    page = chromium.new_page(viewport={"width": 1280, "height": 860})
    problems = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.goto("file://" + out + "#/overview")
    page.wait_for_selector("#kpis .kpi")
    assert [k.inner_text().split("\n")[0] for k in page.query_selector_all("#kpis .kpi")] == [
        "Visitors", "Display impressions", "Visitors who engaged", "Median engaged time"]
    assert page.is_visible("#liveCard") and not page.is_visible("#pane-floor")
    for t, probe in (("floor", "#zoneTable tr.zrow"), ("activity", "#series svg"), ("insights", "#insights .ins"), ("data", "#quality .q-list li")):
        page.click(f"#nav-{t}")
        page.wait_for_selector(probe, state="attached")
        assert page.is_visible(f"#pane-{t}") and not page.is_visible("#pane-overview")
        assert page.get_attribute(f"#nav-{t}", "aria-current") == "page"
    page.click("#brand")  # logo + name -> Overview
    page.wait_for_function("location.hash === '#/overview'")
    assert page.is_visible("#pane-overview") and page.inner_text("#pageTitle") == "Overview"
    page.click("#nav-floor")
    page.click("#zoneTable tr.zrow >> nth=0")  # drill into a display
    assert page.inner_text("#zoneDetail h2") != "Whole store"
    page.click("#nav-activity")  # the focus carries over to Activity
    page.wait_for_function("document.getElementById('seriesZone').value !== ''")
    page.click("#sideToggle")
    assert page.evaluate("document.getElementById('app').classList.contains('collapsed')")
    assert page.is_visible("#nav-insights") and not page.is_visible("#nav-insights .nav-t")  # icon rail
    page.set_viewport_size({"width": 390, "height": 800})  # phone: sidebar becomes a drawer, closes after a pick
    page.click("#menuBtn")
    assert page.is_visible("#nav-insights .nav-t")
    page.click("#nav-insights")
    page.wait_for_function("location.hash === '#/insights'")
    page.wait_for_function("!document.getElementById('app').classList.contains('drawer-open')")
    page.wait_for_timeout(300)  # let the debounced resize redraw run
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.close()
    assert not problems, problems


def test_snapshot_keys_cover_what_the_ui_requests(client, cfg):
    snap = build_snapshot(client.db_path, cfg)
    for k in ("/api/config", "/api/runs", "/api/summary", "/api/zones?runs=1", "/api/heatmap?runs=2",
              "/api/compare?a=1&b=2", "/api/insights?a=1&b=2", "/api/timeseries?zone=endcap_b"):
        assert k in snap, k


# ----------------------------------------------------------------------------------- live view plumbing
@pytest.fixture
def live_dir(tmp_path):
    d = tmp_path / "live"
    d.mkdir()
    (d / "camera.mp4").write_bytes(bytes(range(256)) * 40)
    (d / "floorplan.mp4").write_bytes(bytes(range(256)) * 30)
    (d / "live.json").write_text(json.dumps({"camera": "camera.mp4", "floorplan": "floorplan.mp4", "seconds": 1.0,
                                             "fps": 20, "simulated": True, "note": "n"}))
    return d


def test_live_endpoint_is_empty_without_clips(client):
    assert client.get("/api/live").json() == {}


def test_live_endpoint_and_media_support_range_requests(client, live_dir):
    c = TestClient(create_app(client.db_path, client.cfg_path, str(live_dir)))
    info = c.get("/api/live").json()
    assert info["camera"] == "/media/camera.mp4" and info["floorplan"] == "/media/floorplan.mp4" and info["simulated"]
    r = c.get(info["camera"], headers={"Range": "bytes=0-99"})  # browsers need this to seek
    assert r.status_code == 206 and len(r.content) == 100


def test_report_embeds_the_clips_only_when_they_exist(client, live_dir, tmp_path):
    from storelens.report import build_report
    plain = build_report(client.db_path, client.cfg_path, str(tmp_path / "a.html"), None)
    withlive = build_report(client.db_path, client.cfg_path, str(tmp_path / "b.html"), str(live_dir))
    assert "data:video/mp4;base64," not in open(plain).read()
    html = open(withlive).read()
    assert html.count("data:video/mp4;base64,") == 2 and "/api/live" in html
