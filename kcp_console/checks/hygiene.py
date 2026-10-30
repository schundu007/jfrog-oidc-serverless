"""chart_rbac, log_leaks and values (spec §6)."""

from __future__ import annotations

import re

from .. import model, redact
from ..parse import helm

LEAK_PATTERNS = [re.compile(p) for p in (
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.",       # any JWT
    r"cmVmdGtuOjAxOj",                                  # JFrog reference token
    r"(?i)authorization:\s*bearer\s+(?!\[REDACTED\])\S{8,}",
    r"(?i)accessToken=(?!\*\*\*|\[REDACTED\]|\$)\S{6,}",
    r"\b[a-z2-7]{52}\b",                                # ADO PAT
)]


def chart_rbac(manifest_text: str) -> dict:
    objs = helm.rbac_objects(manifest_text)
    flags = helm.broad_rbac(objs)
    return {"status": "warn" if flags else ("pass" if objs else "unknown"), "objects": objs, "flags": flags,
            "detail": (f"{len(objs)} Role/ClusterRole object(s); " + ("; ".join(flags) if flags else "no wildcards, no secrets, no node writes"))
            if objs else "no manifest read yet (CHECK_JFROG_POD_CONFIG or helm get manifest)"}


def log_leaks(texts: dict) -> dict:
    """texts: {name: stored text}. Any unmasked token pattern is critical."""
    hits = []
    for name, text in texts.items():
        for p in LEAK_PATTERNS:
            if p.search(text or ""):
                hits.append({"where": name, "pattern": p.pattern})
        if redact.text(text or "") != (text or "") and not hits:
            hits.append({"where": name, "pattern": "redactor would change this text"})
    return {"status": "fail" if hits else "pass", "hits": hits,
            "detail": f"{len(hits)} unmasked secret pattern(s): critical" if hits else f"{len(texts)} stored log(s) scanned, no token patterns"}


def values(cfg: dict) -> dict:
    issues = model.validate(model.merged(cfg))
    errs = model.errors(issues)
    return {"status": "fail" if errs else ("warn" if issues else "pass"), "issues": issues}
