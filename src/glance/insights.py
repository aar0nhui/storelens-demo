"""Rule-based layout insights. Deliberately transparent: every card names the rule, the numbers behind it
and a confidence from the sample size, so a store owner can sanity-check it against what they see."""
from __future__ import annotations

import numpy as np


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _conf(n: int) -> str:
    return "high" if n >= 50 else ("medium" if n >= 25 else "low")


def generate_insights(zones: list[dict], flows: dict | None = None, lift: dict | None = None,
                      min_n: int = 20) -> list[dict]:
    out: list[dict] = []
    fx = [z for z in zones if z["kind"] == "fixture" and z["impressions"] >= min_n and z["reach"] is not None]
    if len(fx) >= 3:
        med_reach = float(np.median([z["reach"] for z in fx]))
        med_rate = float(np.median([z["engagement_rate"] for z in fx if z["engagement_rate"] is not None]))
        for z in fx:
            rate, reach = z["engagement_rate"] or 0.0, z["reach"]
            ev = {"reach": reach, "engagement_rate": rate, "impressions": z["impressions"],
                  "store_median_reach": med_reach, "store_median_engagement_rate": med_rate}
            if reach >= 1.2 * med_reach and med_rate > 0 and rate <= 0.7 * med_rate:
                out.append({"id": f"seen-not-engaging:{z['zone_id']}", "kind": "opportunity", "zone_id": z["zone_id"],
                            "title": f"{z['name']}: seen a lot, engaged with little",
                            "detail": f"{_pct(reach)} of visitors come near it (store median {_pct(med_reach)}) but only "
                                      f"{_pct(rate)} stop (store median {_pct(med_rate)}). It has the traffic; the display "
                                      f"or its space isn't converting attention. Test new signage, product mix or "
                                      f"more room to stop, then measure the change with Compare.",
                            "confidence": _conf(z["impressions"]), "evidence": ev})
            elif reach <= 0.7 * med_reach and med_rate > 0 and rate >= 1.3 * med_rate:
                out.append({"id": f"hidden-gem:{z['zone_id']}", "kind": "opportunity", "zone_id": z["zone_id"],
                            "title": f"{z['name']}: engaging, but hard to reach",
                            "detail": f"{_pct(rate)} of people who come near it stop (store median {_pct(med_rate)}), but only "
                                      f"{_pct(reach)} of visitors get there (store median {_pct(med_reach)}). Moving it "
                                      f"onto the main path, or adding wayfinding, could lift engaged visitors.",
                            "confidence": _conf(z["impressions"]), "evidence": ev})
    for z in zones:
        if z["kind"] == "fixture" and z["reach"] is not None and z["reach"] < 0.15 and z["impressions"] >= 3:
            out.append({"id": f"low-reach:{z['zone_id']}", "kind": "warning", "zone_id": z["zone_id"],
                        "title": f"{z['name']}: few visitors reach it",
                        "detail": f"Only {_pct(z['reach'])} of visitors came near it. Check sight-lines and the route to it.",
                        "confidence": _conf(z["impressions"]), "evidence": {"reach": z["reach"]}})
        if z["kind"] == "area" and z["median_dwell_s"] and z["median_dwell_s"] >= 20 and z["impressions"] >= min_n:
            out.append({"id": f"long-dwell:{z['zone_id']}", "kind": "warning", "zone_id": z["zone_id"],
                        "title": f"{z['name']}: long median time ({z['median_dwell_s']:.0f}s)",
                        "detail": "Visitors spend a long time here. For a checkout/queue area, consider another register at "
                                  "peak hours or a quicker flow; check the time-of-day chart for when it peaks.",
                        "confidence": _conf(z["impressions"]), "evidence": {"median_dwell_s": z["median_dwell_s"]}})
    if len(fx) >= 3:
        eng = [z["median_engaged_s"] for z in fx if z["median_engaged_s"]]
        if eng:
            med = float(np.median(eng))
            for z in fx:
                if z["median_engaged_s"] and z["median_engaged_s"] >= 1.5 * med and (z["engaged"] or 0) >= 8:
                    out.append({"id": f"long-consideration:{z['zone_id']}", "kind": "info", "zone_id": z["zone_id"],
                                "title": f"{z['name']}: long consideration time",
                                "detail": f"Engaged visitors spend a median {z['median_engaged_s']:.0f}s here (other displays "
                                          f"{med:.0f}s). Good place for higher-consideration items; consider seating, "
                                          f"samples or a staff touchpoint nearby.",
                                "confidence": _conf(z["engaged"]), "evidence": {"median_engaged_s": z["median_engaged_s"]}})
    if flows and flows.get("edges"):
        fixture_ids = {z["zone_id"] for z in zones if z["kind"] == "fixture"}
        out_tot: dict[str, int] = {}
        for e in flows["edges"]:
            out_tot[e["from"]] = out_tot.get(e["from"], 0) + e["count"]
        for e in flows["edges"]:
            if e["from"] in fixture_ids and e["to"] in fixture_ids and e["count"] >= 6 \
                    and e["count"] / max(out_tot[e["from"]], 1) >= 0.3:
                out.append({"id": f"handoff:{e['from']}>{e['to']}", "kind": "info", "zone_id": e["from"],
                            "title": f"{e['from_name']} → {e['to_name']} is a strong path",
                            "detail": f"{e['count']} visitors went straight from {e['from_name']} to {e['to_name']} "
                                      f"({e['count'] / out_tot[e['from']] * 100:.0f}% of the tracked onward moves from "
                                      f"{e['from_name']}). Complementary items placed along this path are cross-sell candidates.",
                            "confidence": _conf(e["count"]), "evidence": {"count": e["count"]}})
                break
    if lift and lift.get("zones"):
        level = lift.get("ci_level_pct", 95)
        for r in lift["zones"]:
            adj = r.get("adjusted")
            main = adj or r
            verdict = main.get("verdict")
            if verdict in ("improved", "worse"):
                kind_txt = "adjusted for store-wide changes" if adj else "unadjusted"
                out.append({
                    "id": f"lift:{r['zone_id']}", "kind": "result", "zone_id": r["zone_id"],
                    "title": f"{r['name']}: {'+' if main['lift_pct'] >= 0 else ''}{main['lift_pct']:.0f}% engaged visitors "
                             f"({verdict})",
                    "detail": f"Engaged visitors per 100 store visitors went from {r['a']['per_100']:.1f} to "
                              f"{r['b']['per_100']:.1f} ({kind_txt}; {level:.0f}% CI {main['ci_low_pct']:+.0f}% to "
                              f"{main['ci_high_pct']:+.0f}%).",
                    "confidence": "high" if abs(main["ci_low_pct"] - main["ci_high_pct"]) < 80 else "medium",
                    "evidence": {"lift_pct": main["lift_pct"]}})
            elif verdict == "inconclusive" and main.get("lift_pct") is not None and abs(main["lift_pct"]) >= 30:
                need = r.get("visitors_needed_per_period")
                if need and need > max(r["a"]["visitors"], r["b"]["visitors"]):
                    out.append({
                        "id": f"needs-data:{r['zone_id']}", "kind": "result", "zone_id": r["zone_id"],
                        "title": f"{r['name']}: {main['lift_pct']:+.0f}% so far, not yet conclusive",
                        "detail": f"Engaged visitors per 100 went from {r['a']['per_100']:.1f} to {r['b']['per_100']:.1f}, but "
                                  f"with {r['a']['visitors']} and {r['b']['visitors']} visitors the {level:.0f}% CI "
                                  f"({main['ci_low_pct']:+.0f}% to {main['ci_high_pct']:+.0f}%) still includes no change. "
                                  f"Keep measuring: a change of this size needs roughly {need} visitors in each period to confirm.",
                        "confidence": "low", "evidence": {"lift_pct": main["lift_pct"], "visitors_needed": need}})
    order = {"result": 0, "opportunity": 1, "warning": 2, "info": 3}
    out.sort(key=lambda c: order.get(c["kind"], 9))
    return out
