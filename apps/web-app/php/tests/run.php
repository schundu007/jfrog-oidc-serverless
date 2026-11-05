<?php
// php tests/run.php  (no dependencies). Fakes the App Service identity endpoint and JFrog with an
// in-process HTTP function, so no server or network is needed.
declare(strict_types=1);

require __DIR__ . '/../src/Jfrog.php';
require __DIR__ . '/../src/App.php';

use JfrogOidc\{App, HttpAnswer, Jfrog, JfrogFailure};

$fails = 0;
$passes = 0;
function check(bool $ok, string $what): void
{
    global $fails, $passes;
    $ok ? $passes++ : $fails++;
    if (!$ok) {
        fwrite(STDERR, "FAIL: $what\n");
    }
}

$b64 = fn (array $o) => rtrim(strtr(base64_encode(json_encode($o)), '+/', '-_'), '=');
$jwt = fn (array $c) => $b64(['alg' => 'none']) . '.' . $b64($c) . '.sig';
const AUD = 'api://11111111-2222-3333-4444-555555555555';
$ENTRA = $jwt(['iss' => 'https://login.microsoftonline.com/t/v2.0', 'aud' => AUD, 'sub' => 's', 'oid' => 'o', 'tid' => 't']);
$ACCESS = $jwt(['sub' => 'jfac@x/users/web-app', 'exp' => 4102444800]);

function fake(string $entra, string $access, bool $refuse = false): array
{
    $seen = new ArrayObject(['identity' => 0, 'exchange' => 0, 'auth' => []]);
    $http = function (string $method, string $url, array $headers, ?string $body) use ($seen, $entra, $access, $refuse): HttpAnswer {
        $u = parse_url($url);
        parse_str($u['query'] ?? '', $q);
        $path = $u['path'];
        if ($path === '/identity') {
            $seen['identity']++;
            $seen['header'] = $headers['X-IDENTITY-HEADER'] ?? null;
            $seen['resource'] = $q['resource'] ?? null;
            $seen['client_id'] = $q['client_id'] ?? null;
            return new HttpAnswer(200, json_encode(['access_token' => $entra]));
        }
        if ($path === Jfrog::EXCHANGE_PATH) {
            $seen['exchange']++;
            $seen['body'] = json_decode($body, true);
            return $refuse ? new HttpAnswer(401, '{"errors":[{"message":"no mapping"}]}') : new HttpAnswer(200, json_encode(['access_token' => $access, 'expires_in' => 3600]));
        }
        $a = $seen['auth'];
        $a[] = $headers['Authorization'] ?? null;
        $seen['auth'] = $a;
        if ($path === '/artifactory/api/repositories') {
            return new HttpAnswer(200, json_encode([['key' => 'b-remote', 'type' => 'REMOTE', 'packageType' => 'Composer'], ['key' => 'A-local', 'type' => 'LOCAL', 'packageType' => 'Generic']]));
        }
        $seen['read'] = $path;
        return new HttpAnswer(str_ends_with($path, 'ping.txt') ? 200 : 404, 'pong');
    };
    return [$http, $seen];
}

$env = ['JFROG_URL' => 'https://jfrog.test/', 'JFROG_OIDC_PROVIDER' => 'azure-entra', 'JFROG_OIDC_AUDIENCE' => AUD,
    'IDENTITY_ENDPOINT' => 'http://169.254.0.1/identity', 'IDENTITY_HEADER' => 'h', 'AZURE_CLIENT_ID' => 'cid'];

// exchange and cache
[$http, $seen] = fake($ENTRA, $ACCESS);
$now = 1000;
$j = new Jfrog($env, $http, function () use (&$now) { return $now; });
$t = $j->token();
check($t->username === 'web-app', 'username from subject');
check($t->expiresAt === 4600, 'expiry from expires_in');
check($seen['header'] === 'h' && $seen['resource'] === AUD && $seen['client_id'] === 'cid', 'identity request');
check($seen['body']['provider_name'] === 'azure-entra' && $seen['body']['subject_token'] === $ENTRA, 'exchange body');
check($seen['body']['grant_type'] === 'urn:ietf:params:oauth:grant-type:token-exchange', 'grant type');
$j->token();
check($seen['exchange'] === 1, 'cached');
$now = 4600 - 30;
$j->token();
check($seen['exchange'] === 2, 'refreshed within a minute of expiry');
$j->token(true);
check($seen['exchange'] === 3, 'fresh');

// the token never shows up
check(!str_contains(print_r($t, true), $ACCESS) && !str_contains(var_export($t->__debugInfo(), true), $ACCESS), 'no token in dumps');
[$http, $seen] = fake($ENTRA, $ACCESS);
$j = new Jfrog($env, $http);
$r = $j->selftest('generic-local/ping.txt', false);
$all = json_encode($r) . Jfrog::logLine($r) . json_encode($j->identity());
check(!str_contains($all, $ACCESS) && !str_contains($all, $ENTRA), 'no token in selftest, log line or identity');
check($r['ok'] === true && $r['read'] === ['path' => 'generic-local/ping.txt', 'status' => 200], 'selftest read');
check($seen['auth'][0] === "Bearer $ACCESS", 'bearer on the read');
check($seen['read'] === '/artifactory/generic-local/ping.txt', 'read path');

// refused
[$http] = fake($ENTRA, $ACCESS, true);
$j = new Jfrog($env, $http);
$r = $j->selftest('', false);
check($r['ok'] === false && $r['check']['status'] === 401, 'refused selftest');
check(str_contains(Jfrog::logLine($r), 'JFrog refused the exchange via azure-entra'), 'refused log line');
[$s] = App::handle($j, null, 'GET', '/readyz', '1');
check($s === 503, 'readyz 503 when refused');

// configuration
$msg = function (array $e): string {
    try {
        (new Jfrog($e, fn () => new HttpAnswer(500, '')))->token();
        return '';
    } catch (JfrogFailure $x) {
        return $x->getMessage();
    }
};
check(str_contains($msg(['JFROG_URL' => 'http://x', 'JFROG_OIDC_PROVIDER' => 'p', 'JFROG_OIDC_AUDIENCE' => 'a']), 'must be https'), 'https only');
check(str_contains($msg([]), 'missing environment variables: JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE'), 'all settings');
check(str_contains($msg(['JFROG_URL' => 'https://x', 'JFROG_OIDC_PROVIDER' => 'p', 'JFROG_OIDC_AUDIENCE' => 'a']), 'not on App Service'), 'needs identity endpoint');
check(Jfrog::usernameFromSubject('jfac@01/users/svc') === 'svc' && Jfrog::usernameFromSubject('plain') === 'plain', 'username from subject');
check(Jfrog::encodePath('a b/c') === 'a%20b/c', 'path encoding');

// proxy: HTTPS_PROXY for JFrog (https) only; the identity endpoint (http) and NO_PROXY hosts go direct
$o = Jfrog::streamOptions('GET', 'https://jfrog.test/x', [], null, ['HTTPS_PROXY' => 'http://proxy.corp:3128']);
check($o['http']['proxy'] === 'tcp://proxy.corp:3128' && $o['http']['request_fulluri'] === false, 'https via proxy');
$o = Jfrog::streamOptions('GET', 'https://jfrog.test/x', [], null, ['https_proxy' => 'http://u:p@proxy.corp:3128']);
check($o['http']['proxy'] === 'tcp://proxy.corp:3128' && str_contains($o['http']['header'], 'Proxy-Authorization: Basic ' . base64_encode('u:p')), 'proxy credentials');
$o = Jfrog::streamOptions('GET', 'http://169.254.0.1/identity', [], null, ['HTTPS_PROXY' => 'http://proxy.corp:3128']);
check(!isset($o['http']['proxy']), 'identity endpoint direct');
$o = Jfrog::streamOptions('GET', 'https://a.jfrog.test/x', [], null, ['HTTPS_PROXY' => 'http://p:1', 'NO_PROXY' => 'localhost,.jfrog.test']);
check(!isset($o['http']['proxy']), 'NO_PROXY suffix');
$o = Jfrog::streamOptions('GET', 'https://jfrog.test/x', [], null, ['SSL_CERT_FILE' => '/certs/corp.pem']);
check(($o['ssl']['cafile'] ?? null) === '/certs/corp.pem' && $o['ssl']['verify_peer'] === true, 'custom CA file');
check(Jfrog::streamOptions('GET', 'https://jfrog.test/x', [], null, [])['http']['follow_location'] === 0, 'no redirects');

// routes
[$http] = fake($ENTRA, $ACCESS);
$j = new Jfrog($env, $http);
[$s, $h, $b] = App::handle($j, '/generic-local/ping.txt/', 'GET', '/health', '9.9.9');
check($s === 200 && json_decode($b, true)['version'] === '9.9.9', '/health');
check(str_contains($h['Content-Security-Policy'], "default-src 'none'") && $h['X-Frame-Options'] === 'DENY', 'security headers');
[$s] = App::handle($j, null, 'GET', '/readyz', '1');
check($s === 200, '/readyz');
ini_set('error_log', '/dev/null');
[$s, , $b] = App::handle($j, '/generic-local/ping.txt/', 'GET', '/healthz/jfrog?fresh=1', '1');
check($s === 200 && json_decode($b, true)['read']['path'] === 'generic-local/ping.txt', '/healthz/jfrog');
[, , $b] = App::handle($j, null, 'GET', '/api/v1/identity', '1');
check(json_decode($b, true)['jfrog_user'] === 'web-app', '/api/v1/identity');
[, , $b] = App::handle($j, null, 'GET', '/api/v1/repositories', '1');
check(array_column(json_decode($b, true)['repositories'], 'key') === ['A-local', 'b-remote'], '/api/v1/repositories sorted');
[$s, $h, $b] = App::handle($j, null, 'GET', '/', '1');
check($s === 200 && str_contains($b, 'A-local') && !str_contains($b, $ACCESS) && str_starts_with($h['Content-Type'], 'text/html'), '/');
[$s] = App::handle($j, null, 'GET', '/nope', '1');
check($s === 404, '404');
[$s] = App::handle($j, null, 'POST', '/health', '1');
check($s === 404, 'GET only');

echo "pass $passes, fail $fails\n";
exit($fails ? 1 : 0);
