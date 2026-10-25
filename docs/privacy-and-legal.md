# Privacy and legal checklist

This is an engineering checklist to take to a lawyer. It is **not legal advice**; requirements differ by country and
state, and by what a given deployment actually stores.

## What the software guarantees (and where it is tested)

| claim | where |
|---|---|
| No face images, embeddings, or appearance features are computed or stored | no such model in the pipeline; schema test in `tests/test_privacy.py` |
| No per-person trajectory is persisted | DB has no coordinate columns; `tests/test_pipeline_sim.py` |
| Visit keys cannot be linked across days | salt in memory only, rotated every 24 h of data time (`test_keys_rotate_with_the_salt_epoch`) |
| Previews never show unblurred people | mosaic before draw/encode; `blur: none` refused without `--allow-raw-preview` |
| No demographic attributes | no such models |
| The simulated live view does not persist trajectories | per-frame tracks live in memory while the clip renders; only the two videos are written (`src/storelens/live.py`) |

## What it does not guarantee

* The **raw video** still exists wherever the store's CCTV system keeps it. StoreLens reads it; it does not control retention.
* Zone dwell per anonymous visit and the heat map are still data *derived from people's behaviour*. Whether that is
  "personal data" depends on the jurisdiction and on whether the operator could single someone out.
* Running on a hosted server means video leaves the store. Prefer an edge box so that only events leave.

## Questions to settle before a pilot

1. **Jurisdiction.** GDPR/UK GDPR (EU/UK customers), CCPA/CPRA (California), and state biometric laws such as Illinois
   BIPA are the usual starting points. Biometric laws generally turn on *biometric identifiers*; this design computes none,
   but have counsel confirm that for the exact pipeline you ship.
2. **Legal basis and notice.** Signage at the entrance saying that anonymous video analytics are used, who the operator
   is, and who to contact. Some regimes need a documented legitimate-interest assessment or a DPIA.
3. **Roles.** Usually the store is the controller and the vendor a processor: a data-processing agreement is needed.
4. **Retention.** Event rows hold no identity, but set and document a retention period anyway.
5. **Staff.** Employees in view are covered by workplace monitoring rules; consult labour counsel and tell them.
6. **Security.** Encrypt the DB at rest, restrict dashboard access, keep an audit log, and pen-test before real data.
7. **Claims.** Do not market as "GDPR compliant" or "anonymous by law". Say what is measured and what is not stored.
