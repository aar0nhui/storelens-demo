// Cloudflare Access JWT verification (Cf-Access-Jwt-Assertion). Fail closed: missing configuration is an error, not a pass.
// Two Access applications share this Worker's hostname, each with its own AUD tag:
//   dashboard app (whole host)  -> people, identified by e-mail          -> APP_AUD,    ALLOWED_EMAILS
//   ingest app   (path /ingest) -> the store's computer, a service token -> INGEST_AUD, INGEST_CLIENT_IDS
import { createRemoteJWKSet, jwtVerify } from "jose";

const TEAM_RE = /^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/;
const jwksCache = new Map();

/** Default key source: the team's public keys, cached per Worker isolate by jose. */
export function remoteKeys(env) {
  let jwks = jwksCache.get(env.TEAM_DOMAIN);
  if (!jwks) {
    jwks = createRemoteJWKSet(new URL(`${env.TEAM_DOMAIN}/cdn-cgi/access/certs`));
    jwksCache.set(env.TEAM_DOMAIN, jwks);
  }
  return jwks;
}

const list = (s) => String(s || "").split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);

/**
 * @param {"user"|"service"} kind
 * @returns {Promise<{ok:true, identity:string, payload:object} | {ok:false, status:number, reason:string}>}
 */
export async function verifyAccess(request, env, kind, keyFor = remoteKeys) {
  const aud = kind === "user" ? env.APP_AUD : env.INGEST_AUD;
  const allowed = list(kind === "user" ? env.ALLOWED_EMAILS : env.INGEST_CLIENT_IDS);
  if (!TEAM_RE.test(env.TEAM_DOMAIN || "") || !aud || !allowed.length || !env.STORE_ID) {
    console.error("misconfigured: need TEAM_DOMAIN (https://<team>.cloudflareaccess.com), STORE_ID, " +
      (kind === "user" ? "APP_AUD and ALLOWED_EMAILS" : "INGEST_AUD and INGEST_CLIENT_IDS"));
    return { ok: false, status: 500, reason: "server not configured" };
  }
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return { ok: false, status: 403, reason: "missing Access token" };
  let payload;
  try {
    ({ payload } = await jwtVerify(token, keyFor(env), { issuer: env.TEAM_DOMAIN, audience: aud }));
  } catch (e) {
    return { ok: false, status: 403, reason: "invalid Access token" };
  }
  if (kind === "user") {
    const email = typeof payload.email === "string" ? payload.email.toLowerCase() : "";
    if (!email || !allowed.includes(email)) return { ok: false, status: 403, reason: "not on the allow-list" };
    return { ok: true, identity: email, payload };
  }
  const cn = typeof payload.common_name === "string" ? payload.common_name.toLowerCase() : "";
  if (!cn || payload.email || !allowed.includes(cn)) return { ok: false, status: 403, reason: "not an allowed service token" };
  return { ok: true, identity: cn, payload };
}
