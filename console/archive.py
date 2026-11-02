"""Archive: what a console keeps of a workload or cluster whose resources are gone.

When a live read says a thing no longer exists, its saved config, its status and its finished runs
move out of the live pages into <root>/archive/<entry>/ and stay readable there for RETENTION_DAYS;
then they are deleted. If the thing exists again before that, its entry is restored (moved back).

One entry per archiving: <root>/archive/<id>/entry.json ({id, name, kind, reason, archived_at,
released, runs}), config.json, status.json, runs/<run id>/. Paths only move; nothing is rewritten,
so a run read from the archive is the run as it was. Stdlib only; the Kubernetes console keeps an
identical copy (kcp_console/archive.py): the two consoles never import each other.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

RETENTION_DAYS = 7
DAY = 86400
_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
_RUN = re.compile(r"[A-Za-z0-9_.-]{1,80}")


class Archive:
    def __init__(self, root, days: int = RETENTION_DAYS, clock=time.time):
        self.dir = Path(root) / "archive"
        self.days, self.clock = days, clock
        self.lock = threading.RLock()

    # -- reads --------------------------------------------------------------------------------------

    def _entries(self) -> list:
        out = []
        try:
            dirs = [d for d in self.dir.iterdir() if d.is_dir() and _ID.fullmatch(d.name)]
        except OSError:
            return out
        for d in dirs:
            try:
                e = json.loads((d / "entry.json").read_text())
            except (OSError, ValueError):
                continue
            if isinstance(e, dict) and e.get("id") == d.name:
                out.append(e)
        return sorted(out, key=lambda e: e.get("archived_at") or 0, reverse=True)

    def list(self) -> list:
        """Every entry still kept, newest first, with when it is deleted."""
        with self.lock:
            return [dict(e, expires_at=e["archived_at"] + self.days * DAY) for e in self._entries()]

    def names(self) -> set:
        """Names archived and not released: hidden from the live pages."""
        with self.lock:
            return {e["name"] for e in self._entries() if not e.get("released")}

    def entry(self, name: str):
        """The newest unreleased entry for a name, or None."""
        with self.lock:
            return next((e for e in self._entries() if e["name"] == name and not e.get("released")), None)

    def config(self, name: str):
        e = self.entry(name)
        return self._read(e["id"], "config.json") if e else None

    def status(self, name: str):
        e = self.entry(name)
        return self._read(e["id"], "status.json") if e else None

    def _read(self, eid, file):
        try:
            return json.loads((self.dir / eid / file).read_text())
        except (OSError, ValueError):
            return None

    def run_dir(self, run_id: str):
        """The archived run's directory (newest entry first), or None."""
        if not _RUN.fullmatch(str(run_id)) or run_id in (".", ".."):
            return None
        with self.lock:
            for e in self._entries():
                d = self.dir / e["id"] / "runs" / run_id
                if d.is_dir():
                    return d
        return None

    # -- changes ------------------------------------------------------------------------------------

    def add(self, name: str, kind: str, reason: str, files: dict | None = None, runs: dict | None = None,
            data: dict | None = None, source: str = "saved") -> dict:
        """Move files ({"config.json"|"status.json": path}) and run directories ({run id: path}) into a
        new entry; data ({"config.json": obj}) is written as a copy (a config read from git). Missing paths
        are skipped. source says where the config lives: "saved" moves back on restore, "git" stays in git."""
        with self.lock:
            now = self.clock()
            eid = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + "-" + secrets.token_hex(3)
            d = self.dir / eid
            (d / "runs").mkdir(parents=True, exist_ok=True)
            for label, src in (files or {}).items():
                if label in ("config.json", "status.json") and src and Path(src).is_file():
                    shutil.move(str(src), str(d / label))
            for label, obj in (data or {}).items():
                if label in ("config.json", "status.json") and not (d / label).exists():
                    (d / label).write_text(json.dumps(obj))
            kept = []
            for rid, src in (runs or {}).items():
                if _RUN.fullmatch(str(rid)) and src and Path(src).is_dir():
                    shutil.move(str(src), str(d / "runs" / rid))
                    kept.append(rid)
            e = {"id": eid, "name": name, "kind": kind, "reason": reason, "source": source,
                 "archived_at": now, "released": False, "runs": kept}
            self._write(e)
            return e

    def _write(self, e):
        d = self.dir / e["id"]
        tmp = d / ".entry.tmp"
        tmp.write_text(json.dumps(e))
        os.replace(tmp, d / "entry.json")

    def release(self, name: str) -> int:
        """The name is in use again (saved anew): stop hiding it. Its old runs stay archived."""
        n = 0
        with self.lock:
            for e in self._entries():
                if e["name"] == name and not e.get("released"):
                    e["released"] = True
                    self._write(e)
                    n += 1
        return n

    def restore(self, name: str, files: dict | None = None, runs_dir=None) -> dict | None:
        """The thing exists again: move its newest entry back (config/status to files' paths, runs into
        runs_dir) unless something already stands there, then drop the entry."""
        with self.lock:
            e = self.entry(name)
            if not e:
                return None
            d = self.dir / e["id"]
            for label, dst in (files or {}).items():
                src = d / label
                if dst and src.is_file() and not Path(dst).exists():
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
            if runs_dir is not None:
                Path(runs_dir).mkdir(parents=True, exist_ok=True)
                for rid in e.get("runs") or []:
                    src, dst = d / "runs" / rid, Path(runs_dir) / rid
                    if src.is_dir() and not dst.exists():
                        shutil.move(str(src), str(dst))
            shutil.rmtree(d, ignore_errors=True)
            return e

    def purge(self) -> list:
        """Delete every entry older than the retention. Returns their ids."""
        gone = []
        with self.lock:
            cutoff = self.clock() - self.days * DAY
            for e in self._entries():
                if (e.get("archived_at") or 0) <= cutoff:
                    shutil.rmtree(self.dir / e["id"], ignore_errors=True)
                    gone.append(e["id"])
        return gone
