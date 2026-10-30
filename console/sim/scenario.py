"""Scenario schema, loader, built-ins and expectation checks (spec §7.4).

    {name, target_kind, duration, clock_start, seed?, config?, config_overrides{},
     load{cold_starts_per_hour, requests_per_minute, uncached_pull_ratio, instance_lifetime_minutes},
     faults:[{type, start, end?, params?}],
     expect:{verdict, max_failed_pulls?, alert_fired_before?, first_failure_after?,
             root_cause_contains?, min_failed_rotations?}}

Times inside a scenario ("03:00", "+90m") are offsets from clock_start.
A saved console config loads as a scenario with its real TTLs, cron and kind
and no faults.
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import model

SCENARIO_DIR = Path(__file__).parent / "scenarios"

FAULT_TYPES = {
    "jfrog_outage": "JFrog returns 503 (params.services: access, artifactory, registry; default all)",
    "rotator_down": "the rotator's identity endpoint fails, so every run in the window fails",
    "identity_recreated": "the runtime identity is recreated with a new uniqueId / principal ID",
    "sa_recreated": "alias of identity_recreated for GCP service accounts",
    "arm_refresh_forbidden": "ARM Key Vault reference refresh returns 403 to the rotator",
    "mapping_revoked": "an identity mapping is removed (params.mapping: runtime | pull)",
    "integration_misconfigured": "the JFrog integration's issuer is wrong (params.issuer)",
    "federation_disabled": "AWS outbound identity federation is off: STS GetWebIdentityToken returns 403",
}
# Faults that only exist on some clouds (the rest apply everywhere).
FAULT_CLOUDS = {
    "rotator_down": ("gcp", "azure"),
    "arm_refresh_forbidden": ("azure",),
    "sa_recreated": ("gcp",),
    "federation_disabled": ("aws",),
}

DEFAULT_LOAD = {"cold_starts_per_hour": 6.0, "requests_per_minute": 2.0, "uncached_pull_ratio": 0.2,
                "instance_lifetime_minutes": 60.0}
DEFAULT_START = "2026-01-01T00:00:00Z"


def parse_duration(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(value))
    if not m:
        raise ValueError(f"bad duration {value!r} (use 90m, 24h, 3600s)")
    n, unit = float(m.group(1)), m.group(2) or "s"
    return int(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def parse_offset(value) -> int:
    """'03:00' or '27:30' (HH:MM after clock_start), '+90m', or seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,3}):(\d{2})", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    if s.startswith("+"):
        return parse_duration(s[1:])
    raise ValueError(f"bad time {value!r} (use HH:MM or +90m)")


def parse_start(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def builtin_names() -> list:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))


def builtin_summaries() -> list:
    out = []
    for name in builtin_names():
        raw = json.loads((SCENARIO_DIR / f"{name}.json").read_text())
        out.append({k: raw.get(k) for k in ("name", "description", "target_kind", "duration", "faults", "load",
                                            "config_overrides", "expect")})
    return out


def normalize(raw: dict) -> dict:
    """Fill defaults and validate. Raises ValueError with a readable message."""
    s = copy.deepcopy(raw)
    if not s.get("name"):
        raise ValueError("scenario needs a name")
    kind = s.get("target_kind")
    if kind not in model.KINDS:
        raise ValueError(f"unknown target_kind {kind!r}")
    s.setdefault("description", "")
    s["duration_s"] = parse_duration(s.get("duration", "24h"))
    if not 600 <= s["duration_s"] <= 7 * 86400:
        raise ValueError("duration must be between 10 minutes and 7 days")
    s.setdefault("clock_start", DEFAULT_START)
    s["start"] = parse_start(s["clock_start"])
    s["seed"] = int(s.get("seed", 1))
    s["load"] = {**DEFAULT_LOAD, **(s.get("load") or {})}
    for k in DEFAULT_LOAD:
        s["load"][k] = float(s["load"][k])
        if s["load"][k] < 0:
            raise ValueError(f"load.{k} must be >= 0")
    if s["load"]["requests_per_minute"] > 600 or s["load"]["cold_starts_per_hour"] > 3600:
        raise ValueError("load is capped at 600 requests/min and 3600 cold starts/h")
    if s["load"]["instance_lifetime_minutes"] < 1:
        raise ValueError("load.instance_lifetime_minutes must be at least 1")
    s.setdefault("config_overrides", {})
    faults = []
    for f in s.get("faults") or []:
        if f.get("type") not in FAULT_TYPES:
            raise ValueError(f"unknown fault type {f.get('type')!r}; known: {', '.join(FAULT_TYPES)}")
        clouds = FAULT_CLOUDS.get(f["type"])
        if clouds and model.cloud_of(kind) not in clouds and model.cloud_of(kind) not in ("gcp", "azure"):
            raise ValueError(f"fault {f['type']} does not apply to {model.KINDS[kind]['label']}")
        nf = {"type": f["type"], "params": dict(f.get("params") or {})}
        nf["start_s"] = parse_offset(f.get("start", "00:00"))
        nf["end_s"] = parse_offset(f["end"]) if f.get("end") is not None else None
        if nf["end_s"] is not None and nf["end_s"] <= nf["start_s"]:
            raise ValueError(f"fault {f['type']}: end must be after start")
        nf["start"], nf["end"] = f.get("start", "00:00"), f.get("end")
        faults.append(nf)
    s["faults"] = faults
    s.setdefault("expect", {})

    base_name = (s.get("config") or {}).get("name") or "sim-target"
    cfg = model.merge(model.default_config(kind, name=base_name), s.get("config") or {})
    cfg["target"]["kind"] = kind
    for dotted, value in s["config_overrides"].items():
        model.set_path(cfg, dotted, value)
    s["config"] = cfg
    return s


def from_config(cfg: dict, **kw) -> dict:
    """A saved console config as a no-fault scenario with its real settings."""
    raw = {
        "name": f"config:{cfg.get('name', 'unnamed')}",
        "description": "saved console config, no faults",
        "target_kind": model.get_path(cfg, "target.kind"),
        "duration": "24h",
        "config": cfg,
        "faults": [],
        "expect": {"verdict": "pass"},
    }
    return _apply_kw(raw, **kw)


def _apply_kw(raw: dict, seed=None, overrides=None, load=None, faults=None, duration=None) -> dict:
    raw = copy.deepcopy(raw)
    if seed is not None:
        raw["seed"] = seed
    if overrides:
        raw["config_overrides"] = {**raw.get("config_overrides", {}), **overrides}
    if load:
        raw["load"] = {**raw.get("load", {}), **load}
    if faults is not None:
        raw["faults"] = faults
    if duration is not None:
        raw["duration"] = duration
    return normalize(raw)


def load(ref, **kw) -> dict:
    """Load a built-in by name, a scenario file, or a saved console config file."""
    if isinstance(ref, dict):
        return _apply_kw(ref, **kw) if "target_kind" in ref else from_config(ref, **kw)
    ref = str(ref)
    path = Path(ref)
    if ref.endswith(".json") and path.is_file():
        raw = json.loads(path.read_text())
        if "target_kind" in raw:
            return _apply_kw(raw, **kw)
        return from_config(raw, **kw)
    builtin = SCENARIO_DIR / f"{ref}.json"
    if os.sep in ref or "/" in ref or not builtin.is_file():
        raise ValueError(f"no built-in scenario or file named {ref!r}; built-ins: {', '.join(builtin_names())}")
    return _apply_kw(json.loads(builtin.read_text()), **kw)


def check(result: dict, expect: dict) -> list:
    """Compare a result with an expectation. Returns a list of problems (empty = met)."""
    problems = []
    s = result["summary"]
    if "verdict" in expect and result["verdict"] != expect["verdict"]:
        problems.append(f"verdict {result['verdict']} != expected {expect['verdict']}")
    if "max_failed_pulls" in expect and s["pulls_failed"] > expect["max_failed_pulls"]:
        problems.append(f"{s['pulls_failed']} failed pulls > max {expect['max_failed_pulls']}")
    if "alert_fired_before" in expect:
        limit = parse_offset(expect["alert_fired_before"])
        fired = s.get("alert_first_fired_s")
        if fired is None or fired >= limit:
            problems.append(f"alert fired at {fired} s, expected before {expect['alert_fired_before']}")
    if "first_failure_after" in expect:
        limit = parse_offset(expect["first_failure_after"])
        first = s.get("first_failure_s")
        if first is None or first < limit:
            problems.append(f"first failure at {first} s, expected at or after {expect['first_failure_after']}")
    if "root_cause_contains" in expect and expect["root_cause_contains"] not in (result.get("root_cause") or ""):
        problems.append(f"root cause does not mention {expect['root_cause_contains']!r}: {result.get('root_cause')}")
    if "min_failed_rotations" in expect and s["rotations_failed"] < expect["min_failed_rotations"]:
        problems.append(f"{s['rotations_failed']} failed rotations < {expect['min_failed_rotations']}")
    return problems
