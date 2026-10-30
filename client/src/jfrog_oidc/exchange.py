"""Exchange a platform identity token for a short-lived JFrog access token.

JFrogTokenProvider is safe to create once per process (module scope in a
function) and call from many threads. It caches the JFrog token and refreshes
it shortly before expiry, so a warm instance makes one exchange per token
lifetime instead of one per request.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import ExchangeError, IdentityError
from .sources import IdentitySource, detect_source

log = logging.getLogger("jfrog_oidc")

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
EXCHANGE_PATH = "/access/api/v1/oidc/token"


@dataclass(frozen=True)
class JFrogToken:
    access_token: str
    username: str
    expires_at: float  # unix seconds

    def remaining(self, now: Optional[float] = None) -> float:
        return self.expires_at - (now if now is not None else time.time())

    def __repr__(self) -> str:  # never print the token
        return f"JFrogToken(username={self.username!r}, expires_at={self.expires_at:.0f})"


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def _username_from_subject(sub: str) -> str:
    # JFrog access-token subjects look like "jfac@<id>/users/<username>".
    marker = "/users/"
    return sub.split(marker, 1)[1] if marker in sub else sub


class JFrogTokenProvider:
    """Cached, thread-safe JFrog token provider backed by OIDC exchange.

    Args:
        jfrog_url:      Platform base URL, e.g. https://acme.jfrog.io
        provider_name:  JFrog OIDC integration name (must match exactly).
        audience:       Value requested from the identity source
                        (GCP: the integration audience; Azure: api://<app-id>).
        source:         IdentitySource; auto-detected when omitted.
        project_key:    Optional JFrog project key for project-scoped mappings.
        refresh_skew:   Refresh when fewer than this many seconds remain.
                        Clamped to a third of the token lifetime for short TTLs.
        max_attempts:   Attempts per refresh for retryable failures.
    """

    def __init__(
        self,
        jfrog_url: str,
        provider_name: str,
        audience: str,
        source: Optional[IdentitySource] = None,
        project_key: Optional[str] = None,
        refresh_skew: float = 300.0,
        timeout: float = 10.0,
        max_attempts: int = 3,
        failure_backoff: float = 30.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not jfrog_url or not provider_name or not audience:
            raise ValueError("jfrog_url, provider_name and audience are required")
        self.jfrog_url = jfrog_url.rstrip("/")
        if not os.environ.get("JFROG_OIDC_ALLOW_HTTP"):
            # JFROG_OIDC_ALLOW_HTTP is the test/simulator seam (plain http, path prefix).
            if not self.jfrog_url.startswith("https://"):
                raise ValueError("jfrog_url must use https (e.g. https://acme.jfrog.io)")
            parts = urllib.parse.urlsplit(self.jfrog_url)
            if parts.path or parts.query or parts.fragment or parts.username or parts.password:
                # This client appends /access/... and /artifactory/... itself. A URL with a
                # path would double them up, the way the AKS kubelet plugin turned a
                # scheme it also prepends into https://https://... (docs/aks-pilot-lessons.md).
                raise ValueError(f"jfrog_url must be the platform origin only, e.g. https://{parts.hostname}; "
                                 "drop the path (such as /artifactory)")
        self.provider_name = provider_name
        self.audience = audience
        self.source = source or detect_source()
        self.project_key = project_key
        self.refresh_skew = refresh_skew
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.failure_backoff = failure_backoff
        self._retry_after = 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cached: Optional[JFrogToken] = None
        self._lifetime: float = 0.0

    @classmethod
    def from_env(cls, **overrides) -> "JFrogTokenProvider":
        """Build from JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE, JFROG_PROJECT_KEY."""
        missing = [v for v in ("JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE") if not os.environ.get(v)]
        if missing and not all(k in overrides for k in ("jfrog_url", "provider_name", "audience")):
            raise ValueError(f"missing environment variables: {', '.join(missing)}")
        kwargs = dict(
            jfrog_url=os.environ.get("JFROG_URL", ""),
            provider_name=os.environ.get("JFROG_OIDC_PROVIDER", ""),
            audience=os.environ.get("JFROG_OIDC_AUDIENCE", ""),
            project_key=os.environ.get("JFROG_PROJECT_KEY") or None,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- public API ---------------------------------------------------------

    def token(self) -> JFrogToken:
        """Return a valid token, exchanging a new one when needed."""
        cached = self._cached
        if cached and not self._needs_refresh(cached):
            return cached
        usable = cached is not None and cached.remaining(self._clock()) > 30
        if usable:
            # Still valid: never make callers queue behind a slow refresh, and
            # back off after a failure instead of retrying on every call.
            if self._clock() < self._retry_after:
                return cached
            if not self._lock.acquire(blocking=False):
                return cached
        else:
            self._lock.acquire()
        try:
            cached = self._cached
            if cached and not self._needs_refresh(cached):
                return cached
            try:
                fresh = self._exchange_with_retry()
            except (ExchangeError, IdentityError) as exc:
                # Keep serving the old token while it is still valid; a JFrog or
                # metadata blip should not take down a warm instance.
                if cached and cached.remaining(self._clock()) > 30:
                    self._retry_after = self._clock() + self.failure_backoff
                    log.warning(
                        "jfrog_oidc refresh failed, serving cached token (%.0fs left): %s",
                        cached.remaining(self._clock()),
                        exc,
                    )
                    return cached
                raise
            self._retry_after = 0.0
            self._cached = fresh
            self._lifetime = max(fresh.remaining(self._clock()), 0.0)
            log.info(
                "jfrog_oidc exchanged token user=%s ttl=%.0fs provider=%s",
                fresh.username,
                self._lifetime,
                self.provider_name,
            )
            return fresh
        finally:
            self._lock.release()

    def access_token(self) -> str:
        return self.token().access_token

    def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}"}

    def invalidate(self, failed: Optional[JFrogToken] = None) -> None:
        """Drop the cached token after a 401 from Artifactory.

        Pass the token that failed so concurrent 401s don't discard a token
        another thread has just minted.
        """
        with self._lock:
            if failed is None or self._cached is failed:
                self._cached = None
                self._retry_after = 0.0

    # -- internals ----------------------------------------------------------

    def _needs_refresh(self, tok: JFrogToken) -> bool:
        skew = self.refresh_skew
        if self._lifetime:
            skew = min(skew, self._lifetime / 3)
        return tok.remaining(self._clock()) <= skew

    def _exchange_with_retry(self) -> JFrogToken:
        last: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                subject = self.source.get_token(self.audience)
                return self._exchange(subject)
            except ExchangeError as exc:
                last = exc
                if not exc.retryable:
                    raise
            except IdentityError as exc:
                last = exc
            if attempt < self.max_attempts:
                backoff = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())
                self._sleep(backoff)
        assert last is not None
        raise last

    def _exchange(self, subject_token: str) -> JFrogToken:
        body = {
            "grant_type": GRANT_TYPE,
            "subject_token_type": SUBJECT_TOKEN_TYPE,
            "subject_token": subject_token,
            "provider_name": self.provider_name,
        }
        if self.project_key:
            body["project_key"] = self.project_key
        req = urllib.request.Request(
            self.jfrog_url + EXCHANGE_PATH,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            retryable = exc.code == 429 or exc.code >= 500
            raise ExchangeError(
                f"JFrog OIDC exchange failed: HTTP {exc.code} provider={self.provider_name}: {detail}",
                status=exc.code,
                retryable=retryable,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ExchangeError(f"JFrog unreachable: {exc}", retryable=True) from exc
        except ValueError as exc:
            raise ExchangeError("JFrog returned non-JSON", retryable=True) from exc

        access = payload.get("access_token")
        if not access:
            raise ExchangeError("JFrog response missing access_token")
        claims = _jwt_claims(access)
        now = self._clock()
        if payload.get("expires_in"):
            expires_at = now + float(payload["expires_in"])
        elif claims.get("exp"):
            expires_at = float(claims["exp"])
        else:
            expires_at = now + 3600.0
        username = payload.get("username") or _username_from_subject(claims.get("sub", ""))
        return JFrogToken(access_token=access, username=username, expires_at=expires_at)
