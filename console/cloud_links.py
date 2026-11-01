"""Cloud portal pages for a listed workload's resource group, subscription or project.

Built on the server so the static UI carries no external URLs. A link needs a real id (an Azure
subscription GUID, a GCP project id); without one there is no link and the UI shows plain text."""

import re
from urllib.parse import quote

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
AZURE = "https://portal.azure.com/#@/resource"
GCP = "https://console.cloud.google.com"


def links(w: dict) -> dict:
    """{resource_group, scope}: the portal pages for this row, or None for each it can't link."""
    sub, rg, project = str(w.get("subscription") or ""), str(w.get("resource_group") or ""), str(w.get("project") or "")
    out = {"resource_group": None, "scope": None}
    if _GUID.match(sub):
        out["scope"] = f"{AZURE}/subscriptions/{sub}/overview"
        if rg:
            out["resource_group"] = f"{AZURE}/subscriptions/{sub}/resourceGroups/{quote(rg, safe='')}/overview"
    elif project:
        out["scope"] = f"{GCP}/home/dashboard?project={quote(project, safe='')}"
    return out
