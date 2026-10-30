"""Small helpers for calling Artifactory with the exchanged token."""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .exchange import JFrogTokenProvider


def _open(provider: JFrogTokenProvider, url: str, method: str = "GET", data: Optional[bytes] = None, timeout: float = 60.0):
    """Open a request with the bearer token; on 401 refresh once and retry."""
    for attempt in (1, 2):
        tok = provider.token()
        req = urllib.request.Request(url, data=data, method=method)
        # Unredirected: Artifactory redirects downloads to S3/GCS/CDN, which
        # must never receive the JFrog token.
        req.add_unredirected_header("Authorization", f"Bearer {tok.access_token}")
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                provider.invalidate(tok)
                continue
            raise


def download(provider: JFrogTokenProvider, repo_path: str, dest: str, timeout: float = 120.0) -> str:
    """Download <repo>/<path> from Artifactory to dest atomically.

    repo_path: e.g. "generic-local/models/v3/model.bin"
    """
    url = f"{provider.jfrog_url}/artifactory/{urllib.parse.quote(repo_path.lstrip('/'))}"
    directory = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".jfrog-")
    out = os.fdopen(fd, "wb")
    try:
        with out, _open(provider, url, timeout=timeout) as resp:
            shutil.copyfileobj(resp, out, length=1024 * 1024)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return dest


def get_json(provider: JFrogTokenProvider, api_path: str) -> object:
    """GET a JFrog REST path, e.g. "/artifactory/api/storage/generic-local/models"."""
    import json

    with _open(provider, provider.jfrog_url + "/" + api_path.lstrip("/")) as resp:
        return json.loads(resp.read())


def basic_auth_url(provider: JFrogTokenProvider, url: str) -> str:
    """Embed username:token in a URL for tools that only take index URLs (pip).

    Treat the result as a secret: do not log it or write it to disk.
    """
    tok = provider.token()
    parts = urllib.parse.urlsplit(url)
    netloc = f"{urllib.parse.quote(tok.username, safe='')}:{urllib.parse.quote(tok.access_token, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
