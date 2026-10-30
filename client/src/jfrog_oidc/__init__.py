"""Short-lived JFrog tokens for serverless workloads via OIDC token exchange.

    from jfrog_oidc import JFrogTokenProvider
    jfrog = JFrogTokenProvider.from_env()      # create once per process
    headers = jfrog.auth_header()              # cached, refreshed before expiry

Environment:
    JFROG_URL             https://acme.jfrog.io
    JFROG_OIDC_PROVIDER   JFrog OIDC integration name
    JFROG_OIDC_AUDIENCE   GCP: integration audience (e.g. jfrog-gcp)
                          Azure: api://<audience-app-client-id>
    JFROG_OIDC_SOURCE     auto (default) | gcp | azure | ado | file
    AZURE_CLIENT_ID       user-assigned managed identity client ID (Azure)
    JFROG_OIDC_ADO_SERVICE_CONNECTION_ID  Azure DevOps: service connection the
                          pipeline's token is issued for (plus SYSTEM_ACCESSTOKEN)
    JFROG_PROJECT_KEY     optional, for project-scoped identity mappings
"""

from .artifactory import basic_auth_url, download, get_json
from .errors import ExchangeError, IdentityError, JFrogOidcError
from .exchange import JFrogToken, JFrogTokenProvider
from .sources import (
    AzureDevOpsSource,
    AzureManagedIdentitySource,
    FileTokenSource,
    GcpMetadataSource,
    detect_source,
)

__all__ = [
    "JFrogTokenProvider",
    "JFrogToken",
    "GcpMetadataSource",
    "AzureManagedIdentitySource",
    "AzureDevOpsSource",
    "FileTokenSource",
    "detect_source",
    "download",
    "get_json",
    "basic_auth_url",
    "JFrogOidcError",
    "IdentityError",
    "ExchangeError",
]

__version__ = "0.1.0"
