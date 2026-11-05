<?php
// JFrog browser for App Service (PHP), with the same endpoints as the Python, Java, .NET and
// Node.js apps, so the console verifies every runtime the same way:
//
//   /                      identity and repositories (HTML)
//   /api/v1/identity       who this app is in Azure and in JFrog
//   /api/v1/repositories   the repositories JFrog lets it see
//   /health, /healthz      process up
//   /readyz                a JFrog token can be had
//   /healthz/jfrog         claims, exchange and a read of JFROG_HEALTHCHECK_PATH (?fresh=1 exchanges again)
//
// Every path comes here (the stack's start command runs PHP's server with this file as router).
declare(strict_types=1);

require __DIR__ . '/src/Jfrog.php';
require __DIR__ . '/src/App.php';

[$status, $headers, $body] = JfrogOidc\App::handle(
    JfrogOidc\Jfrog::fromEnv(),
    getenv('JFROG_HEALTHCHECK_PATH') ?: null,
    $_SERVER['REQUEST_METHOD'] ?? 'GET',
    $_SERVER['REQUEST_URI'] ?? '/',
    getenv('APP_VERSION') ?: '1.0.0',
);
http_response_code($status);
foreach ($headers as $k => $v) {
    header("$k: $v");
}
echo $body;
