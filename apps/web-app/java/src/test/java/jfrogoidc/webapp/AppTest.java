package jfrogoidc.webapp;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** The real app against emulated endpoints: the App Service identity endpoint and JFrog. */
class AppTest {
    static final String MI_TOKEN = jwt("{\"iss\":\"https://sts.windows.net/t/\",\"aud\":\"api://jfrog\",\"oid\":\"oid-1\",\"sub\":\"oid-1\"}");
    static final String JF_TOKEN = jwt("{\"sub\":\"jfac@x/users/webapp-sa\",\"exp\":4102444800}");

    HttpServer fake;
    HttpServer app;
    String base;
    final AtomicInteger exchanges = new AtomicInteger();
    final AtomicReference<String> exchangeBody = new AtomicReference<>();
    final AtomicReference<String> identityQuery = new AtomicReference<>();
    int exchangeStatus = 200;
    long now = 1_000_000;

    static String jwt(String claims) {
        Base64.Encoder e = Base64.getUrlEncoder().withoutPadding();
        return e.encodeToString("{\"alg\":\"none\"}".getBytes()) + "." + e.encodeToString(claims.getBytes()) + ".sig";
    }

    static void reply(com.sun.net.httpserver.HttpExchange ex, int status, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.sendResponseHeaders(status, b.length == 0 ? -1 : b.length);
        if (b.length > 0) ex.getResponseBody().write(b);
        ex.close();
    }

    @BeforeEach
    void up() throws IOException {
        fake = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fake.createContext("/msi/token", ex -> {
            identityQuery.set(ex.getRequestURI().getRawQuery());
            boolean ok = "secret-header".equals(ex.getRequestHeaders().getFirst("X-IDENTITY-HEADER"));
            reply(ex, ok ? 200 : 400, ok ? "{\"access_token\":\"" + MI_TOKEN + "\",\"expires_on\":\"9999999999\"}" : "{}");
        });
        fake.createContext("/access/api/v1/oidc/token", ex -> {
            exchanges.incrementAndGet();
            exchangeBody.set(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            if (exchangeStatus != 200) {
                reply(ex, exchangeStatus, "{\"errors\":[{\"message\":\"no matching identity mapping\"}]}");
            } else {
                reply(ex, 200, "{\"access_token\":\"" + JF_TOKEN + "\",\"expires_in\":3600}");
            }
        });
        fake.createContext("/artifactory/api/repositories", ex -> {
            boolean ok = ("Bearer " + JF_TOKEN).equals(ex.getRequestHeaders().getFirst("Authorization"));
            reply(ex, ok ? 200 : 401, ok ? "[{\"key\":\"pypi-remote\",\"type\":\"REMOTE\",\"packageType\":\"Pypi\"},"
                    + "{\"key\":\"generic-local\",\"type\":\"LOCAL\",\"packageType\":\"Generic\",\"description\":\"<b>x</b>\"}]" : "");
        });
        fake.createContext("/artifactory/generic-local/", ex -> {
            boolean ok = ("Bearer " + JF_TOKEN).equals(ex.getRequestHeaders().getFirst("Authorization"));
            boolean exists = ex.getRequestURI().getPath().endsWith("/hello.txt");
            reply(ex, !ok ? 401 : exists ? 200 : 404, exists ? "hello" : "");
        });
        fake.start();
        String fakeUrl = "http://127.0.0.1:" + fake.getAddress().getPort();
        Map<String, String> env = new HashMap<>(Map.of(
                "JFROG_URL", fakeUrl + "/", "JFROG_OIDC_PROVIDER", "azure-webapp", "JFROG_OIDC_AUDIENCE", "api://jfrog",
                "JFROG_OIDC_ALLOW_HTTP", "1", "IDENTITY_ENDPOINT", fakeUrl + "/msi/token",
                "IDENTITY_HEADER", "secret-header", "AZURE_CLIENT_ID", "client-1"));
        Jfrog jfrog = new Jfrog(env, HttpClient.newHttpClient(), () -> now);
        app = new App(jfrog, "abc123", "/generic-local/hello.txt").start(0);
        base = "http://127.0.0.1:" + app.getAddress().getPort();
    }

    @AfterEach
    void down() {
        app.stop(0);
        fake.stop(0);
    }

    HttpResponse<String> get(String path) throws Exception {
        return HttpClient.newHttpClient().send(HttpRequest.newBuilder(URI.create(base + path)).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    void noTokens(String body) {
        assertFalse(body.contains(MI_TOKEN), "identity token leaked");
        assertFalse(body.contains(JF_TOKEN), "JFrog token leaked");
        assertFalse(body.contains("secret-header"), "identity header leaked");
    }

    @Test
    void healthIsUpWithoutJfrog() throws Exception {
        HttpResponse<String> r = get("/health");
        assertEquals(200, r.statusCode());
        assertEquals("abc123", Json.object(r.body()).get("version"));
        assertEquals(0, exchanges.get());
        assertTrue(r.headers().firstValue("Content-Security-Policy").isPresent());
    }

    @Test
    void selftestExchangesAndReads() throws Exception {
        HttpResponse<String> r = get("/healthz/jfrog");
        assertEquals(200, r.statusCode(), r.body());
        Map<String, Object> body = Json.object(r.body());
        assertEquals(Boolean.TRUE, body.get("ok"));
        Map<?, ?> check = (Map<?, ?>) body.get("check");
        assertEquals("webapp-sa", check.get("username"));
        assertEquals("azure-webapp", check.get("provider"));
        assertEquals("oid-1", ((Map<?, ?>) body.get("claims")).get("oid"));
        assertEquals(200.0, ((Map<?, ?>) body.get("read")).get("status"));
        noTokens(r.body());
        assertTrue(identityQuery.get().contains("resource=api%3A%2F%2Fjfrog"));
        assertTrue(identityQuery.get().contains("client_id=client-1"));
        Map<String, Object> sent = Json.object(exchangeBody.get());
        assertEquals("azure-webapp", sent.get("provider_name"));
        assertEquals(MI_TOKEN, sent.get("subject_token"));
        assertEquals("urn:ietf:params:oauth:token-type:id_token", sent.get("subject_token_type"));
    }

    @Test
    void tokenIsCachedUntilFreshOrNearExpiry() throws Exception {
        get("/api/v1/identity");
        get("/api/v1/repositories");
        assertEquals(1, exchanges.get());
        get("/healthz/jfrog?fresh=1");
        assertEquals(2, exchanges.get());
        now += 3600 - 30;   // inside the last minute
        get("/readyz");
        assertEquals(3, exchanges.get());
    }

    @Test
    void refusedExchangeIsReportedNotThrown() throws Exception {
        exchangeStatus = 403;
        HttpResponse<String> r = get("/healthz/jfrog");
        assertEquals(503, r.statusCode());
        Map<?, ?> check = (Map<?, ?>) Json.object(r.body()).get("check");
        assertEquals(403.0, check.get("status"));
        assertTrue(String.valueOf(check.get("error")).contains("HTTP 403"));
        assertEquals(403, get("/api/v1/identity").statusCode());
        assertEquals(503, get("/readyz").statusCode());
        noTokens(r.body());
    }

    @Test
    void repositoriesSortedAndHomeEscaped() throws Exception {
        HttpResponse<String> r = get("/api/v1/repositories");
        List<?> repos = (List<?>) Json.object(r.body()).get("repositories");
        assertEquals("generic-local", ((Map<?, ?>) repos.get(0)).get("key"));
        assertEquals("remote", ((Map<?, ?>) repos.get(1)).get("type"));
        HttpResponse<String> home = get("/");
        assertEquals(200, home.statusCode());
        assertTrue(home.body().contains("webapp-sa"));
        assertFalse(home.body().contains("<b>x</b>"));
        noTokens(home.body() + r.body());
    }

    @Test
    void defaultClientUsesTheJvmProxySettings() {
        // -Dhttps.proxyHost/-Dhttps.proxyPort (JAVA_OPTS) for JFrog; HttpClient's own default is no proxy at all.
        assertTrue(Jfrog.defaultHttp().proxy().isPresent());
        assertEquals(HttpClient.Redirect.NORMAL, Jfrog.defaultHttp().followRedirects());
    }

    @Test
    void missingConfigSaysWhat() throws Exception {
        Jfrog bare = new Jfrog(Map.of(), HttpClient.newHttpClient(), () -> now);
        Map<String, Object> r = bare.selftest("", false);
        assertEquals(Boolean.FALSE, r.get("ok"));
        assertTrue(String.valueOf(((Map<?, ?>) r.get("check")).get("error")).contains("JFROG_URL"));
    }

    @Test
    void portFromPlatform() {
        assertEquals(8123, App.port(Map.of("HTTP_PLATFORM_PORT", "8123", "PORT", "9"), null));
        assertEquals(9000, App.port(Map.of("PORT", "9000"), null));
        assertEquals(8081, App.port(Map.of(), "8081"));
        assertEquals(80, App.port(Map.of(), null));
    }

    @Test
    void unknownPathAndMethod() throws Exception {
        assertEquals(404, get("/nope").statusCode());
        HttpResponse<String> r = HttpClient.newHttpClient().send(HttpRequest.newBuilder(URI.create(base + "/health"))
                .POST(HttpRequest.BodyPublishers.noBody()).build(), HttpResponse.BodyHandlers.ofString());
        assertEquals(405, r.statusCode());
    }
}
