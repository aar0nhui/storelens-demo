import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPair, SignJWT } from "jose";
import { createWorker } from "../src/index.js";
import { scanPayload, validateEnvelope } from "../src/guard.js";

const TEAM = "https://acme.cloudflareaccess.com";
const { publicKey, privateKey } = await generateKeyPair("RS256");
const other = await generateKeyPair("RS256");

class FakeR2 {
  constructor() { this.m = new Map(); }
  async put(k, v, o = {}) { this.m.set(k, { v: String(v), meta: o.customMetadata || {} }); }
  async head(k) { const e = this.m.get(k); return e ? { customMetadata: e.meta } : null; }
  async get(k) { const e = this.m.get(k); return e ? { text: async () => e.v } : null; }
}

function makeEnv(over = {}) {
  return {
    STORE_ID: "store-1", TEAM_DOMAIN: TEAM, APP_AUD: "app-aud", INGEST_AUD: "ing-aud",
    ALLOWED_EMAILS: "Owner@Example.com, me@example.com", INGEST_CLIENT_IDS: "abc123.access",
    SNAPSHOTS: new FakeR2(),
    ASSETS: { fetch: async () => new Response("<html>asset</html>", { headers: { "Content-Type": "text/html" } }) },
    ...over,
  };
}
const worker = createWorker({ keyFor: () => publicKey });

async function jwt(claims, { aud = "app-aud", iss = TEAM, key = privateKey, exp = "5m" } = {}) {
  return new SignJWT(claims).setProtectedHeader({ alg: "RS256", kid: "k1" }).setIssuer(iss).setAudience(aud)
    .setIssuedAt().setExpirationTime(exp).sign(key);
}
const userTok = (over, email = "owner@example.com") => jwt({ email, type: "app" }, over);
const svcTok = (over, cn = "abc123.access") => jwt({ common_name: cn, type: "app", sub: "" }, { aud: "ing-aud", ...over });
const req = (path, { method = "GET", token, body, headers = {} } = {}) => new Request("https://app.example.com" + path, {
  method, body, headers: { ...(token ? { "cf-access-jwt-assertion": token } : {}), ...headers } });

function envelope(over = {}, snap = { "/api/summary": { visitors: 12 }, "/api/config": { store: "x" } }) {
  return { schema_version: 1, store_id: "store-1", generated_at: new Date().toISOString(), layout_version: "1", snapshot: snap, ...over };
}
function post(env_, token, extra = {}) {
  const body = JSON.stringify(env_);
  return req("/ingest", { method: "POST", token, body, headers: { "content-type": "application/json", "content-length": String(new TextEncoder().encode(body).length), ...extra } });
}

test("no token: dashboard, data and ingest all refuse", async () => {
  const env = makeEnv();
  for (const p of ["/", "/index.html", "/data/snapshot.js", "/whoami"]) assert.equal((await worker.fetch(req(p), env)).status, 403, p);
  assert.equal((await worker.fetch(post(envelope()), env)).status, 403);
});

test("token problems: wrong audience, issuer, signature, expiry", async () => {
  const env = makeEnv();
  for (const tok of [await userTok({ aud: "other" }), await userTok({ iss: "https://evil.cloudflareaccess.com" }),
                     await userTok({ key: other.privateKey }), await userTok({ exp: "-1m" }), "garbage"]) {
    assert.equal((await worker.fetch(req("/", { token: tok }), env)).status, 403);
  }
});

test("allow-list: case-insensitive e-mail match, others refused, service tokens do not open the dashboard", async () => {
  const env = makeEnv();
  assert.equal((await worker.fetch(req("/", { token: await userTok({}, "OWNER@example.com") }), env)).status, 200);
  assert.equal((await worker.fetch(req("/", { token: await userTok({}, "stranger@example.com") }), env)).status, 403);
  const svcOnDashboard = await jwt({ common_name: "abc123.access", type: "app" }, { aud: "app-aud" });
  assert.equal((await worker.fetch(req("/", { token: svcOnDashboard }), env)).status, 403);
});

test("a person's token cannot ingest, and a service token with the wrong client id cannot either", async () => {
  const env = makeEnv();
  const personOnIngest = await jwt({ email: "owner@example.com" }, { aud: "ing-aud" });
  assert.equal((await worker.fetch(post(envelope(), personOnIngest), env)).status, 403);
  assert.equal((await worker.fetch(post(envelope(), await svcTok({}, "someone-else.access")), env)).status, 403);
  const wrongAud = await jwt({ common_name: "abc123.access" }, { aud: "app-aud" });
  assert.equal((await worker.fetch(post(envelope(), wrongAud), env)).status, 403);
});

test("fails closed when configuration is missing or malformed", async () => {
  for (const over of [{ ALLOWED_EMAILS: "" }, { APP_AUD: "" }, { TEAM_DOMAIN: "https://acme.cloudflareaccess.com/" }, { TEAM_DOMAIN: "http://acme.example.com" }]) {
    assert.equal((await worker.fetch(req("/", { token: await userTok({}) }), makeEnv(over))).status, 500, JSON.stringify(over));
  }
  assert.equal((await worker.fetch(post(envelope(), await svcTok({})), makeEnv({ INGEST_CLIENT_IDS: "" }))).status, 500);
});

test("ingest stores history and latest; the dashboard then reads it back as a script", async () => {
  const env = makeEnv();
  const res = await worker.fetch(post(envelope(), await svcTok({})), env);
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.latest_updated, true);
  assert.ok(env.SNAPSHOTS.m.has("store-1/latest.json"));
  assert.ok([...env.SNAPSHOTS.m.keys()].some((k) => k.startsWith("store-1/history/")));
  const js = await worker.fetch(req("/data/snapshot.js", { token: await userTok({}) }), env);
  assert.equal(js.status, 200);
  assert.match(js.headers.get("content-type"), /javascript/);
  assert.equal(js.headers.get("cache-control"), "private, no-store");
  const text = await js.text();
  assert.match(text, /^window\.__STORELENS_SNAPSHOT__ = \{"\/api\/summary":\{"visitors":12\}/);
  const meta = await (await worker.fetch(req("/data/meta.json", { token: await userTok({}) }), env)).json();
  assert.equal(meta.has_data, true);
});

test("before any data arrives the dashboard gets an explicit empty marker", async () => {
  const js = await (await worker.fetch(req("/data/snapshot.js", { token: await userTok({}) }), makeEnv())).text();
  assert.equal(js.trim(), 'window.__STORELENS_SNAPSHOT__ = {"__empty__":true};');
});

test("an older snapshot is kept in history but does not replace the latest", async () => {
  const env = makeEnv();
  const t = Date.now();
  await worker.fetch(post(envelope({ generated_at: new Date(t).toISOString() }), await svcTok({})), env);
  const old = await (await worker.fetch(post(envelope({ generated_at: new Date(t - 3600e3).toISOString() }, { "/api/summary": { visitors: 1 } }), await svcTok({})), env)).json();
  assert.equal(old.latest_updated, false);
  assert.match(await (await env.SNAPSHOTS.get("store-1/latest.json")).text(), /"visitors":12/);
});

test("ingest validation: store, size, JSON, clock, view names", async () => {
  const env = makeEnv();
  const tok = await svcTok({});
  assert.equal((await worker.fetch(post(envelope({ store_id: "other-store" }), tok), env)).status, 403);
  assert.equal((await worker.fetch(post(envelope({ generated_at: new Date(Date.now() + 3600e3).toISOString() }), tok), env)).status, 400);
  assert.equal((await worker.fetch(post(envelope({ schema_version: 2 }), tok), env)).status, 400);
  assert.equal((await worker.fetch(post(envelope({}, { "/secret": {} }), tok), env)).status, 400);
  assert.equal((await worker.fetch(post(envelope({}, {}), tok), env)).status, 400);
  const bad = req("/ingest", { method: "POST", token: tok, body: "{nope", headers: { "content-length": "5" } });
  assert.equal((await worker.fetch(bad, env)).status, 400);
  const big = req("/ingest", { method: "POST", token: tok, body: "x", headers: { "content-length": String(9 * 1024 * 1024) } });
  assert.equal((await worker.fetch(big, env)).status, 413);
  const noLen = new Request("https://app.example.com/ingest", { method: "POST", body: "{}", headers: { "cf-access-jwt-assertion": tok } });
  assert.ok([411, 400].includes((await worker.fetch(noLen, env)).status));
  assert.equal((await worker.fetch(req("/ingest", { token: tok }), env)).status, 405);
  assert.equal(env.SNAPSHOTS.m.size, 0, "nothing stored for any rejected request");
});

test("ingest refuses per-person or image-like data", async () => {
  const env = makeEnv();
  const tok = await svcTok({});
  const cases = [
    { "/api/zones": [{ visit_key: "ab12", dwell: 3 }] },
    { "/api/zones": { embedding: [0.1, 0.2] } },
    { "/api/live": { camera: "data:video/mp4;base64,AAAA" } },
    { "/api/x": { note: "A".repeat(5000) } },
  ];
  for (const snap of cases) assert.equal((await worker.fetch(post(envelope({}, snap), tok), env)).status, 422, JSON.stringify(snap).slice(0, 50));
  assert.equal(env.SNAPSHOTS.m.size, 0);
});

test("static files come through with security headers; writes are refused", async () => {
  const env = makeEnv();
  const res = await worker.fetch(req("/index.html", { token: await userTok({}) }), env);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-content-type-options"), "nosniff");
  assert.match(res.headers.get("content-security-policy"), /frame-ancestors 'none'/);
  assert.equal((await worker.fetch(req("/index.html", { method: "POST", token: await userTok({}), body: "x" }), env)).status, 405);
  const who = await (await worker.fetch(req("/whoami", { token: await userTok({}) }), env)).json();
  assert.equal(who.email, "owner@example.com");
});

test("guard: scanPayload and validateEnvelope in isolation", () => {
  assert.deepEqual(scanPayload({ a: [{ b: 1 }], image_size: [640, 480] }), []);  // 'image_size' is a config field, not an image
  assert.equal(scanPayload({ a: { Frame: 1 } }).length, 1);                      // case-insensitive
  const ok = validateEnvelope(envelope(), { storeId: "store-1" });
  assert.equal(ok.ok, true);
});
