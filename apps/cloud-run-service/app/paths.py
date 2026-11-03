"""<repo>/<path> as typed in a URL: checked before it reaches JFrog."""

from __future__ import annotations

import re

REPO_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_LENGTH = 1024


class BadPath(ValueError):
    pass


def clean(raw: str) -> str:
    """Return the path without outer slashes, or raise BadPath."""
    path = (raw or "").strip("/")
    if not path:
        raise BadPath("a repository path is required")
    if len(path) > MAX_LENGTH:
        raise BadPath(f"the path is longer than {MAX_LENGTH} characters")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise BadPath("the path has an empty, '.' or '..' segment")
        if "\\" in part or any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise BadPath("the path has a backslash or a control character")
    if not REPO_KEY.match(parts[0]):
        raise BadPath(f"{parts[0]!r} is not a repository key")
    return path


def crumbs(path: str) -> list[tuple[str, str]]:
    """[(name, path up to and including it)] for a breadcrumb trail."""
    parts = path.split("/")
    return [(name, "/".join(parts[: i + 1])) for i, name in enumerate(parts)]


def filename(path: str) -> str:
    """Last segment, safe for a Content-Disposition header."""
    name = path.rsplit("/", 1)[-1]
    return re.sub(r'[^A-Za-z0-9._ -]', "_", name) or "download"
