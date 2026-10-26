# Audience (age band, perceived gender) and recommendations

Two features sit on top of the counting pipeline: an opt-in **Audience** view (who the store's visitors and each
display's audience are, by age band and perceived gender) and **Recommendations** (what to change in the layout and
marketing, with a transparent estimate of what it could be worth). Read [privacy-and-legal.md](privacy-and-legal.md)
before switching audience mode on.

## Audience

```yaml
demographics:
  enabled: false          # off by default; with it off no attribute model is loaded
  age_bands: ["<13", "13-24", "25-44", "45-64", "65+"]
  min_cell: 10            # a group smaller than this is never shown
  bucket_hours: 6         # time resolution of the stored counts, aligned to local midnight
```

**How it works.** While a person is tracked, an estimator keeps a few votes in memory. When the track ends it returns one
estimate (an age band and a perceived gender, either of which can be `unknown`) and deletes its state. The estimate is
added to a counter in the `audience` table: `(run, time bucket, display, metric, age band, gender) -> n`, where the
metric is visitor, impression or engaged. The table has no visit key, no per-person column and no finer time than the
bucket, so it cannot be joined back to a visit or a path.

**What the dashboard shows.**

* Store audience by age band and gender, and how many visitors got an estimate at all (coverage). `unknown` is reported
  as coverage, never folded into a group and never guessed.
* For each display, who engages with it, compared with everyone who visited: the share of each group with a 95% Wilson
  interval, and an **affinity index** (the group's share among that display's engaged people divided by its share among
  all visitors, with a delta-method interval). 1.0 means in line with the store; "over" or "under" is said only when the
  interval excludes 1.
* Small-count protection: groups under `min_cell` people are merged into neighbouring age bands (`<13` + `13-24` becomes
  `<25`) or hidden. A gender split is shown only if both sides reach the minimum. Merging, rather than blanking one cell,
  avoids "total minus the other cells" leaks.

**Demo caveat.** The demo's estimator is simulated: it is a noisy copy of the simulator's ground truth with illustrative
accuracy, because rendered blobs have no faces. It shows the plumbing and the dashboard, not how well a camera can
estimate age. No production model is bundled. A real estimator implements four methods (see `attributes.py`) and must be
measured on the store's own footage with `storelens fieldcheck` before any audience number is shown.

## Recommendations

`GET /api/recommendations` and the Recommendations tab. Every recommendation is a **scenario**: "if this display reached
as many visitors, or stopped as many of them, as the store's median display, you would get about N more engaged shoppers
a day". The measured part is the current reach and engagement rate per display, with sample sizes. The target is the
store's own median display, not a forecast of what a change will do.

A display is called out only when its shortfall is bigger than sampling noise: the 95% interval of its rate has to sit
entirely below the store median. Types:

| type | meaning | scenario |
|---|---|---|
| `improve_conversion` | visitors come near it, but fewer than typical stop | engagement rate rises halfway to the median, or all the way |
| `increase_reach` | people who reach it stop at a good rate, but few reach it | reach rises halfway to the median or all the way, with a candidate floor spot (`placement`) ranked by foot traffic |
| `rework` | below the median on both | engaged per 100 visitors rise toward the median |
| `underserved_group` | audience mode: a group is under-represented among a display's engaged shoppers relative to who visits | a sensitivity table on that group's share |

**Money** = extra engaged shoppers per day x purchase rate x average item value. The last two are **assumptions, not
measurements**, and are shown next to every number. Set them under `business:` and replace them with the store's own
point-of-sale numbers as soon as they are available:

```yaml
business:
  currency: USD
  open_hours_per_day: 10
  visitors_per_day: 450          # leave unset to use measured visitors per hour x opening hours
  avg_item_value: 28
  purchase_rate_given_engaged: [0.05, 0.15]   # low and high share of engaged shoppers who buy
```

**Traffic and marketing.** StoreLens has no data on how many people an advertisement brings through the door, so footfall
effects appear only as sensitivity tables ("if marketing to this group raised its visits by 5%, 10% or 20%, the extra
engaged shoppers and money would be about ..."). The total in the summary adds one layout scenario per display and assumes
they do not affect each other.

**Limits to say out loud.**

* Targets are benchmarks, not predictions. Test anything you act on with Compare (before and after, with intervals) and
  allow two weeks.
* Placement suggestions rank floor spots by observed foot traffic. How many extra people a display would reach there is a
  scenario. The simulator cannot validate it, because simulated shoppers choose displays by goal, not by position.
* Reach and stopping are measured; why a display performs as it does (what it sells, its price) is not.
* Fewer than 30 visitors, or fewer than 3 displays with 20+ impressions, gives a warning instead of recommendations.

## Backtest on simulated worlds

`python scripts/backtest_recommender.py` builds simulated stores where the true behaviour is known (two planted weak
displays, and a control world where every display is identical), runs the recommender, then "applies" the change by
bringing the weak display to the store median and measures what the simulated store really gained. With 200 worlds per size:

* The planted problems were flagged in every world from 300 visitors up (97% at 150 visitors for the reach problem), with
  the right type 92-100% of the time at 300 or more visitors.
* The quoted gain per visitor matched the true gain on average (for example 0.168 quoted against 0.168 true for the
  conversion problem; 0.214-0.216 against 0.220 for the reach problem), with a spread of about +/-0.02 at 300 visitors
  and +/-0.01 at 1,200.
* When no display was actually weak, a layout flag still appeared in about 4-6% of runs at 150-600 visitors and 0% at 1,200.
  That is the chance of a false alarm you accept at these sample sizes, which is why every recommendation should be
  confirmed with Compare.

This checks the arithmetic and the statistics. It does not show that a real store's sales respond like this, and the
purchase-rate and basket-size assumptions are not tested by it at all.
