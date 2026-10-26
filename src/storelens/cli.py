"""storelens command line: sim, run, validate, fieldcheck, audit, calibrate, cameras, serve, report, site, demo."""
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
    from .sim.generate import generate_dataset, generate_empty
    if a.empty:  # footage of the empty store, for the false-alarm test
        video, gt, _ = generate_empty(a.out, minutes=a.minutes, seed=a.seed)
        print(json.dumps({"video": str(video), "gt": str(gt)}, indent=2))
        return 0
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
    from .geometry import draw_calibration_overlay
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
    from .cameras import calibration_report
    out = draw_calibration_overlay(frame, cfg, cam)
    cv2.imwrite(a.out, out)
    r = calibration_report(cfg, cam)
    loo = f", leave-one-out {r['loo_rmse_cm']:.1f} cm" if r["loo_rmse_cm"] is not None else ""
    print(f"calibration RMSE: {r['rmse_cm']:.1f} cm{loo} ({r['status']}) → overlay written to {a.out}")
    for w in r["warnings"]:
        print("  !", w)
    print("Check by eye: zone outlines must sit on the real fixtures; entrance line on the door.")
    return 0


def cmd_cameras(a) -> int:
    """Calibration quality per camera, coverage of the floor, and which camera owns which display."""
    from .cameras import CameraNetwork, calibration_report, render_coverage_png
    cfg = load_config(a.config)
    net = CameraNetwork(cfg)
    reports = [calibration_report(cfg, c) for c in cfg.cameras]
    for r in reports:
        loo = f"{r['loo_rmse_cm']:.1f} cm" if r["loo_rmse_cm"] is not None else "n/a (4 points)"
        print(f"{r['camera_id']}: {r['n_points']} points · reprojection RMSE {r['rmse_cm']:.1f} cm · leave-one-out {loo} · "
              f"{r['cm_per_px_centre']:.1f} cm/px · points cover {r['hull_fraction'] * 100:.0f}% of floor → {r['status']}")
        sp = r["suggested_detector_params"]
        have = next(c for c in cfg.cameras if c.id == r["camera_id"]).detector_params
        print(f"   suggested detector params for the background-subtraction detector: typical_area={sp['typical_area']}, "
              f"min_area={sp['min_area']}, max_area={sp['max_area']}"
              + (f"  (set: {have})" if have else "  (not set: the defaults are tuned for a 53 px/m overhead camera)"))
        for w in r["warnings"]:
            print("   !", w)
    cov = net.coverage()
    print(f"coverage: {cov['covered_fraction'] * 100:.0f}% of the floor seen, {cov['overlap_fraction'] * 100:.0f}% by "
          f"more than one camera, {cov['blind_fraction'] * 100:.0f}% blind")
    print("display owners:", ", ".join(f"{z}->{o}" for z, o in cov["zone_owner"].items()))
    for w in cov["warnings"]:
        print("   !", w)
    if a.png:
        print("coverage map:", render_coverage_png(cfg, net, a.png))
    if a.json:
        Path(a.json).write_text(json.dumps({"calibration": reports, "coverage": {k: v for k, v in cov.items()
                                                                                    if k != "owner_grid"}}, indent=2))
    return 0 if all(r["status"] != "poor" for r in reports) else 2


def cmd_fieldcheck(a) -> int:
    from .fieldcheck import format_fieldcheck, gates_digest, load_spec, run_fieldcheck, write_templates
    if a.init:
        made = write_templates(a.init)
        print(f"wrote {len(made)} template file(s) to {a.init}: {', '.join(made) or '(all existed already)'}")
        return 0
    if not (a.config and a.db and a.spec):
        print("fieldcheck needs --config, --db and --spec (or --init DIR to write blank templates)", file=sys.stderr)
        return 2
    from .store import EventStore
    cfg = load_config(a.config)
    spec = load_spec(a.spec)
    if a.gates_sha and a.gates_sha != gates_digest(spec["gates"]):
        print(f"gates changed since they were recorded: expected {a.gates_sha}, file gives {gates_digest(spec['gates'])}",
              file=sys.stderr)
        return 3
    r = run_fieldcheck(cfg, EventStore(a.db), spec)
    print(format_fieldcheck(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, default=float))
    return 0 if r["overall"] in ("PASS", "UNCERTAIN") else 1


def cmd_audit(a) -> int:
    from .audit import coarsen_audience, format_audit, run_audit, vacuum
    from .store import EventStore
    from .validate import load_gt
    cfg = load_config(a.config)
    gt = load_gt(a.gt) if a.gt else None
    now = _parse_start(a.as_of, cfg.output.timezone) if a.as_of else None
    r = run_audit(cfg, a.db, data_dir=a.dir or str(Path(a.db).resolve().parent), gt=gt, run_id=a.run,
                  coarsen_days=a.coarsen_days, suppress=not a.no_suppress, now=now)
    print(format_audit(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, default=float))
    if a.apply:
        if a.coarsen_days is None:
            print("--apply needs --coarsen-days", file=sys.stderr)
            return 2
        st = EventStore(a.db)
        s = coarsen_audience(st, cfg, a.coarsen_days, now=now, suppress_below=None if a.no_suppress else cfg.demographics.min_cell)
        vacuum(st)
        st.close()
        print(f"\napplied to {a.db}: {s['rows_before']} rows -> {s['rows_after']}; database vacuumed (irreversible)")
    return 1 if r["overall"] == "FAIL" else 0


def cmd_serve(a) -> int:
    import uvicorn
    from .server import create_app
    uvicorn.run(create_app(a.db, a.config, a.live), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_report(a) -> int:
    from .report import build_report
    print("wrote", build_report(a.db, a.config, a.out, a.live))
    return 0


def cmd_snapshot(a) -> int:
    from . import cloudsync
    cfg = load_config(a.config)
    env = cloudsync.make_envelope(a.db, cfg, a.store_id)
    body = cloudsync.encode(env)
    Path(a.out).write_bytes(body)
    print(f"wrote {a.out}: {len(body) / 1e3:.0f} KB, {len(env['snapshot'])} views, store '{a.store_id}', "
          f"generated {env['generated_at']}")
    return 0


def cmd_push(a) -> int:
    from . import cloudsync
    body = Path(a.snapshot).read_bytes()
    problems = cloudsync.scan_payload(json.loads(body))
    if problems or len(body) > cloudsync.MAX_BYTES:
        print("refusing to send: " + ("; ".join(problems[:5]) or "payload too large"), file=sys.stderr)
        return 2
    if a.dry_run:
        print(f"dry run: would POST {len(body) / 1e3:.0f} KB to {a.url} (payload check passed)")
        return 0
    try:
        r = cloudsync.push(a.url, body)
    except (RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"pushed: HTTP {r['status']} {json.dumps(r['body'])} (attempt {r['attempts']})")
    return 0


def cmd_site(a) -> int:
    from .site import build_site
    if not a.private and not (a.config and a.db):
        print("site needs --config and --db (or --private for the app shell of the cloud Worker)", file=sys.stderr)
        return 2
    r = build_site(a.db, a.config, a.out, a.live, demo_label=a.demo_label, cta_text=a.cta_text, cta_url=a.cta_url,
                   noindex=a.noindex, private=a.private)
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
    from .attributes import make_estimator
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
        # The demo's audience numbers come from a SIMULATED attribute model fed by the simulator's ground truth.
        est = make_estimator(cfg, gt_path=p["gt"], seed=a.seed)
        pipe = Pipeline(cfg, store, label=p["label"], attributes=est)
        meta = pipe.run(p["video"], _parse_start(starts[p["label"]], cfg.output.timezone),
                        preview=str(out / f"{p['label']}_preview.mp4") if a.preview else None, progress=False)
        r = validate(cfg, store, meta["run_id"], load_gt(p["gt"]), estimator=est)
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
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--empty", action="store_true", help="generate only a clip of the EMPTY store (no people, no staff)")
    p.set_defaults(fn=cmd_sim)

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

    p = sub.add_parser("fieldcheck", help="score the pipeline against hand tallies from the customer's own footage (CIs and gates)")
    p.add_argument("--config"); p.add_argument("--db"); p.add_argument("--spec", help="fieldcheck.yaml listing the label files and gates")
    p.add_argument("--json", help="write the full result here"); p.add_argument("--gates-sha", help="refuse to run if the gates differ from this recorded digest")
    p.add_argument("--init", metavar="DIR", help="write blank label templates and a spec into DIR and exit")
    p.set_defaults(fn=cmd_fieldcheck)

    p = sub.add_parser("audit", help="privacy audit: schema, files on disk, timestamp resolution and a linkage attack on the stored counts")
    p.add_argument("--config", required=True); p.add_argument("--db", required=True)
    p.add_argument("--dir", help="folder to scan for images/embeddings (default: the folder holding the database)")
    p.add_argument("--gt", help="simulator ground truth: also run the attack against the true attributes"); p.add_argument("--run", type=int)
    p.add_argument("--coarsen-days", type=float, help="what if audience rows older than this many days were merged into days?")
    p.add_argument("--as-of", help="treat this ISO time as 'now' for --coarsen-days (default: the real now)")
    p.add_argument("--no-suppress", action="store_true", help="do not fold small cells into 'unknown' in the what-if / apply")
    p.add_argument("--apply", action="store_true", help="really apply --coarsen-days to the database and VACUUM it (irreversible)")
    p.add_argument("--json"); p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("calibrate", help="overlay zones on a camera frame to eyeball the calibration")
    p.add_argument("--config", required=True); p.add_argument("--camera")
    p.add_argument("--video"); p.add_argument("--image"); p.add_argument("--frame", type=int)
    p.add_argument("--out", default="calibration_check.png"); p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("cameras", help="calibration quality, floor coverage and display ownership for every camera")
    p.add_argument("--config", required=True); p.add_argument("--png", help="write a coverage map picture here")
    p.add_argument("--json", help="write the numbers here"); p.set_defaults(fn=cmd_cameras)

    p = sub.add_parser("serve", help="start the dashboard"); p.add_argument("--config", required=True)
    p.add_argument("--db", required=True); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--live", help="folder with the simulated live-view clips (default: live/ next to the db)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("report", help="write a standalone HTML report"); p.add_argument("--config", required=True)
    p.add_argument("--db", required=True); p.add_argument("--out", default="report.html")
    p.add_argument("--live", help="folder with the simulated live-view clips (default: live/ next to the db)")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("site", help="write a static website folder (for Cloudflare Pages/Workers, GitHub Pages, any CDN)")
    p.add_argument("--config"); p.add_argument("--db"); p.add_argument("--out", default="site")
    p.add_argument("--private", action="store_true", help="app shell only (no data, no clips) for the private cloud Worker in cloud/")
    p.add_argument("--live", help="folder with the simulated live-view clips (default: live/ next to the db)")
    p.add_argument("--demo-label", help='show a chip such as "Demo · simulated data" in the header')
    p.add_argument("--cta-text", default=None, help='text of an optional header button (default "Request a pilot")')
    p.add_argument("--cta-url", help="link for the header button: https://... or mailto:... (e.g. a Google Form)")
    p.add_argument("--noindex", action="store_true", help="ask search engines not to index the site")
    p.set_defaults(fn=cmd_site)

    p = sub.add_parser("snapshot", help="write the dashboard's pre-computed views as one JSON file for the cloud app")
    p.add_argument("--config", required=True); p.add_argument("--db", required=True)
    p.add_argument("--store-id", required=True, help="must equal STORE_ID in the Worker"); p.add_argument("--out", default="snapshot.json")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("push", help="upload a snapshot to the cloud app (Access service token from CF_ACCESS_CLIENT_ID / _SECRET)")
    p.add_argument("--snapshot", default="snapshot.json"); p.add_argument("--url", required=True, help="https://<app>/ingest")
    p.add_argument("--dry-run", action="store_true", help="check the payload and show what would be sent"); p.set_defaults(fn=cmd_push)

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
