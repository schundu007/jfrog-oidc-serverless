"""Settings from the environment, read once at start-up.

JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE, JFROG_OIDC_SOURCE  the client's (set by the console's Wire step)
JFROG_HEALTHCHECK_PATH   <repo>/<path> the health check reads; its folder is the home folder
APP_HOME_PATH            <repo>/<folder> linked from the home page (default: the health-check file's folder)
APP_MAX_DOWNLOAD_MB      largest file /download streams (default 100)
APP_VERSION              image revision (set at build time)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


@dataclass(frozen=True)
class Settings:
    version: str
    jfrog_url: str
    home_path: str
    healthcheck_path: Optional[str]
    max_download_bytes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        check = (env.get("JFROG_HEALTHCHECK_PATH") or "").strip("/")
        home = (env.get("APP_HOME_PATH") or "").strip("/") or (check.rsplit("/", 1)[0] if "/" in check else "")
        return cls(
            version=env.get("APP_VERSION") or "dev",
            jfrog_url=(env.get("JFROG_URL") or "").rstrip("/"),
            home_path=home,
            healthcheck_path=check or None,
            max_download_bytes=_int(env, "APP_MAX_DOWNLOAD_MB", 100, 1, 2048) * 1024 * 1024,
        )
