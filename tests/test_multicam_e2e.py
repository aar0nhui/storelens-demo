"""Two cameras, one floor plan: shoppers are counted once, and every display is measured by exactly one camera."""
import pytest

from storelens.cameras import CameraNetwork
from storelens.pipeline import Pipeline
from storelens.queries import Analytics, Window
from storelens.sim.generate import Period
from storelens.sim.layout import default_layout
from storelens.sim.multicam import build_multicam_config, generate_multicam_period
from storelens.store import EventStore
from storelens.validate import gt_events, load_gt

pytestmark = pytest.mark.slow
START = 1_800_000_000.0


@pytest.fixture(scope="module")
def mc(tmp_path_factory):
    d = tmp_path_factory.mktemp("mc")
    layout = default_layout()
    cfg = build_multicam_config(layout, seed=5)
    res = generate_multicam_period(layout, Period("clip", minutes=4.0, seed=33, base_per_min=7.0), d, verbose=False)
    st = EventStore(d / "m.db")
    for cid, video in res["videos"].items():
        Pipeline(cfg, st, camera_id=cid, label="clip").run(video, START, progress=False)
    gt = load_gt(res["gt"])
    cust = [p for p in gt_events(gt, cfg, START) if p["role"] == "customer" and p["a"].dropped is None]
    return {"cfg": cfg, "st": st, "an": Analytics(st, cfg), "cust": cust}


def test_each_shopper_is_counted_once_across_cameras(mc):
    s = mc["an"].summary(Window())
    gt_entries = sum(1 for p in mc["cust"] for _, d in p["a"].entries if d > 0)
    assert gt_entries >= 10
    assert s["visitors"] == s["footfall"], "visitors are the people who came through the door, hand-off tracks are not visitors"
    assert abs(s["footfall"] - gt_entries) <= max(3, 0.25 * gt_entries)


def test_a_display_is_only_measured_by_the_camera_that_owns_it(mc):
    owners = CameraNetwork(mc["cfg"]).zone_owners()
    rows = mc["st"].query("SELECT r.camera_id, zv.zone_id FROM zone_visits zv JOIN runs r USING(run_id)")
    stray = [(c, z) for c, z in rows if z in owners and owners[z] not in (None, c) and mc["cfg"].zone(z).kind == "fixture"]
    # the pipeline filters at the source, so a camera does not even write zone visits for displays it does not own
    assert stray == []


def test_impressions_are_close_to_truth_and_not_doubled(mc):
    gt_imp = 0
    for p in mc["cust"]:
        seen = set()
        for z in p["a"].zone_visits:
            if mc["cfg"].zone(z.zone_id).kind == "fixture" and z.zone_id not in seen:
                seen.add(z.zone_id)
                gt_imp += 1
    pl = sum(z["impressions"] for z in mc["an"].zones(Window()) if z["kind"] == "fixture")
    assert gt_imp > 15 and 0.6 * gt_imp <= pl <= 1.15 * gt_imp, (pl, gt_imp)
