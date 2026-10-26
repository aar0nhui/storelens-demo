"""Privacy audit: can anything we STORE be used to remember or single out a person?

Four checks, none of which needs the camera:

  schema     the database holds only the documented tables and columns, no BLOBs, nothing named like an
             embedding / face / image / trajectory, and the audience table has no visit key
  files      the data folder holds no still images, embeddings or numeric-vector dumps
  time       audience timestamps are no finer than the configured bucket
  linkage    an attacker who knows when someone entered and which displays they visited (a staff member, a neighbour,
             a stalker) reads the stored audience counts. How much better than the store-wide prior can they guess that
             person's age band / gender? Computed from the stored counts alone ("exposure") and, on simulator data,
             against the true attributes ("realised").

The linkage numbers are the point: small cells leak. Mitigations that can be applied after the fact are
``coarsen_audience`` (merge old buckets into days, fold small cells into 'unknown') and a VACUUM so deleted fine-grained
rows do not linger in the file. We measure the effect instead of asserting it.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from .attributes import UNKNOWN, bucket_start
from .audience import wilson
from .config import StoreConfig
from .store import EventStore

ALLOWED = {
    "runs": {"run_id", "label", "camera_id", "source", "t_start", "t_end", "created_at", "meta"},
    "visits": {"run_id", "visit_key", "role", "t_first", "t_last", "duration_s"},
    "zone_visits": {"run_id", "visit_key", "zone_id", "t_enter", "t_exit", "dwell_s", "stopped_s", "facing_stopped_s"},
    "entries": {"run_id", "visit_key", "t", "direction"},
    "heat": {"run_id", "bucket_ts", "gx", "gy", "seconds"},
    "audience": {"run_id", "bucket_ts", "zone_id", "metric", "age_band", "gender", "n"},
    "sqlite_sequence": {"name", "seq"},
}
SUSPICIOUS = re.compile(r"embed|face|image|img|frame|traj|feature|vector|thumb|crop|landmark|descriptor|biometric|\bxy\b|^[xy]$", re.I)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".heic"}
VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
EMBED_EXT = {".npy", ".npz", ".pkl", ".pickle", ".pt", ".pth", ".faiss", ".index", ".h5", ".hdf5", ".feather", ".parquet", ".safetensors"}
TEXT_EXT = {".log", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml", ".md"}
SKIP_DIRS = {".git", "node_modules", ".wrangler", "__pycache__", ".venv", "venv"}
FLOAT_RUN = re.compile(r"(?:-?\d+\.\d{3,}[\s,\[\]]+){63,}")  # 64+ long decimals in a row = a vector, not an event


# ------------------------------------------------------------------------------------------------ structural checks
def schema_scan(store: EventStore) -> dict:
    tables = {r[0] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    problems: list[str] = []
    for t in sorted(tables):
        cols = store.query(f"PRAGMA table_info({t})")
        names = {c[1] for c in cols}
        if t not in ALLOWED:
            problems.append(f"unexpected table '{t}'")
            continue
        for extra in sorted(names - ALLOWED[t]):
            problems.append(f"unexpected column {t}.{extra}")
        for c in cols:
            if (c[2] or "").upper() == "BLOB":
                problems.append(f"BLOB column {t}.{c[1]}")
            if SUSPICIOUS.search(c[1]) and c[1] not in ALLOWED.get(t, set()):
                problems.append(f"suspicious column name {t}.{c[1]}")
        n_blob = 0
        for c in cols:
            n_blob += store.query(f"SELECT COUNT(*) FROM {t} WHERE typeof({c[1]})='blob'")[0][0]
        if n_blob:
            problems.append(f"{n_blob} BLOB values in {t}")
    aud = {c[1] for c in store.query("PRAGMA table_info(audience)")}
    heat = {c[1] for c in store.query("PRAGMA table_info(heat)")}
    if "visit_key" in aud or "visit_key" in heat:
        problems.append("audience/heat carry a visit key (they must not: it would join demographics to a visit)")
    return {"name": "schema", "status": "FAIL" if problems else "PASS", "problems": problems,
            "summary": "only the documented tables and columns, no BLOBs, no visit key on the aggregate tables"
            if not problems else "; ".join(problems[:4])}


def file_scan(root: str | Path, max_text_mb: float = 8.0) -> dict:
    root = Path(root)
    images, embeds, videos, vectors = [], [], [], []
    n = 0
    for p in root.rglob("*"):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        n += 1
        ext = p.suffix.lower()
        rel = str(p.relative_to(root))
        if ext in IMAGE_EXT:
            images.append(rel)
        elif ext in EMBED_EXT:
            embeds.append(rel)
        elif ext in VIDEO_EXT:
            videos.append(rel)
        elif ext in TEXT_EXT and p.stat().st_size <= max_text_mb * 1e6:
            try:
                if FLOAT_RUN.search(p.read_text(errors="ignore")):
                    vectors.append(rel)
            except OSError:
                pass
    fail = images + embeds + vectors
    status = "FAIL" if fail else ("WARN" if videos else "PASS")
    parts = []
    if images:
        parts.append(f"{len(images)} still image(s): {images[:3]}")
    if embeds:
        parts.append(f"{len(embeds)} array/pickle/model-state file(s): {embeds[:3]}")
    if vectors:
        parts.append(f"{len(vectors)} text file(s) with long numeric vectors: {vectors[:3]}")
    if videos:
        parts.append(f"{len(videos)} video file(s) to confirm are obscured previews or simulator clips: {videos[:3]}")
    return {"name": "files", "status": status, "scanned": n, "images": images, "embeddings": embeds,
            "numeric_vectors": vectors, "videos": videos,
            "summary": f"{n} files scanned; " + ("; ".join(parts) if parts else "no images, embeddings or vector dumps")}


def time_check(store: EventStore, cfg: StoreConfig) -> dict:
    tz = ZoneInfo(cfg.output.timezone)
    ok_hours = {cfg.demographics.bucket_hours, 24}
    bad = [b for (b,) in store.query("SELECT DISTINCT bucket_ts FROM audience")
           if not any(abs(bucket_start(b, h, tz) - b) < 1e-6 for h in ok_hours)]
    return {"name": "time_resolution", "status": "FAIL" if bad else "PASS", "misaligned_buckets": len(bad),
            "summary": f"all audience timestamps are aligned to {cfg.demographics.bucket_hours}-hour (or daily) buckets"
            if not bad else f"{len(bad)} audience timestamps finer than the bucket"}


# ------------------------------------------------------------------------------------------------ exposure (stored data only)
def _cells(store: EventStore, run_ids: list[int] | None) -> dict:
    q = "SELECT bucket_ts,zone_id,metric,age_band,gender,SUM(n) FROM audience"
    args: tuple = ()
    if run_ids:
        q += f" WHERE run_id IN ({','.join('?' * len(run_ids))})"
        args = tuple(run_ids)
    cells: dict[tuple, dict[tuple[str, str], int]] = {}
    for b, z, m, a, g, n in store.query(q + " GROUP BY 1,2,3,4,5", args):
        cells.setdefault((b, z, m), {})[(a, g)] = int(n)
    return cells


def _mode(counts: dict[str, int]) -> tuple[str | None, int, int]:
    known = {k: v for k, v in counts.items() if k != UNKNOWN and v > 0}
    if not known:
        return None, 0, 0
    best = max(known, key=known.get)
    return best, known[best], sum(known.values())


def store_exposure(store: EventStore, cfg: StoreConfig, run_ids: list[int] | None = None, k: int | None = None) -> dict:
    """What the stored counts alone give away. For each cell (time bucket x display x metric) an attacker who knows a
    person is in that cell guesses its most common band. ``expected_accuracy`` is how often that is right IF the person
    is typical of the cell; ``baseline`` is the store-wide most common band. The gap is what the extra detail leaks."""
    k = k or cfg.demographics.min_cell
    cells = _cells(store, run_ids)
    out: dict = {"k": k, "metrics": {}}
    for metric in ("visitor", "impression", "engaged"):
        sel = {key: v for key, v in cells.items() if key[2] == metric}
        if not sel:
            continue
        tot_age: dict[str, int] = {}
        tot_gen: dict[str, int] = {}
        people = small_people = small_cells = 0
        hit_a = hit_g = known_a = known_g = 0
        sizes = []
        for key, cell in sel.items():
            n = sum(cell.values())
            informative = sum(c for (a, g), c in cell.items() if (a, g) != (UNKNOWN, UNKNOWN))  # fully-unknown people reveal nothing
            people += n
            sizes.append(n)
            if 0 < informative < k:
                small_people += informative
                small_cells += 1
            ages: dict[str, int] = {}
            gens: dict[str, int] = {}
            for (a, g), c in cell.items():
                ages[a] = ages.get(a, 0) + c
                gens[g] = gens.get(g, 0) + c
                tot_age[a] = tot_age.get(a, 0) + c
                tot_gen[g] = tot_gen.get(g, 0) + c
            _, h, t = _mode(ages)
            hit_a, known_a = hit_a + h, known_a + t
            _, h, t = _mode(gens)
            hit_g, known_g = hit_g + h, known_g + t
        _, ba, ta = _mode(tot_age)
        _, bg, tg = _mode(tot_gen)
        out["metrics"][metric] = {
            "cells": len(sel), "people": people, "median_cell_size": float(np.median(sizes)), "smallest_cell": int(min(sizes)),
            "cells_below_k": small_cells, "people_in_cells_below_k": small_people,
            "share_in_cells_below_k": round(small_people / people, 4) if people else 0.0,  # people with a known attribute
            "age_expected_accuracy": round(hit_a / known_a, 4) if known_a else None,
            "age_baseline": round(ba / ta, 4) if ta else None,
            "gender_expected_accuracy": round(hit_g / known_g, 4) if known_g else None,
            "gender_baseline": round(bg / tg, 4) if tg else None}
    return out


# ------------------------------------------------------------------------------------------------ realised attack (simulator)
def attack_with_truth(cfg: StoreConfig, store: EventStore, run_id: int, gt: dict, run_ids: list[int] | None = None) -> dict:
    """Side-knowledge attack scored against the simulator's true attributes. The attacker knows when a person entered and
    which displays they visited, and reads the stored counts for the most specific cell that contains them."""
    from .validate import gt_events
    tz = ZoneInfo(cfg.output.timezone)
    t0 = float(store.query("SELECT t_start FROM runs WHERE run_id=?", (run_id,))[0][0])
    cells = _cells(store, run_ids or [run_id])
    hours = (cfg.demographics.bucket_hours, 24)
    tot_age: dict[str, int] = {}
    tot_gen: dict[str, int] = {}
    for (_, z, m), cell in cells.items():
        if m == "visitor":
            for (a, g), c in cell.items():
                tot_age[a] = tot_age.get(a, 0) + c
                tot_gen[g] = tot_gen.get(g, 0) + c
    prior_a, prior_g = _mode(tot_age)[0], _mode(tot_gen)[0]
    pooled: dict[tuple, dict[tuple[str, str], int]] = {}  # (display, metric) summed over ALL time buckets
    for (_, z, m), cell in cells.items():
        for ag, c in cell.items():
            pooled.setdefault((z, m), {})
            pooled[(z, m)][ag] = pooled[(z, m)].get(ag, 0) + c
    n_buckets = len({b for (b, _, m) in cells if m == "visitor"})
    attrs = {p["id"]: (p.get("age_band"), p.get("gender")) for p in gt["people"] if p["role"] == "customer"}
    fixtures = {z.id for z in cfg.zones if z.kind == "fixture"}
    rows = []
    for p in gt_events(gt, cfg, t0):
        if p["role"] != "customer" or p["a"].dropped is not None or p["id"] not in attrs or not attrs[p["id"]][0]:
            continue
        a = p["a"]
        ent = [t for t, d in a.entries if d > 0]
        t = min(ent) if ent else (min(z.t_enter for z in a.zone_visits) if a.zone_visits else None)
        if t is None:
            continue
        cand = []
        for h in hours:
            b = bucket_start(t, h, tz)
            for key in [(b, "", "visitor")] + [(b, z.zone_id, "impression") for z in a.zone_visits if z.zone_id in fixtures]:
                if key in cells:
                    cand.append((sum(cells[key].values()), key))
            if cand:
                break  # finest resolution that exists
        if not cand:
            continue
        size, key = min(cand)
        cell = cells[key]
        ages: dict[str, int] = {}
        gens: dict[str, int] = {}
        for (aa, gg), c in cell.items():
            ages[aa] = ages.get(aa, 0) + c
            gens[gg] = gens.get(gg, 0) + c
        ga, gg = _mode(ages)[0], _mode(gens)[0]
        pa, pg = {}, {}
        for (aa, gg2), c in pooled[(key[1], key[2])].items():
            pa[aa] = pa.get(aa, 0) + c
            pg[gg2] = pg.get(gg2, 0) + c
        ta, tg = attrs[p["id"]]
        rows.append({"size": size, "age": ga == ta, "gender": gg == tg, "prior_age": prior_a == ta,
                     "prior_gender": prior_g == tg, "pooled_age": _mode(pa)[0] == ta, "pooled_gender": _mode(pg)[0] == tg})
    n = len(rows)
    out = {"name": "linkage_attack", "n": n, "bucket_hours": cfg.demographics.bucket_hours}
    if n == 0:
        return {**out, "status": "INSUFFICIENT", "summary": "no audience data or no ground truth to attack"}

    def acc(sel, key):
        return sum(r[key] for r in sel) / len(sel) if sel else float("nan")

    k = cfg.demographics.min_cell
    small = [r for r in rows if r["size"] < k]
    res = {}
    for dim, ok, prior, pool in (("age", "age", "prior_age", "pooled_age"), ("gender", "gender", "prior_gender", "pooled_gender")):
        hit, base, pl = sum(r[ok] for r in rows), sum(r[prior] for r in rows), sum(r[pool] for r in rows)
        lo, hi = wilson(hit, n)
        res[dim] = {"attack": round(hit / n, 4), "attack_ci": [round(lo, 4), round(hi, 4)], "prior": round(base / n, 4),
                    "lift_points": round(100 * (hit - base) / n, 2),
                    "pooled_prior": round(pl / n, 4), "lift_over_pooled_points": round(100 * (hit - pl) / n, 2),
                    "in_small_cells": {"n": len(small), "attack": round(acc(small, ok), 4) if small else None,
                                       "prior": round(acc(small, prior), 4) if small else None}}
    # Two different things: (1) knowing which display someone visited tells you about them because audiences differ by display.
    # That is the product's purpose and exists at any aggregation level. (2) What the TIME bucket adds on top of the
    # display's all-time audience is the leak that coarser buckets can remove; it can only be measured with several buckets.
    lift_time = max(res["age"]["lift_over_pooled_points"], res["gender"]["lift_over_pooled_points"])
    testable = n_buckets >= 4
    status = "PASS" if testable and lift_time <= 5.0 and len(small) / n <= 0.05 else "WARN"
    return {**out, "status": status, **res, "people_in_cells_below_k": len(small), "time_buckets": n_buckets,
            "time_leak_testable": testable,
            "summary": f"attacker knowing entry time + displays visited, {n} people: age {res['age']['attack']:.0%} (store prior "
                       f"{res['age']['prior']:.0%}, display's all-time audience {res['age']['pooled_prior']:.0%}), gender "
                       f"{res['gender']['attack']:.0%} (prior {res['gender']['prior']:.0%}, display {res['gender']['pooled_prior']:.0%}); "
                       f"{len(small)} people in cells below k={k}; "
                       + (f"time buckets add {lift_time:+.1f} pts" if testable else
                          f"only {n_buckets} time bucket(s) in the data, so what the time resolution leaks cannot be measured yet")}


# ------------------------------------------------------------------------------------------------ mitigation
def coarsen_audience(store: EventStore, cfg: StoreConfig, older_than_days: float, now: float | None = None,
                     to_hours: int = 24, suppress_below: int | None = None) -> dict:
    """Merge audience rows older than ``older_than_days`` into ``to_hours`` buckets and fold cells smaller than
    ``suppress_below`` into (unknown, unknown) so totals still reconcile. Irreversible: call VACUUM afterwards
    (``vacuum=True`` in ``apply``) so the old fine-grained rows do not remain in free pages of the file."""
    tz = ZoneInfo(cfg.output.timezone)
    now = time.time() if now is None else now
    cutoff = now - older_than_days * 86400
    old = store.query("SELECT run_id,bucket_ts,zone_id,metric,age_band,gender,n FROM audience WHERE bucket_ts < ?", (cutoff,))
    merged: dict[tuple, int] = {}
    for rid, b, z, m, a, g, n in old:
        key = (rid, bucket_start(b, to_hours, tz), z, m, a, g)
        merged[key] = merged.get(key, 0) + n
    suppressed_cells = suppressed_people = 0
    if suppress_below:
        totals: dict[tuple, int] = {}
        for (rid, b, z, m, a, g), n in merged.items():
            totals[(rid, b, z, m)] = totals.get((rid, b, z, m), 0) + n
        folded: dict[tuple, int] = {}
        for (rid, b, z, m, a, g), n in merged.items():
            if totals[(rid, b, z, m)] < suppress_below and (a, g) != (UNKNOWN, UNKNOWN):
                key = (rid, b, z, m, UNKNOWN, UNKNOWN)
                suppressed_people += n
            else:
                key = (rid, b, z, m, a, g)
            folded[key] = folded.get(key, 0) + n
        suppressed_cells = sum(1 for v in totals.values() if v < suppress_below)
        merged = folded
    store.conn.execute("DELETE FROM audience WHERE bucket_ts < ?", (cutoff,))
    store.conn.executemany(
        "INSERT INTO audience VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id, bucket_ts, zone_id, metric, age_band, gender) "
        "DO UPDATE SET n = n + excluded.n", [(*k, v) for k, v in merged.items()])
    store.conn.commit()
    return {"rows_before": len(old), "rows_after": len(merged), "cutoff_iso": time.strftime("%Y-%m-%d", time.localtime(cutoff)),
            "to_hours": to_hours, "suppressed_cells": suppressed_cells, "suppressed_people": suppressed_people}


def vacuum(store: EventStore) -> None:
    store.conn.commit()
    store.conn.execute("VACUUM")


# ------------------------------------------------------------------------------------------------ driver
def run_audit(cfg: StoreConfig, db_path: str | Path, data_dir: str | Path | None = None, gt: dict | None = None,
              run_id: int | None = None, coarsen_days: float | None = None, suppress: bool = True,
              now: float | None = None) -> dict:
    store = EventStore(db_path)
    checks = [schema_scan(store), time_check(store, cfg)]
    if data_dir:
        checks.append(file_scan(data_dir))
    result: dict = {"checks": checks, "exposure": None, "attack": None, "what_if": None}
    if cfg.demographics.enabled:
        result["exposure"] = store_exposure(store, cfg)
        if gt is not None and run_id is not None:
            result["attack"] = attack_with_truth(cfg, store, run_id, gt)
            checks.append(result["attack"])
        if coarsen_days is not None:
            tmp = Path(tempfile.mkdtemp(prefix="storelens-audit-"))
            try:
                store.conn.commit()
                shutil.copy(str(db_path), tmp / "copy.db")
                twin = EventStore(tmp / "copy.db")
                stats = coarsen_audience(twin, cfg, coarsen_days, now=now, suppress_below=cfg.demographics.min_cell if suppress else None)
                result["what_if"] = {"coarsen_days": coarsen_days, "suppress_below": cfg.demographics.min_cell if suppress else None,
                                     **stats, "exposure_after": store_exposure(twin, cfg)}
                twin.close()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    store.close()
    bad = [c for c in checks if c["status"] == "FAIL"]
    warn = [c for c in checks if c["status"] in ("WARN", "INSUFFICIENT")]
    result["overall"] = "FAIL" if bad else ("WARN" if warn else "PASS")
    result["notes"] = [
        "Exposure and the realised attack use only the stored aggregate counts plus side knowledge of ONE person's entry "
        "time and displays visited. They do not cover the camera, the edge box or backups that hold more.",
        "Cross-day linking is structurally impossible here because visit keys use a salt that exists only in memory and "
        "rotates; a staged two-day volunteer test is still the proof to show a customer.",
        "Rows deleted or coarsened stay in free pages of the SQLite file until VACUUM runs."]
    return result


def format_audit(r: dict) -> str:
    L = ["=== Privacy audit ==="]
    for c in r["checks"]:
        L.append(f"[{c['status']:<12}] {c['name']}: {c['summary']}")
    ex = r.get("exposure")
    if ex:
        L.append("")
        L.append(f"Exposure of the stored audience counts (k={ex['k']}; accuracy = how often 'most common band in the cell' is right):")
        for m, v in ex["metrics"].items():
            L.append(f"  {m:<10} cells {v['cells']:>4}  median size {v['median_cell_size']:>5.0f}  smallest {v['smallest_cell']:>3}  "
                     f"people in cells <k {v['share_in_cells_below_k']:>5.1%}  age {v['age_expected_accuracy']:.0%} vs baseline "
                     f"{v['age_baseline']:.0%}  gender {v['gender_expected_accuracy']:.0%} vs {v['gender_baseline']:.0%}")
    w = r.get("what_if")
    if w:
        L.append("")
        L.append(f"What-if: merge rows older than {w['coarsen_days']:g} days into {w['to_hours']}-hour buckets"
                 + (f" and fold cells below {w['suppress_below']} into 'unknown'" if w["suppress_below"] else "")
                 + f": {w['rows_before']} rows -> {w['rows_after']}, {w['suppressed_people']} people folded into unknown")
        for m, v in w["exposure_after"]["metrics"].items():
            L.append(f"  {m:<10} cells {v['cells']:>4}  people in cells <k {v['share_in_cells_below_k']:>5.1%}  "
                     f"age {v['age_expected_accuracy']:.0%} vs baseline {v['age_baseline']:.0%}")
    L.append("")
    L.append(f"OVERALL: {r['overall']}")
    L.extend("  note: " + n for n in r["notes"])
    return "\n".join(L)
