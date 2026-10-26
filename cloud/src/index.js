// Glance private app. Routes (every one needs a valid Cloudflare Access JWT, checked here and not only at the edge):
//   POST /ingest             store's computer -> snapshot JSON (service token)         -> R2
//   GET  /data/snapshot.js   dashboard data as the script the app already loads    (e-mail login)
//   GET  /data/meta.json     when the data was generated
//   GET  /whoami             who Access says you are
//   anything else            the dashboard's static files (env.ASSETS)
import { verifyAccess, remoteKeys } from "./auth.js";
import { validateEnvelope } from "./guard.js";

const MAX_BYTES = 8 * 1024 * 1024;
const HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Content-Security-Policy": "frame-ancestors 'none'",
  "Cache-Control": "private, no-store",
  "X-Robots-Tag": "noindex",
};

function withHeaders(res, extra = {}) {
  const r = new Response(res.body, res);
  for (const [k, v] of Object.entries({ ...HEADERS, ...extra })) r.headers.set(k, v);
  return r;
}
const json = (obj, status = 200) =>
  withHeaders(new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json; charset=utf-8" } }));
const deny = (a) => json({ error: a.reason }, a.status);

async function readLimited(request, max) {
  const declared = Number(request.headers.get("content-length"));
  if (!Number.isFinite(declared) || declared <= 0) return { error: "Content-Length required", status: 411 };
  if (declared > max) return { error: "payload too large", status: 413 };
  const buf = await request.arrayBuffer();
  if (buf.byteLength > max) return { error: "payload too large", status: 413 };
  return { buf };
}

async function ingest(request, env, keyFor) {
  const a = await verifyAccess(request, env, "service", keyFor);
  if (!a.ok) return deny(a);
  if (request.method !== "POST") return json({ error: "POST only" }, 405);
  const body = await readLimited(request, MAX_BYTES);
  if (body.error) return json({ error: body.error }, body.status);
  let env_;
  try {
    env_ = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body.buf));
  } catch (e) {
    return json({ error: "body is not valid UTF-8 JSON" }, 400);
  }
  const v = validateEnvelope(env_, { storeId: env.STORE_ID });
  if (!v.ok) return json({ error: v.error }, v.status);

  const store = env.STORE_ID;
  const iso = new Date(v.generatedMs).toISOString();
  const text = JSON.stringify(env_.snapshot);
  const meta = { generated_at: iso, layout_version: env_.layout_version, bytes: String(text.length), received_at: new Date().toISOString() };
  // history first (never overwritten), then 'latest' only if this snapshot is newer than the one stored
  const histKey = `${store}/history/${iso.slice(0, 10)}/${iso.replace(/[:.]/g, "-")}.json`;
  const opts = { httpMetadata: { contentType: "application/json" }, customMetadata: meta };
  await env.SNAPSHOTS.put(histKey, text, opts);
  const cur = await env.SNAPSHOTS.head(`${store}/latest.json`);
  const curMs = cur?.customMetadata?.generated_at ? Date.parse(cur.customMetadata.generated_at) : -Infinity;
  const updated = v.generatedMs > curMs;
  if (updated) await env.SNAPSHOTS.put(`${store}/latest.json`, text, opts);
  return json({ ok: true, latest_updated: updated, history_key: histKey, bytes: text.length, generated_at: iso });
}

async function data(request, env, url) {
  if (request.method !== "GET" && request.method !== "HEAD") return json({ error: "GET only" }, 405);
  const key = `${env.STORE_ID}/latest.json`;
  if (url.pathname === "/data/meta.json") {
    const h = await env.SNAPSHOTS.head(key);
    return json(h ? { ...h.customMetadata, has_data: true } : { has_data: false });
  }
  const obj = await env.SNAPSHOTS.get(key);
  // The page treats {"__empty__":true} as "nothing has arrived yet" and says so instead of showing broken charts.
  const snap = obj ? await obj.text() : '{"__empty__":true}';
  return withHeaders(new Response(`window.__GLANCE_SNAPSHOT__ = ${snap};\n`, {
    headers: { "Content-Type": "application/javascript; charset=utf-8" } }));
}

export function createWorker({ keyFor = remoteKeys } = {}) {
  return {
    async fetch(request, env) {
      const url = new URL(request.url);
      try {
        if (url.pathname === "/ingest") return await ingest(request, env, keyFor);
        const a = await verifyAccess(request, env, "user", keyFor);
        if (!a.ok) return deny(a);
        if (url.pathname === "/whoami") return json({ email: a.identity, store: env.STORE_ID });
        if (url.pathname.startsWith("/data/")) return await data(request, env, url);
        if (request.method !== "GET" && request.method !== "HEAD") return json({ error: "GET only" }, 405);
        return withHeaders(await env.ASSETS.fetch(request), { "Cache-Control": "private, max-age=300" });
      } catch (e) {
        console.error("unhandled", e && e.stack ? e.stack : String(e));
        return json({ error: "internal error" }, 500);
      }
    },
  };
}

export default createWorker();
