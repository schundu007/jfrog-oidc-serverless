using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace JfrogOidc.WebApp;

/// <summary>What JFrog or the identity endpoint answered; Status 0 when there was no HTTP answer.</summary>
public sealed class JfrogFailure(string message, int status = 0) : Exception(message)
{
    public int Status { get; } = status;
}

public sealed record JfrogToken(string Value, string Username, long ExpiresAt)
{
    public long Remaining(long now) => Math.Max(0, ExpiresAt - now);
}

/// <summary>
/// The OIDC exchange, as jfrog_oidc does it: the App Service managed identity token for
/// JFROG_OIDC_AUDIENCE (IDENTITY_ENDPOINT + IDENTITY_HEADER; AZURE_CLIENT_ID picks the identity)
/// is exchanged at JFrog for a short-lived access token, cached until a minute before it expires.
/// Nothing here ever returns, prints or logs a token.
/// </summary>
public sealed class Jfrog
{
    public const string ExchangePath = "/access/api/v1/oidc/token";
    static readonly string[] ClaimKeys = ["iss", "aud", "sub", "oid", "tid", "azp", "email", "ver", "iat", "exp"];

    readonly IReadOnlyDictionary<string, string> _env;
    readonly HttpClient _http;
    readonly Func<long> _clock;
    readonly SemaphoreSlim _lock = new(1, 1);
    JfrogToken? _cached;

    public string Url { get; }
    public string Provider { get; }
    public string Audience { get; }

    public Jfrog(IReadOnlyDictionary<string, string> env, HttpClient http, Func<long> clockSeconds)
    {
        _env = env;
        _http = http;
        _clock = clockSeconds;
        Url = Get("JFROG_URL").TrimEnd('/');
        Provider = Get("JFROG_OIDC_PROVIDER");
        Audience = Get("JFROG_OIDC_AUDIENCE");
    }

    public static Jfrog FromEnvironment()
    {
        var env = new Dictionary<string, string>();
        foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables())
            env[(string)e.Key] = (string?)e.Value ?? "";
        return new Jfrog(env, new HttpClient { Timeout = TimeSpan.FromSeconds(30) },
            () => DateTimeOffset.UtcNow.ToUnixTimeSeconds());
    }

    string Get(string key) => _env.TryGetValue(key, out var v) ? v : "";

    void Configured()
    {
        var missing = new[] { "JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE" }.Where(k => Get(k) == "").ToList();
        if (missing.Count > 0) throw new JfrogFailure("missing environment variables: " + string.Join(", ", missing));
        if (!Url.StartsWith("https://", StringComparison.Ordinal) && Get("JFROG_OIDC_ALLOW_HTTP") == "")
            throw new JfrogFailure("JFROG_URL must be https");
    }

    static string Detail(string body) => body.Length > 500 ? body[..500] : body;

    async Task<HttpResponseMessage> Send(HttpRequestMessage req, string what)
    {
        try
        {
            return await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            throw new JfrogFailure($"{what} unreachable: {e.Message}");
        }
    }

    static JsonObject ObjectOf(string text, string what)
    {
        try
        {
            return JsonNode.Parse(text) as JsonObject ?? throw new JfrogFailure($"{what} returned non-JSON");
        }
        catch (JsonException)
        {
            throw new JfrogFailure($"{what} returned non-JSON");
        }
    }

    /// <summary>The managed identity's token for the JFrog audience.</summary>
    public async Task<string> IdentityToken()
    {
        var endpoint = Get("IDENTITY_ENDPOINT");
        var header = Get("IDENTITY_HEADER");
        if (endpoint == "" || header == "")
            throw new JfrogFailure("no managed identity endpoint (IDENTITY_ENDPOINT/IDENTITY_HEADER): not on App Service?");
        var q = $"resource={Uri.EscapeDataString(Audience)}&api-version=2019-08-01";
        if (Get("AZURE_CLIENT_ID") is { Length: > 0 } clientId) q += $"&client_id={Uri.EscapeDataString(clientId)}";
        var req = new HttpRequestMessage(HttpMethod.Get, $"{endpoint}?{q}");
        req.Headers.Add("X-IDENTITY-HEADER", header);
        using var res = await Send(req, "managed identity endpoint");
        var body = await res.Content.ReadAsStringAsync();
        if ((int)res.StatusCode != 200)
            throw new JfrogFailure($"managed identity endpoint answered HTTP {(int)res.StatusCode}: {Detail(body)}", (int)res.StatusCode);
        var token = ObjectOf(body, "managed identity endpoint")["access_token"]?.GetValue<string>();
        if (string.IsNullOrEmpty(token)) throw new JfrogFailure("managed identity response missing access_token");
        return token;
    }

    /// <summary>A JFrog token, from the cache unless fresh or within a minute of expiry.</summary>
    public async Task<JfrogToken> Token(bool fresh = false)
    {
        Configured();
        await _lock.WaitAsync();
        try
        {
            var now = _clock();
            if (!fresh && _cached is not null && _cached.Remaining(now) > 60) return _cached;
            var subject = await IdentityToken();
            var payloadOut = new JsonObject
            {
                ["grant_type"] = "urn:ietf:params:oauth:grant-type:token-exchange",
                ["subject_token_type"] = "urn:ietf:params:oauth:token-type:id_token",
                ["subject_token"] = subject,
                ["provider_name"] = Provider,
            };
            var req = new HttpRequestMessage(HttpMethod.Post, Url + ExchangePath)
            {
                Content = new StringContent(payloadOut.ToJsonString(), Encoding.UTF8, "application/json"),
            };
            req.Headers.Add("Accept", "application/json");
            using var res = await Send(req, "JFrog");
            var body = await res.Content.ReadAsStringAsync();
            if ((int)res.StatusCode != 200)
                throw new JfrogFailure($"JFrog OIDC exchange failed: HTTP {(int)res.StatusCode} provider={Provider}: {Detail(body)}",
                    (int)res.StatusCode);
            var payload = ObjectOf(body, "JFrog");
            var access = payload["access_token"]?.GetValue<string>();
            if (string.IsNullOrEmpty(access)) throw new JfrogFailure("JFrog response missing access_token");
            var claims = Claims(access);
            long expires = payload["expires_in"] is JsonValue ei && ei.TryGetValue<long>(out var secs) ? now + secs
                : claims.TryGetValue("exp", out var exp) && exp is JsonElement { ValueKind: JsonValueKind.Number } ee ? ee.GetInt64()
                : now + 3600;
            var user = payload["username"]?.GetValue<string>();
            if (string.IsNullOrEmpty(user))
                user = UsernameFromSubject(claims.TryGetValue("sub", out var sub) ? sub?.ToString() ?? "" : "");
            _cached = new JfrogToken(access, user, expires);
            return _cached;
        }
        finally
        {
            _lock.Release();
        }
    }

    public static string UsernameFromSubject(string sub)
    {
        var k = sub.IndexOf("/users/", StringComparison.Ordinal);
        return k >= 0 ? sub[(k + "/users/".Length)..] : sub;
    }

    /// <summary>A JWT's claims (no signature check: for display and mapping checks only).</summary>
    public static Dictionary<string, object?> Claims(string jwt)
    {
        var parts = jwt.Split('.');
        if (parts.Length < 2) return [];
        try
        {
            var p = parts[1].Replace('-', '+').Replace('_', '/');
            p = p.PadRight(p.Length + (4 - p.Length % 4) % 4, '=');
            using var doc = JsonDocument.Parse(Convert.FromBase64String(p));
            return doc.RootElement.EnumerateObject().ToDictionary(e => e.Name, e => (object?)e.Value.Clone());
        }
        catch (Exception e) when (e is FormatException or JsonException or InvalidOperationException)
        {
            return [];
        }
    }

    async Task<HttpRequestMessage> Authed(string path)
    {
        var t = await Token();
        var req = new HttpRequestMessage(HttpMethod.Get, Url + path);
        req.Headers.Add("Authorization", "Bearer " + t.Value);
        return req;
    }

    public static string EncodePath(string p) => string.Join('/', p.Split('/').Select(Uri.EscapeDataString));

    /// <summary>GET &lt;repo&gt;/&lt;path&gt; and return the HTTP status; the body is discarded.</summary>
    public async Task<int> Read(string repoPath)
    {
        using var res = await Send(await Authed("/artifactory/" + EncodePath(repoPath.TrimStart('/'))), "JFrog");
        await res.Content.CopyToAsync(Stream.Null);
        return (int)res.StatusCode;
    }

    async Task<Dictionary<string, object?>> ClaimsOrError()
    {
        try
        {
            var all = Claims(await IdentityToken());
            return ClaimKeys.Where(all.ContainsKey).ToDictionary(k => k, k => all[k]);
        }
        catch (JfrogFailure f)
        {
            return new() { ["error"] = f.Message };
        }
    }

    /// <summary>Same shape as jfrog_oidc.probe.selftest: ok, claims, check and read.</summary>
    public async Task<Dictionary<string, object?>> Selftest(string? checkPath, bool fresh)
    {
        var r = new Dictionary<string, object?> { ["ok"] = false, ["claims"] = null, ["check"] = null, ["read"] = null };
        try
        {
            Configured();
        }
        catch (JfrogFailure f)
        {
            r["check"] = new Dictionary<string, object?> { ["error"] = f.Message };
            return r;
        }
        r["claims"] = await ClaimsOrError();
        JfrogToken t;
        try
        {
            t = await Token(fresh);
        }
        catch (JfrogFailure f)
        {
            var failed = new Dictionary<string, object?> { ["error"] = f.Message, ["provider"] = Provider };
            if (f.Status > 0) failed["status"] = f.Status;
            r["check"] = failed;
            return r;
        }
        r["check"] = new Dictionary<string, object?>
        {
            ["username"] = t.Username,
            ["expires_in"] = t.Remaining(_clock()),
            ["expires_at"] = DateTimeOffset.FromUnixTimeSeconds(t.ExpiresAt).UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ"),
            ["provider"] = Provider,
            ["source"] = "azure",
        };
        if (string.IsNullOrEmpty(checkPath))
        {
            r["ok"] = true;
            return r;
        }
        var read = new Dictionary<string, object?> { ["path"] = checkPath };
        try
        {
            var status = await Read(checkPath);
            read["status"] = status;
            r["ok"] = status == 200;
        }
        catch (JfrogFailure f)
        {
            read["error"] = f.Message;
        }
        r["read"] = read;
        return r;
    }

    /// <summary>One JSON line for the platform log about a selftest: what JFrog answered, never a token.</summary>
    public static string LogLine(Dictionary<string, object?> result)
    {
        var claims = result["claims"] as Dictionary<string, object?> ?? [];
        var check = result["check"] as Dictionary<string, object?> ?? [];
        var read = result["read"] as Dictionary<string, object?> ?? [];
        check.TryGetValue("status", out var status);
        string msg;
        if (check.TryGetValue("username", out var user) && user is not null)
        {
            status = 200;
            var mins = check.TryGetValue("expires_in", out var ei) && ei is long l ? l / 60 : 0;
            msg = $"JFrog issued a token for {user} via {check.GetValueOrDefault("provider")}, expires in {mins} min";
            if (read.GetValueOrDefault("status") is not null) msg += $"; read {read["path"]}: HTTP {read["status"]}";
            else if (read.GetValueOrDefault("error") is not null) msg += $"; read {read["path"]} failed: {read["error"]}";
        }
        else
        {
            msg = $"JFrog refused the exchange via {check.GetValueOrDefault("provider") ?? "its provider"}: "
                + $"{check.GetValueOrDefault("error") ?? "no answer"}";
        }
        var line = new Dictionary<string, object?>
        {
            ["event"] = "jfrog_oidc_selftest",
            ["ok"] = result["ok"] is true,
            ["jfrog_status"] = status,
            ["provider"] = check.GetValueOrDefault("provider"),
            ["jfrog_user"] = check.GetValueOrDefault("username"),
        };
        foreach (var k in new[] { "iss", "sub", "aud" })
            if (claims.TryGetValue(k, out var v)) line[k] = v;
        line["message"] = msg.Length > 600 ? msg[..600] : msg;
        return JsonSerializer.Serialize(line);
    }

    /// <summary>Who this app is in Azure and in JFrog (claims and token times, never the token).</summary>
    public async Task<Dictionary<string, object?>> Identity()
    {
        var t = await Token();
        var c = await ClaimsOrError();
        return new()
        {
            ["cloud_identity"] = c.GetValueOrDefault("email") ?? c.GetValueOrDefault("oid") ?? c.GetValueOrDefault("sub"),
            ["issuer"] = c.GetValueOrDefault("iss"),
            ["audience"] = c.GetValueOrDefault("aud"),
            ["jfrog_user"] = t.Username,
            ["expires_at"] = DateTimeOffset.FromUnixTimeSeconds(t.ExpiresAt).UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ"),
            ["expires_in"] = t.Remaining(_clock()),
        };
    }

    public async Task<List<Dictionary<string, string>>> Repositories()
    {
        using var res = await Send(await Authed("/artifactory/api/repositories"), "JFrog");
        var body = await res.Content.ReadAsStringAsync();
        if ((int)res.StatusCode != 200) throw new JfrogFailure($"JFrog repositories: HTTP {(int)res.StatusCode}", (int)res.StatusCode);
        JsonArray list;
        try
        {
            list = JsonNode.Parse(body) as JsonArray ?? [];
        }
        catch (JsonException)
        {
            throw new JfrogFailure("JFrog repositories: not JSON");
        }
        string S(JsonNode? n) => n is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";
        return list.OfType<JsonObject>()
            .Where(r => S(r["key"]) != "")
            .Select(r => new Dictionary<string, string>
            {
                ["key"] = S(r["key"]),
                ["type"] = S(r["type"]).ToLowerInvariant(),
                ["package_type"] = S(r["packageType"]).ToLowerInvariant(),
                ["description"] = S(r["description"]),
            })
            .OrderBy(r => r["key"], StringComparer.OrdinalIgnoreCase)
            .ToList();
    }
}
