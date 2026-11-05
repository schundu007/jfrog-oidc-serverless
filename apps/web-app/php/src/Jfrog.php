<?php
// JFrog OIDC client for PHP on Azure App Service (PHP 8.1+, no extensions beyond the defaults).
//
// The exchange, as jfrog_oidc does it: the App Service managed identity token for
// JFROG_OIDC_AUDIENCE (IDENTITY_ENDPOINT + IDENTITY_HEADER; AZURE_CLIENT_ID picks the identity)
// is exchanged at JFrog for a short-lived access token. PHP keeps nothing between requests, so the
// token is cached in APCu (memory) when it is enabled, otherwise exchanged once per request.
// It is never written to disk, printed or logged.
//
//   $jfrog = JfrogOidc\Jfrog::fromEnv();
//   $token = $jfrog->token()->value;               // cached, refreshed for you
//   // header: "Authorization: Bearer $token"
declare(strict_types=1);

namespace JfrogOidc;

final class JfrogFailure extends \RuntimeException
{
    public function __construct(string $message, public readonly int $status = 0)
    {
        parent::__construct($message);
    }
}

final class JfrogToken
{
    public function __construct(
        #[\SensitiveParameter] public readonly string $value,
        public readonly string $username,
        public readonly int $expiresAt,
    ) {}

    public function remaining(int $now): int
    {
        return max(0, $this->expiresAt - $now);
    }

    /** var_dump/print_r show no token. */
    public function __debugInfo(): array
    {
        return ['username' => $this->username, 'expiresAt' => $this->expiresAt];
    }
}

/** One HTTP answer: status and body. */
final class HttpAnswer
{
    public function __construct(public readonly int $status, public readonly string $body) {}
}

final class Jfrog
{
    public const EXCHANGE_PATH = '/access/api/v1/oidc/token';
    private const CLAIM_KEYS = ['iss', 'aud', 'sub', 'oid', 'tid', 'azp', 'email', 'ver', 'iat', 'exp'];

    public readonly string $url;
    public readonly string $provider;
    public readonly string $audience;
    private ?JfrogToken $cached = null;
    /** @var callable(string, string, array<string,string>, ?string): HttpAnswer */
    private $http;
    /** @var callable(): int */
    private $clock;

    /** @param array<string,string> $env */
    public function __construct(private readonly array $env, ?callable $http = null, ?callable $clock = null)
    {
        $this->url = rtrim($env['JFROG_URL'] ?? '', '/');
        $this->provider = $env['JFROG_OIDC_PROVIDER'] ?? '';
        $this->audience = $env['JFROG_OIDC_AUDIENCE'] ?? '';
        $this->http = $http ?? [self::class, 'request'];
        $this->clock = $clock ?? 'time';
    }

    public static function fromEnv(): self
    {
        return new self(getenv());
    }

    /**
     * Stream options: no redirects; HTTPS_PROXY (and NO_PROXY) for https URLs only, so the identity
     * endpoint stays direct; SSL_CERT_FILE for a private CA (self-hosted JFrog).
     * @param array<string,string> $headers
     * @param array<string,string> $env
     */
    public static function streamOptions(string $method, string $url, array $headers, ?string $body, array $env): array
    {
        $http = ['method' => $method, 'content' => $body ?? '', 'follow_location' => 0, 'ignore_errors' => true, 'timeout' => 30];
        $host = strtolower((string) parse_url($url, PHP_URL_HOST));
        $proxy = $env['HTTPS_PROXY'] ?? $env['https_proxy'] ?? '';
        $skip = array_filter(array_map('trim', explode(',', strtolower($env['NO_PROXY'] ?? $env['no_proxy'] ?? ''))));
        $bypass = false;
        foreach ($skip as $n) {
            $n = ltrim($n, '.');
            $bypass = $bypass || $n === '*' || $host === $n || str_ends_with($host, ".$n");
        }
        if (str_starts_with($url, 'https://') && $proxy !== '' && !$bypass) {
            $p = parse_url(str_contains($proxy, '://') ? $proxy : "http://$proxy");
            $http['proxy'] = 'tcp://' . ($p['host'] ?? '') . ':' . ($p['port'] ?? 80);
            $http['request_fulluri'] = false;
            if (isset($p['user'])) {
                $headers['Proxy-Authorization'] = 'Basic ' . base64_encode(urldecode($p['user']) . ':' . urldecode($p['pass'] ?? ''));
            }
        }
        $lines = [];
        foreach ($headers as $k => $v) {
            $lines[] = "$k: $v";
        }
        $http['header'] = implode("\r\n", $lines);
        $ssl = ['verify_peer' => true, 'verify_peer_name' => true];
        if (($env['SSL_CERT_FILE'] ?? '') !== '') {
            $ssl['cafile'] = $env['SSL_CERT_FILE'];
        }
        return ['http' => $http, 'ssl' => $ssl];
    }

    /** GET/POST without following redirects, so the bearer token never goes elsewhere. */
    public static function request(string $method, string $url, array $headers, ?string $body): HttpAnswer
    {
        $ctx = stream_context_create(self::streamOptions($method, $url, $headers, $body, getenv()));
        $out = @file_get_contents($url, false, $ctx);
        $status = 0;
        // PHP 8.4+: http_get_last_response_headers(); older: the $http_response_header local.
        $got = function_exists('http_get_last_response_headers') ? http_get_last_response_headers() : ($http_response_header ?? []);
        foreach ($got ?? [] as $h) {
            if (preg_match('#^HTTP/\S+\s+(\d{3})#', $h, $m)) {
                $status = (int) $m[1];
            }
        }
        if ($out === false && $status === 0) {
            $err = error_get_last()['message'] ?? 'no answer';
            // The message may hold the URL: never one with a token (tokens only travel in headers).
            throw new JfrogFailure('unreachable: ' . substr($err, 0, 200));
        }
        return new HttpAnswer($status, $out === false ? '' : $out);
    }

    private function now(): int
    {
        return ($this->clock)();
    }

    private function send(string $method, string $url, array $headers, ?string $body, string $what): HttpAnswer
    {
        try {
            return ($this->http)($method, $url, $headers, $body);
        } catch (JfrogFailure $e) {
            throw new JfrogFailure("$what " . $e->getMessage());
        }
    }

    private static function detail(string $body): string
    {
        return strlen($body) > 500 ? substr($body, 0, 500) : $body;
    }

    private function configured(): void
    {
        $missing = array_values(array_filter(['JFROG_URL', 'JFROG_OIDC_PROVIDER', 'JFROG_OIDC_AUDIENCE'], fn ($k) => ($this->env[$k] ?? '') === ''));
        if ($missing) {
            throw new JfrogFailure('missing environment variables: ' . implode(', ', $missing));
        }
        if (!str_starts_with($this->url, 'https://') && ($this->env['JFROG_OIDC_ALLOW_HTTP'] ?? '') === '') {
            throw new JfrogFailure('JFROG_URL must be https');
        }
    }

    /** @return array<string,mixed> */
    private static function object(string $body, string $what): array
    {
        $v = json_decode($body, true);
        if (!is_array($v)) {
            throw new JfrogFailure("$what returned non-JSON");
        }
        return $v;
    }

    /** The managed identity's token for the JFrog audience. */
    public function identityToken(): string
    {
        $endpoint = $this->env['IDENTITY_ENDPOINT'] ?? '';
        $header = $this->env['IDENTITY_HEADER'] ?? '';
        if ($endpoint === '' || $header === '') {
            throw new JfrogFailure('no managed identity endpoint (IDENTITY_ENDPOINT/IDENTITY_HEADER): not on App Service?');
        }
        $q = ['resource' => $this->audience, 'api-version' => '2019-08-01'];
        if (($this->env['AZURE_CLIENT_ID'] ?? '') !== '') {
            $q['client_id'] = $this->env['AZURE_CLIENT_ID'];
        }
        $a = $this->send('GET', $endpoint . '?' . http_build_query($q), ['X-IDENTITY-HEADER' => $header], null, 'managed identity endpoint');
        if ($a->status !== 200) {
            throw new JfrogFailure("managed identity endpoint answered HTTP {$a->status}: " . self::detail($a->body), $a->status);
        }
        $token = self::object($a->body, 'managed identity endpoint')['access_token'] ?? '';
        if (!is_string($token) || $token === '') {
            throw new JfrogFailure('managed identity response missing access_token');
        }
        return $token;
    }

    private function cacheKey(): string
    {
        return 'jfrog-oidc:' . hash('sha256', $this->url . "\n" . $this->provider . "\n" . $this->audience);
    }

    /** A JFrog token, from the cache unless fresh or within a minute of expiry. */
    public function token(bool $fresh = false): JfrogToken
    {
        $this->configured();
        $now = $this->now();
        $apcu = function_exists('apcu_enabled') && apcu_enabled();
        if (!$fresh && $this->cached === null && $apcu) {
            $hit = apcu_fetch($this->cacheKey());
            if (is_array($hit)) {
                $this->cached = new JfrogToken($hit[0], $hit[1], $hit[2]);
            }
        }
        if (!$fresh && $this->cached !== null && $this->cached->remaining($now) > 60) {
            return $this->cached;
        }
        $subject = $this->identityToken();
        $a = $this->send('POST', $this->url . self::EXCHANGE_PATH, ['Content-Type' => 'application/json', 'Accept' => 'application/json'], json_encode([
            'grant_type' => 'urn:ietf:params:oauth:grant-type:token-exchange',
            'subject_token_type' => 'urn:ietf:params:oauth:token-type:id_token',
            'subject_token' => $subject,
            'provider_name' => $this->provider,
        ]), 'JFrog');
        if ($a->status !== 200) {
            throw new JfrogFailure("JFrog OIDC exchange failed: HTTP {$a->status} provider={$this->provider}: " . self::detail($a->body), $a->status);
        }
        $p = self::object($a->body, 'JFrog');
        $access = $p['access_token'] ?? '';
        if (!is_string($access) || $access === '') {
            throw new JfrogFailure('JFrog response missing access_token');
        }
        $claims = self::claims($access);
        $expires = is_int($p['expires_in'] ?? null) ? $now + $p['expires_in'] : (is_int($claims['exp'] ?? null) ? $claims['exp'] : $now + 3600);
        $user = is_string($p['username'] ?? null) && $p['username'] !== '' ? $p['username'] : self::usernameFromSubject((string) ($claims['sub'] ?? ''));
        $this->cached = new JfrogToken($access, $user, $expires);
        if ($apcu) {
            apcu_store($this->cacheKey(), [$access, $user, $expires], max(1, $expires - $now - 60));
        }
        return $this->cached;
    }

    public static function usernameFromSubject(string $sub): string
    {
        $k = strpos($sub, '/users/');
        return $k === false ? $sub : substr($sub, $k + strlen('/users/'));
    }

    /** A JWT's claims (no signature check: for display and mapping checks only). */
    public static function claims(string $jwt): array
    {
        $parts = explode('.', $jwt);
        if (count($parts) < 2) {
            return [];
        }
        $json = base64_decode(strtr($parts[1], '-_', '+/'), true);
        $v = $json === false ? null : json_decode($json, true);
        return is_array($v) ? $v : [];
    }

    private function authed(string $path): HttpAnswer
    {
        $t = $this->token();
        return $this->send('GET', $this->url . $path, ['Authorization' => 'Bearer ' . $t->value], null, 'JFrog');
    }

    public static function encodePath(string $p): string
    {
        return implode('/', array_map('rawurlencode', explode('/', $p)));
    }

    /** GET <repo>/<path>; returns the HTTP status (body discarded). */
    public function read(string $repoPath): int
    {
        return $this->authed('/artifactory/' . self::encodePath(ltrim($repoPath, '/')))->status;
    }

    private function claimsOrError(): array
    {
        try {
            $all = self::claims($this->identityToken());
            return array_intersect_key($all, array_flip(self::CLAIM_KEYS));
        } catch (JfrogFailure $e) {
            return ['error' => $e->getMessage()];
        }
    }

    private static function iso(int $secs): string
    {
        return gmdate('Y-m-d\TH:i:s\Z', $secs);
    }

    /** Same shape as jfrog_oidc.probe.selftest: ok, claims, check and read. */
    public function selftest(?string $checkPath, bool $fresh): array
    {
        $r = ['ok' => false, 'claims' => null, 'check' => null, 'read' => null];
        try {
            $this->configured();
        } catch (JfrogFailure $e) {
            $r['check'] = ['error' => $e->getMessage()];
            return $r;
        }
        $r['claims'] = $this->claimsOrError();
        try {
            $t = $this->token($fresh);
        } catch (JfrogFailure $e) {
            $r['check'] = ['error' => $e->getMessage(), 'provider' => $this->provider] + ($e->status > 0 ? ['status' => $e->status] : []);
            return $r;
        }
        $r['check'] = ['username' => $t->username, 'expires_in' => $t->remaining($this->now()), 'expires_at' => self::iso($t->expiresAt),
            'provider' => $this->provider, 'source' => 'azure'];
        if ($checkPath === null || $checkPath === '') {
            $r['ok'] = true;
            return $r;
        }
        $r['read'] = ['path' => $checkPath];
        try {
            $r['read']['status'] = $this->read($checkPath);
            $r['ok'] = $r['read']['status'] === 200;
        } catch (JfrogFailure $e) {
            $r['read']['error'] = $e->getMessage();
        }
        return $r;
    }

    /** One JSON line for the platform log about a selftest: what JFrog answered, never a token. */
    public static function logLine(array $result): string
    {
        $c = $result['claims'] ?? [];
        $check = $result['check'] ?? [];
        $read = $result['read'] ?? [];
        $status = $check['status'] ?? null;
        if (isset($check['username'])) {
            $status = 200;
            $msg = "JFrog issued a token for {$check['username']} via {$check['provider']}, expires in " . intdiv((int) $check['expires_in'], 60) . ' min';
            if (isset($read['status'])) {
                $msg .= "; read {$read['path']}: HTTP {$read['status']}";
            } elseif (isset($read['error'])) {
                $msg .= "; read {$read['path']} failed: {$read['error']}";
            }
        } else {
            $msg = 'JFrog refused the exchange via ' . ($check['provider'] ?? 'its provider') . ': ' . ($check['error'] ?? 'no answer');
        }
        $line = ['event' => 'jfrog_oidc_selftest', 'ok' => ($result['ok'] ?? false) === true, 'jfrog_status' => $status,
            'provider' => $check['provider'] ?? null, 'jfrog_user' => $check['username'] ?? null];
        foreach (['iss', 'sub', 'aud'] as $k) {
            if (array_key_exists($k, $c)) {
                $line[$k] = $c[$k];
            }
        }
        $line['message'] = substr($msg, 0, 600);
        return json_encode($line, JSON_UNESCAPED_SLASHES);
    }

    /** Who this app is in Azure and in JFrog (claims and token times, never the token). */
    public function identity(): array
    {
        $t = $this->token();
        $c = $this->claimsOrError();
        return ['cloud_identity' => $c['email'] ?? $c['oid'] ?? $c['sub'] ?? null, 'issuer' => $c['iss'] ?? null, 'audience' => $c['aud'] ?? null,
            'jfrog_user' => $t->username, 'expires_at' => self::iso($t->expiresAt), 'expires_in' => $t->remaining($this->now())];
    }

    public function repositories(): array
    {
        $a = $this->authed('/artifactory/api/repositories');
        if ($a->status !== 200) {
            throw new JfrogFailure("JFrog repositories: HTTP {$a->status}", $a->status);
        }
        $list = json_decode($a->body, true);
        if (!is_array($list)) {
            throw new JfrogFailure('JFrog repositories: not JSON');
        }
        $s = fn ($x) => is_string($x) ? $x : '';
        $out = [];
        foreach ($list as $r) {
            if (is_array($r) && $s($r['key'] ?? null) !== '') {
                $out[] = ['key' => $r['key'], 'type' => strtolower($s($r['type'] ?? null)), 'package_type' => strtolower($s($r['packageType'] ?? null)),
                    'description' => $s($r['description'] ?? null)];
            }
        }
        usort($out, fn ($a, $b) => strcasecmp($a['key'], $b['key']));
        return $out;
    }
}
