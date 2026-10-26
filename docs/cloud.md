# Private cloud app (one customer)

The public demo (`site/`, simulated data) and the private app (`cloud/`, real data) are separate. Never put real data in
the public project, and never run `wrangler deploy` from the repo root: the root `wrangler.jsonc` is the public demo.

```
store computer ──(HTTPS POST, Access service token)──▶ Worker /ingest ──▶ R2 bucket (snapshots)
                                                            ▲
customer's browser ──(Cloudflare Access login)──▶ Worker: dashboard files + /data/snapshot.js
```

The store computer **pushes**; the cloud never reaches into the store network. What it pushes is the dashboard's
pre-computed answers (the same aggregates the dashboard shows), not video, frames, tracks or visit keys. The pusher
checks the payload before it leaves and the Worker checks it again: a bug that put an image, an embedding or a visit key
into the snapshot is stopped at both ends. Heavy statistics (bootstrap intervals, recommendations) stay in Python on the
store computer, so the Worker needs no CPU-hungry code and stays on the free plan.

## One-time setup (about 30 minutes)

1. **Cloudflare Zero Trust**: choose a team name (`<team>.cloudflareaccess.com`). The free plan covers up to 50 users.
2. **R2**: create a bucket named `storelens-snapshots` (free tier: 10 GB).
3. **Deploy once** so the Worker has a URL: edit `cloud/wrangler.jsonc` (`STORE_ID`, `TEAM_DOMAIN`), then
   `cd cloud && npm ci && npx wrangler deploy`. Until the Access apps below exist, every request returns 403/500
   (it fails closed).
4. **Access application 1, the dashboard**: whole hostname, a policy that allows the customer's email (and yours), identity
   by one-time PIN or their Google/Microsoft login. Copy its **AUD tag** into `APP_AUD`; list the allowed emails in
   `ALLOWED_EMAILS` (comma separated).
5. **Access application 2, ingest**: the same hostname with path `/ingest`, with a **Service Auth** policy. Create a service
   token (Zero Trust > Access > Service credentials); the secret is shown once. Copy this app's AUD tag into `INGEST_AUD`
   and the token's client id into `INGEST_CLIENT_IDS`. In the dashboard, check that the path app takes precedence over the
   whole-host app (Cloudflare applies the more specific path first, but confirm it in the Access policy tester).
6. **Build the app shell and redeploy**:
   ```bash
   glance site --private --out cloud/public     # app shell only: no data, no video clips
   cd cloud && npx wrangler deploy
   ```
7. **On the store computer**:
   ```bash
   export CF_ACCESS_CLIENT_ID=...  CF_ACCESS_CLIENT_SECRET=...      # keep with 600 permissions or in the OS keyring
   glance snapshot --config store.yaml --db store.db --store-id acme-la --out snapshot.json
   glance push --snapshot snapshot.json --url https://<app>/ingest [--dry-run]
   ```
   Run it from cron or a systemd timer after each processing batch. A push retries connection errors and server errors
   with backoff and stops on a 4xx (fix the cause). `--dry-run` shows what would be sent.

## What the Worker checks

* Every route, including static files, runs through the Worker first (`run_worker_first`), and the Worker verifies the
  `Cf-Access-Jwt-Assertion` token itself (signature against the team's public keys, issuer, audience) so security does
  not depend only on the edge rule. Missing configuration is an error, not a pass.
* People must be on `ALLOWED_EMAILS`. The store computer must present a service-token JWT whose `common_name` is in
  `INGEST_CLIENT_IDS`; a person's token is refused on `/ingest` and a service token is refused on the dashboard.
* `/ingest` needs `Content-Length`, refuses bodies over 8 MB, requires valid UTF-8 JSON, the right `store_id` and schema
  version, a `generated_at` that is not in the future, snapshot keys that are all `/api/...` views, and no key names that suggest per-person or image data. Older snapshots never replace a
  newer `latest`, and every accepted snapshot is also kept under `history/` so nothing is overwritten.
* Responses carry `Cache-Control: private, no-store` for data, `X-Robots-Tag: noindex`, `nosniff`, no referrer, and
  `frame-ancestors 'none'`. (The dashboard's own strict Content-Security-Policy comes from the page.)

## Running the tests

```bash
cd cloud && npm test                      # Worker unit tests (JWT, allow-lists, guard, R2 ordering)
python -m pytest tests/test_cloud_e2e.py  # real demo snapshot -> the actual Worker code, pusher retries, refusals
```

Both use a stubbed key set instead of Cloudflare's, so they prove the logic, not the Access configuration. After setup,
test it for real: an unlisted email must be refused, the same URL without login must redirect to Access, and a push with
a wrong secret must return 403.

## Off-boarding and logs

Remove the email from the Access policy and `ALLOWED_EMAILS`, and revoke or rotate the service token (rotation has a grace
period where both secrets work). Access logs record logins; keep them for the retention period you document. The store
computer's disk should use full-disk encryption.

## What is not done

Multiple customers (add an email-to-store table in D1 or KV when customer two arrives), a self-serve camera wizard, and an
automatic push schedule. None is needed for one store.
