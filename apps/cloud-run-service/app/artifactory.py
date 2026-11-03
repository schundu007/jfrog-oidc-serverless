"""JFrog reads for the web app, with the service's short-lived OIDC token.

Every JFrog failure becomes one of three errors the web layer maps to a status:
NotFound (404), Denied (403) or Unavailable (502). Nothing here returns, logs or
raises a token.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Optional

from jfrog_oidc import JFrogOidcError, JFrogTokenProvider, get_json, open_artifact
from jfrog_oidc.probe import selftest


class JFrogFailure(Exception):
    status = 502


class NotFound(JFrogFailure):
    status = 404


class Denied(JFrogFailure):
    status = 403


class Unavailable(JFrogFailure):
    status = 502


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Artifactory:
    def __init__(self, provider_factory: Callable[[], JFrogTokenProvider] = JFrogTokenProvider.from_env,
                 timeout: float = 60.0):
        self._factory = provider_factory
        self._provider: Optional[JFrogTokenProvider] = None
        self._lock = threading.Lock()
        self.timeout = timeout

    @property
    def provider(self) -> JFrogTokenProvider:
        if self._provider is None:
            with self._lock:
                if self._provider is None:
                    try:
                        self._provider = self._factory()
                    except ValueError as exc:   # missing JFROG_* settings: not wired yet
                        raise Unavailable(f"JFrog is not configured: {exc}") from None
        return self._provider

    def _call(self, what: str, fn):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(f"{what}: not found") from None
            if exc.code in (401, 403):
                raise Denied(f"{what}: this service may not read it") from None
            raise Unavailable(f"{what}: JFrog answered HTTP {exc.code}") from None
        except JFrogOidcError as exc:
            raise Unavailable(f"JFrog sign-in failed: {exc}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Unavailable(f"{what}: JFrog could not be reached ({reason})") from None

    def identity(self) -> dict:
        """Who this service is in the cloud and in JFrog (claims and token times, never the token)."""
        tok = self._call("sign-in", self.provider.token)
        claims = selftest(None, provider=self.provider).get("claims") or {}
        return {"cloud_identity": claims.get("email") or claims.get("oid") or claims.get("sub"),
                "issuer": claims.get("iss"), "audience": claims.get("aud"),
                "jfrog_user": tok.username, "expires_at": _iso(tok.expires_at),
                "expires_in": max(0, int(tok.remaining()))}

    def ready(self) -> None:
        """Raise unless a JFrog token can be had (cached tokens count)."""
        self._call("sign-in", self.provider.token)

    def repositories(self) -> list[dict]:
        data = self._call("repositories", lambda: get_json(self.provider, "/artifactory/api/repositories"))
        repos = [{"key": r.get("key", ""), "type": (r.get("type") or "").lower(),
                  "package_type": (r.get("packageType") or "").lower(), "description": r.get("description") or ""}
                 for r in data or [] if isinstance(r, dict) and r.get("key")]
        return sorted(repos, key=lambda r: r["key"].lower())

    def item(self, path: str) -> dict:
        """A folder (with its children) or a file (with size, checksum, dates)."""
        api = "/artifactory/api/storage/" + urllib.parse.quote(path)
        data = self._call(path, lambda: get_json(self.provider, api)) or {}
        base = {"path": path, "created": data.get("created"), "modified": data.get("lastModified")}
        if "children" in data:
            children = [{"name": c["uri"].strip("/"), "folder": bool(c.get("folder"))}
                        for c in data.get("children") or [] if c.get("uri")]
            children.sort(key=lambda c: (not c["folder"], c["name"].lower()))
            return {**base, "folder": True, "children": children}
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return {**base, "folder": False, "size": size, "mime_type": data.get("mimeType") or "application/octet-stream",
                "sha256": (data.get("checksums") or {}).get("sha256")}

    def open(self, path: str):
        """The file's bytes as a stream (close it)."""
        return self._call(path, lambda: open_artifact(self.provider, path, timeout=self.timeout))


class Clock:
    """Seconds since start, for /healthz (monkeypatched in tests)."""

    def __init__(self, now: Callable[[], float] = time.monotonic):
        self._now, self._start = now, now()

    def uptime(self) -> int:
        return int(self._now() - self._start)
