<?php
declare(strict_types=1);

namespace JfrogOidc;

/** The routes, as a pure function of the request, so tests need no web server. */
final class App
{
    public const CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'";

    private static function esc(mixed $s): string
    {
        return htmlspecialchars((string) ($s ?? ''), ENT_QUOTES | ENT_HTML5, 'UTF-8');
    }

    /** @return array{0:int,1:array<string,string>,2:string} */
    public static function handle(Jfrog $jfrog, ?string $checkPath, string $method, string $uri, string $version): array
    {
        $headers = ['Content-Type' => 'application/json', 'Content-Security-Policy' => self::CSP, 'X-Content-Type-Options' => 'nosniff',
            'X-Frame-Options' => 'DENY', 'Cache-Control' => 'no-store', 'Referrer-Policy' => 'no-referrer'];
        $json = fn (int $s, mixed $v) => [$s, $headers, json_encode($v, JSON_UNESCAPED_SLASHES)];
        $path = parse_url($uri, PHP_URL_PATH) ?: '/';
        parse_str((string) parse_url($uri, PHP_URL_QUERY), $q);
        $check = $checkPath === null ? null : trim($checkPath, '/');
        if ($method !== 'GET') {
            return $json(404, ['error' => 'not found']);
        }
        try {
            switch ($path) {
                case '/health':
                case '/healthz':
                    return $json(200, ['status' => 'ok', 'version' => $version]);
                case '/readyz':
                    try {
                        $jfrog->token();
                        return $json(200, ['status' => 'ready']);
                    } catch (JfrogFailure $e) {
                        return $json(503, ['status' => 'unavailable', 'reason' => $e->getMessage()]);
                    }
                case '/healthz/jfrog':
                    $r = $jfrog->selftest($check, ($q['fresh'] ?? '') === '1');
                    error_log(Jfrog::logLine($r));
                    return $json($r['ok'] ? 200 : 503, $r);
                case '/api/v1/identity':
                    return $json(200, $jfrog->identity());
                case '/api/v1/repositories':
                    return $json(200, ['repositories' => $jfrog->repositories()]);
                case '/':
                    $b = '<!doctype html><html lang=en><meta charset=utf-8><title>JFrog browser (PHP)</title>'
                        . '<style>body{font:14px system-ui,sans-serif;margin:24px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>'
                        . '<h1>JFrog browser</h1><p>PHP ' . self::esc(PHP_VERSION) . ' &middot; ' . self::esc($version) . '</p>';
                    try {
                        $b .= '<h2>Identity</h2><table>';
                        foreach ($jfrog->identity() as $k => $v) {
                            $b .= '<tr><th>' . self::esc($k) . '<td>' . self::esc($v);
                        }
                        $b .= '</table><h2>Repositories</h2><table><tr><th>Key<th>Type<th>Package';
                        foreach ($jfrog->repositories() as $r) {
                            $b .= '<tr><td>' . self::esc($r['key']) . '<td>' . self::esc($r['type']) . '<td>' . self::esc($r['package_type']);
                        }
                        $b .= '</table>';
                    } catch (JfrogFailure $e) {
                        $b .= '<p>' . self::esc($e->getMessage()) . '</p>';
                    }
                    return [200, ['Content-Type' => 'text/html; charset=utf-8'] + $headers, $b];
                default:
                    return $json(404, ['error' => 'not found']);
            }
        } catch (JfrogFailure $e) {
            return $json($e->status >= 400 ? $e->status : 502, ['error' => $e->getMessage()]);
        }
    }
}
