# Validating on a real store: field check and privacy audit

The simulator shows the logic works. It does not show the detector is accurate on a real store. These two commands are
what to run on the customer's footage before any number is shown to them.

## `storelens fieldcheck`

Compares the pipeline with a person's hand tallies of the same footage and prints error figures with 95% intervals.

```bash
storelens fieldcheck --init labels/                 # blank CSV templates + fieldcheck.yaml (never overwrites existing files)
# 1. Edit the gates in labels/fieldcheck.yaml and COMMIT the file before looking at any result.
# 2. Process the footage:  storelens run ... (one run per clip or camera)
# 3. Label by hand (below), then:
storelens fieldcheck --config store.yaml --db store.db --spec labels/fieldcheck.yaml --json fieldcheck.json
```

`--gates-sha <digest>` refuses to run if the gates differ from the digest you recorded earlier, so thresholds cannot be
adjusted after seeing results.

| label file | who makes it | what it checks |
|---|---|---|
| `door_tally.csv` | two people with tally clickers, whole hours, 30+ windows | door count error and **bias** (sign matters: crowds usually undercount) |
| `crowd.csv` | freeze sampled frames, count customers in view (staff excluded) | count error by crowd level (1-3, 4-8, 9+), 30+ snapshots per level |
| `stops.csv` | every stand-still at a display in a sampled stretch | stops found, stops at the wrong display (needs `stops_exhaustive: true`) |
| `walks.csv` | consenting staff walk in groups, windows apart | missed people and duplicates (double counting) |
| empty runs | footage of the empty store, 8+ hours overnight | false people per hour, with an exact Poisson upper bound |
| `demo_labels.csv` | human-labelled apparent age band / perceived gender of door visitors | whether the estimated age/gender mix matches (audience mode only) |
| `demo_pairs.csv` | staged volunteer session with written consent; delete after scoring | per-person accuracy of age band and gender, and the gap between subgroups |

Each check ends in one status: **PASS** (the point estimate and the whole interval are inside the gate), **UNCERTAIN**
(the estimate passes but the interval crosses the gate: label more), **FAIL**, or **INSUFFICIENT** (fewer samples than
the minimum written in the gates; this is never a pass). The overall verdict is the worst check.

Default gates (starting values, tune with the customer, and write them down first): door error within 5% and bias under
3% over 30+ windows; crowd bias under 10% per level; at most 10% of stops missed and 5% at the wrong display; duplicates
under 3% and misses under 5% on staged walks; no more than 0.25 false people per hour over 8+ empty hours; age within one
band for 85% of people and gender right for 85%, with no subgroup trailing by more than 10 points.

How many people to label (Wilson 95% interval at a true recall of 95%): 100 people gives about +/-4.5 points, 200 gives
+/-3.1, 400 gives +/-2.2, 800 gives +/-1.5.

`scripts/make_fieldcheck_example.py` writes a worked example from the simulator with an imperfect simulated annotator.
It shows the harness running end to end; the numbers it prints say nothing about real accuracy.

## `storelens audit`

Asks: can anything we store be used to remember or single out a person?

```bash
storelens audit --config store.yaml --db store.db [--dir data/] [--json audit.json]
storelens audit ... --coarsen-days 14 [--as-of 2026-11-30]      # what-if: merge audience rows older than 14 days into days
storelens audit ... --coarsen-days 14 --apply                   # really do it, then VACUUM (irreversible)
storelens audit ... --gt clip_gt.json.gz --run 1                # simulator only: attack against the true attributes
```

* **schema**: only the documented tables and columns, no BLOBs, nothing named like an embedding, face, image or
  trajectory, and no visit key on the audience table.
* **files**: no still images, embeddings or numeric-vector dumps in the data folder (video files only warn).
* **time**: every audience timestamp sits on a bucket boundary.
* **linkage**: an attacker who knows when a person came in and which displays they visited reads the stored counts. How
  much better than the store-wide prior can they guess that person's age band or gender? "Exposure" is computed from the
  stored counts alone; "realised" compares with the true attributes (simulator data only).

How to read it: which display someone visited does tell you something about them. That is the product, not a leak. The
audit separates that from what the **time bucket** adds and from small cells ("65+ female, display 3, Tuesday morning"
may be one person). When the counts leave small cells at rest, coarsening and suppression bring that exposure down, and
the audit prints the before and after. On a 14-minute simulated run (about 100 visitors), 13% of people in the engaged-display
counts sat in cells under 10 at rest and 0% after the what-if; on the 90-minute demo (about 500 visitors per period) none did.
Those figures are about the demo's size, not a property of real stores: a quiet store with a 6-hour bucket will have
small cells.

Limits: with only one time bucket in the data the time-resolution leak cannot be tested (the tool says so and warns
rather than passing). Cross-day linkage cannot happen in the code, because the salt rotates and no appearance feature is
kept, but the proof is a staged two-day test with consenting volunteers, which no software run replaces.
