""".kcp-console/ persistence: saved configs and the session token. Never secrets.

Configs live in <root>/configs/<name>.json, written atomically with mode 0600
in a 0700 directory. A config the redactor would change (a JWT, a PAT, a
token-named field...) is refused, so a secret pasted into a form field can't
reach the disk. The root is $KCP_CONSOLE_HOME or ./.kcp-console.
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
    return Path(os.environ.get("KCP_CONSOLE_HOME") or Path.cwd() / ".kcp-console")


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
            out.append({"name": p.stem, "mode": cfg.get("mode", "pilot"),
                        "cluster": model.get_path(cfg, "cluster.name") or model.get_path(cfg, "template.ClusterName") or "",
                        "environment": model.get_path(cfg, "cluster.environment") or "",
                        "resource_group": model.get_path(cfg, "cluster.resource_group") or "",
                        "subscription": model.get_path(cfg, "cluster.subscription_name") or model.get_path(cfg, "cluster.subscription_id") or "",
                        "ado": bool(model.get_path(cfg, "ado.organization")),
                        "provider": model.get_path(cfg, "jfrog.oidc_provider") or model.get_path(cfg, "values.provider_name") or "",
                        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))})
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
            raise StoreError("config contains a value that looks like a secret (token, PAT or password); "
                             "configs are saved in plain text and must not hold secrets")
        data = json.dumps(cfg, indent=2) + "\n"
        if len(data) > 256 * 1024:
            raise StoreError("config too large")
        _atomic_write(path, data)
        return cfg

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.is_file():
            path.unlink()
            return True
        return False

    def session_token(self, renew: bool = False) -> str:
        """The console's sign-in token, kept in <root>/session-token (0600) so a restart
        doesn't sign the browser out. It only unlocks this local console."""
        path = self.root / "session-token"
        if not renew:
            try:
                token = path.read_text().strip()
                if SESSION_TOKEN_RE.match(token):
                    return token
            except OSError:
                pass
        token = secrets.token_urlsafe(32)
        _atomic_write(path, token + "\n")
        return token


RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


class RunStore:
    """runs/<id>/run.json (the record), events.jsonl (one redacted line per event), log.txt (the redacted
    pipeline log). Everything written here has been through redact."""

    def __init__(self, root: Path):
        self.dir = Path(root) / "runs"

    def _d(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id or ""):
            raise StoreError("bad run id")
        return self.dir / run_id

    def save(self, run: dict) -> None:
        _atomic_write(self._d(run["id"]) / "run.json", json.dumps(redact.obj(run), indent=2) + "\n")

    def event(self, run_id: str, ev: dict) -> None:
        d = self._d(run_id)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = d / "events.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(redact.obj(ev)) + "\n")

    def save_log(self, run_id: str, text: str) -> None:
        _atomic_write(self._d(run_id) / "log.txt", redact.text(text or ""))

    def log(self, run_id: str) -> str:
        p = self._d(run_id) / "log.txt"
        return p.read_text() if p.is_file() else ""

    def events(self, run_id: str) -> list:
        p = self._d(run_id) / "events.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.is_file() else []

    def all(self) -> list:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*/run.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
        return out

    def logs(self) -> dict:
        return {p.parent.name: p.read_text() for p in sorted(self.dir.glob("*/log.txt"))} if self.dir.is_dir() else {}


class StatusStore:
    """status/<config>.json: open items, security rows, demo checklist, notes, the last Discover, audit records."""

    def __init__(self, root: Path):
        self.dir = Path(root) / "status"

    def _p(self, name: str) -> Path:
        if not model.NAME_RE.match(name or ""):
            raise StoreError("bad config name")
        return self.dir / f"{name}.json"

    def get(self, name: str) -> dict:
        p = self._p(name)
        return json.loads(p.read_text()) if p.is_file() else {}

    def update(self, name: str, patch: dict) -> dict:
        cur = self.get(name)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            else:
                cur[k] = v
        if redact.obj(cur) != cur:
            raise StoreError("status contains a value that looks like a secret")
        _atomic_write(self._p(name), json.dumps(cur, indent=2) + "\n")
        return cur
