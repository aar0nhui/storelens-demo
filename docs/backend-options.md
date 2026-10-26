# After the static demo: backend, auth and storage (cheap first, free where possible)

The static demo needs no backend. This note is about what to add when real stores and real users arrive, in the order
that keeps cost near zero. Prices and limits are from vendor pages read in Oct 2026; they change, so re-check before
committing. Nothing here has been deployed.

## What our data looks like (measured on the 90-minute demo database)

* About **1.1 KB of SQLite per visitor**, all tables included. A store with 300 visitors a day is roughly 330 KB a day,
  about 120 MB a year.
* Rows per visitor are small for events (about 6: visit, zone visits, door entries) but **the heat grid dominates**: 66 % of
  all rows (13,169 of 19,904) are 5-minute x 0.5 m heat cells. At that rate one 12-hour store day writes more than
  50,000 heat rows. Summed per cell per day it is a few hundred. **Aggregate heat before it leaves the store.**
* No video, faces or trajectories are in the database, only anonymous events. That is what makes a cheap cloud
  backend defensible: the sensitive material never leaves the store.

## Staged plan

| Stage | Who uses it | What you add | Cost |
|---|---|---|---|
| 0. Now | anyone, simulated data | static site on Cloudflare (see `deploy-static-demo.md`) | $0 |
| 1. Private pilot demos | named stakeholders | **Cloudflare Access** in front of the same site (email one-time PIN, no passwords) | $0 (the Free plan is meant for up to 50 users) |
| 2. First real stores (1-10) | store owners you invite | edge box computes, a **Worker** receives a nightly snapshot, **R2** stores it, Access checks who may see which store | $0 within free limits |
| 3. Self-serve, date-range queries (10+ stores) | anyone who signs up | Postgres + real auth (**Supabase** or similar), a small Python API host | free to roughly $25-50/month |

Do not skip ahead: stage 2 is a few hundred lines and has no server to keep alive; stage 3 is a real product.

### Stage 1: restrict the demo (no code)

Cloudflare Access can sit in front of the site and require a login. The Free plan is meant for teams of up to 50 users, and a one-time
PIN sent by email is a built-in login method, so invited people do not need an account anywhere
([one-time PIN](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/),
[plans](https://www.cloudflare.com/sase/products/access/)). Beyond that, the pay-as-you-go price shown is $7 per user per
month (billed annually). Use this for investor/partner demos and early pilot owners.

### Stage 2: first real stores (recommended architecture)

```
store (laptop / mini PC)                        Cloudflare (free tier)
  CCTV -> glance run -> local SQLite           Worker  POST /ingest   (per-store API key, hashed)
  nightly: build snapshot JSON, aggregate heat -> R2     stores snapshot per store and period
                                                  Worker  GET /data/<store>.js  (checks Access identity -> store)
  video never leaves the store                    static dashboard (same site/ folder) behind Cloudflare Access
```

Why this fits what we built: the dashboard already reads a precomputed snapshot (`build_snapshot`), the statistics
(bootstrap lift) stay in Python on the store's box, and the cloud only stores and serves JSON. No database, no
always-on server. A tiny table (email to store id) lives in D1 or KV. What to add to the repo: a `glance snapshot`
command that writes one store's snapshot JSON, an `/ingest` Worker, a `/data` Worker, and a loader in `app.js` that
fetches the snapshot instead of reading `window.__GLANCE_SNAPSHOT__`.

Limits you give up: the dashboard shows the periods you precomputed (for example the last 7 and 28 days), not arbitrary
date ranges. That is fine for pilots and is the trigger for stage 3.

Free-tier numbers that matter ([Workers limits](https://developers.cloudflare.com/workers/platform/limits/),
[R2 pricing](https://developers.cloudflare.com/r2/pricing/), [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/)):

* Workers Free: 100,000 requests/day, 10 ms CPU per request, 50 subrequests. Static asset requests do not count.
  Serving small JSON is fine; **do not run the bootstrap statistics in a Worker**.
* R2: 10 GB-month storage, 1 million writes and 10 million reads per month free, and no fee to send data out to the internet.
* D1: 5 GB storage, 5 million rows read and 100,000 rows written per day free; paid is $0.001 per million rows read and
  $1.00 per million rows written beyond the included amounts.

### Stage 3: self-serve and queries

Move to a relational database when you need arbitrary date ranges, many stores, or users who sign themselves up.

* **Supabase** (Postgres, auth, row-level security): Free has a 500 MB database, 50,000 monthly active users for auth,
  1 GB files, 5 GB egress, 2 active projects, and **free projects pause after one week of inactivity**. Fine for a pilot,
  risky for a customer-facing product; Pro starts at $25/month ([pricing](https://supabase.com/pricing.md)).
* **D1** as the database, if you want to stay on Cloudflare: the schema is already SQLite. The heat-row issue above applies.
* The Python analytics (FastAPI + numpy/scipy) needs a host that is not a free Worker. I have not checked current free
  tiers of container hosts; budget a few dollars a month and check prices when you get there. A cheaper trick is to keep
  computing snapshots on the store's box and only add the database for raw per-visit queries.

## Auth options

| Option | Free allowance | Good for | Watch out |
|---|---|---|---|
| Cloudflare Access (one-time PIN) | Free plan, meant for up to 50 users | invited owners, investor demos; zero code | no self-serve sign-up; $7/user/month after 50 |
| Supabase Auth | 50,000 monthly active users | self-serve sign-up with Postgres row-level security | the project pause on the free plan |
| Clerk | 50,000 monthly retained users per app; MFA is paid ([pricing](https://clerk.com/pricing)) | polished sign-in UI, organizations (store teams) | free tier caps organizations (100 per app, 20 members each); no MFA, branding stays |
| Store devices | not user auth | the edge box uploading data | use a per-store API key you can revoke; store only its hash |

Recommendation: Cloudflare Access for stages 1-2 (no passwords to leak, no code to write), then Supabase Auth or Clerk when
you want people to sign themselves up.

## Security and privacy checklist for stage 2 onward

* One key per store for uploads, shown once, stored hashed, revocable; reject oversized or malformed uploads.
* Every read is scoped by store id on the server, never trusted from the browser.
* Keep the rule from the MVP: no video, frames, faces, embeddings or trajectories in the cloud. Only anonymous aggregates.
* Retention: delete or roll up per-visit data after a fixed period (R2 lifecycle rules or a scheduled job).
* Keep the visit-key salt rotation (24 h) as is; it means even the stored events cannot be linked across days.
* Backups: export the user table and snapshots weekly to a bucket you control.
* Store signage, a data-protection assessment and local law are the customer's and your legal duty; see
  `docs/privacy-and-legal.md`. This note is engineering advice, not legal advice.

## What breaks the free tier first

1. Cloudflare Access past 50 users (then about $7 per user per month).
2. Unaggregated heat rows against D1's 100,000 writes per day.
3. Supabase pausing an idle project (a reliability problem, not a cost).
4. Workers' 10 ms CPU limit if heavy statistics are moved into a Worker.
5. Clerk's paid-only features (MFA, more than 3 social providers, SMS).

## Suggested next steps

1. Deploy the static demo (30 minutes) and add `--cta-url` with a form, so customer-discovery interviews have a place to land.
2. Put Access in front of a second, private deployment of the same site for named pilot contacts.
3. When the first pilot store has footage: build the three small pieces of stage 2 (`snapshot` command, `/ingest`, `/data`).
4. Decide on stage 3 only when a customer needs date-range queries or self-serve sign-up.
