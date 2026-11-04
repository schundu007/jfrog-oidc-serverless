package jfrogoidc.webapp;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.LongSupplier;

/**
 * The OIDC exchange, as jfrog_oidc does it: the App Service managed identity token for
 * JFROG_OIDC_AUDIENCE (IDENTITY_ENDPOINT + IDENTITY_HEADER, AZURE_CLIENT_ID picks the identity)
 * is exchanged at JFrog for a short-lived access token, cached until a minute before it expires.
 * Nothing here ever returns, prints or logs a token.
 */
final class Jfrog {
    static final String EXCHANGE_PATH = "/access/api/v1/oidc/token";
    static final List<String> CLAIM_KEYS = List.of("iss", "aud", "sub", "oid", "tid", "azp", "email", "ver", "iat", "exp");

    /** What JFrog or the identity endpoint answered; status 0 when there was no HTTP answer. */
    static final class Failure extends Exception {
        private static final long serialVersionUID = 1L;
        final int status;

        Failure(String message, int status) {
            super(message);
            this.status = status;
        }
    }

    record Token(String value, String username, long expiresAt) {
        long remaining(long now) {
            return Math.max(0, expiresAt - now);
        }
    }

    final String url;
    final String provider;
    final String audience;
    private final Map<String, String> env;
    private final HttpClient http;
    private final LongSupplier clock;
    private Token cached;

    Jfrog(Map<String, String> env, HttpClient http, LongSupplier clockSeconds) {
        this.env = env;
        this.http = http;
        this.clock = clockSeconds;
        this.url = env.getOrDefault("JFROG_URL", "").replaceAll("/+$", "");
        this.provider = env.getOrDefault("JFROG_OIDC_PROVIDER", "");
        this.audience = env.getOrDefault("JFROG_OIDC_AUDIENCE", "");
    }

    static Jfrog fromEnv() {
        HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10))
                .followRedirects(HttpClient.Redirect.NORMAL).build();
        return new Jfrog(System.getenv(), http, () -> Instant.now().getEpochSecond());
    }

    private void configured() throws Failure {
        List<String> missing = new ArrayList<>();
        for (String k : List.of("JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE")) {
            if (env.getOrDefault(k, "").isEmpty()) missing.add(k);
        }
        if (!missing.isEmpty()) throw new Failure("missing environment variables: " + String.join(", ", missing), 0);
        if (!url.startsWith("https://") && env.getOrDefault("JFROG_OIDC_ALLOW_HTTP", "").isEmpty()) {
            throw new Failure("JFROG_URL must be https", 0);
        }
    }

    private static String enc(String v) {
        return URLEncoder.encode(v, StandardCharsets.UTF_8);
    }

    private HttpResponse<String> send(HttpRequest req, String what) throws Failure {
        try {
            return http.send(req, HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            throw new Failure(what + " unreachable: " + e.getMessage(), 0);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new Failure(what + " interrupted", 0);
        }
    }

    private static String detail(String body) {
        return body.length() > 500 ? body.substring(0, 500) : body;
    }

    /** The managed identity's token for the JFrog audience. */
    String identityToken() throws Failure {
        String endpoint = env.getOrDefault("IDENTITY_ENDPOINT", "");
        String header = env.getOrDefault("IDENTITY_HEADER", "");
        if (endpoint.isEmpty() || header.isEmpty()) {
            throw new Failure("no managed identity endpoint (IDENTITY_ENDPOINT/IDENTITY_HEADER): not on App Service?", 0);
        }
        String q = "resource=" + enc(audience) + "&api-version=2019-08-01";
        String clientId = env.getOrDefault("AZURE_CLIENT_ID", "");
        if (!clientId.isEmpty()) q += "&client_id=" + enc(clientId);
        HttpRequest req = HttpRequest.newBuilder(URI.create(endpoint + "?" + q))
                .header("X-IDENTITY-HEADER", header).timeout(Duration.ofSeconds(15)).GET().build();
        HttpResponse<String> res = send(req, "managed identity endpoint");
        if (res.statusCode() != 200) {
            throw new Failure("managed identity endpoint answered HTTP " + res.statusCode() + ": " + detail(res.body()), res.statusCode());
        }
        Object token;
        try {
            token = Json.object(res.body()).get("access_token");
        } catch (IllegalArgumentException e) {
            throw new Failure("managed identity endpoint returned non-JSON", 0);
        }
        if (!(token instanceof String t) || t.isEmpty()) throw new Failure("managed identity response missing access_token", 0);
        return t;
    }

    /** A JFrog token, from the cache unless fresh or within a minute of expiry. */
    synchronized Token token(boolean fresh) throws Failure {
        configured();
        long now = clock.getAsLong();
        if (!fresh && cached != null && cached.remaining(now) > 60) return cached;
        String subject = identityToken();
        String body = Json.write(Map.of(
                "grant_type", "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token_type", "urn:ietf:params:oauth:token-type:id_token",
                "subject_token", subject,
                "provider_name", provider));
        HttpRequest req = HttpRequest.newBuilder(URI.create(url + EXCHANGE_PATH))
                .header("Content-Type", "application/json").header("Accept", "application/json")
                .timeout(Duration.ofSeconds(30)).POST(HttpRequest.BodyPublishers.ofString(body)).build();
        HttpResponse<String> res = send(req, "JFrog");
        if (res.statusCode() != 200) {
            throw new Failure("JFrog OIDC exchange failed: HTTP " + res.statusCode() + " provider=" + provider + ": "
                    + detail(res.body()), res.statusCode());
        }
        Map<String, Object> payload;
        try {
            payload = Json.object(res.body());
        } catch (IllegalArgumentException e) {
            throw new Failure("JFrog returned non-JSON", 0);
        }
        if (!(payload.get("access_token") instanceof String access) || access.isEmpty()) {
            throw new Failure("JFrog response missing access_token", 0);
        }
        Map<String, Object> claims = claims(access);
        long expires;
        if (payload.get("expires_in") instanceof Number n) expires = now + n.longValue();
        else if (claims.get("exp") instanceof Number n) expires = n.longValue();
        else expires = now + 3600;
        String user = payload.get("username") instanceof String u && !u.isEmpty() ? u
                : usernameFromSubject(String.valueOf(claims.getOrDefault("sub", "")));
        cached = new Token(access, user, expires);
        return cached;
    }

    static String usernameFromSubject(String sub) {
        int k = sub.indexOf("/users/");
        return k >= 0 ? sub.substring(k + "/users/".length()) : sub;
    }

    /** A JWT's claims (no signature check: for display and mapping checks only). */
    static Map<String, Object> claims(String jwt) {
        String[] parts = jwt.split("\\.");
        if (parts.length < 2) return Map.of();
        try {
            return Json.object(new String(Base64.getUrlDecoder().decode(parts[1]), StandardCharsets.UTF_8));
        } catch (IllegalArgumentException e) {
            return Map.of();
        }
    }

    private HttpRequest.Builder authed(String path) throws Failure {
        Token t = token(false);
        return HttpRequest.newBuilder(URI.create(url + path)).header("Authorization", "Bearer " + t.value())
                .timeout(Duration.ofSeconds(30));
    }

    Object getJson(String path, String what) throws Failure {
        HttpResponse<String> res = send(authed(path).header("Accept", "application/json").GET().build(), "JFrog");
        if (res.statusCode() != 200) throw new Failure("JFrog " + what + ": HTTP " + res.statusCode(), res.statusCode());
        try {
            return Json.parse(res.body());
        } catch (IllegalArgumentException e) {
            throw new Failure("JFrog " + what + ": not JSON", 0);
        }
    }

    /** GET <repo>/<path> and return the HTTP status; the body is discarded. */
    int read(String repoPath) throws Failure {
        String path = "/artifactory/" + encodePath(repoPath.replaceAll("^/+", ""));
        try {
            HttpResponse<InputStream> res = http.send(authed(path).GET().build(), HttpResponse.BodyHandlers.ofInputStream());
            try (InputStream in = res.body()) {
                in.transferTo(java.io.OutputStream.nullOutputStream());
            }
            return res.statusCode();
        } catch (IOException e) {
            throw new Failure("JFrog unreachable: " + e.getMessage(), 0);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new Failure("interrupted", 0);
        }
    }

    static String encodePath(String p) {
        StringBuilder b = new StringBuilder();
        for (String seg : p.split("/", -1)) {
            if (b.length() > 0) b.append('/');
            b.append(enc(seg).replace("+", "%20"));
        }
        return b.toString();
    }

    private Map<String, Object> claimsOrError() {
        try {
            Map<String, Object> all = claims(identityToken());
            Map<String, Object> keep = new LinkedHashMap<>();
            for (String k : CLAIM_KEYS) if (all.containsKey(k)) keep.put(k, all.get(k));
            return keep;
        } catch (Failure f) {
            return Map.of("error", f.getMessage());
        }
    }

    /** Same shape as jfrog_oidc.probe.selftest: claims, check, read and ok. */
    Map<String, Object> selftest(String checkPath, boolean fresh) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("ok", false);
        r.put("claims", null);
        r.put("check", null);
        r.put("read", null);
        try {
            configured();
        } catch (Failure f) {
            r.put("check", Map.of("error", f.getMessage()));
            return r;
        }
        r.put("claims", claimsOrError());
        Token t;
        try {
            t = token(fresh);
        } catch (Failure f) {
            Map<String, Object> check = new LinkedHashMap<>();
            check.put("error", f.getMessage());
            check.put("provider", provider);
            if (f.status > 0) check.put("status", f.status);
            r.put("check", check);
            return r;
        }
        Map<String, Object> check = new LinkedHashMap<>();
        check.put("username", t.username());
        check.put("expires_in", t.remaining(clock.getAsLong()));
        check.put("expires_at", Instant.ofEpochSecond(t.expiresAt()).toString());
        check.put("provider", provider);
        check.put("source", "azure");
        r.put("check", check);
        if (checkPath == null || checkPath.isEmpty()) {
            r.put("ok", true);
            return r;
        }
        Map<String, Object> read = new LinkedHashMap<>();
        read.put("path", checkPath);
        try {
            int status = read(checkPath);
            read.put("status", status);
            r.put("ok", status == 200);
        } catch (Failure f) {
            read.put("error", f.getMessage());
        }
        r.put("read", read);
        return r;
    }

    /** One JSON line for the platform log about a selftest: what JFrog answered, never a token. */
    static String logLine(Map<String, Object> result) {
        Map<?, ?> claims = result.get("claims") instanceof Map<?, ?> m ? m : Map.of();
        Map<?, ?> check = result.get("check") instanceof Map<?, ?> m ? m : Map.of();
        Map<?, ?> read = result.get("read") instanceof Map<?, ?> m ? m : Map.of();
        Object status = check.get("status");
        String msg;
        if (check.get("username") != null) {
            status = 200;
            long mins = check.get("expires_in") instanceof Number n ? n.longValue() / 60 : 0;
            msg = "JFrog issued a token for " + check.get("username") + " via " + check.get("provider") + ", expires in " + mins + " min";
            if (read.get("status") != null) msg += "; read " + read.get("path") + ": HTTP " + read.get("status");
            else if (read.get("error") != null) msg += "; read " + read.get("path") + " failed: " + read.get("error");
        } else {
            msg = "JFrog refused the exchange via " + (check.get("provider") != null ? check.get("provider") : "its provider")
                    + ": " + (check.get("error") != null ? check.get("error") : "no answer");
        }
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("event", "jfrog_oidc_selftest");
        line.put("ok", Boolean.TRUE.equals(result.get("ok")));
        line.put("jfrog_status", status);
        line.put("provider", check.get("provider"));
        line.put("jfrog_user", check.get("username"));
        for (String k : List.of("iss", "sub", "aud")) if (claims.containsKey(k)) line.put(k, claims.get(k));
        line.put("message", msg.length() > 600 ? msg.substring(0, 600) : msg);
        return Json.write(line);
    }

    /** Who this app is in Azure and in JFrog (claims and token times, never the token). */
    Map<String, Object> identity() throws Failure {
        Token t = token(false);
        Map<String, Object> c = claimsOrError();
        Map<String, Object> out = new LinkedHashMap<>();
        Object who = c.get("email") != null ? c.get("email") : c.get("oid") != null ? c.get("oid") : c.get("sub");
        out.put("cloud_identity", who);
        out.put("issuer", c.get("iss"));
        out.put("audience", c.get("aud"));
        out.put("jfrog_user", t.username());
        out.put("expires_at", Instant.ofEpochSecond(t.expiresAt()).toString());
        out.put("expires_in", t.remaining(clock.getAsLong()));
        return out;
    }

    List<Map<String, Object>> repositories() throws Failure {
        Object data = getJson("/artifactory/api/repositories", "repositories");
        List<Map<String, Object>> out = new ArrayList<>();
        if (data instanceof List<?> list) {
            for (Object o : list) {
                if (!(o instanceof Map<?, ?> r) || !(r.get("key") instanceof String key) || key.isEmpty()) continue;
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("key", key);
                m.put("type", String.valueOf(r.get("type") == null ? "" : r.get("type")).toLowerCase());
                m.put("package_type", String.valueOf(r.get("packageType") == null ? "" : r.get("packageType")).toLowerCase());
                m.put("description", r.get("description") == null ? "" : r.get("description"));
                out.add(m);
            }
        }
        out.sort(Comparator.comparing(m -> ((String) m.get("key")).toLowerCase()));
        return out;
    }
}
