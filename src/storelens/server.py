"""FastAPI backend + static dashboard. Read-only over the event store."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import StoreConfig, load_config
from .insights import generate_insights
from .queries import Analytics, Window
from .store import EventStore

WEB_DIR = Path(__file__).parent / "web"


def _parse_runs(runs: str | None) -> list[int] | None:
    if not runs:
        return None
    try:
        return [int(x) for x in runs.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "runs must be a comma-separated list of integers")


def _parse_t(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            raise HTTPException(400, f"bad timestamp {v!r}")


def find_live(db_path: str, live_dir: str | None = None) -> Path | None:
    """The simulated live-view clips (``storelens live``): an explicit dir, else ``live/`` next to the database."""
    d = Path(live_dir) if live_dir else Path(db_path).resolve().parent / "live"
    return d if (d / "live.json").exists() and (d / "camera.mp4").exists() and (d / "floorplan.mp4").exists() else None


def live_info(live: Path | None, url_prefix: str = "/media/") -> dict:
    if live is None:
        return {}
    info = json.loads((live / "live.json").read_text())
    info["camera"], info["floorplan"] = url_prefix + info["camera"], url_prefix + info["floorplan"]
    return info


def build_snapshot(db_path: str, cfg: StoreConfig, live: Path | None = None) -> dict:
    """Pre-computed answers to every default dashboard query, keyed by URL (used by the static report)."""
    store = EventStore(db_path)
    an = Analytics(store, cfg)
    runs = an.runs()
    snap: dict = {"/api/config": an.config_view(), "/api/runs": runs}
    if live is not None:  # embed the clips so the report stays a single self-contained file
        info = json.loads((live / "live.json").read_text())
        for key in ("camera", "floorplan"):
            raw = (live / info[key]).read_bytes()
            info[key] = "data:video/mp4;base64," + base64.b64encode(raw).decode()
        snap["/api/live"] = info
    selections: list[tuple[str, Window]] = [("", Window())] + [(f"?runs={r['run_id']}", Window([r["run_id"]])) for r in runs]
    for suffix, w in selections:
        sep = "&" if suffix else "?"
        zones = an.zones(w)
        flows = an.flows(w)
        snap["/api/summary" + suffix] = an.summary(w)
        snap["/api/zones" + suffix] = zones
        snap["/api/heatmap" + suffix] = an.heatmap(w)
        snap["/api/timeseries" + suffix] = an.timeseries(w)
        for z in cfg.measured_zones():
            snap[f"/api/timeseries{suffix}{sep}zone={z.id}"] = an.timeseries(w, z.id)
        snap["/api/flows" + suffix] = flows
        snap["/api/insights" + suffix] = generate_insights(zones, flows)
        snap["/api/quality" + suffix] = an.quality(w)
    if len(runs) >= 2:
        a, b = runs[0]["run_id"], runs[1]["run_id"]
        cmp_ = an.lift(Window([a]), Window([b]))
        snap[f"/api/compare?a={a}&b={b}"] = cmp_
        zones_b = an.zones(Window([b]))
        snap[f"/api/insights?a={a}&b={b}"] = generate_insights(zones_b, an.flows(Window([b])), cmp_)
    store.close()
    return snap


def create_app(db_path: str, config_path: str, live_dir: str | None = None) -> FastAPI:
    cfg = load_config(config_path)
    app = FastAPI(title="StoreLens", version="0.1.0")
    live = find_live(db_path, live_dir)

    def analytics():
        store = EventStore(db_path)
        return store, Analytics(store, cfg)

    def window(runs, t0, t1) -> Window:
        return Window(_parse_runs(runs), _parse_t(t0), _parse_t(t1))

    @app.get("/api/config")
    def config():
        s, a = analytics()
        try:
            return a.config_view()
        finally:
            s.close()

    @app.get("/api/runs")
    def runs():
        s, a = analytics()
        try:
            return a.runs()
        finally:
            s.close()

    @app.get("/api/summary")
    def summary(runs: str | None = None, t0: str | None = None, t1: str | None = None):
        s, a = analytics()
        try:
            return a.summary(window(runs, t0, t1))
        finally:
            s.close()

    @app.get("/api/zones")
    def zones(runs: str | None = None, t0: str | None = None, t1: str | None = None):
        s, a = analytics()
        try:
            return a.zones(window(runs, t0, t1))
        finally:
            s.close()

    @app.get("/api/heatmap")
    def heatmap(runs: str | None = None, t0: str | None = None, t1: str | None = None):
        s, a = analytics()
        try:
            return a.heatmap(window(runs, t0, t1))
        finally:
            s.close()

    @app.get("/api/timeseries")
    def timeseries(runs: str | None = None, zone: str | None = None, t0: str | None = None, t1: str | None = None):
        s, a = analytics()
        try:
            return a.timeseries(window(runs, t0, t1), zone)
        finally:
            s.close()

    @app.get("/api/flows")
    def flows(runs: str | None = None, t0: str | None = None, t1: str | None = None):
        s, a = analytics()
        try:
            return a.flows(window(runs, t0, t1))
        finally:
            s.close()

    @app.get("/api/insights")
    def insights(runs: str | None = None, a: int | None = None, b: int | None = None):
        s, an = analytics()
        try:
            lift = None
            if a is not None and b is not None:
                lift = an.lift(Window([a]), Window([b]))
                w = Window([b])
            else:
                w = Window(_parse_runs(runs))
            return generate_insights(an.zones(w), an.flows(w), lift)
        finally:
            s.close()

    @app.get("/api/compare")
    def compare(a: int, b: int, zones: str | None = None, controls: str | None = None):
        s, an = analytics()
        try:
            return an.lift(Window([a]), Window([b]), zones.split(",") if zones else None,
                           controls.split(",") if controls else None)
        finally:
            s.close()

    @app.get("/api/quality")
    def quality(runs: str | None = None):
        s, a = analytics()
        try:
            return a.quality(Window(_parse_runs(runs)))
        finally:
            s.close()

    @app.get("/api/live")
    def live_view():
        """Simulated live-view clips (empty object when none were generated)."""
        return live_info(live)

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    if live is not None:
        app.mount("/media", StaticFiles(directory=live), name="media")
    return app
