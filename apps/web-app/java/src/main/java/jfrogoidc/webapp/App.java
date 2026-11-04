package jfrogoidc.webapp;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.Executors;

/**
 * JFrog browser for App Service (Java SE), with the same endpoints as the Python service
 * (apps/cloud-run-service), so the console verifies all three runtimes the same way:
 *
 *   /                      identity and repositories (HTML)
 *   /api/v1/identity       who this app is in Azure and in JFrog
 *   /api/v1/repositories   the repositories JFrog lets it see
 *   /health, /healthz      process up
 *   /readyz                a JFrog token can be had
 *   /healthz/jfrog         claims, exchange and a read of JFROG_HEALTHCHECK_PATH (?fresh=1 exchanges again)
 *
 * Port: HTTP_PLATFORM_PORT (Windows), PORT, SERVER_PORT or -Dserver.port; else 80 (Linux Java SE).
 */
public final class App {
    static final String CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'";

    private final Jfrog jfrog;
    private final String version;
    private final String checkPath;
    private final long started = Instant.now().getEpochSecond();

    App(Jfrog jfrog, String version, String checkPath) {
        this.jfrog = jfrog;
        this.version = version;
        this.checkPath = checkPath == null ? "" : checkPath.replaceAll("^/+|/+$", "");
    }

    public static void main(String[] args) throws IOException {
        int port = port(System.getenv(), System.getProperty("server.port"));
        String version = System.getenv().getOrDefault("APP_VERSION", "");
        if (version.isEmpty()) version = buildVersion();
        App app = new App(Jfrog.fromEnv(), version, System.getenv("JFROG_HEALTHCHECK_PATH"));
        HttpServer server = app.start(port);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> server.stop(2)));
        System.out.println(Json.write(Map.of("event", "started", "port", port, "version", version)));
    }

    static int port(Map<String, String> env, String property) {
        for (String k : List.of("HTTP_PLATFORM_PORT", "PORT", "SERVER_PORT")) {
            String v = env.get(k);
            if (v != null && v.matches("\\d+")) return Integer.parseInt(v);
        }
        return property != null && property.matches("\\d+") ? Integer.parseInt(property) : 80;
    }

    private static String buildVersion() {
        try (InputStream in = App.class.getResourceAsStream("/app.properties")) {
            Properties p = new Properties();
            if (in != null) p.load(in);
            return p.getProperty("version", "dev");
        } catch (IOException e) {
            return "dev";
        }
    }

    HttpServer start(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", this::handle);
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        server.start();
        return server;
    }

    private void handle(HttpExchange ex) throws IOException {
        try (ex) {
            String path = ex.getRequestURI().getPath();
            if (!"GET".equals(ex.getRequestMethod()) && !"HEAD".equals(ex.getRequestMethod())) {
                json(ex, 405, Map.of("error", "method not allowed"));
                return;
            }
            switch (path) {
                case "/health", "/healthz" -> json(ex, 200, Map.of("status", "ok", "version", version,
                        "uptime_seconds", Instant.now().getEpochSecond() - started));
                case "/readyz" -> {
                    try {
                        jfrog.token(false);
                        json(ex, 200, Map.of("status", "ready"));
                    } catch (Jfrog.Failure f) {
                        json(ex, 503, Map.of("status", "unavailable", "reason", f.getMessage()));
                    }
                }
                case "/healthz/jfrog" -> {
                    String q = ex.getRequestURI().getQuery();
                    Map<String, Object> r = jfrog.selftest(checkPath, q != null && q.contains("fresh=1"));
                    System.out.println(Jfrog.logLine(r));
                    json(ex, Boolean.TRUE.equals(r.get("ok")) ? 200 : 503, r);
                }
                case "/api/v1/identity" -> guarded(ex, () -> jfrog.identity());
                case "/api/v1/repositories" -> guarded(ex, () -> Map.of("repositories", jfrog.repositories()));
                case "/" -> home(ex);
                default -> json(ex, 404, Map.of("error", "not found"));
            }
        } catch (RuntimeException e) {
            System.out.println(Json.write(Map.of("event", "error", "message", String.valueOf(e))));
            json(ex, 500, Map.of("error", "internal error"));
        }
    }

    interface Call {
        Object get() throws Jfrog.Failure;
    }

    private void guarded(HttpExchange ex, Call call) throws IOException {
        try {
            json(ex, 200, call.get());
        } catch (Jfrog.Failure f) {
            json(ex, f.status >= 400 ? f.status : 502, Map.of("error", f.getMessage()));
        }
    }

    private void home(HttpExchange ex) throws IOException {
        StringBuilder b = new StringBuilder();
        b.append("<!doctype html><html lang=en><meta charset=utf-8><title>JFrog browser (Java)</title>")
         .append("<style>body{font:14px system-ui,sans-serif;margin:24px}table{border-collapse:collapse}")
         .append("td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>")
         .append("<h1>JFrog browser</h1><p>Java ").append(esc(System.getProperty("java.version")))
         .append(" &middot; ").append(esc(version)).append("</p>");
        try {
            Map<String, Object> id = jfrog.identity();
            b.append("<h2>Identity</h2><table>");
            for (Map.Entry<String, Object> e : id.entrySet()) {
                b.append("<tr><th>").append(esc(e.getKey())).append("<td>").append(esc(String.valueOf(e.getValue())));
            }
            b.append("</table><h2>Repositories</h2><table><tr><th>Key<th>Type<th>Package");
            for (Map<String, Object> r : jfrog.repositories()) {
                b.append("<tr><td>").append(esc((String) r.get("key"))).append("<td>").append(esc((String) r.get("type")))
                 .append("<td>").append(esc((String) r.get("package_type")));
            }
            b.append("</table>");
        } catch (Jfrog.Failure f) {
            b.append("<p>").append(esc(f.getMessage())).append("</p>");
        }
        send(ex, 200, "text/html; charset=utf-8", b.toString());
    }

    static String esc(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&#39;");
    }

    private static void json(HttpExchange ex, int status, Object body) throws IOException {
        send(ex, status, "application/json", Json.write(body));
    }

    private static void send(HttpExchange ex, int status, String type, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        var h = ex.getResponseHeaders();
        h.set("Content-Type", type);
        h.set("Content-Security-Policy", CSP);
        h.set("X-Content-Type-Options", "nosniff");
        h.set("Referrer-Policy", "no-referrer");
        h.set("X-Frame-Options", "DENY");
        h.set("Cache-Control", "no-store");
        boolean head = "HEAD".equals(ex.getRequestMethod());
        ex.sendResponseHeaders(status, head ? -1 : bytes.length);
        if (!head) {
            try (OutputStream out = ex.getResponseBody()) {
                out.write(bytes);
            }
        }
    }
}
