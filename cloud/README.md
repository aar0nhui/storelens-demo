# Glance private app (Cloudflare Worker)

The dashboard for ONE customer: static files plus `/ingest` and `/data`, all behind Cloudflare Access, with data kept in
an R2 bucket. Setup steps, the two Access applications, the variables in `wrangler.jsonc` and the tests are in
[`../docs/cloud.md`](../docs/cloud.md).

Deploy from this folder only (`npx wrangler deploy`). The `wrangler.jsonc` in the repo root is the public demo and must
never receive real data.
