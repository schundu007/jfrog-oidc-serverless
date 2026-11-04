using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.Extensions.DependencyInjection;
using JfrogOidc.WebApp;
using Xunit;

namespace JfrogOidc.WebApp.Tests;

/// <summary>The real app against emulated endpoints: the App Service identity endpoint and JFrog.</summary>
public sealed class AppTests : IAsyncLifetime
{
    static string Jwt(string claims)
    {
        static string B(string s) => Convert.ToBase64String(Encoding.UTF8.GetBytes(s)).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        return $"{B("{\"alg\":\"none\"}")}.{B(claims)}.sig";
    }

    static readonly string MiToken = Jwt("{\"iss\":\"https://sts.windows.net/t/\",\"aud\":\"api://jfrog\",\"oid\":\"oid-1\",\"sub\":\"oid-1\"}");
    static readonly string JfToken = Jwt("{\"sub\":\"jfac@x/users/webapp-sa\",\"exp\":4102444800}");

    HttpListener _fake = null!;
    WebApplication _app = null!;
    readonly HttpClient _client = new();
    string _base = "";
    int _exchanges;
    string _exchangeBody = "";
    string _identityQuery = "";
    int _exchangeStatus = 200;
    long _now = 1_000_000;
    Jfrog _jfrog = null!;

    static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    async Task Serve()
    {
        while (_fake.IsListening)
        {
            HttpListenerContext ctx;
            try { ctx = await _fake.GetContextAsync(); } catch { return; }
            var path = ctx.Request.Url!.AbsolutePath;
            var auth = ctx.Request.Headers["Authorization"] == "Bearer " + JfToken;
            (int status, string body) r = path switch
            {
                "/msi/token" => Identity(ctx),
                "/access/api/v1/oidc/token" => Exchange(ctx),
                "/artifactory/api/repositories" => auth
                    ? (200, "[{\"key\":\"pypi-remote\",\"type\":\"REMOTE\",\"packageType\":\"Pypi\"},"
                        + "{\"key\":\"generic-local\",\"type\":\"LOCAL\",\"packageType\":\"Generic\",\"description\":\"<b>x</b>\"}]")
                    : (401, ""),
                "/artifactory/generic-local/hello.txt" => auth ? (200, "hello") : (401, ""),
                _ => (404, ""),
            };
            ctx.Response.StatusCode = r.status;
            var bytes = Encoding.UTF8.GetBytes(r.body);
            await ctx.Response.OutputStream.WriteAsync(bytes);
            ctx.Response.Close();
        }
    }

    (int, string) Identity(HttpListenerContext ctx)
    {
        _identityQuery = ctx.Request.Url!.Query;
        return ctx.Request.Headers["X-IDENTITY-HEADER"] == "secret-header"
            ? (200, $"{{\"access_token\":\"{MiToken}\",\"expires_on\":\"9999999999\"}}")
            : (400, "{}");
    }

    (int, string) Exchange(HttpListenerContext ctx)
    {
        Interlocked.Increment(ref _exchanges);
        using var reader = new StreamReader(ctx.Request.InputStream);
        _exchangeBody = reader.ReadToEnd();
        return _exchangeStatus == 200
            ? (200, $"{{\"access_token\":\"{JfToken}\",\"expires_in\":3600}}")
            : (_exchangeStatus, "{\"errors\":[{\"message\":\"no matching identity mapping\"}]}");
    }

    public async Task InitializeAsync()
    {
        var fakeUrl = $"http://127.0.0.1:{FreePort()}";
        _fake = new HttpListener();
        _fake.Prefixes.Add(fakeUrl + "/");
        _fake.Start();
        _ = Serve();
        var env = new Dictionary<string, string>
        {
            ["JFROG_URL"] = fakeUrl + "/", ["JFROG_OIDC_PROVIDER"] = "azure-webapp", ["JFROG_OIDC_AUDIENCE"] = "api://jfrog",
            ["JFROG_OIDC_ALLOW_HTTP"] = "1", ["IDENTITY_ENDPOINT"] = fakeUrl + "/msi/token",
            ["IDENTITY_HEADER"] = "secret-header", ["AZURE_CLIENT_ID"] = "client-1",
        };
        _jfrog = new Jfrog(env, new HttpClient(), () => _now);
        _app = WebApp.Build(["--urls", "http://127.0.0.1:0"], _jfrog, "/generic-local/hello.txt");
        await _app.StartAsync();
        _base = _app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.First();
    }

    public async Task DisposeAsync()
    {
        await _app.StopAsync();
        _fake.Stop();
    }

    async Task<(int status, string body, HttpResponseMessage res)> Get(string path)
    {
        var res = await _client.GetAsync(_base + path);
        return ((int)res.StatusCode, await res.Content.ReadAsStringAsync(), res);
    }

    static void NoTokens(string body)
    {
        Assert.DoesNotContain(MiToken, body);
        Assert.DoesNotContain(JfToken, body);
        Assert.DoesNotContain("secret-header", body);
    }

    [Fact]
    public async Task HealthIsUpWithoutJfrog()
    {
        var (status, body, res) = await Get("/health");
        Assert.Equal(200, status);
        Assert.Equal("ok", JsonDocument.Parse(body).RootElement.GetProperty("status").GetString());
        Assert.Equal(0, _exchanges);
        Assert.True(res.Headers.Contains("Content-Security-Policy"));
    }

    [Fact]
    public async Task SelftestExchangesAndReads()
    {
        var (status, body, _) = await Get("/healthz/jfrog");
        Assert.Equal(200, status);
        var root = JsonDocument.Parse(body).RootElement;
        Assert.True(root.GetProperty("ok").GetBoolean());
        Assert.Equal("webapp-sa", root.GetProperty("check").GetProperty("username").GetString());
        Assert.Equal("azure-webapp", root.GetProperty("check").GetProperty("provider").GetString());
        Assert.Equal("oid-1", root.GetProperty("claims").GetProperty("oid").GetString());
        Assert.Equal(200, root.GetProperty("read").GetProperty("status").GetInt32());
        NoTokens(body);
        Assert.Contains("resource=api%3A%2F%2Fjfrog", _identityQuery);
        Assert.Contains("client_id=client-1", _identityQuery);
        var sent = JsonDocument.Parse(_exchangeBody).RootElement;
        Assert.Equal("azure-webapp", sent.GetProperty("provider_name").GetString());
        Assert.Equal(MiToken, sent.GetProperty("subject_token").GetString());
        Assert.Equal("urn:ietf:params:oauth:token-type:id_token", sent.GetProperty("subject_token_type").GetString());
    }

    [Fact]
    public async Task TokenIsCachedUntilFreshOrNearExpiry()
    {
        await Get("/api/v1/identity");
        await Get("/api/v1/repositories");
        Assert.Equal(1, _exchanges);
        await Get("/healthz/jfrog?fresh=1");
        Assert.Equal(2, _exchanges);
        _now += 3600 - 30;
        await Get("/readyz");
        Assert.Equal(3, _exchanges);
    }

    [Fact]
    public async Task RefusedExchangeIsReportedNotThrown()
    {
        _exchangeStatus = 403;
        var (status, body, _) = await Get("/healthz/jfrog");
        Assert.Equal(503, status);
        var check = JsonDocument.Parse(body).RootElement.GetProperty("check");
        Assert.Equal(403, check.GetProperty("status").GetInt32());
        Assert.Contains("HTTP 403", check.GetProperty("error").GetString());
        Assert.Equal(403, (await Get("/api/v1/identity")).status);
        Assert.Equal(503, (await Get("/readyz")).status);
        NoTokens(body);
    }

    [Fact]
    public async Task RepositoriesSortedAndHomeEscaped()
    {
        var (_, body, _) = await Get("/api/v1/repositories");
        var repos = JsonDocument.Parse(body).RootElement.GetProperty("repositories");
        Assert.Equal("generic-local", repos[0].GetProperty("key").GetString());
        Assert.Equal("remote", repos[1].GetProperty("type").GetString());
        var (status, home, _) = await Get("/");
        Assert.Equal(200, status);
        Assert.Contains("webapp-sa", home);
        Assert.DoesNotContain("<b>x</b>", home);
        NoTokens(home + body);
    }

    [Fact]
    public async Task MissingConfigSaysWhat()
    {
        var bare = new Jfrog(new Dictionary<string, string>(), new HttpClient(), () => _now);
        var r = await bare.Selftest("", false);
        Assert.Equal(false, r["ok"]);
        Assert.Contains("JFROG_URL", (string?)((Dictionary<string, object?>)r["check"]!)["error"]);
    }

    [Fact]
    public async Task LogLineNamesTheUserNeverTheToken()
    {
        var r = await _jfrog.Selftest("generic-local/hello.txt", false);
        var line = Jfrog.LogLine(r);
        var doc = JsonDocument.Parse(line).RootElement;
        Assert.Equal("jfrog_oidc_selftest", doc.GetProperty("event").GetString());
        Assert.Equal("webapp-sa", doc.GetProperty("jfrog_user").GetString());
        Assert.Contains("expires in 60 min", doc.GetProperty("message").GetString());
        NoTokens(line);
    }

    [Fact]
    public async Task UnknownPath()
    {
        Assert.Equal(404, (await Get("/nope")).status);
    }
}
