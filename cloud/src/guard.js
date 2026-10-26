// Payload guard: the cloud only ever stores aggregates. A snapshot that carries per-person or image data is rejected,
// even though the store's computer already checked (src/glance/cloudsync.py: keep the two lists in sync).

export const FORBIDDEN_KEYS = new Set([
  "embedding", "embeddings", "face", "faces", "face_crop", "crop", "crops", "thumbnail", "thumbnails", "landmark",
  "landmarks", "descriptor", "descriptors", "biometric", "biometrics", "image", "images", "frame", "frames",
  "jpeg", "png", "trajectory", "trajectories", "track_id", "visit_key", "visit_keys", "uid",
]);
export const MAX_STRING = 4000;
const MAX_DEPTH = 14;

/** @returns {string[]} problems found (empty = fine) */
export function scanPayload(value, path = "$", depth = 0, problems = []) {
  if (problems.length >= 20 || depth > MAX_DEPTH) return problems;
  if (Array.isArray(value)) {
    for (let i = 0; i < Math.min(value.length, 5000); i++) scanPayload(value[i], `${path}[${i}]`, depth + 1, problems);
  } else if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) {
      if (FORBIDDEN_KEYS.has(k.toLowerCase())) problems.push(`${path}.${k}: key name suggests per-person or image data`);
      scanPayload(v, `${path}.${k}`, depth + 1, problems);
    }
  } else if (typeof value === "string") {
    if (value.startsWith("data:image") || value.startsWith("data:video") || value.length > MAX_STRING) {
      problems.push(`${path}: ${value.length}-character string / embedded media`);
    }
  }
  return problems;
}

const ID_RE = /^[a-z0-9][a-z0-9_-]{1,62}$/;

/** Check the envelope the store's computer sends. Returns {ok, error?, status?}. */
export function validateEnvelope(env, { storeId, now = Date.now() }) {
  if (!env || typeof env !== "object" || Array.isArray(env)) return { ok: false, status: 400, error: "body must be a JSON object" };
  if (env.schema_version !== 1) return { ok: false, status: 400, error: "unsupported schema_version" };
  if (typeof env.store_id !== "string" || !ID_RE.test(env.store_id)) return { ok: false, status: 400, error: "bad store_id" };
  if (env.store_id !== storeId) return { ok: false, status: 403, error: "this app does not accept data for that store" };
  const t = Date.parse(env.generated_at);
  if (!Number.isFinite(t)) return { ok: false, status: 400, error: "generated_at must be an ISO 8601 time" };
  if (t > now + 10 * 60 * 1000) return { ok: false, status: 400, error: "generated_at is in the future (check the store computer's clock)" };
  if (typeof env.layout_version !== "string" || env.layout_version.length > 40) return { ok: false, status: 400, error: "bad layout_version" };
  const snap = env.snapshot;
  if (!snap || typeof snap !== "object" || Array.isArray(snap)) return { ok: false, status: 400, error: "snapshot must be an object" };
  const keys = Object.keys(snap);
  if (keys.length === 0 || keys.length > 400) return { ok: false, status: 400, error: "snapshot has no views or too many" };
  if (!keys.every((k) => k.startsWith("/api/"))) return { ok: false, status: 400, error: "snapshot keys must be /api/... views" };
  const problems = scanPayload(env);
  if (problems.length) return { ok: false, status: 422, error: "payload rejected: " + problems.slice(0, 3).join("; ") };
  return { ok: true, generatedMs: t };
}
