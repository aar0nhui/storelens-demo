# Privacy and legal checklist

This is an engineering checklist to take to a lawyer. It is **not legal advice**; requirements differ by country and
state, and by what a given deployment actually stores. California points below come from secondary sources read in
October 2026 and need confirming by a California privacy attorney before real footage is processed.

## Two modes

| | **Counting mode** (default) | **Audience mode** (`demographics.enabled: true`) |
|---|---|---|
| Face or appearance model | none runs | a face-region model runs on the store computer, in memory, once per tracked person |
| What is stored | anonymous visit rows, zone dwell, heat grid | the same, plus an aggregate counter of age band x perceived gender per display and time bucket |
| Notice needed | signage for anonymous video analytics | signage must also say that age range and perceived gender are estimated from appearance, and that nothing is stored per person |
| Ready to use | yes (still needs the checklist below) | **no production model is bundled**; the demo uses a simulated estimator. Do not switch it on for real footage before counsel signs off |

With audience mode off, no attribute model is even loaded. Do not describe audience mode as "no faces analysed": faces are
analysed momentarily in memory. The honest wording is "faces are analysed in memory to estimate an age range and are
never stored".

## What the software guarantees (and where it is tested)

| claim | where |
|---|---|
| No face images, crops, embeddings or appearance features are written anywhere | schema scan and file scan: `tests/test_privacy.py`, `tests/test_audit.py`; `storelens audit` repeats both on a real database and folder |
| No per-person trajectory is persisted | DB has no coordinate columns; `tests/test_pipeline_sim.py` |
| Visit keys cannot be linked across days | salt in memory only, rotated every 24 h of data time (`test_keys_rotate_with_the_salt_epoch`) |
| Previews never show unblurred people | mosaic before draw/encode; `blur: none` refused without `--allow-raw-preview` |
| Age band and gender are never attached to a visit | the `audience` table has no visit key and no per-person column (`test_audience_table_is_aggregate_only_and_visit_tables_hold_no_demographics`) |
| An estimator forgets a person when their track ends | `test_estimator_forgets_a_person_after_finalize_and_discard` |
| Time buckets are coarse | 6 hours by default, aligned to local midnight; `storelens audit` checks every stored timestamp sits on a boundary |
| Small groups are not shown | groups under `min_cell` (default 10) are merged into neighbouring age bands or hidden; gender is shown only when both sides reach the minimum (`test_the_dashboard_view_never_shows_a_group_below_k`) |
| "Unknown" is a real answer | low-confidence estimates are never forced into a guess; the dashboard shows what share of visitors got an estimate |
| The cloud copy holds only what the dashboard shows | the store computer and the Worker both refuse per-person keys, images and embedded media (`tests/test_cloud_e2e.py`, `cloud/test/worker.test.js`) |
| The simulated live view does not persist trajectories | per-frame tracks live in memory while the clip renders; only the two videos are written (`src/storelens/live.py`) |

## What it does not guarantee

* The **raw video** still exists wherever the store's CCTV system keeps it. StoreLens reads it; it does not control retention.
* Zone dwell per anonymous visit and the heat map are still data *derived from people's behaviour*. Whether that is
  "personal data" depends on the jurisdiction and on whether the operator could single someone out.
* **Which display someone visited tells you something about them.** That is inherent to the product (it is what the
  audience insight is) and is not a leak. What the audit can reduce is how much the *time bucket* adds on top. A tiny
  cell at rest ("65+ female, display 3, Tuesday 10:00-16:00") describes one person, which is why cells under `min_cell`
  are suppressed in the dashboard and why `storelens audit --coarsen-days N --apply` can merge old buckets into days and
  fold the remaining small cells into "unknown".
* Cross-day linkage is structurally impossible in the code (the salt rotates and no appearance feature is kept). The
  proof is still a staged test with consenting volunteers on two days, not the code review alone.
* Age and gender estimates are **wrong sometimes**, and error is not even across groups; audits of commercial classifiers
  have found larger errors for darker-skinned women. "Perceived gender" is not a fact about anyone, and it misclassifies
  trans and non-binary people. Use it only for aggregate marketing context.
* Overhead ceiling cameras usually cannot see faces. Audience mode needs an eye-level or entrance camera, and accuracy
  must be measured on the store's own footage (`storelens fieldcheck`, see [validation.md](validation.md)).
* Hosting on a server means footage leaves the store. Keep video on the store computer; only the dashboard's aggregate
  snapshot is sent to the cloud app ([cloud.md](cloud.md)).

## Rules the product should keep

1. Audience data is for **aggregate layout and marketing decisions only**. It never drives pricing, offers, service level
   or who is flagged (California's Unruh Civil Rights Act applies to perceived sex, gender identity and age, and
   differing prices or service by sex are a classic violation). Put this in the customer contract.
2. No emotion inference. California AB 1883 (effective Jan 1, 2027) restricts AI inference of employees' emotional
   states. Do not add it.
3. Do not train or fine-tune any model on customer footage without counsel (the CPPA regulations list training
   face/emotion-recognition on personal information as a risk-assessment trigger).
4. Do not turn audience mode on outside California without separate review: Illinois, Texas, Washington, New York City,
   Erie County NY and Portland OR have stricter face-geometry or biometric rules.
5. Never add cross-camera appearance matching. Each floor point has one owning camera ([cameras.md](cameras.md)); that is
   a legal decision as much as an engineering one.

## Questions to settle before a pilot

1. **Jurisdiction.** GDPR/UK GDPR (EU/UK customers), CCPA/CPRA (California), and state biometric laws such as Illinois
   BIPA are the usual starting points. Biometric laws generally turn on *biometric identifiers* used to identify a person;
   an age-band output does not identify anyone, and this design stores no template, but have counsel confirm that for the
   exact pipeline you ship.
2. **Does the CCPA reach this customer?** It applies to a for-profit business over any one threshold: annual gross revenue
   above the adjusted limit (about $26.6 million as read in Oct 2026; verify), data of 100,000+ consumers/households/devices
   a year, or half of revenue from selling or sharing. A single small store may be under all three. Other California
   law still applies (Penal Code, Labor Code, Unruh, unfair-practices rules), so design as if covered.
3. **Notice at collection.** For offline collection California allows prominent signage that points to an online notice
   listing the categories collected, purposes, retention and whether anything is sold or shared. The store's existing
   "CCTV for security" sign probably does not cover a new marketing-analytics purpose or age/gender estimation.
4. **Roles and contract.** The store is usually the controller and the vendor a service provider/processor: a written
   agreement that limits use, bans selling or sharing, and states the deidentification commitment. List Cloudflare as a
   subprocessor.
5. **Audio.** California is all-party consent for confidential communications (Penal Code 632). Cameras often have
   microphones: disable them and drop the audio track at ingest.
6. **Private areas.** Penal Code 647(j) and Labor Code 435 bar cameras in restrooms, fitting and changing rooms. Use
   per-camera privacy masks (blanked before detection) and a camera-placement audit.
7. **Staff.** Employees in view are covered by workplace-monitoring rules (AB 1331, effective Jan 1, 2027, for some
   areas); consult employment counsel and tell staff.
8. **Retention.** Event rows hold no identity, but set and document a retention period. The audit tool can coarsen old
   audience rows.
9. **Security.** Encrypt the disk of the store computer, restrict dashboard access (Cloudflare Access with an allow-list
   and a JWT check in the Worker), keep an access log, and pen-test before real data.
10. **Claims.** Do not market as "GDPR compliant", "anonymous by law", "no personal data" or "no faces". Say what is
    measured and what is not stored. After audience mode is on, "no faces analysed" is false.
11. **Risk assessment.** A short written assessment (what is collected, why, risks, mitigations) is cheap insurance, and
    California's new regulations require one for some kinds of processing.
12. **Los Angeles.** I found no city or county ordinance restricting private businesses' video analytics, but that search
    was not exhaustive. Have counsel check the Los Angeles Municipal Code, the lease or mall rules, and any business
    licence conditions.
