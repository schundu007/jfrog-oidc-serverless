""".console/ persistence (spec §2): saved configs. Never secrets.

Configs live in <root>/configs/<name>.json, written atomically with mode 0600
in a 0700 directory. A config that contains anything the redactor would mask
(a JWT, a password field, a Bearer value...) is refused, so a token pasted into
a form field can't end up on disk. The root is $CONSOLE_HOME or ./.console.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from . import model, redact


SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,}$")


class StoreError(ValueError):
    pass


def default_root() -> Path:
    return Path(os.environ.get("CONSOLE_HOME") or Path.cwd() / ".console")


def summary(cfg: dict, name: str, updated: str = "") -> dict:
    """One Workloads row: where the target runs and the identity Discover found."""
    kind = model.get_path(cfg, "target.kind") or ""
    cloud = model.KINDS.get(kind, {}).get("cloud") or ("azure" if kind.startswith("azure") else "gcp")
    t = model.get_path(cfg, f"target.{cloud}") or {}
    where = {"gcp": ["project", "region", "name"], "azure": ["resource_group", "name"],
             "aws": ["account", "region", "name"], "openshift": ["context", "namespace", "name"]}[cloud]
    disc = model.get_path(cfg, "target.discovered") or {}
    return {"name": name, "kind": kind, "discovered": bool(disc),
            "workload": " / ".join(str(t[k]) for k in where if t.get(k)),
            "identity": disc.get("service_account") or disc.get("principal_id") or "",
            "updated": updated}


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else default_root()
        self.configs = self.root / "configs"

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not model.NAME_RE.match(name):
            raise StoreError("config name must be lowercase letters, digits and dashes (max 63)")
        return self.configs / f"{name}.json"

    def list(self) -> list:
        out = []
        if not self.configs.is_dir():
            return out
        for p in sorted(self.configs.glob("*.json")):
            try:
                cfg = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            out.append(summary(cfg, p.stem, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))))
        return out

    def get(self, name: str) -> dict:
        path = self._path(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        return json.loads(path.read_text())

    def put(self, name: str, cfg: dict) -> dict:
        path = self._path(name)
        if not isinstance(cfg, dict):
            raise StoreError("config must be an object")
        cfg = dict(cfg, name=name)
        if redact.obj(cfg) != cfg:
            raise StoreError("config contains a value that looks like a secret (token, password or key); "
                             "configs are saved in plain text and must not hold secrets")
        data = json.dumps(cfg, indent=2, sort_keys=False) + "\n"
        if len(data) > 256 * 1024:
            raise StoreError("config too large")
        self.configs.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.configs, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return cfg

    def session_token(self, renew: bool = False) -> str:
        """The console's own sign-in token, kept in <root>/session-token (0600) so a
        restart doesn't sign the browser out. It only unlocks this local console."""
        path = self.root / "session-token"
        if not renew:
            try:
                token = path.read_text().strip()
                if SESSION_TOKEN_RE.match(token):
                    return token
            except OSError:
                pass
        token = secrets.token_urlsafe(32)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".session-token.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return token

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.is_file():
            path.unlink()
            return True
        return False
