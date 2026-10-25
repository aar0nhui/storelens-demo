"""storelens command line: sim, run, validate, calibrate, serve, report, site, demo."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2

from .config import load_config


def _parse_start(s: str, tz: str) -> float:
    try:
        return float(s)
    except ValueError:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.timestamp()


def cmd_sim(a) -> int:
    from .sim.generate import generate_dataset
    res = generate_dataset(a.out, minutes=a.minutes, seed=a.seed)
    print(json.dumps(res, indent=2))
    return 0


def cmd_run(a) -> int:
    from .pipeline import Pipeline
    from .store import EventStore
    cfg = load_config(a.config)
    if a.detector:
        cfg.detector.kind = a.detector
        if a.model:
            cfg.detector.params["model_path"] = a.model
    if a.engagement_mode:
        cfg.thresholds.engagement_mode = a.engagement_mode
    store = EventStore(a.db)
    pipe = Pipeline(cfg, store, camera_id=a.camera, label=a.label or Path(a.video).stem)
    meta = pipe.run(a.video, _parse_start(a.start, cfg.output.timezone), preview=a.preview,
                    max_frames=a.max_frames, allow_raw_preview=a.allow_raw_preview)
    store.close()
    print(json.dumps({k: v for k, v in meta.items() if k != "privacy"}, indent=2))
    return 0


def cmd_validate(a) -> int:
    from .store import EventStore
    from .validate import format_report, load_gt, validate
    cfg = load_config(a.config)
    store = EventStore(a.db)
    r = validate(cfg, store, a.run, load_gt(a.gt))
    print(format_report(r, a.title or ""))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, default=float))
    return 0


def cmd_calibrate(a) -> int:
    from .geometry import Homography, draw_calibration_overlay
    cfg = load_config(a.config)
    cam = cfg.camera(a.camera)
    if a.video:
        cap = cv2.VideoCapture(a.video)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.set(cv2.CAP_PROP_POS_FRAMES, a.frame if a.frame is not None else n // 2)
        ok, frame = cap.read()
        if not ok:
            print("could not read frame", file=sys.stderr)
            return 1
    else:
        frame = cv2.imread(a.image)
    out = draw_calibration_overlay(frame, cfg, cam)
    cv2.imwrite(a.out, out)
    print(f"calibration RMSE: {Homography.from_camera(cam).reprojection_rmse_cm():.1f} cm → overlay written to {a.out}")
    print("Check by eye: zone outlines must sit on the real fixtures; entrance line on the door.")
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    from .server import create_app
    uvicorn.run(create_app(a.db, a.config, a.live), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_report(a) -> int:
    from .report import build_report
    print("wrote", build_report(a.db, a.config, a.out, a.live))
    return 0


def cmd_site(a) -> int:
    from .site import build_site
    r = build_site(a.db, a.config, a.out, a.live, demo_label=a.demo_label, cta_text=a.cta_text, cta_url=a.cta_url,
                   noindex=a.noindex)
    print(f"wrote {r['files']} files ({r['bytes'] / 1e6:.1f} MB) to {r['out']}; largest: {r['largest'][1]} ({r['largest'][0] / 1e6:.1f} MB)")
    return 0


def cmd_live(a) -> int:
    from .live import make_live_clip
    info = make_live_clip(a.out, seconds=a.seconds, seed=a.seed)
    print(json.dumps({k: v for k, v in info.items() if k != "note"}, indent=2))
    return 0


def cmd_demo(a) -> int:
    """sim -> run (both periods) -> validate -> report. Everything under --out."""
    from .pipeline import Pipeline
    from .report import build_report
    from .sim.generate import generate_dataset
    from .store import EventStore
    from .validate import format_report, load_gt, validate
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ds = generate_dataset(out, minutes=a.minutes, seed=a.seed)
    if not a.no_live:
        from .live import make_live_clip
        make_live_clip(out / "live", seconds=60, seed=a.seed)
    cfg = load_config(ds["config"])
    db = out / "demo.db"
    if db.exists():
        db.unlink()
    store = EventStore(db)
    starts = {"baseline": "2026-11-02T10:00", "after_change": "2026-11-09T10:00"}
    reports = {}
    for p in ds["periods"]:
        print(f"processing {p['label']} ...")
        pipe = Pipeline(cfg, store, label=p["label"])
        meta = pipe.run(p["video"], _parse_start(starts[p["label"]], cfg.output.timezone),
                        preview=str(out / f"{p['label']}_preview.mp4") if a.preview else None, progress=False)
        r = validate(cfg, store, meta["run_id"], load_gt(p["gt"]))
        reports[p["label"]] = r
        print(format_report(r, p["label"]))
    (out / "validation.json").write_text(json.dumps(reports, indent=2, default=float))
    store.close()
    rep = build_report(str(db), ds["config"], str(out / "report.html"))
    print(f"\nreport: {rep}\nlive dashboard: storelens serve --config {ds['config']} --db {db}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="storelens", description="Privacy-first retail analytics on existing CCTV")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sim", help="generate a synthetic store dataset with ground truth")
    p.add_argument("--out", default="sim_data"); p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--seed", type=int, default=11); p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("run", help="process a video into events")
    p.add_argument("--config", required=True); p.add_argument("--video", required=True)
    p.add_argument("--start", required=True, help="wall-clock start of the video, ISO (store tz) or epoch")
    p.add_argument("--db", default="storelens.db"); p.add_argument("--label"); p.add_argument("--camera")
    p.add_argument("--preview", help="write an anonymised (obscured) preview video here")
    p.add_argument("--allow-raw-preview", action="store_true", help="only needed when privacy.blur is 'none'")
    p.add_argument("--detector", choices=["bgsub", "yolox_onnx", "rfdetr"]); p.add_argument("--model")
    p.add_argument("--engagement-mode", choices=["stop", "stop_facing"]); p.add_argument("--max-frames", type=int)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("validate", help="compare a run against simulator ground truth")
    p.add_argument("--config", required=True); p.add_argument("--db", required=True); p.add_argument("--run", type=int, required=True)
    p.add_argument("--gt", required=True); p.add_argument("--title"); p.add_argument("--json"); p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("calibrate", help="overlay zones on a camera frame to eyeball the calibration")
    p.add_argument("--config", required=True); p.add_argument("--camera")
    p.add_argument("--video"); p.add_argument("--image"); p.add_argument("--frame", type=int)
    p.add_argument("--out", default="calibration_check.png"); p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("serve", help="start the dashboard"); p.add_argument("--config", required=True)
    p.add_argument("--db", required=True); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--live", help="folder with the simulated live-view clips (default: live/ next to the db)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("report", help="write a standalone HTML report"); p.add_argument("--config", required=True)
    p.add_argument("--db", required=True); p.add_argument("--out", default="report.html")
    p.add_argument("--live", help="folder with the simulated live-view clips (default: live/ next to the db)")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("site", help="write a static website folder (for Cloudflare Pages/Workers, GitHub Pages, any CDN)")
    p.add_argument("--config", required=True); p.add_argument("--db", required=True); p.add_argument("--out", default="site")
    p.add_argument("--live", help="folder with the simulated live-view clips (default: live/ next to the db)")
    p.add_argument("--demo-label", help='show a chip such as "Demo · simulated data" in the header')
    p.add_argument("--cta-text", default=None, help='text of an optional header button (default "Request a pilot")')
    p.add_argument("--cta-url", help="link for the header button: https://... or mailto:... (e.g. a Google Form)")
    p.add_argument("--noindex", action="store_true", help="ask search engines not to index the site")
    p.set_defaults(fn=cmd_site)

    p = sub.add_parser("live", help="simulate a live camera feed + live floor plan (webcam view and floor plan, in sync)")
    p.add_argument("--out", default="live"); p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--seed", type=int, default=11); p.set_defaults(fn=cmd_live)

    p = sub.add_parser("demo", help="simulate, process, validate and report in one go")
    p.add_argument("--out", default="demo_out"); p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--seed", type=int, default=11); p.add_argument("--preview", action="store_true")
    p.add_argument("--no-live", action="store_true", help="skip the simulated live-view clips"); p.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
