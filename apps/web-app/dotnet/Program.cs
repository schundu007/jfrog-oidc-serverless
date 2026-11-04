// JFrog browser for App Service (.NET), with the same endpoints as the Python service
// (apps/cloud-run-service), so the console verifies all three runtimes the same way:
//
//   /                      identity and repositories (HTML)
//   /api/v1/identity       who this app is in Azure and in JFrog
//   /api/v1/repositories   the repositories JFrog lets it see
//   /health, /healthz      process up
//   /readyz                a JFrog token can be had
//   /healthz/jfrog         claims, exchange and a read of JFROG_HEALTHCHECK_PATH (?fresh=1 exchanges again)
//
// The platform sets the listening address (Linux: ASPNETCORE_URLS; Windows: IIS in-process).
using System.Net;
using System.Reflection;
using System.Text;
using JfrogOidc.WebApp;

var app = WebApp.Build(args, Jfrog.FromEnvironment(), Environment.GetEnvironmentVariable("JFROG_HEALTHCHECK_PATH"));
app.Run();

namespace JfrogOidc.WebApp
{
    public static class WebApp
    {
        public const string Csp = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'";

        public static WebApplication Build(string[] args, Jfrog jfrog, string? checkPath)
        {
            var builder = WebApplication.CreateBuilder(args);
            builder.Logging.ClearProviders();
            builder.Logging.AddSimpleConsole(o => o.SingleLine = true);
            var app = builder.Build();
            var started = DateTimeOffset.UtcNow;
            var version = Environment.GetEnvironmentVariable("APP_VERSION") is { Length: > 0 } v ? v
                : Assembly.GetExecutingAssembly().GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "dev";
            var check = checkPath?.Trim('/');

            app.Use(async (ctx, next) =>
            {
                var h = ctx.Response.Headers;
                h.ContentSecurityPolicy = Csp;
                h.XContentTypeOptions = "nosniff";
                h.XFrameOptions = "DENY";
                h.CacheControl = "no-store";
                h["Referrer-Policy"] = "no-referrer";
                try
                {
                    await next();
                }
                catch (JfrogFailure f)
                {
                    ctx.Response.StatusCode = f.Status >= 400 ? f.Status : 502;
                    await ctx.Response.WriteAsJsonAsync(new { error = f.Message });
                }
            });

            app.MapGet("/health", () => Results.Json(new
            {
                status = "ok", version, uptime_seconds = (long)(DateTimeOffset.UtcNow - started).TotalSeconds,
            }));
            app.MapGet("/healthz", () => Results.Json(new
            {
                status = "ok", version, uptime_seconds = (long)(DateTimeOffset.UtcNow - started).TotalSeconds,
            }));
            app.MapGet("/readyz", async () =>
            {
                try
                {
                    await jfrog.Token();
                    return Results.Json(new { status = "ready" });
                }
                catch (JfrogFailure f)
                {
                    return Results.Json(new { status = "unavailable", reason = f.Message }, statusCode: 503);
                }
            });
            app.MapGet("/healthz/jfrog", async (HttpRequest req) =>
            {
                var r = await jfrog.Selftest(check, req.Query["fresh"] == "1");
                Console.WriteLine(Jfrog.LogLine(r));
                return Results.Json(r, statusCode: r["ok"] is true ? 200 : 503);
            });
            app.MapGet("/api/v1/identity", async () => Results.Json(await jfrog.Identity()));
            app.MapGet("/api/v1/repositories", async () => Results.Json(new { repositories = await jfrog.Repositories() }));
            app.MapGet("/", async () =>
            {
                var b = new StringBuilder();
                b.Append("<!doctype html><html lang=en><meta charset=utf-8><title>JFrog browser (.NET)</title>")
                 .Append("<style>body{font:14px system-ui,sans-serif;margin:24px}table{border-collapse:collapse}")
                 .Append("td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>")
                 .Append("<h1>JFrog browser</h1><p>.NET ").Append(Esc(Environment.Version.ToString()))
                 .Append(" &middot; ").Append(Esc(version)).Append("</p>");
                try
                {
                    var id = await jfrog.Identity();
                    b.Append("<h2>Identity</h2><table>");
                    foreach (var (k, val) in id)
                        b.Append("<tr><th>").Append(Esc(k)).Append("<td>").Append(Esc(val?.ToString()));
                    b.Append("</table><h2>Repositories</h2><table><tr><th>Key<th>Type<th>Package");
                    foreach (var r in await jfrog.Repositories())
                        b.Append("<tr><td>").Append(Esc(r["key"])).Append("<td>").Append(Esc(r["type"]))
                         .Append("<td>").Append(Esc(r["package_type"]));
                    b.Append("</table>");
                }
                catch (JfrogFailure f)
                {
                    b.Append("<p>").Append(Esc(f.Message)).Append("</p>");
                }
                return Results.Content(b.ToString(), "text/html; charset=utf-8");
            });
            return app;
        }

        public static string Esc(string? s) => WebUtility.HtmlEncode(s ?? "");
    }
}
