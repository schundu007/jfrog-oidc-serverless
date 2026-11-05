// JFrog browser for App Service (Node.js), with the same endpoints as the Python, Java and .NET
// apps, so the console verifies every runtime the same way:
//
//   /                      identity and repositories (HTML)
//   /api/v1/identity       who this app is in Azure and in JFrog
//   /api/v1/repositories   the repositories JFrog lets it see
//   /health, /healthz      process up
//   /readyz                a JFrog token can be had
//   /healthz/jfrog         claims, exchange and a read of JFROG_HEALTHCHECK_PATH (?fresh=1 exchanges again)
//
// Listens on PORT (Linux: set by App Service; Windows: iisnode passes a named pipe).
"use strict";

const http = require("node:http");
const { Jfrog, JfrogFailure } = require("./jfrog");

const CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function build(jfrog, checkPath, version = process.env.APP_VERSION || require("./package.json").version) {
  const started = Date.now();
  const check = (checkPath || "").replace(/^\/+|\/+$/g, "");

  const send = (res, status, body, type = "application/json") => {
    res.writeHead(status, {
      "Content-Type": type,
      "Content-Security-Policy": CSP,
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
    });
    res.end(type === "application/json" ? JSON.stringify(body) : body);
  };
  const health = () => ({ status: "ok", version, uptime_seconds: Math.floor((Date.now() - started) / 1000) });

  const routes = {
    "/health": async () => [200, health()],
    "/healthz": async () => [200, health()],
    "/readyz": async () => {
      try {
        await jfrog.token();
        return [200, { status: "ready" }];
      } catch (e) {
        if (!(e instanceof JfrogFailure)) throw e;
        return [503, { status: "unavailable", reason: e.message }];
      }
    },
    "/healthz/jfrog": async (q) => {
      const r = await jfrog.selftest(check, q.get("fresh") === "1");
      console.log(Jfrog.logLine(r));
      return [r.ok ? 200 : 503, r];
    },
    "/api/v1/identity": async () => [200, await jfrog.identity()],
    "/api/v1/repositories": async () => [200, { repositories: await jfrog.repositories() }],
    "/": async () => {
      let b = `<!doctype html><html lang=en><meta charset=utf-8><title>JFrog browser (Node.js)</title>`
        + `<style>body{font:14px system-ui,sans-serif;margin:24px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>`
        + `<h1>JFrog browser</h1><p>Node.js ${esc(process.versions.node)} &middot; ${esc(version)}</p>`;
      try {
        const id = await jfrog.identity();
        b += "<h2>Identity</h2><table>" + Object.entries(id).map(([k, v]) => `<tr><th>${esc(k)}<td>${esc(v)}`).join("") + "</table>";
        b += "<h2>Repositories</h2><table><tr><th>Key<th>Type<th>Package"
          + (await jfrog.repositories()).map((r) => `<tr><td>${esc(r.key)}<td>${esc(r.type)}<td>${esc(r.package_type)}`).join("") + "</table>";
      } catch (e) {
        if (!(e instanceof JfrogFailure)) throw e;
        b += `<p>${esc(e.message)}</p>`;
      }
      return [200, b, "text/html; charset=utf-8"];
    },
  };

  return http.createServer(async (req, res) => {
    const u = new URL(req.url, "http://localhost");
    const route = req.method === "GET" ? routes[u.pathname] : null;
    if (!route) return send(res, 404, { error: "not found" });
    try {
      const [status, body, type] = await route(u.searchParams);
      send(res, status, body, type);
    } catch (e) {
      if (e instanceof JfrogFailure) return send(res, e.status >= 400 ? e.status : 502, { error: e.message });
      console.error(JSON.stringify({ event: "error", message: String(e && e.message).slice(0, 300) }));
      send(res, 500, { error: "internal error" });
    }
  });
}

// iisnode (Windows) loads this file through its interceptor, so require.main is not this module there.
if (require.main === module || process.env.IISNODE_VERSION) {
  build(Jfrog.fromEnv(), process.env.JFROG_HEALTHCHECK_PATH).listen(process.env.PORT || 8080);
}

module.exports = { build };
