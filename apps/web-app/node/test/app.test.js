// npm test  (no dependencies). Fakes the App Service identity endpoint and JFrog.
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const http = require("node:http");
const { Jfrog, JfrogFailure, usernameFromSubject } = require("../jfrog");
const { build } = require("../server");

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const jwt = (claims) => `${b64({ alg: "none" })}.${b64(claims)}.sig`;
const AUD = "api://11111111-2222-3333-4444-555555555555";
const ENTRA = jwt({ iss: "https://login.microsoftonline.com/t/v2.0", aud: AUD, sub: "s", oid: "o", tid: "t" });
const ACCESS = jwt({ sub: "jfac@x/users/web-app", exp: 4102444800 });

function fake(opts = {}) {
  const seen = { identity: 0, exchange: 0, reads: [], auth: [] };
  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      if (u.pathname === "/identity") {
        seen.identity++;
        assert.strictEqual(req.headers["x-identity-header"], "h");
        seen.resource = u.searchParams.get("resource");
        seen.clientId = u.searchParams.get("client_id");
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ access_token: ENTRA }));
      }
      if (u.pathname === "/access/api/v1/oidc/token") {
        seen.exchange++;
        seen.exchangeBody = JSON.parse(body);
        if (opts.refuse) {
          res.writeHead(401);
          return res.end('{"errors":[{"message":"no mapping"}]}');
        }
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ access_token: ACCESS, expires_in: opts.ttl ?? 3600, username: opts.username }));
      }
      seen.auth.push(req.headers.authorization);
      if (u.pathname === "/artifactory/api/repositories") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify([{ key: "b-remote", type: "REMOTE", packageType: "Npm" }, { key: "A-local", type: "LOCAL", packageType: "Generic" }]));
      }
      if (u.pathname === "/artifactory/redirect/x") {
        res.writeHead(302, { Location: "http://127.0.0.1:1/elsewhere" });
        return res.end();
      }
      seen.reads.push(u.pathname);
      res.writeHead(u.pathname.endsWith("ping.txt") ? 200 : 404);
      res.end("pong");
    });
  });
  return new Promise((ok) => srv.listen(0, "127.0.0.1", () => ok({ srv, seen, base: `http://127.0.0.1:${srv.address().port}` })));
}

const envFor = (base, extra = {}) => ({
  JFROG_URL: base, JFROG_OIDC_ALLOW_HTTP: "1", JFROG_OIDC_PROVIDER: "azure-entra", JFROG_OIDC_AUDIENCE: AUD,
  IDENTITY_ENDPOINT: `${base}/identity`, IDENTITY_HEADER: "h", AZURE_CLIENT_ID: "cid", ...extra,
});

test("exchange sends the identity token to the provider and caches the JFrog token", async (t) => {
  const f = await fake();
  t.after(() => f.srv.close());
  let now = 1000;
  const j = new Jfrog(envFor(f.base), { clock: () => now });
  const a = await j.token();
  assert.strictEqual(a.username, "web-app");
  assert.strictEqual(a.expiresAt, 4600);
  assert.strictEqual(f.seen.resource, AUD);
  assert.strictEqual(f.seen.clientId, "cid");
  assert.strictEqual(f.seen.exchangeBody.provider_name, "azure-entra");
  assert.strictEqual(f.seen.exchangeBody.subject_token, ENTRA);
  await j.token();
  assert.strictEqual(f.seen.exchange, 1, "cached");
  now = 4600 - 30;
  await j.token();
  assert.strictEqual(f.seen.exchange, 2, "refreshed within a minute of expiry");
  await j.token(true);
  assert.strictEqual(f.seen.exchange, 3, "fresh");
});

test("concurrent callers share one exchange", async (t) => {
  const f = await fake();
  t.after(() => f.srv.close());
  const j = new Jfrog(envFor(f.base));
  await Promise.all([j.token(), j.token(), j.token()]);
  assert.strictEqual(f.seen.exchange, 1);
});

test("the token never appears in JSON or a log line", async (t) => {
  const f = await fake();
  t.after(() => f.srv.close());
  const j = new Jfrog(envFor(f.base, { JFROG_HEALTHCHECK_PATH: "generic-local/ping.txt" }));
  const tok = await j.token();
  assert.ok(!JSON.stringify(tok).includes(ACCESS));
  const r = await j.selftest("generic-local/ping.txt", false);
  const all = JSON.stringify(r) + Jfrog.logLine(r) + JSON.stringify(await j.identity());
  assert.ok(!all.includes(ACCESS) && !all.includes(ENTRA));
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.read, { path: "generic-local/ping.txt", status: 200 });
  assert.strictEqual(f.seen.auth[0], `Bearer ${ACCESS}`);
});

test("refused exchange: selftest reports JFrog's answer, /readyz 503", async (t) => {
  const f = await fake({ refuse: true });
  t.after(() => f.srv.close());
  const j = new Jfrog(envFor(f.base));
  const r = await j.selftest("", false);
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.check.status, 401);
  assert.match(Jfrog.logLine(r), /JFrog refused the exchange via azure-entra/);
  await assert.rejects(j.token(), JfrogFailure);
});

test("configuration is checked: https only unless allowed, all three settings", async () => {
  await assert.rejects(new Jfrog({ JFROG_URL: "http://x", JFROG_OIDC_PROVIDER: "p", JFROG_OIDC_AUDIENCE: "a" }).token(), /must be https/);
  await assert.rejects(new Jfrog({}).token(), /missing environment variables: JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE/);
  await assert.rejects(new Jfrog({ JFROG_URL: "https://x", JFROG_OIDC_PROVIDER: "p", JFROG_OIDC_AUDIENCE: "a" }).token(), /not on App Service/);
});

test("the bearer token does not follow a redirect", async (t) => {
  const f = await fake();
  t.after(() => f.srv.close());
  const j = new Jfrog(envFor(f.base));
  assert.strictEqual(await j.read("redirect/x"), 302);
});

test("username from the subject when JFrog sends none", () => {
  assert.strictEqual(usernameFromSubject("jfac@01abc/users/svc"), "svc");
  assert.strictEqual(usernameFromSubject("plain"), "plain");
});

test("server endpoints match the other runtimes", async (t) => {
  const f = await fake();
  const srv = build(new Jfrog(envFor(f.base)), "/generic-local/ping.txt/", "9.9.9");
  await new Promise((ok) => srv.listen(0, "127.0.0.1", ok));
  t.after(() => { srv.close(); f.srv.close(); });
  const base = `http://127.0.0.1:${srv.address().port}`;
  const get = async (p) => {
    const r = await fetch(base + p);
    return { status: r.status, csp: r.headers.get("content-security-policy"), text: await r.text() };
  };
  const h = await get("/health");
  assert.strictEqual(h.status, 200);
  assert.strictEqual(JSON.parse(h.text).version, "9.9.9");
  assert.match(h.csp, /default-src 'none'/);
  assert.strictEqual((await get("/readyz")).status, 200);
  const hj = await get("/healthz/jfrog?fresh=1");
  assert.strictEqual(hj.status, 200);
  assert.strictEqual(JSON.parse(hj.text).read.path, "generic-local/ping.txt");
  assert.strictEqual(JSON.parse((await get("/api/v1/identity")).text).jfrog_user, "web-app");
  assert.deepStrictEqual(JSON.parse((await get("/api/v1/repositories")).text).repositories.map((r) => r.key), ["A-local", "b-remote"]);
  const home = await get("/");
  assert.match(home.text, /A-local/);
  assert.ok(!home.text.includes(ACCESS));
  assert.strictEqual((await get("/nope")).status, 404);
});

test("starts when loaded by iisnode's interceptor (require.main is not server.js)", async () => {
  const { spawn } = require("node:child_process");
  const path = require("node:path");
  const port = 20000 + Math.floor(Math.random() * 20000);
  const loader = `process.env.IISNODE_VERSION = "0.2.26"; require(${JSON.stringify(path.join(__dirname, "..", "server.js"))});`;
  const child = spawn(process.execPath, ["-e", loader], { env: { ...process.env, PORT: String(port) }, stdio: "ignore" });
  try {
    let status = 0;
    for (let i = 0; i < 50 && !status; i++) {
      await new Promise((r) => setTimeout(r, 100));
      status = await new Promise((r) => http.get(`http://127.0.0.1:${port}/health`, (res) => { res.resume(); r(res.statusCode); }).on("error", () => r(0)));
    }
    assert.strictEqual(status, 200);
  } finally {
    child.kill();
  }
});
