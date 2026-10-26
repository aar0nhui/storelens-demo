// Local stand-in for "Cloudflare Access + the Worker", used by tests/test_cloud_e2e.py:
// requests carrying the right service-token headers get a service JWT, requests with X-Test-User get that user's JWT,
// the rest get none (so the Worker must refuse them). Prints {"port": N} when ready. NOT for production use.
import http from "node:http";
import { generateKeyPair, SignJWT } from "jose";
import { createWorker } from "../src/index.js";

const TEAM = "https://test.cloudflareaccess.com";
const CID = "testclient.access", SECRET = "testsecret";
const { publicKey, privateKey } = await generateKeyPair("RS256");
const mem = new Map();
const env = {
  STORE_ID: "demo-store", TEAM_DOMAIN: TEAM, APP_AUD: "app-aud", INGEST_AUD: "ing-aud",
  ALLOWED_EMAILS: "owner@example.com", INGEST_CLIENT_IDS: CID,
  SNAPSHOTS: {
    async put(k, v, o = {}) { mem.set(k, { v: String(v), meta: o.customMetadata || {} }); },
    async head(k) { const e = mem.get(k); return e ? { customMetadata: e.meta } : null; },
    async get(k) { const e = mem.get(k); return e ? { text: async () => e.v } : null; },
  },
  ASSETS: { fetch: async () => new Response("asset") },
};
const worker = createWorker({ keyFor: () => publicKey });
const sign = (claims, aud) => new SignJWT(claims).setProtectedHeader({ alg: "RS256" }).setIssuer(TEAM).setAudience(aud)
  .setIssuedAt().setExpirationTime("5m").sign(privateKey);

const server = http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) if (k !== "cf-access-jwt-assertion") headers.set(k, String(v));
  if (req.headers["cf-access-client-id"] === CID && req.headers["cf-access-client-secret"] === SECRET)
    headers.set("cf-access-jwt-assertion", await sign({ common_name: CID, type: "app", sub: "" }, "ing-aud"));
  else if (req.headers["x-test-user"])
    headers.set("cf-access-jwt-assertion", await sign({ email: req.headers["x-test-user"], type: "app" }, "app-aud"));
  const body = chunks.length ? Buffer.concat(chunks) : undefined;
  const r = await worker.fetch(new Request(`http://127.0.0.1${req.url}`, { method: req.method, headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : body }), env);
  res.writeHead(r.status, Object.fromEntries(r.headers));
  res.end(Buffer.from(await r.arrayBuffer()));
});
server.listen(0, "127.0.0.1", () => console.log(JSON.stringify({ port: server.address().port, client_id: CID, client_secret: SECRET })));
