# Deploy the static demo (free)

Goal: a public URL anyone can open, updated by pushing to GitHub. No server, no database, no cost.

The demo is the same dashboard you run locally, frozen on **simulated** data: every default view is precomputed into
`site/data/snapshot.js`, and the two live-view clips are plain MP4 files. Nothing is requested from any other domain,
nothing is stored in the browser except the sidebar open/closed preference and the light/dark choice, and the page
ships with a strict Content-Security-Policy.

## 1. What gets deployed

```
site/                      10 files, 3.1 MB (largest: media/camera.mp4, 1.8 MB)
  index.html               the dashboard, with a CSP <meta> tag and relative URLs (also works under /some/sub/path/)
  static/app.js, style.css the dashboard code
  data/snapshot.js         every default view as JSON, built from demo_data/
  media/*.mp4              simulated live view (store camera + live floor plan)
  _headers                 security headers (read by Cloudflare; ignored by other hosts)
  404.html robots.txt .nojekyll
```

`site/` is generated from `demo_data/` (a 90-minute simulated week pair, 1.2 MB database plus the clips) by
`make site`. You commit the generated folder, so the host needs **no build step** and no Python.

## 2. Build and check locally

```bash
pip install -e ".[dev]"
make site                              # same as: glance site --config demo_data/store.yaml --db demo_data/demo.db --out site
python -m http.server -d site 8000     # open http://localhost:8000
```

Useful flags on `glance site`: `--demo-label "Demo · simulated data"` (header chip, set by `make site`),
`--cta-url https://forms.gle/... --cta-text "Request a pilot"` (a header button for customer-discovery sign-ups; also
accepts `mailto:`), and `--noindex` (asks search engines not to list it; this is not access control).

## 3. Put it on GitHub

```bash
git init -b main
git add .                              # .gitignore keeps raw simulated video, weights and caches out (7 MB tracked in total)
git commit -m "Glance MVP + static demo"
gh repo create glance --public --source . --push     # or create it on github.com and `git push -u origin main`
```

Rules for this repo: it may hold code, docs, the simulated `demo_data/` and the built `site/`. It must **never** hold real
footage, real store databases or API keys. If the repo might ever hold anything sensitive, keep it private: Cloudflare
deploys from private repos, GitHub Pages does not (on the free plan).

## 4. Connect Cloudflare

You asked for Cloudflare Pages, and it works for this. One thing changed: Cloudflare's docs now say **"Start new projects
with Workers"** (Workers static assets) and describe Pages as the older path, without announcing a shutdown. Both are free
for this site and serve the identical `site/` folder, so the choice is cheap to reverse. Recommendation: use Workers,
because the backend in `docs/backend-options.md` (ingest endpoint, auth check) will be Workers code too, and then one
project holds the pages and the API. Pick Pages if you prefer the simplest dashboard flow.

**Option A: Workers static assets (recommended).** The repo already contains `wrangler.jsonc` pointing at `./site`.
In the Cloudflare dashboard go to *Workers & Pages → Create application*, import the GitHub repo, leave the build command
empty and keep the default deploy command (`npx wrangler deploy`). Labels in the dashboard change from time to time;
follow the prompts. Check it locally first with `npx wrangler dev` (we did: pages, 404 page and headers behave).

**Option B: Pages.** *Workers & Pages → Create application → Pages → Import an existing Git repository.*
Production branch `main`, build command `exit 0`, build output directory `site`. You get `<project>.pages.dev`; every pull
request gets a preview deployment. (`wrangler.jsonc` is only used for local development on Pages.)

## 5. Updating the demo

1. Change code or data, run `make site`, commit, push to `main`. Cloudflare redeploys automatically.
2. New simulated data: `make demo-data` (a few minutes, writes ~150 MB of temporary video that is git-ignored), then `make site`.
3. `.github/workflows/ci.yml` runs the tests and a trial `glance site` build on every push and pull request. It does
   not deploy; Cloudflare does that. (Written for GitHub Actions, not yet run on GitHub: check the action versions.)

A custom domain is free on Pages (100 per project). On Workers, custom domains need the domain's DNS to be on Cloudflare.
Otherwise use the free `*.pages.dev` / `*.workers.dev` address.

## 6. Before you share the link

* **It is synthetic and says so.** The header chip reads "Demo · simulated data" and the store is "Demo Store (synthetic)".
  Do not remove this: nothing in the demo is a real person or store, and visitors should not mistake it for results.
* **Add a way to reach you.** Use `--cta-url` with a Google Form or a `mailto:` link. That is a complete customer-discovery
  funnel with no backend.
* **Privacy claims.** "Anonymous · no faces stored" describes what this pipeline stores. For a real deployment, store
  signage, a data-protection assessment and local law apply; see `docs/privacy-and-legal.md` (not legal advice).
* **Need it private?** Put Cloudflare Access in front (see `docs/backend-options.md`). `--noindex` only hides it from search.

## 7. Limits and costs (checked against vendor docs, Oct 2026)

| Service | Free allowance relevant here | This site |
|---|---|---|
| Cloudflare Pages | 500 builds/month, 20,000 files, 25 MiB per file, 100 custom domains per project ([limits](https://developers.cloudflare.com/pages/platform/limits/)) | no builds (prebuilt), 10 files, largest 1.8 MB |
| Workers static assets | static asset requests are free and unlimited, 20,000 assets, 25 MiB per file ([billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)) | 10 assets |
| Workers Builds (Git-connected builds) | 3,000 build minutes/month, 1 concurrent build, 20 min timeout ([limits](https://developers.cloudflare.com/workers/ci-cd/builds/limits-and-pricing/)) | seconds per deploy |
| GitHub Pages (alternative) | public repos only on the free plan, 1 GB site, 100 GB/month soft bandwidth, 10 builds/hour soft ([limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)) | fine technically |

GitHub Pages is the weaker fit for a product: its documentation says it is not for sites primarily about commercial
transactions or selling software as a service, and it needs a public repo. I did not review Cloudflare's terms; read them
before using the demo as a sales page.

What I verified: the build, the CSP, relative URLs under a sub-path, the click-through in a browser, and `wrangler dev`
serving the folder with the `_headers` applied. What I did not do: deploy to Cloudflare (that needs your account),
run the GitHub Actions workflow, or confirm that the real CDN answers video Range requests (the clips are small, so it works
either way).
