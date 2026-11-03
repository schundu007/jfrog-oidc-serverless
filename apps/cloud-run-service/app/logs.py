"""One JSON object per line on stdout: Cloud Logging reads severity, httpRequest and
trace from it; Azure Log Analytics keeps it as a JSON column.

Anything that looks like a JWT or bearer value is replaced before it is written.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import urllib.request
from typing import Optional

SECRET = re.compile(r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,})")
TRACE_HEADER = "X-Cloud-Trace-Context"


def scrub(text: str) -> str:
    return SECRET.sub("[redacted]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        # extra={...} keys a record may carry, and the field each is written as
        for attr, key in (("httpRequest", "httpRequest"), ("trace_name", "logging.googleapis.com/trace"),
                          ("request_id", "request_id"), ("fields", "fields")):
            value = getattr(record, attr, None)
            if value is not None:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


_project: Optional[str] = None
_project_read = False


def gcp_project() -> Optional[str]:
    """The project id from the metadata server (once); None off Google Cloud."""
    global _project, _project_read
    if not _project_read:
        _project_read = True
        req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/project/project-id",
                                     headers={"Metadata-Flavor": "Google"})
        try:
            with urllib.request.urlopen(req, timeout=1) as resp:
                _project = resp.read().decode().strip() or None
        except OSError:
            _project = None
    return _project


def trace(header: Optional[str], project: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(trace id, Cloud Logging trace name) from X-Cloud-Trace-Context: TRACE_ID/SPAN;o=1."""
    trace_id = (header or "").split("/", 1)[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
        return None, None
    return trace_id, (f"projects/{project}/traces/{trace_id}" if project else None)
