"""Fixture strings taken from the sandbox runbook.

Quoted lines (containerd's 401, the plugin's doubled URL and DNS error, the
pull events, kubelet's exec failure) are the runbook's exact text, with the
elided `...` parts filled with plausible values. The node-file layouts and the
QUICK SUMMARY footer are reconstructed from the runbook's field names.
"""

VALUES_SANDBOX = '''providerConfig:
  - name: jfrog-credentials-provider
    # Bare hostname, NOT a URL: the plugin prepends "https://" itself.
    artifactoryUrl: "example.jfrog.io"
    matchImages:
      # The repo's dedicated subdomain (the bare-domain path method does not work here).
      - "example-aks-sandbox-test-docker-local.jfrog.io"
    defaultCacheDuration: 5h
    tokenAttributes:
      enabled: true
      serviceAccountTokenAudience: "api://AzureADTokenExchange"
    azure:
      enabled: true
      azure_app_audience: "api://AzureADTokenExchange"
      jfrog_oidc_provider_name: "aks-sandbox-oidc"
rbac:
  create: true
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: credentialsProviderEnabled
              operator: In
              values:
                - "true"
downloadUrl: "https://example.jfrog.io/artifactory/aks-sandbox-generic-local/jfrog-credential-provider/1.4.0/jfrog-credential-provider-linux"
'''

# Node file on vmss00000g before the reset: the old https:// value, one JFrog entry.
NODE_CONFIG_STALE = '''apiVersion: kubelet.config.k8s.io/v1
kind: CredentialProviderConfig
providers:
  - name: acr-credential-provider
    apiVersion: credentialprovider.kubelet.k8s.io/v1
    matchImages:
      - "*.azurecr.io"
      - "*.azurecr.cn"
      - "*.azurecr.de"
      - "*.azurecr.us"
    defaultCacheDuration: 10m
    args:
      - /etc/kubernetes/azure.json
  - name: jfrog-credentials-provider
    apiVersion: credentialprovider.kubelet.k8s.io/v1
    matchImages:
      - "example-aks-sandbox-test-docker-local.jfrog.io"
    defaultCacheDuration: 5h
    env:
      - name: artifactory_url
        value: https://example.jfrog.io
      - name: azure_app_audience
        value: api://AzureADTokenExchange
      - name: jfrog_oidc_provider_name
        value: aks-sandbox-oidc
      - name: disable_provider_autoupdate
        value: "true"
    tokenAttributes:
      serviceAccountTokenAudience: api://AzureADTokenExchange
      cacheType: ServiceAccount
      requireServiceAccount: true
      requiredServiceAccountAnnotationKeys:
        - JFrogExchange
      optionalServiceAccountAnnotationKeys:
        - azure.workload.identity/client-id
'''

NODE_CONFIG_FIXED = NODE_CONFIG_STALE.replace("value: https://example.jfrog.io", "value: example.jfrog.io")

# The reverting writer's values on the old VM.
NODE_CONFIG_REVERTED = (NODE_CONFIG_STALE
                        .replace('"example-aks-sandbox-test-docker-local.jfrog.io"', '"example-jfrog-releases-remote.jfrog.io"')
                        .replace("value: aks-sandbox-oidc", "value: aks-other-cluster-oidc"))

# JFrog entry first, ACR after it: the reset must abort rather than cut acr-credential-provider.
NODE_CONFIG_JFROG_FIRST = '''apiVersion: kubelet.config.k8s.io/v1
kind: CredentialProviderConfig
providers:
  - name: jfrog-credentials-provider
    matchImages:
      - "example-aks-sandbox-test-docker-local.jfrog.io"
    defaultCacheDuration: 5h
    env:
      - name: artifactory_url
        value: https://example.jfrog.io
  - name: acr-credential-provider
    matchImages:
      - "*.azurecr.io"
'''

CONTAINERD_401 = ('failed to fetch anonymous token: unexpected status from GET request to '
                  'https://example-aks-sandbox-test-docker-local.jfrog.io/artifactory/api/docker/aks-sandbox-test-docker-local'
                  '/v2/token?scope=repository%3Amcp%2Fsonarqube%3Apull&service=example-aks-sandbox-test-docker-local.jfrog.io : 401')

PLUGIN_DOUBLED = 'RT oidc token url :https://https://example.jfrog.io/access/api/v1/oidc/token'
PLUGIN_DNS_ERROR = ('ERROR createArtifactoryToken failed Cause Post "https://https//example.jfrog.io/access/api/v1/oidc/token": '
                    'dial tcp: lookup https on 168.63.129.16:53: no such host')
PLUGIN_FIXED = 'RT oidc token url :https://example.jfrog.io/access/api/v1/oidc/token'
PLUGIN_ENVS = 'getting envs - azureAppAudience: api://AzureADTokenExchange, jfrogOidcProviderName: aks-sandbox-oidc'
PLUGIN_SA = 'Service Account Token obtained using Pod Identity (Kubernetes Workload Identity)'
PLUGIN_AUTOUPDATE = 'Auto-update functionality is disabled'


def jl(time, msg, level="info"):
    import json
    return json.dumps({"level": level, "time": time, "msg": msg})


PLUGIN_LOG = "\n".join([
    jl("2026-10-16T13:40:02Z", PLUGIN_AUTOUPDATE),
    jl("2026-10-16T13:40:02Z", PLUGIN_ENVS),
    jl("2026-10-16T13:40:02Z", PLUGIN_SA),
    jl("2026-10-16T13:40:02Z", PLUGIN_DOUBLED),
    jl("2026-10-16T13:40:03Z", PLUGIN_DNS_ERROR, "error"),
    jl("2026-10-16T14:03:05Z", PLUGIN_AUTOUPDATE),
    jl("2026-10-16T14:03:05Z", PLUGIN_ENVS),
    jl("2026-10-16T14:03:05Z", PLUGIN_SA),
    jl("2026-10-16T14:03:05Z", PLUGIN_FIXED),
])

EVENTS_FAIL = f'''Events:
  Type     Reason     Age   From               Message
  ----     ------     ----  ----               -------
  Normal   Scheduled  60s   default-scheduler  Successfully assigned jfrog/secretless-pull-test to aks-nodepool1-12345678-vmss00000g
  Normal   Pulling    58s   kubelet            Pulling image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1"
  Warning  Failed     57s   kubelet            Failed to pull image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1": {CONTAINERD_401}
  Warning  Failed     57s   kubelet            Error: ErrImagePull
  Normal   BackOff    40s   kubelet            Back-off pulling image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1"
  Warning  Failed     40s   kubelet            Error: ImagePullBackOff
'''

EVENTS_PASS = '''Events:
  Type    Reason     Age   From               Message
  ----    ------     ----  ----               -------
  Normal  Scheduled  30s   default-scheduler  Successfully assigned jfrog/secretless-pull-test to aks-nodepool1-12345678-vmss00000g
  Normal  Pulling    29s   kubelet            Pulling image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1"
  Normal  Pulled     18s   kubelet            Successfully pulled image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1" in 10.487s (10.487s including waiting). Image size: 142944877 bytes.
  Normal  Created    18s   kubelet            Created container: test
  Normal  Started    18s   kubelet            Started container test
'''

EVENTS_CACHED = '''Events:
  Normal  Scheduled  12s   default-scheduler  Successfully assigned jfrog/secretless-pull-test to aks-nodepool1-12345678-vmss00000g
  Normal  Pulled     11s   kubelet            Container image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1" already present on machine
  Normal  Created    11s   kubelet            Created container: test
  Normal  Started    11s   kubelet            Started container test
'''

JOURNAL_EXEC_FAILED = ('Oct 16 13:40:03 aks-nodepool1-12345678-vmss00000G kubelet[3121]: E1016 13:40:03.120 '
                       'Failed to provide credentials for image "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1": '
                       'error execing credential provider plugin jfrog-credentials-provider for image '
                       '"example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1": exit status 1:')

FORBIDDEN_USER = ('Error from server (Forbidden): pods "secretless-pull-test" is forbidden: User "operator@example.com" '
                  'cannot delete resource "pods" in API group "" in the namespace "jfrog": '
                  'User does not have access to the resource in Azure. Update role assignment to allow access.')

FORBIDDEN_SP = ('Error from server (Forbidden): pods is forbidden: User "3f1c2d4e-0000-4000-8000-00000000abcd" '
                'cannot list resource "pods" in API group "" at the cluster scope')


def footer(node, reason, started, mtime, entry_url, token_lines):
    entry = NODE_CONFIG_FIXED if entry_url == "example.jfrog.io" else NODE_CONFIG_STALE
    jfrog_part = entry.split("  - name: jfrog-credentials-provider", 1)[1]
    lines = [
        "================ QUICK SUMMARY ================",
        f"Node inspected: {node}",
        f"Node chosen because: {reason}",
        "Kubelet flags: --image-credential-provider-config=/var/lib/kubelet/credential-provider-config.yaml "
        "--image-credential-provider-bin-dir=/var/lib/kubelet/credential-provider",
        f"Kubelet started at: {started}",
        f"Config file mtime: {mtime}",
        "Providers in config: acr-credential-provider, jfrog-credentials-provider",
        "Bin dir: acr-credential-provider, jfrog-credentials-provider (14359603 bytes)",
        "-- jfrog provider entry --",
        "  - name: jfrog-credentials-provider" + jfrog_part.rstrip("\n"),
        "-- latest OIDC token URLs --",
        *token_lines,
        "===============================================",
    ]
    return "\n".join(lines)


def ado(text, start="2026-10-16T14:05:01.0000000Z"):
    """Prefix every line the way ADO's log API does."""
    return "\n".join(f"{start} {l}" for l in text.splitlines())


CHECK_KUBELET_BEFORE = ado("\n".join([
    "===== kubelet flags =====",
    "/usr/local/bin/kubelet --image-credential-provider-config=/var/lib/kubelet/credential-provider-config.yaml "
    "--image-credential-provider-bin-dir=/var/lib/kubelet/credential-provider",
    "===== node config =====",
    NODE_CONFIG_STALE,
    "===== plugin log (last 10) =====",
    jl("2026-10-16T13:40:02Z", PLUGIN_DOUBLED),
    jl("2026-10-16T13:40:03Z", PLUGIN_DNS_ERROR, "error"),
    footer("aks-nodepool1-12345678-vmss00000g", "node of pod jfrog/secretless-pull-test",
           "2026-10-16T13:20:11Z", "2026-10-16T13:20:10Z", "https://example.jfrog.io",
           [jl("2026-10-16T13:40:02Z", PLUGIN_DOUBLED)]),
]))

CHECK_KUBELET_AFTER = ado("\n".join([
    footer("aks-nodepool1-12345678-vmss00000g", "node of pod jfrog/secretless-pull-test",
           "2026-10-16T14:02:41Z", "2026-10-16T14:02:40Z", "example.jfrog.io",
           [jl("2026-10-16T13:40:02Z", PLUGIN_DOUBLED), jl("2026-10-16T14:03:05Z", PLUGIN_FIXED)]),
]))

# Config fixed on disk, but kubelet started before the write.
CHECK_KUBELET_NOT_RESTARTED = ado(footer(
    "aks-nodepool1-12345678-vmss00000g", "node of pod jfrog/secretless-pull-test",
    "2026-10-16T13:20:11Z", "2026-10-16T14:02:40Z", "example.jfrog.io",
    [jl("2026-10-16T14:03:05Z", PLUGIN_DOUBLED)]))

CHECK_POD_CONFIG = ado("\n".join([
    "===== helm get values -a =====",
    VALUES_SANDBOX,
    "===== rendered artifactory_url =====",
    '        - name: artifactory_url',
    '          value: "example.jfrog.io"',
    "===== chart =====",
    "NAME                 NAMESPACE  REVISION  STATUS    CHART                            APP VERSION",
    "jfrog-cred-provider  jfrog      15        deployed  jfrog-credential-provider-1.4.0  1.4.0",
    "===== service account =====",
    "apiVersion: v1",
    "kind: ServiceAccount",
    "metadata:",
    "  name: jfrog-provider-sa",
    "  namespace: jfrog",
    "  annotations:",
    '    JFrogExchange: "true"',
    "===== restart =====",
    "daemonset.apps/jfrog-cred-provider-jfrog-credential-provider-daemonset restarted",
]))

HELM_MANIFEST = '''---
# Source: jfrog-credential-provider/templates/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: jfrog-cred-provider-jfrog-credential-provider-config
  labels:
    helm.sh/chart: jfrog-credential-provider-1.4.0
data:
  provider-config.yaml: |
    - name: jfrog-credentials-provider
      matchImages:
        - "example-aks-sandbox-test-docker-local.jfrog.io"
      defaultCacheDuration: 5h
      env:
        - name: artifactory_url
          value: "example.jfrog.io"
        - name: jfrog_oidc_provider_name
          value: "aks-sandbox-oidc"
---
# Source: jfrog-credential-provider/templates/role.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: jfrog-cred-provider-jfrog-credential-provider
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["serviceaccounts"]
    verbs: ["get"]
'''

WWW_AUTH_DEDICATED = ('Bearer realm="https://example-aks-sandbox-test-docker-local.jfrog.io/artifactory/api/docker/'
                      'aks-sandbox-test-docker-local/v2/token",service="example-aks-sandbox-test-docker-local.jfrog.io"')
WWW_AUTH_BARE = 'Bearer realm="https://example.jfrog.io/artifactory/api/docker/null/v2/token",service="example.jfrog.io"'
UNSUPPORTED_BARE = "Unsupported v2 repository request for ''"
UNSUPPORTED_VIRTUAL = "Unsupported V2 repository request for 'docker-virtual'"
DORMANT_LOCATION = "https://landing.jfrog.com/reactivate-server/example-docker-remote"
