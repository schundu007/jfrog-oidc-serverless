// JFrog OIDC client for Node.js on Azure App Service (Node 18+, no dependencies).
//
// The exchange, as jfrog_oidc does it: the App Service managed identity token for
// JFROG_OIDC_AUDIENCE (IDENTITY_ENDPOINT + IDENTITY_HEADER; AZURE_CLIENT_ID picks the identity)
// is exchanged at JFrog for a short-lived access token, cached until a minute before it expires.
// Nothing here ever returns, prints or logs a token, except token() to the caller.
//
//   const { Jfrog } = require("./jfrog");
//   const jfrog = Jfrog.fromEnv();                 // once, at startup
//   const { value } = await jfrog.token();          // per call: cached, refreshed for you
//   fetch(url, { headers: { Authorization: `Bearer ${value}` } });
"use strict";

const EXCHANGE_PATH = "/access/api/v1/oidc/token";
const CLAIM_KEYS = ["iss", "aud", "sub", "oid", "tid", "azp", "email", "ver", "iat", "exp"];

class JfrogFailure extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "JfrogFailure";
    this.status = status;
  }
}

const detail = (body) => (body.length > 500 ? body.slice(0, 500) : body);
const iso = (secs) => new Date(secs * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

function claims(jwt) {
  const parts = String(jwt || "").split(".");
  if (parts.length < 2) return {};
  try {
    return JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) || {};
  } catch {
    return {};
  }
}

function usernameFromSubject(sub) {
  const k = String(sub || "").indexOf("/users/");
  return k >= 0 ? sub.slice(k + "/users/".length) : String(sub || "");
}

const encodePath = (p) => p.split("/").map(encodeURIComponent).join("/");

class Jfrog {
  constructor(env, { fetchImpl = globalThis.fetch, clock = () => Math.floor(Date.now() / 1000), timeoutMs = 30000 } = {}) {
    this.env = env;
    this.fetch = fetchImpl;
    this.clock = clock;
    this.timeoutMs = timeoutMs;
    this.url = (env.JFROG_URL || "").replace(/\/+$/, "");
    this.provider = env.JFROG_OIDC_PROVIDER || "";
    this.audience = env.JFROG_OIDC_AUDIENCE || "";
    this.cached = null;
    this.pending = null;
  }

  static fromEnv() {
    return new Jfrog(process.env);
  }

  configured() {
    const missing = ["JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE"].filter((k) => !this.env[k]);
    if (missing.length) throw new JfrogFailure(`missing environment variables: ${missing.join(", ")}`);
    if (!this.url.startsWith("https://") && !this.env.JFROG_OIDC_ALLOW_HTTP) throw new JfrogFailure("JFROG_URL must be https");
  }

  async send(url, init, what) {
    try {
      // redirect: "manual" so the Authorization header never follows a redirect elsewhere.
      return await this.fetch(url, { ...init, redirect: "manual", signal: AbortSignal.timeout(this.timeoutMs) });
    } catch (e) {
      throw new JfrogFailure(`${what} unreachable: ${e.message}`);
    }
  }

  static async json(res, what) {
    const text = await res.text();
    try {
      const v = JSON.parse(text);
      if (v && typeof v === "object") return { v, text };
    } catch {
      /* fall through */
    }
    throw new JfrogFailure(`${what} returned non-JSON`, res.status);
  }

  /** The managed identity's token for the JFrog audience. */
  async identityToken() {
    const endpoint = this.env.IDENTITY_ENDPOINT;
    const header = this.env.IDENTITY_HEADER;
    if (!endpoint || !header) throw new JfrogFailure("no managed identity endpoint (IDENTITY_ENDPOINT/IDENTITY_HEADER): not on App Service?");
    const q = new URLSearchParams({ resource: this.audience, "api-version": "2019-08-01" });
    if (this.env.AZURE_CLIENT_ID) q.set("client_id", this.env.AZURE_CLIENT_ID);
    const res = await this.send(`${endpoint}?${q}`, { headers: { "X-IDENTITY-HEADER": header } }, "managed identity endpoint");
    if (res.status !== 200) throw new JfrogFailure(`managed identity endpoint answered HTTP ${res.status}: ${detail(await res.text())}`, res.status);
    const { v } = await Jfrog.json(res, "managed identity endpoint");
    if (!v.access_token) throw new JfrogFailure("managed identity response missing access_token");
    return v.access_token;
  }

  /** A JFrog token { value, username, expiresAt }, from the cache unless fresh or within a minute of expiry. */
  async token(fresh = false) {
    this.configured();
    const now = this.clock();
    if (!fresh && this.cached && this.cached.expiresAt - now > 60) return this.cached;
    if (!this.pending) this.pending = this.exchange().finally(() => { this.pending = null; });
    return this.pending;
  }

  async exchange() {
    const now = this.clock();
    const subject = await this.identityToken();
    const res = await this.send(this.url + EXCHANGE_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        grant_type: "urn:ietf:params:oauth:grant-type:token-exchange",
        subject_token_type: "urn:ietf:params:oauth:token-type:id_token",
        subject_token: subject,
        provider_name: this.provider,
      }),
    }, "JFrog");
    if (res.status !== 200) throw new JfrogFailure(`JFrog OIDC exchange failed: HTTP ${res.status} provider=${this.provider}: ${detail(await res.text())}`, res.status);
    const { v } = await Jfrog.json(res, "JFrog");
    if (!v.access_token) throw new JfrogFailure("JFrog response missing access_token");
    const c = claims(v.access_token);
    const expiresAt = Number.isFinite(v.expires_in) ? now + v.expires_in : Number.isFinite(c.exp) ? c.exp : now + 3600;
    const username = v.username || usernameFromSubject(c.sub);
    const t = { value: v.access_token, username, expiresAt };
    Object.defineProperty(t, "value", { enumerable: false }); // never in JSON.stringify or console.log
    this.cached = t;
    return t;
  }

  async authed(path) {
    const t = await this.token();
    return this.send(this.url + path, { headers: { Authorization: `Bearer ${t.value}` } }, "JFrog");
  }

  /** GET <repo>/<path>; returns the HTTP status (body discarded). */
  async read(repoPath) {
    const res = await this.authed("/artifactory/" + encodePath(repoPath.replace(/^\/+/, "")));
    await res.arrayBuffer();
    return res.status;
  }

  async claimsOrError() {
    try {
      const all = claims(await this.identityToken());
      return Object.fromEntries(CLAIM_KEYS.filter((k) => k in all).map((k) => [k, all[k]]));
    } catch (e) {
      if (e instanceof JfrogFailure) return { error: e.message };
      throw e;
    }
  }

  /** Same shape as jfrog_oidc.probe.selftest: ok, claims, check and read. */
  async selftest(checkPath, fresh) {
    const r = { ok: false, claims: null, check: null, read: null };
    try {
      this.configured();
    } catch (e) {
      r.check = { error: e.message };
      return r;
    }
    r.claims = await this.claimsOrError();
    let t;
    try {
      t = await this.token(fresh);
    } catch (e) {
      if (!(e instanceof JfrogFailure)) throw e;
      r.check = { error: e.message, provider: this.provider, ...(e.status > 0 ? { status: e.status } : {}) };
      return r;
    }
    r.check = { username: t.username, expires_in: Math.max(0, t.expiresAt - this.clock()), expires_at: iso(t.expiresAt), provider: this.provider, source: "azure" };
    if (!checkPath) {
      r.ok = true;
      return r;
    }
    r.read = { path: checkPath };
    try {
      r.read.status = await this.read(checkPath);
      r.ok = r.read.status === 200;
    } catch (e) {
      if (!(e instanceof JfrogFailure)) throw e;
      r.read.error = e.message;
    }
    return r;
  }

  /** One JSON line for the platform log about a selftest: what JFrog answered, never a token. */
  static logLine(result) {
    const c = result.claims || {};
    const check = result.check || {};
    const read = result.read || {};
    let status = check.status;
    let msg;
    if (check.username) {
      status = 200;
      msg = `JFrog issued a token for ${check.username} via ${check.provider}, expires in ${Math.floor((check.expires_in || 0) / 60)} min`;
      if (read.status != null) msg += `; read ${read.path}: HTTP ${read.status}`;
      else if (read.error) msg += `; read ${read.path} failed: ${read.error}`;
    } else {
      msg = `JFrog refused the exchange via ${check.provider || "its provider"}: ${check.error || "no answer"}`;
    }
    const line = { event: "jfrog_oidc_selftest", ok: result.ok === true, jfrog_status: status ?? null, provider: check.provider ?? null, jfrog_user: check.username ?? null };
    for (const k of ["iss", "sub", "aud"]) if (k in c) line[k] = c[k];
    line.message = msg.slice(0, 600);
    return JSON.stringify(line);
  }

  /** Who this app is in Azure and in JFrog (claims and token times, never the token). */
  async identity() {
    const t = await this.token();
    const c = await this.claimsOrError();
    return {
      cloud_identity: c.email || c.oid || c.sub || null,
      issuer: c.iss ?? null,
      audience: c.aud ?? null,
      jfrog_user: t.username,
      expires_at: iso(t.expiresAt),
      expires_in: Math.max(0, t.expiresAt - this.clock()),
    };
  }

  async repositories() {
    const res = await this.authed("/artifactory/api/repositories");
    if (res.status !== 200) {
      await res.arrayBuffer();
      throw new JfrogFailure(`JFrog repositories: HTTP ${res.status}`, res.status);
    }
    let list;
    try {
      list = JSON.parse(await res.text());
    } catch {
      throw new JfrogFailure("JFrog repositories: not JSON");
    }
    const s = (x) => (typeof x === "string" ? x : "");
    return (Array.isArray(list) ? list : [])
      .filter((r) => r && s(r.key))
      .map((r) => ({ key: s(r.key), type: s(r.type).toLowerCase(), package_type: s(r.packageType).toLowerCase(), description: s(r.description) }))
      .sort((a, b) => a.key.localeCompare(b.key, undefined, { sensitivity: "base" }));
  }
}

module.exports = { Jfrog, JfrogFailure, claims, usernameFromSubject, EXCHANGE_PATH };
