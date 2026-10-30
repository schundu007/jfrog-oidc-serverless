"""How each platform pulls before user code runs (spec §7.2, Platforms).

- Cloud Run (all GCP kinds): a cold start pulls through an AR remote repo.
  Cached artifacts are served without contacting JFrog; an uncached pull
  resolves the repo's secret-version pointer and authenticates upstream.
- Container Apps / Functions on ACA: a replica start pulls from the JFrog
  registry with the registry secret the platform has cached. The engine
  refreshes that cache 5–30 min after each new Key Vault version.
- Premium Functions: the container pull uses the resolved Key Vault reference,
  cached for 24 h unless the rotator refreshes it through ARM.
- Flex never pulls.

These model the *platforms*, not the code under test, so they are allowed to
read emulator state directly. They still authenticate to the JFrog emulator
over HTTP so registry auth is decided in one place.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def registry_pull(jfrog_url: str, username: str, password: str, image: str = "docker-virtual/app/manifests/1.0"):
    """GET a manifest with Basic auth. Returns (status, message)."""
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(f"{jfrog_url}/v2/{image}", headers={"Authorization": f"Basic {auth}"})
    try:
        with _opener.open(req, timeout=10) as resp:
            resp.read()
            return resp.status, "ok"
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                body = json.loads(exc.read() or b"{}")
                msg = body.get("errors", [{}])[0].get("message", "")
            except ValueError:
                msg = ""
            return exc.code, msg
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"unreachable: {exc}"


def _pull_result(ok, cached, version, status=200, reason=""):
    return {"ok": ok, "cached": cached, "version": version, "status": status, "reason": reason}


class Platform:
    pulls = True
    label = "platform"
    action = "start"

    def holds(self):
        return None

    def load(self, now: float) -> None:
        pass


class ArPlatform(Platform):
    label = "Cloud Run"
    action = "cold start"

    def __init__(self, gcp, jfrog_url: str, repo: str, uncached_ratio: float):
        self.gcp, self.jfrog_url, self.repo, self.uncached_ratio = gcp, jfrog_url, repo, uncached_ratio

    def holds(self):
        pointer = self.gcp.repo(self.repo)["passwordSecretVersion"]
        return "v" + pointer.rsplit("/", 1)[-1]

    def pull(self, rng) -> dict:
        version = self.holds()
        if rng.random() >= self.uncached_ratio:
            return _pull_result(True, True, version)
        creds = self.gcp.repo(self.repo)
        ver = self.gcp.version(creds["passwordSecretVersion"])
        if ver is None or ver["state"] != "ENABLED":
            state = ver["state"] if ver else "missing"
            return _pull_result(False, False, version, 0, f"AR upstream credential secret {version} is {state}")
        status, msg = registry_pull(self.jfrog_url, creds["username"], ver["data"].decode())
        if status == 200:
            return _pull_result(True, False, version)
        return _pull_result(False, False, version, status, f"AR upstream fetch: registry {status}: {msg}")


class KvPlatform(Platform):
    """A platform that pulls with a cached copy of a Key Vault secret."""

    def __init__(self, azure, jfrog_url: str, vault: str, secret: str, username: str, label: str, action: str):
        self.azure, self.jfrog_url, self.vault, self.secret, self.username = azure, jfrog_url, vault, secret, username
        self.label, self.action = label, action
        self.held = None
        self.loaded_at = None

    def load(self, now: float) -> None:
        v = self.azure.latest(self.vault, self.secret)
        self.held = {"label": v["label"], "value": v["value"], "exp": v["exp"]}
        self.loaded_at = now

    def holds(self):
        return self.held["label"] if self.held else None

    def pull(self, rng) -> dict:
        status, msg = registry_pull(self.jfrog_url, self.username, self.held["value"])
        if status == 200:
            return _pull_result(True, False, self.held["label"])
        return _pull_result(False, False, self.held["label"], status, f"registry {status}: {msg}")


class FlexPlatform(Platform):
    pulls = False
    label = "Flex Consumption"
    action = "instance start"
