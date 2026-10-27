"""Layout and marketing recommendations with transparent what-if estimates.

What this is, and is not
------------------------
Every recommendation is a *scenario*: "if display X reached as many visitors as the typical display, you would get about
N more engaged shoppers a day". The measured part is the current reach / engagement rate per display (with sample
sizes). The target is a stated benchmark (the store's own median display), not a forecast of what a change will do. The
money part multiplies extra engaged shoppers by TWO ASSUMPTIONS that StoreLens does not measure: the share of engaged
shoppers who buy (a low-high range) and what they spend. Those come from ``business:`` in the config and are shown, and
editable, next to every number. Footfall effects of marketing are sensitivity tables ("+10% visitors from this group"),
never predictions: StoreLens has no data on how many people an advertisement brings through the door.

Test any recommendation you act on with Compare (before/after, with intervals).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

from .audience import band_label, wilson
from .queries import Analytics, Window

MIN_N = 20  # impressions needed before a display gets a layout recommendation
UPLIFTS = (0.05, 0.10, 0.20)


# ----------------------------------------------------------------------------------------------- helpers

def window_hours(an: Analytics, w: Window) -> float:
    hrs = 0.0
    for r in an.runs():
        if w.run_ids and r["run_id"] not in w.run_ids:
            continue
        t0, t1 = r["t_start"], r["t_end"] or r["t_start"]
        if w.t0 is not None:
            t0 = max(t0, w.t0)
        if w.t1 is not None:
            t1 = min(t1, w.t1)
        hrs += max(t1 - t0, 0.0) / 3600.0
    return hrs


def _rng(lo: float, hi: float) -> list[float]:
    return [float(lo), float(hi)]


def _money(d_engaged: list[float], pr: tuple[float, float], value: float) -> list[float]:
    return [d_engaged[0] * pr[0] * value, d_engaged[1] * pr[1] * value]


def _conf(n: int) -> str:
    return "high" if n >= 100 else ("medium" if n >= 40 else "low")


def _sum_cells(cell: dict, dim: str, members: list[str]) -> int:
    ms = set(members)
    return sum(n for (a, g), n in cell.items() if (a if dim == "age" else g) in ms)


# ----------------------------------------------------------------------------------------------- placement

def suggest_placement(an: Analytics, w: Window, zone_id: str, heat: dict | None = None) -> dict | None:
    """Where a display of the same footprint would sit in a busier part of the floor (from the occupancy heat map).

    Keeps clear of every other zone and of the door lobby. It only RANKS places by how much people occupy them
    today; whether the display would then be reached is the scenario the recommendation states."""
    cfg = an.cfg
    heat = heat or an.heatmap(w)
    grid = np.asarray(heat["grid"], float)
    if grid.size == 0 or heat["total_s"] <= 0:
        return None
    cell = heat["cell_m"]
    z = cfg.zone(zone_id)
    xs, ys = [p[0] for p in z.polygon], [p[1] for p in z.polygon]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    fw, fh = x1 - x0, y1 - y0
    others = [(min(p[0] for p in o.polygon), min(p[1] for p in o.polygon), max(p[0] for p in o.polygon),
               max(p[1] for p in o.polygon)) for o in cfg.zones if o.id != zone_id]
    e = cfg.entrance
    lobby = (min(e.p1[0], e.p2[0]) - 1.0, min(e.p1[1], e.p2[1]) - 2.0, max(e.p1[0], e.p2[0]) + 1.0, cfg.floor.height_m)
    others.append(lobby)

    def density(a, b, c, d) -> float:
        i0, i1 = int(a / cell), max(int(math.ceil(c / cell)), int(a / cell) + 1)
        j0, j1 = int(b / cell), max(int(math.ceil(d / cell)), int(b / cell) + 1)
        sub = grid[max(j0, 0):j1, max(i0, 0):i1]
        return float(sub.mean()) if sub.size else 0.0

    def overlap(r1, r2) -> float:
        w_ = min(r1[2], r2[2]) - max(r1[0], r2[0])
        h_ = min(r1[3], r2[3]) - max(r1[1], r2[1])
        return max(w_, 0) * max(h_, 0)

    cur = density(x0, y0, x1, y1)
    best, best_d = None, 0.0
    step = max(cell, 0.5)
    ax = 0.0
    while ax + fw <= cfg.floor.width_m + 1e-6:
        ay = 0.0
        while ay + fh <= cfg.floor.height_m + 1e-6:
            r = (ax, ay, ax + fw, ay + fh)
            if all(overlap(r, o) <= 1e-6 for o in others) and overlap(r, (x0, y0, x1, y1)) < 0.3 * fw * fh:
                d = density(*r)
                if d > best_d:
                    best, best_d = r, d
            ay += step
        ax += step
    if best is None or cur <= 0 or best_d < 1.25 * cur:
        return None
    return {"zone_id": zone_id, "from": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            "to": [[best[0], best[1]], [best[2], best[1]], [best[2], best[3]], [best[0], best[3]]],
            "traffic_ratio": round(best_d / cur, 2)}


# ----------------------------------------------------------------------------------------------- main

def recommend(an: Analytics, w: Window, audience: dict | None = None, top: int = 8) -> dict:
    cfg = an.cfg
    biz = cfg.business
    summary = an.summary(w)
    zones = an.zones(w)
    n_vis = summary["visitors"]
    hours = window_hours(an, w)
    measured_per_day = (n_vis / hours * biz.open_hours_per_day) if hours > 0 else None
    V = biz.visitors_per_day if biz.visitors_per_day is not None else measured_per_day
    pr = tuple(biz.purchase_rate_given_engaged)
    value = biz.avg_item_value
    assumptions = {
        "currency": biz.currency, "avg_item_value": value, "purchase_rate": list(pr),
        "visitors_per_day": V, "visitors_per_day_source": "assumed (config)" if biz.visitors_per_day is not None
        else "measured visitors per hour x opening hours",
        "open_hours_per_day": biz.open_hours_per_day,
        "note": "These are inputs, not measurements. Replace them with your own door-counter and POS numbers.",
    }
    out = {"assumptions": assumptions, "window": {"visitors": n_vis, "hours": round(hours, 2)}, "warnings": [],
           "recommendations": [], "marketing": [], "layout_plan": {"moves": []}, "summary": None}
    versions = sorted({str(r["meta"].get("layout_version", "?")) for r in an.runs()
                       if not (w.run_ids and r["run_id"] not in w.run_ids)})
    if len(versions) > 1:
        out["warnings"].append(f"These runs span layout versions {', '.join(versions)}: metrics mix different "
                               f"floor plans. Pick runs from one version for recommendations.")
    if n_vis < 30 or not V:
        out["warnings"].append("Fewer than 30 visitors in this selection: too little data for recommendations.")
        return out

    fx = [z for z in zones if z["kind"] == "fixture" and z["impressions"] >= MIN_N and z["reach"] is not None
          and z["engagement_rate"] is not None]
    if len(fx) < 3:
        out["warnings"].append("Need at least 3 displays with 20+ impressions to compare them with each other.")
        return out
    # Looking at k displays gives k chances for one to look weak by luck, so the interval is widened for k (family-wise
    # 95%, Bonferroni): with identical displays and 300 visitors this took layout flags from about 15% of runs to about 4%
    # (scripts/backtest_recommender.py); below a few hundred visitors the page still says the data is thin.
    zk = float(norm.ppf(1 - 0.05 / (2 * len(fx))))
    med_reach = float(np.median([z["reach"] for z in fx]))
    med_rate = float(np.median([z["engagement_rate"] for z in fx]))
    med_e100 = float(np.median([z["engaged_per_100"] for z in fx]))
    out["benchmarks"] = {"median_reach": med_reach, "median_engagement_rate": med_rate,
                         "median_engaged_per_100": med_e100}
    heat = an.heatmap(w)

    def effect(d_engaged_pv: list[float], d_passers_pv: list[float] | None = None, basis: str = "") -> dict:
        per_day = [x * V for x in d_engaged_pv]
        eff = {"basis": basis,
               "engaged_per_visitor": _rng(*d_engaged_pv),
               "extra_engaged_per_day": _rng(*per_day),
               "revenue_per_day": _money(per_day, pr, value)}
        if d_passers_pv is not None:
            eff["passers_per_visitor"] = _rng(*d_passers_pv)
            eff["extra_passers_per_day"] = _rng(d_passers_pv[0] * V, d_passers_pv[1] * V)
        eff["revenue_per_month"] = [x * 30 for x in eff["revenue_per_day"]]
        return eff

    recs: list[dict] = []
    for z in fx:
        reach, rate, imp = z["reach"], z["engagement_rate"], z["impressions"]
        ev = {"reach": reach, "engagement_rate": rate, "engaged_per_100": z["engaged_per_100"], "impressions": imp,
              "median_reach": med_reach, "median_engagement_rate": med_rate}
        common = {"zone_id": z["zone_id"], "zone": z["name"], "confidence": _conf(imp), "evidence": ev}
        # A display is only called out when its shortfall is bigger than sampling noise: the upper end of its interval
        # (95% across all displays compared) must still sit below the store median.
        rate_hi = wilson(z["engaged"], imp, zk)[1]
        reach_hi = wilson(imp, n_vis, zk)[1]
        seen_not_engaging = reach >= 0.95 * med_reach and rate < 0.85 * med_rate and rate_hi < med_rate
        hidden_gem = reach < 0.85 * med_reach and reach_hi < med_reach and rate >= 0.9 * med_rate
        if seen_not_engaging:
            tgt = [rate + 0.5 * (med_rate - rate), med_rate]
            d = [reach * (t - rate) for t in tgt]
            recs.append({**common, "id": f"convert:{z['zone_id']}", "type": "improve_conversion", "lever": "layout",
                         "title": f"{z['name']}: turn more of the people who pass it into people who stop",
                         "why": f"{reach * 100:.0f}% of visitors come near it (store median {med_reach * 100:.0f}%) but only "
                                f"{rate * 100:.0f}% of them stop (median {med_rate * 100:.0f}%).",
                         "actions": ["Change what is on show: signage, price/promo message, the product at eye level.",
                                     "Give people room to stop: clear the approach, add a place to put a basket.",
                                     "Re-measure with Compare after two weeks."],
                         "scenario": f"engagement rate rises from {rate * 100:.0f}% to "
                                     f"{tgt[0] * 100:.0f}-{tgt[1] * 100:.0f}% (the store median is the upper end)",
                         "effect": effect(d, None, "scenario")})
        elif hidden_gem:
            tgt = [reach + 0.5 * (med_reach - reach), med_reach]
            dp = [t - reach for t in tgt]
            d = [p * rate for p in dp]
            place = suggest_placement(an, w, z["zone_id"], heat)
            acts = ["Move it onto a busier path, or add wayfinding/sight-lines so more people reach it."]
            if place:
                acts.insert(0, f"Candidate spot on the floor map: about {place['traffic_ratio']:.1f}x the foot traffic "
                               f"of where it is now (if the fixture can move).")
                out["layout_plan"]["moves"].append(
                    {**place, "reason": f"{z['name']} converts well but few visitors reach it"})
            acts.append("Re-measure with Compare after two weeks.")
            recs.append({**common, "id": f"reach:{z['zone_id']}", "type": "increase_reach", "lever": "layout",
                         "title": f"{z['name']}: get it in front of more visitors",
                         "why": f"{rate * 100:.0f}% of people who come near it stop (store median {med_rate * 100:.0f}%), "
                                f"but only {reach * 100:.0f}% of visitors get near it (median {med_reach * 100:.0f}%).",
                         "actions": acts, "placement": place,
                         "scenario": f"reach rises from {reach * 100:.0f}% to {tgt[0] * 100:.0f}-{tgt[1] * 100:.0f}% of "
                                     f"visitors (the store median is the upper end)",
                         "effect": effect(d, dp, "scenario")})
        elif z["engaged_per_100"] < 0.8 * med_e100 and wilson(z["engaged"], n_vis, zk)[1] * 100 < med_e100:
            gap = (med_e100 - z["engaged_per_100"]) / 100.0
            recs.append({**common, "id": f"rework:{z['zone_id']}", "type": "rework", "lever": "layout",
                         "title": f"{z['name']}: below the typical display on both reach and stopping",
                         "why": f"{z['engaged_per_100']:.0f} engaged per 100 visitors vs a store median of {med_e100:.0f}.",
                         "actions": ["Treat it as a candidate to rework or swap with a stronger category.",
                                     "Re-measure with Compare after two weeks."],
                         "scenario": f"engaged visitors per 100 rise toward the store median ({med_e100:.0f})",
                         "effect": effect([0.5 * gap, gap], None, "scenario")})

    # --- audience-driven recommendations ---
    seg_recs, mkt = _segment_recs(an, w, audience, V, pr, value, effect)
    recs += seg_recs
    out["marketing"] = mkt

    def mid(r):
        return sum(r["effect"]["revenue_per_day"]) / 2 if r.get("effect") else 0.0

    recs.sort(key=lambda r: (-mid(r), r["id"]))
    out["recommendations"] = recs[:top]
    # Combined potential: one layout recommendation per display, summed (assumes independence: stated in the note).
    seen, lo, hi, eng_lo, eng_hi = set(), 0.0, 0.0, 0.0, 0.0
    for r in recs:
        if r["lever"] != "layout" or r["zone_id"] in seen:
            continue
        seen.add(r["zone_id"])
        lo += r["effect"]["revenue_per_day"][0]
        hi += r["effect"]["revenue_per_day"][1]
        eng_lo += r["effect"]["extra_engaged_per_day"][0]
        eng_hi += r["effect"]["extra_engaged_per_day"][1]
    if seen:
        out["summary"] = {"displays": sorted(seen), "extra_engaged_per_day": [eng_lo, eng_hi],
                          "revenue_per_day": [lo, hi], "revenue_per_month": [lo * 30, hi * 30],
                          "note": "Sum of the layout scenarios above, one per display, assuming they do not affect each "
                                  "other. Every figure rests on the purchase-rate and basket-size assumptions."}
    out["caveats"] = [
        "Targets are benchmarks from your own store (the median display), not predictions of what a change will do.",
        "Money = extra engaged shoppers x purchase rate x basket size. The last two are assumptions, not measurements.",
        "Reach and stopping are measured from the camera; why a display performs as it does (what it sells, its price) "
        "is not. Test changes with Compare before rolling them out.",
    ]
    return out


# ----------------------------------------------------------------------------------------------- segments

def _segment_recs(an: Analytics, w: Window, audience: dict | None, V: float, pr, value, effect) -> tuple[list, list]:
    if not audience or not audience.get("has_data"):
        return [], []
    cells = an._audience_cells(w)
    vis = cells.get(("", "visitor"), {})
    fixtures = [z.id for z in an.cfg.measured_zones() if z.kind == "fixture"]
    known = {"age": sum(n for (a, g), n in vis.items() if a != "unknown"),
             "gender": sum(n for (a, g), n in vis.items() if g != "unknown")}
    recs: list[dict] = []
    mkt: list[dict] = []
    used_zone: set[str] = set()
    cands = []
    for zr in audience["zones"]:
        if zr["kind"] != "fixture":
            continue
        for dim in ("age", "gender"):
            for g in zr.get(dim) or []:
                if g["verdict"] == "over" and g["index"] and g["n"] >= audience["min_cell"]:
                    cands.append((g["index"], zr, dim, g))
    cands.sort(key=lambda c: -c[0])
    for idx, zr, dim, g in cands:
        if zr["zone_id"] in used_zone or len(mkt) >= 3:
            continue
        used_zone.add(zr["zone_id"])
        members = g["members"]
        nv = _sum_cells(vis, dim, members)
        if nv < audience["min_cell"] or not known[dim]:
            continue
        share = nv / known[dim]
        e_zone = _sum_cells(cells.get((zr["zone_id"], "engaged"), {}), dim, members) / nv
        e_all = sum(_sum_cells(cells.get((f, "engaged"), {}), dim, members) for f in fixtures) / nv
        label = f"{g['label']}" if dim == "age" else f"{g['label']} shoppers"
        scenarios = []
        for u in UPLIFTS:
            extra = V * share * u
            eng_zone, eng_all = extra * e_zone, extra * e_all
            scenarios.append({"uplift_pct": int(u * 100), "extra_visitors_per_day": extra,
                              "extra_engaged_here_per_day": eng_zone, "extra_engaged_all_per_day": eng_all,
                              "revenue_per_day": _money([eng_all, eng_all], pr, value)})
        mkt.append({
            "id": f"target:{zr['zone_id']}:{dim}:{g['label']}", "zone_id": zr["zone_id"], "zone": zr["name"],
            "group": g["label"], "dimension": dim, "index": g["index"], "index_ci": g["ci"], "n": g["n"],
            "share_of_visitors": share, "engaged_per_visitor_here": e_zone, "engaged_per_visitor_all": e_all,
            "title": f"{zr['name']}: draws {label} ({g['index']:.1f}x their share of visitors)",
            "why": f"{label} are {share * 100:.0f}% of visitors but {g['share'] * 100:.0f}% of the people who engage with "
                   f"{zr['name']} (index {g['index']:.1f}, 95% CI {g['ci'][0]:.1f}-{g['ci'][1]:.1f}).",
            "actions": [f"Feature the items and messages that suit {label} at {zr['name']}.",
                        f"Aim promotions for {zr['name']} at {label} (their channels, their wording).",
                        "Re-measure the audience mix after the campaign."],
            "scenarios": scenarios, "confidence": zr["confidence"],
            "note": "Sensitivity table, not a forecast: StoreLens does not measure how many extra people a campaign "
                    "brings in. It shows what extra visitors from this group would be worth at today's behaviour.",
        })
    # an under-served group: engages with the displays clearly less than the store average
    allE = sum(sum(c.values()) for (z, m), c in cells.items() if m == "engaged" and z in fixtures)
    nvis_total = sum(vis.values())
    if nvis_total and allE:
        E_avg = allE / nvis_total
        worst = None
        for dim in ("age", "gender"):
            # use the store-level groups (already merged to be big enough to quote)
            groups = [g["bands"] for g in audience["store"]["by_age"]] if dim == "age" else \
                ([[g["label"]] for g in (audience["store"]["by_gender"] or [])])
            for members in groups:
                nv = _sum_cells(vis, dim, members)
                if nv < max(audience["min_cell"], 30) or not known[dim]:
                    continue
                e = sum(_sum_cells(cells.get((f, "engaged"), {}), dim, members) for f in fixtures)
                e_pv = e / nv
                if e_pv < 0.8 * E_avg and (worst is None or e_pv / E_avg < worst[0]):
                    worst = (e_pv / E_avg, dim, members, nv, e_pv)
        if worst:
            ratio, dim, members, nv, e_pv = worst
            lab = band_label(members) if dim == "age" else f"{members[0]} shoppers"
            share = nv / known[dim]
            d_pv = [0.5 * share * (E_avg - e_pv), share * (E_avg - e_pv)]
            recs.append({
                "id": f"underserved:{dim}:{lab}", "zone_id": None, "zone": "Whole store", "type": "underserved_group",
                "lever": "assortment", "confidence": _conf(nv),
                "title": f"{lab.capitalize() if dim == 'gender' else lab}: engage with the displays "
                         f"{(1 - ratio) * 100:.0f}% less than the average visitor",
                "why": f"{share * 100:.0f}% of visitors, but {e_pv:.2f} engaged visits per visitor vs {E_avg:.2f} for "
                       f"everyone ({nv} visitors counted).",
                "actions": ["Look at which display this group engages with most and put related items on their route.",
                            "Check whether the range, the prices or the signage speak to them.",
                            "Re-measure the audience mix after any change."],
                "scenario": "their engagement per visitor rises halfway to fully to the store average",
                "evidence": {"engaged_per_visitor": e_pv, "store_average": E_avg, "visitors": nv},
                "effect": effect(d_pv, None, "scenario")})
    return recs, mkt
