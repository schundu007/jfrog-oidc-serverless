"""The sandbox runbook as data ("JFrog Kubelet Credential Provider — sandbox Runbook",
last updated 2026-10-16, plus the 2026-10-20 security self-review).

The UI (GET /api/knowledge) and the report render these structures. Code that
acts on a fact (the scenario registry's "changes state" column, protected
providers, guards) reads it from here so the text and the behaviour cannot
drift apart. `tests/test_knowledge.py` pins the counts the traceability
checklist names (11 scenarios, 19 bugs, 9 troubleshooting rows, ...).
"""

from __future__ import annotations

RUNBOOK = {"title": "JFrog Kubelet Credential Provider — sandbox Runbook", "updated": "2026-10-16"}

# --------------------------------------------------------------------------- §3 overview

STATUS_AT_A_GLANCE = [
    {"item": "Goal", "state": "Pull an image from a scoped Artifactory repo with no imagePullSecret, authenticated by an OIDC token exchange", "status": "info"},
    {"item": "Result", "state": "Achieved on sandbox. secretless-pull-test pulled mcp/sonarqube:1.26-v1 (142,944,877 bytes, 10.487 s) and reached Running with imagePullSecrets: []", "status": "pass", "evidence_key": "pull"},
    {"item": "Verified directly", "state": "The node config has the bare artifactory_url; the plugin builds a single-scheme token URL; the pod pulled and started; the service account carries the JFrogExchange annotation", "status": "pass"},
    {"item": "Not yet verified", "state": "That Artifactory's audit log attributes the pull to kcp-pull-user (the OIDC-mapped identity). The pull working is strong evidence, but the attribution check has not been done", "status": "open", "evidence_key": "audit"},
    {"item": "Not yet tested", "state": "A brand-new node where the DaemonSet writes the provider entry for the first time (the fix here migrated an existing entry). A pull after the 5 h token cache expires", "status": "open", "evidence_key": "fresh_node"},
    {"item": "Biggest lesson", "state": "Three separate layers each looked correct while the result was wrong: the Helm values, the rendered release, and the file on the node. Check all three, and check the plugin's own log file, not kubelet's summary of it", "status": "info"},
]

STORY = ("A pod with imagePullSecrets: [] pulls a private image because kubelet asks a plugin for a short-lived "
         "token, and the plugin gets it by exchanging the pod's own Kubernetes identity at Artifactory. No password "
         "or token is stored anywhere in the cluster.")

DIAGRAMS = {
    "before_after": {
        "before": {"label": "BEFORE: how every other workload still pulls",
                   "chain": ["Vault (static JFrog credential)", "imagePullSecret (a K8s Secret, in etcd)", "Pod"]},
        "after": {"label": "AFTER: the secretless-pull test (this pilot)",
                  "chain": ["jfrog-provider-sa (JFrogExchange: \"true\")", "projected SA token (short-lived, in-memory only)",
                            "Pod with imagePullSecrets: []"]},
    },
    "option_b_flow": {
        "steps": ["Pod secretless-pull-test (imagePullSecrets: [])", "kubelet (on the node)",
                  "jfrog-credentials-provider plugin binary (invoked per matchImages)",
                  "Artifactory Access (OIDC provider aks-sandbox-oidc + identity mapping)", "Artifactory registry pull"],
        "note": ("Steps 3 and 5 never touch Azure AD. The projected token goes straight to Artifactory Access, which "
                 "validates it against the OIDC provider and identity mapping created in Phase 3. Option A would route "
                 "through an Azure AD app registration instead."),
        "why_b": ("Option B was chosen because it is simpler, and sandbox does not need per-node identity granularity for "
                  "a one-pod pilot."),
    },
    "node": {
        "source": "what CHECK_KUBELET_PROVIDER actually found",
        "config_file": "credential-provider-config.yaml",
        "providers": [{"name": "acr-credential-provider", "matchImages": ["*.azurecr.io"]},
                      {"name": "jfrog-credentials-provider", "matchImages": ["example…jfrog.io"]}],
        "bin_dir": "credential-provider/",
        "binaries": [{"name": "acr-credential-provider", "note": "Microsoft, pre-installed"},
                     {"name": "jfrog-credentials-provider", "note": "~14 MB, placed there by the DaemonSet's init container and downloaded from aks-sandbox-generic-local"}],
        "kubelet": ("The kubelet process was started with --image-credential-provider-config and "
                    "--image-credential-provider-bin-dir. AKS already sets these for the Microsoft provider; JFrog's "
                    "entry just rides along."),
    },
}

# --------------------------------------------------------------------------- §4 environment

ENVIRONMENT = [
    {"item": "Cluster", "value": "aks-sandbox-01"},
    {"item": "Resource group", "value": "rg-aks-sandbox"},
    {"item": "Subscription", "value": "00000000-0000-0000-0000-0000000000a1 (sandbox-subscription)"},
    {"item": "AKS OIDC issuer", "value": "https://centralus.oic.prod-aks.azure.com/00000000-0000-0000-0000-0000000000b2/00000000-0000-0000-0000-0000000000c3/"},
    {"item": "Nodes carrying the provider", "value": "aks-nodepool1-12345678-vmss00000e and aks-nodepool1-12345678-vmss00000g (DaemonSet 2 of 2 at the final run)",
     "note": "Kubelet logs show the second as vmss00000G: only the display case differs, so node names are compared case-insensitively. The node used for most of the pilot was replaced mid-way."},
    {"item": "Namespace", "value": "jfrog"},
    {"item": "ServiceAccount", "value": "jfrog-provider-sa, annotated JFrogExchange: \"true\" (confirmed 2026-10-16; created 2026-07-29)",
     "note": "Option B needs no azure.workload.identity/client-id"},
    {"item": "Helm release", "value": "jfrog-cred-provider in jfrog; chart jfrog-credential-provider-1.4.0, app version 1.4.0, revision 15 at the final checks"},
    {"item": "DaemonSet", "value": "jfrog-cred-provider-jfrog-credential-provider-daemonset"},
    {"item": "Artifactory OIDC provider", "value": "aks-sandbox-oidc"},
    {"item": "Identity mapping", "value": "aks-workload-identity-mapping: sub: system:serviceaccount:jfrog:jfrog-provider-sa maps to JFrog user kcp-pull-user, scope applied-permissions/user"},
    {"item": "Another OIDC provider you will see", "value": "aks-other-cluster-oidc",
     "note": "A real, legitimate provider for a different cluster (eastus2, different issuer GUID). It is not sandbox's. Do not delete it. The console marks it protected and never offers delete or edit on it.", "protected": True},
    {"item": "Test image repo", "value": "aks-sandbox-test-docker-local, reached at its dedicated host example-aks-sandbox-test-docker-local.jfrog.io"},
    {"item": "Test image", "value": "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1"},
    {"item": "Binary mirror repo", "value": "aks-sandbox-generic-local"},
    {"item": "Test pod", "value": "secretless-pull-test in jfrog",
     "note": "A stale pilot-test-pod from before this mechanism worked is still in the namespace (harmless; cleanup offered on the Rollback tab)."},
    {"item": "Node config file", "value": "/var/lib/kubelet/credential-provider-config.yaml"},
    {"item": "Node plugin directory", "value": "/var/lib/kubelet/credential-provider/ (holds acr-credential-provider and jfrog-credentials-provider)"},
    {"item": "Plugin log file", "value": "/var/log/jfrog-credentials-provider/jfrog-credentials-provider.log (JSON lines, on the node)"},
    {"item": "Artifactory", "value": "example.jfrog.io"},
    {"item": "Repo / branch", "value": "ArgoCD.Helm.Template, branch feature/jfrog-kcp"},
    {"item": "Pilot files", "value": "scripts/argocd-utility.sh, scripts/jfrog-provider/values-sandbox.yaml, .fdsecure/argocd-utility.yml",
     "note": "All run as pipeline scenarios, not as ad-hoc local commands."},
    {"item": "Pipeline service connection", "value": "The NPE connection already used for AKS access in this pipeline",
     "note": "Not the prod-scoped connection borrowed from another pipeline: that one failed the Key Vault lookup."},
    {"item": "Key Vault / secret", "value": "kv-ci-secrets, secret artifactory-download-token",
     "note": "The static token used by the mirror step; read via AzureKeyVault@2. The console shows the name only, never the value."},
    {"item": "Cluster inventory", "value": "Cluster subscription IDs live in the cluster inventory JSON in e3-argocd-maintenance"},
]

DISCOVER_READS = ["az aks show for the issuer and the Workload Identity flags", "the Helm release",
                  "the DaemonSet and the SA annotation", "the node list", "Artifactory's OIDC providers and mappings"]

PROTECTED_PROVIDERS = [{"name": "aks-other-cluster-oidc",
                        "reason": "A real provider for a different cluster (eastus2, different issuer GUID). Investigated during the pilot because it kept appearing in a stale node config. Legitimate; leave it alone."}]

# --------------------------------------------------------------------------- §5 sources

SOURCES = [
    {"doc": "README.md", "url": "https://github.com/jfrog/jfrog-credentials-provider",
     "covers": "What the provider is; the end-to-end flow (pod, kubelet, plugin, Artifactory, token); supported platforms (EKS/AKS/GKE/OpenShift); the three binary-distribution modes",
     "gave": "Confirms the mechanism and the property that credentials never touch etcd. Confirms that binary mirroring is one of the three documented modes, not a workaround"},
    {"doc": "AZURE.md", "url": "https://github.com/jfrog/jfrog-credentials-provider/blob/main/AZURE.md",
     "covers": "Azure setup: Option A (nodepool managed identity, needs an Azure AD app registration) vs Option B (projected ServiceAccount token, no Azure AD at all)",
     "gave": "We run Option B as documented: the same annotation (JFrogExchange: \"true\"), the same OIDC provider and identity-mapping JSON shape, the same Helm azure: block, and a bare hostname for the Artifactory URL"},
    {"doc": "Plugin source, internal/handlers/jfrog.go", "url": "https://github.com/jfrog/jfrog-credentials-provider/blob/main/internal/handlers/jfrog.go",
     "covers": "Builds the token URL",
     "gave": "The plugin builds the URL as \"https://\" + artifactory_url + \"/access/api/v1/oidc/token\", so artifactory_url must be a bare hostname",
     "caveat": True},
    {"doc": "Plugin source, internal/utils/utils.go (MergeFiles)", "url": "https://github.com/jfrog/jfrog-credentials-provider/blob/main/internal/utils/utils.go",
     "covers": "Merges the chart's provider entry into the node's credential-provider-config.yaml",
     "gave": "An existing entry is replaced only when its artifactory_url equals the new one. A changed spelling of the URL is not treated as the same entry",
     "caveat": True},
]
SOURCES_CAVEAT = ("The two source-file rows were read from the repository's main branch on 2026-10-16. The deployed chart "
                  "is 1.4.0: re-check both against the 1.4.0 tag.")

EXECUTION_MODEL = {
    "why": ("Personal accounts on these clusters have only Azure Kubernetes Service Cluster User and RBAC Reader: "
            "read-only access, on namespaced objects only. Nodes are cluster-scoped and excluded."),
    "rules": ["Never runs a state-changing kubectl or helm as the operator.",
              "Queues Azure DevOps pipeline runs with parameters (az pipelines run), using the operator's az login and the ADO organization/project from the config, then streams and stores their logs.",
              "Uses local kubectl only for reads that personal RBAC allows, and falls back to the read-only pipeline scenarios on `Forbidden ... User does not have access to the resource in Azure`.",
              "Calls Artifactory REST directly for read checks, with JFROG_ACCESS_TOKEN from the environment. JFrog writes (creating OIDC providers or mappings, Promote) happen only behind an approval gate."],
}

SCOPE = {
    "supported": "AKS, Option B. The cluster's own projected ServiceAccount token goes directly to Artifactory Access, with no Azure AD app.",
    "modelled": "Option A (nodepool managed identity plus an Azure AD app registration). The security review needs a documented decision between A and B.",
    "option_a_needs": ["An Azure AD app registration whose audience the plugin requests (azure_app_audience)",
                       "A user-assigned managed identity on each nodepool (kubelet identity) federated to that app",
                       "azure.workload.identity/client-id on the ServiceAccount (optional annotation key)",
                       "An Artifactory OIDC provider whose issuer is Entra ID (login.microsoftonline.com/<tenant>/v2.0), not the cluster issuer",
                       "An identity mapping on the app/managed identity claims instead of system:serviceaccount:<ns>:<sa>",
                       "Not built in this console: shown for the A-vs-B decision only"],
    "later": ["EKS", "GKE", "OpenShift"],
}

# --------------------------------------------------------------------------- §6 pilot scenarios

PILOT_SCENARIOS = [
    {"name": "MIRROR_JFROG_BINARY", "does": "Downloads the plugin binary on the pipeline agent and uploads it to aks-sandbox-generic-local",
     "changes_state": "Yes (Artifactory)", "mutates": True, "destructive": False, "restarts_daemonset": False, "dry_run": False},
    {"name": "INSTALL_JFROG_PROVIDER", "does": "Labels nodes; runs helm upgrade --install with values-sandbox.yaml; deletes old provider pods so they pick up the new config",
     "changes_state": "Yes (cluster)", "mutates": True, "destructive": False, "restarts_daemonset": True, "dry_run": True},
    {"name": "CREATE_TEST_POD", "does": "Deletes and recreates secretless-pull-test, waits up to 2 minutes, prints pod status and events",
     "changes_state": "Yes (test pod only)", "mutates": True, "destructive": False, "restarts_daemonset": False, "dry_run": False},
    {"name": "CHECK_KUBELET_PROVIDER", "does": "A short-lived privileged pod with a read-only host mount. Prints kubelet flags, node config, plugin log file, kubelet journal, and a QUICK SUMMARY footer",
     "changes_state": "Creates and deletes a diagnostic pod", "mutates": False, "destructive": False, "restarts_daemonset": False, "dry_run": False, "diagnostic_pod": True},
    {"name": "CHECK_JFROG_POD_CONFIG", "does": "Prints helm get values, the rendered artifactory_url, the chart version and the service account annotations. It also restarts the DaemonSet",
     "changes_state": "Yes (DaemonSet restart)", "mutates": True, "destructive": False, "restarts_daemonset": True, "dry_run": False,
     "warning": "Looks like a read-only check but restarts the DaemonSet. Prefer a read-only `kubectl get sa -o json` when personal RBAC allows it."},
    {"name": "RESET_NODE_JFROG_ENTRY", "does": "On nodes whose artifactory_url still starts with https://: backs up the node config, removes the JFrog entry, restarts the DaemonSet so it re-adds it",
     "changes_state": "Yes (node file; dry run supported)", "mutates": True, "destructive": True, "restarts_daemonset": True, "dry_run": True},
    {"name": "CLEANUP_STALE_JFROG_DAEMONSET", "does": "Removes an orphaned DaemonSet from an older install",
     "changes_state": "Yes (deletes)", "mutates": True, "destructive": True, "restarts_daemonset": False, "dry_run": False},
    {"name": "FIND_CREDENTIAL_PROVIDER_WRITERS", "does": "Searches workloads in all namespaces for anything that writes the provider config path",
     "changes_state": "No", "mutates": False, "destructive": False, "restarts_daemonset": False, "dry_run": False},
    {"name": "CHECK_AKS_NODEPOOL_CONFIG", "does": "Reads the AKS nodepool configuration and Azure Policy assignments for anything touching the provider",
     "changes_state": "No", "mutates": False, "destructive": False, "restarts_daemonset": False, "dry_run": False},
    {"name": "CHECK_NODE_VMSS_EXTENSIONS", "does": "Reads VMSS extensions and custom data for the nodepool",
     "changes_state": "No", "mutates": False, "destructive": False, "restarts_daemonset": False, "dry_run": False},
    {"name": "MIRROR_TEST_IMAGE", "does": "Docker pull/push mirroring through a Docker Hub remote. Superseded; not used for the final result (Phase 3b)",
     "changes_state": "Yes", "mutates": True, "destructive": False, "restarts_daemonset": False, "dry_run": False, "superseded": True},
]
# Added by the console (Demo step 7); produces a diff to argocd-utility.sh for review.
CONSOLE_SCENARIOS = [
    {"name": "CREATE_CONTROL_POD", "does": "Like CREATE_TEST_POD but the pod uses the namespace's default service account (TEST_POD_SA override); the pull must be refused",
     "changes_state": "Yes (test pod only)", "mutates": True, "destructive": False, "restarts_daemonset": False, "dry_run": False,
     "not_rehearsed": True, "requires_script_change": True},
    {"name": "DELETE_TEST_POD", "does": "kubectl delete pod <name> -n jfrog (secretless-pull-test, or the stale pilot-test-pod)",
     "changes_state": "Yes (deletes)", "mutates": True, "destructive": True, "restarts_daemonset": False, "dry_run": True},
    {"name": "UNINSTALL_JFROG_PROVIDER", "does": "helm uninstall jfrog-cred-provider -n jfrog. Node entries and the node label stay",
     "changes_state": "Yes (deletes)", "mutates": True, "destructive": True, "restarts_daemonset": False, "dry_run": True, "untested": True},
    {"name": "REMOVE_NODE_LABEL", "does": "Removes credentialsProviderEnabled=true from every node (INSTALL_JFROG_PROVIDER adds it; uninstall does not remove it)",
     "changes_state": "Yes (cluster)", "mutates": True, "destructive": True, "restarts_daemonset": False, "dry_run": True},
]

OUTPUT_RULES = {
    "lesson": ("Output is capped. Pasting output over roughly 50,000 characters into a tool lost the end, including "
               "the summary blocks."),
    "rules": ["filter journal noise", "window the journal", "cap the plugin-log grep at the last 10 lines",
              "repeat the key results in a QUICK SUMMARY footer"],
    "parser": "The console parses the QUICK SUMMARY footer first and falls back to the full log.",
}

PIPELINE_DIAGRAM = [
    {"scenario": "MIRROR_JFROG_BINARY", "text": "Downloads the binary (with -L and a size guard) and PUTs it into aks-sandbox-generic-local. Its token comes from Key Vault (artifactory-download-token)."},
    {"scenario": "INSTALL_JFROG_PROVIDER", "text": "Runs helm upgrade --install and deletes the old pods to force pickup of the fresh config. Reads values-sandbox.yaml, which is committed and reviewed in a PR, not a runtime variable."},
    {"scenario": "DaemonSet", "text": "Produces the DaemonSet jfrog-cred-provider (on nodes labelled credentialsProviderEnabled)."},
    {"scenario": "CREATE_TEST_POD", "text": "Deletes and recreates secretless-pull-test, then polls and dumps events."},
]

# --------------------------------------------------------------------------- §7 phases

PHASES = [
    {"id": "0", "title": "Confirm the mechanism is even possible here",
     "text": ("AKS supports the out-of-tree kubelet image-credential-provider mechanism, and Microsoft already uses it: "
              "every AKS node runs acr-credential-provider for ACR/MCR pulls, with kubelet started with "
              "--image-credential-provider-config=/var/lib/kubelet/credential-provider-config.yaml and "
              "--image-credential-provider-bin-dir=/var/lib/kubelet/credential-provider. JFrog's provider therefore "
              "only has to become a second entry in the same config. README.md frames this as the same standard that "
              "AWS ECR, Google Cloud and Azure use for registry authentication. We register a second, ordinary "
              "implementation of a mechanism AKS already runs in production for its own registry. "
              "acr-credential-provider is on every AKS node whether or not you use ACR."),
     "scenario": "CHECK_KUBELET_PROVIDER",
     "notes": [
         "CHECK_KUBELET_PROVIDER reads the config file and bin-dir off the host through a hostPath mount, and removes itself afterwards.",
         "Which node it checks: the node the real JFrog pod or test pod is on, falling back to the first node only if neither exists. The first version always used node 0, which with two nodepools is not reliably the node you care about. The console shows which node was inspected and why.",
         "The host is mounted read-only. That is deliberate (it is a diagnostic), but you cannot meaningfully execute the plugin binary from inside that pod: its logger tries to create files under /var/log and fails with `read-only file system`. That error is an artifact of the diagnostic pod and says nothing about the real node. Read the plugin's log file instead. The binary was fine: 14,359,603 bytes, and it runs.",
     ]},
    {"id": "1", "title": "AKS prerequisites",
     "commands": ["az aks show --resource-group <rg> --name <cluster> --query oidcIssuerProfile.enabled -o tsv",
                  "az aks show --resource-group <rg> --name <cluster> --query securityProfile.workloadIdentity.enabled -o tsv",
                  "az aks show --resource-group <rg> --name <cluster> --query oidcIssuerProfile.issuerUrl -o tsv"],
     "notes": ["Both must return true.",
               "If not, the fix is `az aks update --enable-oidc-issuer --enable-workload-identity`. Shown as a hint; the console never runs it (it was never needed on sandbox).",
               "Phase 3 needs the cluster's real issuer URL."],
     "fix_hint": "az aks update --resource-group <rg> --name <cluster> --enable-oidc-issuer --enable-workload-identity"},
    {"id": "2", "title": "Kubernetes-side setup",
     "notes": ["Namespace jfrog.",
               "ServiceAccount jfrog-provider-sa, annotated JFrogExchange: \"true\". The chart's config requires it: requiredServiceAccountAnnotationKeys: [JFrogExchange].",
               "A Pod can only reference a ServiceAccount in its own namespace, so the test pod has to live in jfrog too.",
               "Without personal namespace access, check the annotation with CHECK_JFROG_POD_CONFIG: its last section prints the SA metadata, and the expected line is JFrogExchange: \"true\" (confirmed 2026-10-16). Remember this scenario restarts the DaemonSet. Prefer a read-only `kubectl get sa -o json` when personal RBAC allows it."]},
    {"id": "3", "title": "Artifactory-side OIDC setup (JFrog writes, behind approval)",
     "provider_request": {"method": "POST", "path": "/access/api/v1/oidc",
                          "body": {"name": "aks-sandbox-oidc", "issuer_url": "<cluster issuer URL>", "provider_type": "Azure",
                                   "token_issuer": "<cluster issuer URL>", "use_default_proxy": False,
                                   "description": "OIDC provider for Azure AKS Workload Identity"}},
     "mapping_request": {"method": "POST", "path": "/access/api/v1/oidc/aks-sandbox-oidc/identity_mappings",
                         "body": {"name": "aks-workload-identity-mapping", "description": "Azure AKS Workload Identity mapping",
                                  "claims": {"iss": "<cluster issuer URL>", "sub": "system:serviceaccount:jfrog:jfrog-provider-sa"},
                                  "token_spec": {"username": "kcp-pull-user", "scope": "applied-permissions/user",
                                                 "audience": "*@*", "expires_in": 3600},
                                  "priority": 1}},
     "validate": "GET /access/api/v1/oidc (returns a flat JSON array of providers)",
     "notes": ["The identity mapping is the heart of the design: it ties a specific Kubernetes identity to a specific JFrog identity.",
               "On this instance the provider list also contains aks-other-cluster-oidc, which belongs to a different cluster (eastus2, issuer GUID different from sandbox's). It was investigated during the pilot because it kept appearing in a stale node config. The entry is legitimate and must be left alone.",
               "Provider name: blank defaults to aks-<ClusterName>-oidc. An existing provider with a different issuer is refused, never reused. Same issuer means reuse, with a note.",
               "In the identity-mapping preview, iss must equal the provider's issuer, and sub must equal system:serviceaccount:<ns>:<sa> exactly.",
               "Issuer drift: if the cluster issuer the console discovers differs from the provider's issuer_url, the console shows a red mismatch."]},
    {"id": "3b", "title": "Getting a test image into the scoped repo",
     "intro": ("The test pod must pull from the scoped repo aks-sandbox-test-docker-local, not from some other registry. "
               "There is no Docker on the working laptop, and enterprise policy limits software sources to approved "
               "Artifactory/ADO/GitHub locations."),
     "steps": [
         {"title": "Use the repo's dedicated subdomain, not the bare domain",
          "text": ("The Repository Path Method (example.jfrog.io/<repo>/<image>) does not work for local repos on this instance. "
                   "A registry request against the bare domain failed with `Unsupported v2 repository request for ''`, and the token "
                   "realm in the WWW-Authenticate header came back without the repo key, consistently across three nodes. Every Docker "
                   "repo has its own subdomain <server>-<repo-key>.jfrog.io. The same host goes into the image reference and into matchImages."),
          "probe": "curl -sI https://example-aks-sandbox-test-docker-local.jfrog.io/v2/",
          "expect": "401 with WWW-Authenticate: Bearer realm=... whose realm URL names aks-sandbox-test-docker-local"},
         {"title": "Find the image in a local repo",
          "text": ("The Docker Promote API copies an image server-side between repositories, so no client-side push is needed. Its "
                   "source must be a local repository: a virtual one (such as docker-virtual) fails with "
                   "`Unsupported V2 repository request for 'docker-virtual'`. In this case the image was held by "
                   "containers-docker-local."),
          "request": {"method": "POST", "path": "/artifactory/api/search/aql", "content_type": "text/plain",
                      "body": 'items.find({"path":{"$match":"mcp/sonarqube*"}})'}},
         {"title": "Promote it",
          "request": {"method": "POST", "path": "/artifactory/api/docker/containers-docker-local/v2/promote",
                      "body": {"targetRepo": "aks-sandbox-test-docker-local", "dockerRepository": "mcp/sonarqube", "tag": "1.26-v1", "copy": True}},
          "expect": "Promotion ended successfully",
          "caveat": "This body is the documented shape. The exact payload used in the pilot was not saved in the pipeline log: confirm field names against JFrog's current documentation."},
         {"title": "Know where it lands",
          "text": ("Promote keeps the source path, so the image is mcp/sonarqube:1.26-v1 in the target repo, not a renamed or "
                   "flattened name. The test pod references example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1."),
          "request": {"method": "GET", "path": "/artifactory/api/storage/aks-sandbox-test-docker-local/mcp/sonarqube/1.26-v1"}},
     ],
     "dont": [
         "Pushing from a laptop: no Docker installed.",
         "Pulling through the Docker Hub remote example-docker-remote.jfrog.io. That repo was dormant: JFrog Cloud pauses idle repos, and requests returned a 302 to landing.jfrog.com/reactivate-server/example-docker-remote. It needs someone with Artifactory admin to reactivate it. MIRROR_TEST_IMAGE was written for this route and is left in the script, but was not used for the result. The console detects a 302 to reactivate-server and says \"dormant repo, needs an admin\".",
         "Pointing the test pod at a different registry host to sidestep the routing problem. Tried once and reverted: the point of the pilot is to pull from the scoped repo only.",
     ]},
    {"id": "4", "title": "Deploying the provider itself",
     "notes": [
         "MIRROR_JFROG_BINARY downloads the jfrog-credential-provider binary and uploads it to aks-sandbox-generic-local. The in-cluster init container cannot reach releases.jfrog.io (corporate TLS interception), so the download happens on the pipeline agent, which trusts the intercepting proxy's certificate, and the binary is mirrored into our own Artifactory.",
         "INSTALL_JFROG_PROVIDER labels all nodes credentialsProviderEnabled=true (the chart's node affinity requires it), then runs `helm upgrade --install jfrog-cred-provider jfrog/jfrog-credential-provider` with the committed values file (no --wait), and deletes the existing provider pods so they restart and pick up the new config.",
     ],
     "values_history": {
         "text": ("Originally every setting was a loose pipeline variable that could be overridden at run time. That caused two "
                  "real, several-hour bugs, and neither threw an obvious error:"),
         "bugs": ["The OIDC provider name was set to aks-other-cluster-oidc, which belongs to another cluster. The right one is aks-sandbox-oidc.",
                  "matchImages was set to example-jfrog-releases-remote.jfrog.io, which does not match the test image's registry, so kubelet never invoked the provider."],
         "rule": ("All security-relevant settings are committed in scripts/jfrog-provider/values-sandbox.yaml. Only the download token is "
                  "injected at run time (--set binaryDownload.auth.accessToken=...), because it is a real secret. The console's config is "
                  "the source of truth for that file: it renders it, diffs it against the committed file and the live helm get values -a, "
                  "and never lets the security-relevant fields come from free-form run-time variables."),
     },
     "upstream_check": {
         "text": ("The Option B sample in AZURE.md sets both tokenAttributes.enabled: true and tokenAttributes.serviceAccountTokenAudience. "
                  "Ours sets only enabled: true. This was initially flagged as a gap, and it is not one: the config the chart writes onto "
                  "the node already contains all of the following, supplied by the chart. Nothing needs adding. The console checks these on the node."),
         "keys": ["serviceAccountTokenAudience: api://AzureADTokenExchange", "cacheType: ServiceAccount", "requireServiceAccount: true",
                  "requiredServiceAccountAnnotationKeys: [JFrogExchange]", "optionalServiceAccountAnnotationKeys: [azure.workload.identity/client-id]"],
     },
     "cache_note": "defaultCacheDuration: 5h differs from AZURE.md's example of 5 minutes. It is a tuning choice, not a defect, but it matters for retesting (Phase 5).",
     "binary_modes": {
         "modes": ["a custom downloadUrl (recommended against an anonymous-access repo)", "an offline internalBinaryHostPath",
                   "an authenticated download through binaryDownload.auth"],
         "ours": ("We use the third, because no anonymous-read repo exists on this instance. Making aks-sandbox-generic-local "
                  "anonymous-read would remove the need for JFROG_DOWNLOAD_TOKEN entirely."),
     }},
    {"id": "5", "title": "The test pod, retesting, logs, troubleshooting",
     "notes": [
         "CREATE_TEST_POD always deletes and recreates the pod. An earlier version skipped creation if the pod existed, which just replayed the old failure. It polls for up to two minutes for Running or a pull error, and prints events.",
         "The image can be overridden with the TEST_POD_IMAGE variable. The point of the pod is the empty imagePullSecrets: []: if it reaches Running, the image was pulled with no stored credential.",
         "Result on 2026-10-16: after the node entry was corrected, the events showed `Pulling image ...` followed by `Successfully pulled image \"example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1\" in 10.487s ... Image size: 142944877 bytes`, then Created and Started. The previous attempts had ended in ImagePullBackOff with the anonymous-token 401.",
         "A later re-run of CREATE_TEST_POD reported `Container image ... already present on machine`. That run did not contact the registry and is not evidence of a fresh exchange. The earlier pull is the proof.",
         "Two caches hide a retest: the image is cached on the node, and the plugin's token is cached for defaultCacheDuration (5 h).",
         "To force a real exchange, pull a different tag of an image that exists in the scoped repo, or remove the image from the node, and run the pod again. Then read the plugin log.",
     ]},
]

TEST_POD_YAML = """apiVersion: v1
kind: Pod
metadata:
  name: secretless-pull-test
  namespace: jfrog
spec:
  serviceAccountName: jfrog-provider-sa
  imagePullSecrets: []
  containers:
    - name: test
      image: example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1
"""

VALUES_FILE_PATH = "scripts/jfrog-provider/values-sandbox.yaml"

VALIDATOR_RULES = [
    "artifactoryUrl must be a bare hostname: no scheme, no path (`https://` here produces `https://https://...`).",
    "Every matchImages entry is a bare dedicated subdomain. Warn if it is the platform host.",
    "Every test or target image host must be matched by matchImages.",
    "jfrog_oidc_provider_name must exist in Artifactory with this cluster's issuer.",
    "downloadUrl must not end in an arch suffix such as -amd64. The chart appends its own.",
    "defaultCacheDuration must be at most the mapping's expires_in. Warn when it exceeds 30m (the template's default; the pilot's 5h was never tested against token expiry).",
    "Chart repo must be https://charts.jfrog.io and chart jfrog/jfrog-credential-provider (and is flagged as not on the approved-sources list).",
]

# --------------------------------------------------------------------------- the doubled https:// problem

DOUBLED_HTTPS = {
    "containerd": ("failed to fetch anonymous token: unexpected status from GET request to "
                   "https://example-aks-sandbox-test-docker-local.jfrog.io/artifactory/api/docker/aks-sandbox-test-docker-local/v2/token?... : 401"),
    "containerd_meaning": ("That text is containerd's. It only means \"no credentials were supplied\", so it looks identical whether "
                           "the plugin was never invoked or ran and failed. The real error was only in the plugin's own log."),
    "plugin_lines": ["RT oidc token url :https://https://example.jfrog.io/access/api/v1/oidc/token",
                     "ERROR ... Cause Post \"https://https//example.jfrog.io/access/api/v1/oidc/token\": dial tcp: lookup https ... no such host"],
    "formula": "\"https://\" + artifactory_url + \"/access/api/v1/oidc/token\"",
    "explanation": ("Given https://example.jfrog.io, it produces https://https://example.jfrog.io/..., which fails DNS resolution before "
                    "any token exchange happens. The OIDC provider, identity mapping and repo permissions were never reached, so they "
                    "were not the problem."),
    "three_places": [
        {"layer": "values-sandbox.yaml", "value": "artifactoryUrl: \"example.jfrog.io\"", "note": "fixed, bare"},
        {"layer": "helm get manifest", "value": "artifactory_url: \"example.jfrog.io\"", "note": "verified bare, revision 15"},
        {"layer": "node config file", "value": "artifactory_url: https://example.jfrog.io", "note": "still the old value"},
        {"layer": "plugin builds", "value": "https://https://example.jfrog.io/...", "note": ""},
    ],
    "observed": ["The release and the rendered manifest were correct.",
                 "The node file on vmss00000g kept the old https://example.jfrog.io value, with one JFrog entry (not two).",
                 "The plugin's log showed the doubled URL until the node entry was removed.",
                 "After removing it and restarting the DaemonSet, the node file showed the bare value and the plugin's newest log line showed a single https://. The pod then pulled."],
    "inferred": ["The chart's merge step replaces an existing entry only when artifactory_url is identical, so the old spelling was not recognized as the same entry.",
                 "Why no second (new) entry appeared alongside the old one was not established."],
    "conclusion": ("If the spelling of artifactory_url ever changes, the existing node entry has to be removed so it is recreated "
                   "from the release."),
}

NODE_MIGRATION = {
    "when": "Whenever CHECK_KUBELET_PROVIDER shows a node whose JFrog artifactory_url value differs from the release.",
    "steps": [
        {"n": 1, "scenario": "CHECK_JFROG_POD_CONFIG", "text": "Compare `helm get values -a | grep artifactoryUrl` with the rendered artifactory_url in the manifest. Both should be the bare hostname. If not, fix values-sandbox.yaml and re-run INSTALL_JFROG_PROVIDER first."},
        {"n": 2, "scenario": "RESET_NODE_JFROG_ENTRY", "dry_run": True, "text": "Run with DryRun: true. It lists the labelled nodes it would touch."},
        {"n": 3, "scenario": "RESET_NODE_JFROG_ENTRY", "dry_run": False, "text": "Run with DryRun: false. Per node it skips if no artifactory_url starts with https:// (`Nothing to fix on this node`); aborts if any provider-level entry other than the JFrog one follows it (never deletes acr-credential-provider; counts only provider-level entries by indentation — the first version counted env: items and aborted on both nodes); copies the file to credential-provider-config.yaml.bak-before-jfrog-reset; deletes the JFrog entry and prints the file. Then it restarts the DaemonSet so the setup step re-adds the entry from the release."},
        {"n": 4, "scenario": "CHECK_KUBELET_PROVIDER", "text": "In the footer, the `jfrog provider entry` block must show artifactory_url without https://, and the newest line under `latest OIDC token URLs` must contain a single https://. Older lines still show the doubled form, so go by the newest timestamp."},
        {"n": 5, "scenario": "CHECK_KUBELET_PROVIDER", "text": "Kubelet reads credential-provider-config.yaml once, at its own start; a correct file does not take effect until kubelet restarts. During this pilot a kubelet restart was observed alongside each config write by the setup step. Confirm `Kubelet started at` is later than `Config file mtime`. The console computes this and flags \"kubelet is running an older config\"."},
    ],
    "rollback": {"text": "Copy the .bak-before-jfrog-reset file back over credential-provider-config.yaml and restart kubelet.", "tested": False},
}

# --------------------------------------------------------------------------- §8 bugs

BUGS = [
    {"what": "JFrogToolInstaller@0 task failed: \"task is missing\"", "cause": "The JFrog Azure DevOps Marketplace extension is not installed in this org, although an older pipeline references it", "fix": "Reverted to a Key Vault-sourced static token (artifactory-download-token) for the mirror step, instead of the OIDC-based jf CLI", "rule": "ado-task-missing", "guard": "ado log parser"},
    {"what": "Key Vault lookup in the pipeline failed or was forbidden", "cause": "Used the wrong Azure service connection (a prod-scoped one borrowed from another pipeline)", "fix": "Switched to the same NPE connection already used for AKS access in this pipeline", "rule": "kv-wrong-connection", "guard": "model: service connection"},
    {"what": "helm repo add ... https://jfrog.github.io/... returned 404", "cause": "Wrong repo URL and chart name, guessed before checking the docs", "fix": "The correct repo is https://charts.jfrog.io, chart jfrog/jfrog-credential-provider (singular)", "rule": "helm-repo-404", "guard": "model: chart repo/name"},
    {"what": "Init container: curl: (22) ... 401", "cause": "The downloadUrl already ended in -amd64 and the chart appends its own, so the path was doubled", "fix": "Strip the arch suffix from the URL passed to the chart", "rule": "downloadurl-arch", "guard": "model: downloadUrl", "scenario": "downloadurl-arch-doubled"},
    {"what": "Init container \"succeeded\" but wrote a 0-byte binary", "cause": "curl did not follow the HTTP 302 from releases.jfrog.io; it saved the empty redirect body and reported success", "fix": "Added -L, plus a size guard that refuses to mirror anything under 1 MB", "rule": "zero-byte-binary", "guard": "binary_integrity", "scenario": "zero-byte-binary"},
    {"what": "DaemonSet stuck at Available: 0/1; helm upgrade --wait timed out each time", "cause": "Kubernetes does not restart a pod when a ConfigMap it mounts changes, so every upgrade polled the same broken pod", "fix": "Dropped --wait. The script deletes existing provider pods after every install", "rule": "helm-wait", "guard": "no --wait; pods recreated after install", "scenario": "helm-wait-timeout"},
    {"what": "CHECK_KUBELET_PROVIDER showed only acr-credential-provider after a successful install", "cause": "It inspected node 0, not the node the JFrog pod ran on", "fix": "The scenario looks up the real pod's node first", "rule": "wrong-node-checked", "guard": "summary: node inspected and why", "scenario": "checked-wrong-node"},
    {"what": "Pulls from example.jfrog.io/<repo>/... failed with Unsupported v2 repository request for ''", "cause": "The bare-domain Repository Path Method does not work for local repos on this instance", "fix": "Use the repo's dedicated subdomain (Phase 3b)", "rule": "unsupported-v2", "guard": "matchImages validator; realm probe", "scenario": "bare-domain-path-method"},
    {"what": "Docker Promote failed with Unsupported V2 repository request for 'docker-virtual'", "cause": "Promote needs a local source repo, and a virtual repo was given", "fix": "Found the real local repo with AQL (containers-docker-local) and promoted from it", "rule": "promote-virtual", "guard": "promote: source must be local", "scenario": "promote-from-virtual"},
    {"what": "The promoted image was not at the expected path", "cause": "Promote preserves the source path (mcp/sonarqube)", "fix": "Updated the test pod's default image to the real path", "rule": "promote-path", "guard": "storage verify after Promote"},
    {"what": "The node config kept reverting to old values (example-jfrog-releases-remote.jfrog.io, aks-other-cluster-oidc) within about a second of every correct write, each time with a kubelet restart", "cause": "A 78-day-old orphaned DaemonSet from an earlier install was still in namespace jfrog-provider, writing to the same host path. It was deleted with CLEANUP_STALE_JFROG_DAEMONSET. The reversion continued on the old VM after the DaemonSet was gone and stopped only when the node was replaced. The exact mechanism on that VM was not proven (a leftover pod from the deleted DaemonSet is the leading suspect)", "fix": "Delete the orphaned DaemonSet; node replacement cleared the rest. A fresh node then held the correct config for 20+ minutes. Ruled out: AKS nodepool config, Azure Policy, VMSS extensions and custom data, and any other workload writing the path", "rule": "config-reverts", "guard": "writers check; revert detection", "scenario": "orphaned-daemonset-reverts-config"},
    {"what": "A diagnostic scenario printed grep: : No such file or directory", "cause": "In an unquoted heredoc, the outer script expanded $f (meant for the script inside the pod) to empty", "fix": "Escape inner variables as \\$f. This applies to every kubectl apply -f - <<EOF that embeds a script", "rule": "heredoc-expansion", "guard": "generated scripts are files or quoted heredocs (<<'EOF')"},
    {"what": "A diagnostic scenario failed with awk: command not found", "cause": "The mcr.microsoft.com/azurelinux/base/core:3.0 image has no awk", "fix": "Use sed -n '/start/,/end/p'", "rule": "awk-missing", "guard": "parser never depends on awk"},
    {"what": "Pasted scenario output kept omitting the important sections", "cause": "The output exceeded the roughly 50,000-character limit of the tool it was pasted into and was cut off, losing the end, including the summary blocks", "fix": "Filter journal noise, window the journal, cap the plugin-log grep at the last 10 lines, and repeat the key results in a QUICK SUMMARY footer", "rule": "output-truncated", "guard": "QUICK SUMMARY first, full-log fallback"},
    {"what": "The pull still failed with the same 401 after artifactoryUrl was changed to a bare hostname", "cause": "The node's existing entry kept the old https:// value", "fix": "RESET_NODE_JFROG_ENTRY", "rule": "doubled-https", "guard": "three-layer diff", "scenario": "stale-node-entry-after-fix"},
    {"what": "RESET_NODE_JFROG_ENTRY aborted on both nodes with \"another provider entry follows\"", "cause": "Its safety check counted every - name: line, including the env: items inside the JFrog entry", "fix": "Count only provider-level entries (matching the entry's own indentation) and skip nodes that are already correct", "rule": "reset-safety-old", "guard": "logic.reset_plan", "scenario": "reset-safety-counts-env-items"},
    {"what": "Executing the plugin from the diagnostic pod printed read-only file system and exit 1, suggesting a broken binary", "cause": "The diagnostic pod mounts the host read-only, so the plugin's logger could not open its log file. The binary was fine (14,359,603 bytes; it runs)", "fix": "Read the plugin's log file on the host directly instead of executing the binary", "rule": "diagnostic-readonly", "guard": "plugin-log parser labels it"},
    {"what": "git push to the feature branch returned 403", "cause": "Write access to the repo was lost mid-session", "fix": "Needed an actual permission grant; no client-side fix existed", "rule": "git-push-403", "guard": "git helper stops"},
    {"what": "Several git push rejections (\"fetch first\")", "cause": "Other sessions pushed to the same branch in the meantime", "fix": "git fetch, review the diff, git pull --no-edit. One real merge conflict (two scenario names added at the same list position in .fdsecure/argocd-utility.yml) was resolved by keeping both", "rule": "git-push-rejected", "guard": "git helper: fetch before push; stop on conflict"},
]

GUARDS = [
    "Refuse a chart repo other than https://charts.jfrog.io, and a chart name other than jfrog/jfrog-credential-provider. Also flag the chart source as not on the approved-sources list.",
    "The mirror step must use curl -L and refuse binaries under 1 MB. The console checks the mirrored artifact's size through the storage API (expected about 14 MB; warn on anything far from 14,359,603 bytes for 1.4.0).",
    "Never helm upgrade --wait. After an install, check that provider pods were recreated (pod start time later than the install time).",
    "DaemonSet writers: before install and in the Nodes view, list every DaemonSet in every namespace that mounts /var/lib/kubelet or the provider config path (FIND_CREDENTIAL_PROVIDER_WRITERS plus a read-only kubectl get daemonset -A); flag an orphaned JFrog DaemonSet in another namespace (jfrog-provider) as critical; offer CLEANUP_STALE_JFROG_DAEMONSET behind approval.",
    "Detect \"config reverts after a correct write\": two consecutive checks where the node file goes back to an older value, or the file's mtime changes within seconds of a correct write. Point to writers, then to node replacement.",
    "Generate any in-pod scripts as files, not unquoted heredocs. If the console emits a heredoc, it is quoted (<<'EOF').",
    "The parser never depends on awk being in the diagnostic image.",
    "Git: fetch before push. On a rejection, pull with --no-edit, and stop for a human on a conflict.",
]

# --------------------------------------------------------------------------- §9 logs and troubleshooting

PLUGIN_LOG_TABLE = [
    {"line": "getting envs - azureAppAudience: ..., jfrogOidcProviderName: ...", "meaning": "The values the plugin actually received from the node config. Compare with the values file"},
    {"line": "Service Account Token obtained using Pod Identity (Kubernetes Workload Identity)", "meaning": "kubelet passed a projected ServiceAccount token (the Option B path is active)"},
    {"line": "RT oidc token url :https://<host>/access/api/v1/oidc/token", "meaning": "The URL it will call. It must contain exactly one https://"},
    {"line": "ERROR ... createArtifactoryToken ... dial tcp: lookup ... no such host", "meaning": "Name resolution failed: look at the URL on the previous line"},
    {"line": "Auto-update functionality is disabled", "meaning": "Expected; disable_provider_autoupdate is \"true\""},
]

KUBELET_LINES = [
    {"line": "Failed to provide credentials for image ... error execing credential provider plugin jfrog-credentials-provider ... exit status 1: (followed by nothing)",
     "meaning": "Kubelet does not capture the plugin's output, so this tells you the plugin was invoked and failed, not why. The reason is in the plugin's own log."},
    {"line": "failed to fetch anonymous token ... 401 (in the pod events)",
     "meaning": "containerd reporting that no credential was supplied. It is the symptom for both \"plugin not invoked\" and \"plugin failed\"."},
]

AUDIT = {
    "text": ("The remaining check for the pilot goal is Artifactory's audit or access log for the pull time. The request should "
             "be attributed to kcp-pull-user (the identity the mapping resolves to), not to a static service account. This has "
             "not been done yet."),
    "console": ["query Artifactory's access/request log for the pull window (from the pod event timestamps)", "show the attributed user",
                "pass only when it is the mapped user"],
    "manual_steps": [
        "Note the pull time from the pod events (the Pulling / Successfully pulled lines) and convert it to UTC.",
        "As an Artifactory admin: Administration → Monitoring → System Logs (or download artifactory-request.log and access-request.log from the support bundle).",
        "In artifactory-request.log, find GET lines for /api/docker/aks-sandbox-test-docker-local/v2/mcp/sonarqube/manifests/1.26-v1 within ±2 minutes of the pull. The username column must be kcp-pull-user.",
        "In access-request.log, find the POST /access/api/v1/oidc/token at the same time; it should succeed (200) shortly before the manifest GET.",
        "Record the lines (no tokens) in the console's Verify tab as evidence.",
    ],
}

TROUBLESHOOTING = [
    {"id": "anon-401", "symptom": "Pod ErrImagePull with failed to fetch anonymous token ... 401", "meaning": "Plugin not invoked, or invoked and failed. These look identical here", "where": "Plugin log file; matchImages on the node vs the image host", "observed": True},
    {"id": "doubled-https", "symptom": "Plugin log shows https://https://", "meaning": "artifactory_url has a scheme somewhere", "where": "helm get values -a, the rendered manifest, and the node file's entry. Fix the layer that disagrees; use RESET_NODE_JFROG_ENTRY for a stale node entry", "observed": True},
    {"id": "matchimages-mismatch", "symptom": "matchImages on the node is not the image host", "meaning": "Config not applied, or the wrong node was checked", "where": "CHECK_KUBELET_PROVIDER footer, live matchImages; check the node the pod is on", "observed": True},
    {"id": "kubelet-old-config", "symptom": "Node config fixed but behaviour unchanged", "meaning": "Kubelet still running with the config it read at start", "where": "Compare Kubelet started at with Config file mtime", "observed": True},
    {"id": "config-reverts", "symptom": "Config reverts right after a correct write", "meaning": "Another writer to the same host path (orphaned DaemonSet from an old install)", "where": "FIND_CREDENTIAL_PROVIDER_WRITERS, kubectl get daemonset -A", "observed": True},
    {"id": "token-endpoint-refused", "symptom": "Plugin reaches Artifactory and gets 401/403 from the token endpoint", "meaning": "OIDC provider, issuer or identity-mapping mismatch (sub must equal system:serviceaccount:jfrog:jfrog-provider-sa)", "where": "The Phase 3 validation call", "observed": False},
    {"id": "pull-403", "symptom": "Token issued but the pull gets 403", "meaning": "The mapped user lacks permission on the repo", "where": "Permission target for kcp-pull-user on the repo; a remote repo also needs Deploy/Cache for the first pull", "observed": True},
    {"id": "unsupported-v2", "symptom": "Registry request returns Unsupported v2 repository request", "meaning": "Using the bare domain, or a virtual repo as a Promote source", "where": "Use the dedicated subdomain; promote from a local repo", "observed": True},
    {"id": "azure-forbidden", "symptom": "Forbidden ... does not have access to the resource in Azure from local kubectl", "meaning": "Personal accounts are read-only by design", "where": "Run the check as a pipeline scenario", "observed": True},
]
NOT_OBSERVED_LABEL = "expected, not observed"

# --------------------------------------------------------------------------- §10 template mode

TEMPLATE_PATHS = [
    {"piece": "Install pipeline (one cluster per run)", "path": ".fdsecure/jfrog-kcp-cluster.yml"},
    {"piece": "Reusable steps for other pipelines", "path": ".fdsecure/templates/jfrog-kcp-steps.yml"},
    {"piece": "Reusable run template", "path": ".fdsecure/templates/jfrog-kcp-run.yml"},
    {"piece": "Install script", "path": "scripts/jfrog-kcp/jfrog-kcp.sh"},
    {"piece": "Values template", "path": "scripts/jfrog-kcp/values.tpl.yaml"},
    {"piece": "Mappings pipeline", "path": ".fdsecure/jfrog-kcp-mappings.yml"},
    {"piece": "Mappings reconcile script", "path": "scripts/jfrog-kcp/reconcile-mappings.py"},
    {"piece": "Mapping manifests and team guide", "path": "mappings/<cluster>.json, mappings/README.md"},
]
TEMPLATE_STATUS = {
    "offline": ["script selftest 12 of 12", "reconcile selftest 13 of 13", "manifest validation", "syntax and structure checks"],
    "banner": ("Neither pipeline has been run in Azure DevOps yet. Treat the first runs on sandbox as a test of the pipelines "
               "themselves, not of the provider."),
    "pilot_unchanged": "The pilot pipeline is unchanged and still works.",
}

TEMPLATE_ACTIONS = [
    {"name": "Preflight", "does": "Read-only. Confirms the OIDC issuer and Workload Identity are enabled, lists foreign JFrog provider DaemonSets and any existing release, and samples nodes (one per nodepool) to confirm the Azure registry provider entry is present", "mutates": False, "destructive": False},
    {"name": "Install", "does": "One stage that fully configures and deploys the provider: Preflight, then Artifactory (mirror the plugin binary if missing; create this cluster's OIDC provider if missing), then install (namespace, annotated service account, node label, pinned Helm release, DaemonSet rollout, node entry check). Does not pull a test image; run Verify separately for that", "mutates": True, "destructive": False, "restarts_daemonset": True},
    {"name": "Verify", "does": "A pod with imagePullSecrets: [] on a provider node, with event and plugin-log checks. Reports INCONCLUSIVE (a warning, not a pass) when the image was already cached on the node", "mutates": True, "destructive": False},
    {"name": "ResetNodeEntry", "does": "Backs up and removes a stale provider entry on each labelled node (capped at 20 nodes by default), then restarts the DaemonSet", "mutates": True, "destructive": True, "restarts_daemonset": True},
    {"name": "Uninstall", "does": "Removes the Helm release only. Node entries and the node label are left behind. Untested", "mutates": True, "destructive": True, "untested": True},
]

TEMPLATE_INPUTS = [
    {"name": "ClusterName", "meaning": "Required.", "default": ""},
    {"name": "SubscriptionId", "meaning": "Required. The cluster's subscription ID is in the cluster inventory JSON in e3-argocd-maintenance (the console looks it up if that repo is readable).", "default": ""},
    {"name": "Environment", "meaning": "SBX, NPE or PRD. Picks the service connection and the Azure DevOps environment jfrog-kcp-<env>. Add an approval check to jfrog-kcp-prd; the console warns if it can see none.", "default": "SBX"},
    {"name": "MatchImages", "meaning": "Comma-separated registry hosts: dedicated subdomains, no scheme or path. Required for Install.", "default": ""},
    {"name": "BinaryMirrorRepo", "meaning": "The generic Artifactory repo that holds the plugin binary. Required for Install; no default, on purpose.", "default": ""},
    {"name": "ArtifactoryHost", "meaning": "Bare hostname. A value with https:// or a path is rejected.", "default": "example.jfrog.io"},
    {"name": "OidcProviderName", "meaning": "Blank means aks-<ClusterName>-oidc. An existing provider with a different issuer is refused, never reused.", "default": ""},
    {"name": "DefaultCacheDuration", "meaning": "Inside the 1 hour token lifetime. The pilot used 5h, which was never tested against token expiry.", "default": "30m"},
    {"name": "MaxUnavailable", "meaning": "DaemonSet rollout batch. The script warns if the chart does not apply it.", "default": "10%"},
    {"name": "Nodepools", "meaning": "Blank enables all nodes. A comma-separated list enables only those pools, for a canary.", "default": ""},
    {"name": "VerifySampleNodes", "meaning": "At least one node per nodepool.", "default": "3"},
    {"name": "TestImage", "meaning": "Used only by the separate Verify action, not by Install. The image for the secretless test; use one that no node has pulled.", "default": ""},
    {"name": "ResetStaleNodeEntries", "meaning": "Install resets sampled nodes whose entry is stale instead of failing.", "default": "false"},
    {"name": "ChartVersion", "meaning": "Pinned.", "default": "1.4.0"},
    {"name": "BinaryVersion", "meaning": "Pinned.", "default": "1.4.0"},
]

SECRET_VARIABLE = {
    "name": "JfrogDownloadToken",
    "text": ("The download token for the binary is a secret pipeline variable named JfrogDownloadToken, set at queue time "
             "(Variables → New variable → Keep this value secret). It is not a parameter, because parameters are visible in the "
             "resolved YAML and the run history."),
    "console": ["never puts it in parameters", "checks the pipeline definition has the variable marked secret",
                "tells the operator to set it at queue time, or passes it only as a secret variable through the ADO API"],
}

FIRST_RUN = [
    {"n": 1, "action": "Preflight", "DryRun": True, "text": "Run Preflight with DryRun true. Fix anything it reports."},
    {"n": 2, "action": "Install", "DryRun": True, "text": "Run Install with DryRun true. This validates the values and the Helm release server-side without applying them."},
    {"n": 3, "action": "Install", "DryRun": False, "text": "Run Install with DryRun false, MatchImages set, and Nodepools set to one pool if the cluster is large. Check the log's node entry check: every sampled node should be OK. This is the full configure-and-deploy stage; it does not pull a test image."},
    {"n": 4, "action": "Verify", "DryRun": False, "text": "Optionally, run Verify separately with a TestImage that is not on the node, then check Artifactory's audit log for the pull."},
    {"n": 5, "action": "Mappings", "DryRun": True, "text": "Ask for identity mappings through a pull request."},
]

MAPPING_RULES = {
    "text": ("Mappings are per cluster, because each cluster has its own OIDC provider, and they are managed as data. A team adds "
             "an entry to mappings/<cluster>.json and opens a pull request; jfrog-kcp-mappings applies it. The manifest format is "
             "JSON rather than YAML so the script needs no YAML parser."),
    "rules": ["exact namespace and service account names (no wildcards)", "no admin scope", "token lifetime at most 3600 seconds",
              "one mapping per namespace and service account", "the manifest's cluster must match the run"],
    "untested": ("The Access API calls for listing, updating and deleting mappings have not been exercised; only creation was, "
                 "during the pilot. Default to DryRun true and rehearse on sandbox first."),
    "prune": "Prune defaults to false: mappings that exist in Artifactory but not in the file are reported and left alone.",
}

LARGE_CLUSTERS = [
    "The provider installs per node and authorizes per service account, so node count and the number of distinct service accounts matter, not pod count.",
    "Each node restarts kubelet during setup. Use MaxUnavailable and Nodepools to roll in batches. Rollout time and kubelet restarts at large node counts have not been tested.",
    "Exchanges happen per node, per service account, per registry, per cache window, not per pod. Artifactory's rate limits for the OIDC endpoint are not known; ask the JFrog administrators before a large PRD rollout. The console estimates exchanges per hour as nodes × SAs × registries ÷ cache window.",
    "The node entry check samples nodes rather than probing every node. It reports how many labelled nodes exist and how many it checked.",
    "A new registry host means re-running Install, which restarts kubelet on each node. Wildcard matchImages patterns were considered and deferred until the fallback to imagePullSecrets is tested.",
]

TEMPLATE_GAPS = [
    "Not yet run in Azure DevOps; the YAML was checked structurally only.",
    "PRD Key Vault and token: the pilot only used the NPE service connection with kv-ci-secrets. Confirm what the prod connection should use.",
    "kubectl and kubelogin default to latest (matching the org's own admin templates); Helm defaults to 3.17.0 (the org's pin). Pin all three once a standard is chosen.",
    "The chart repository https://charts.jfrog.io was used in the pilot. Confirm it against the enterprise approved-sources list.",
    "Whether the chart applies updateStrategy is checked after install, not assumed.",
    "The tainted system nodepool is skipped by the DaemonSet (the chart sets no tolerations by default). The console lists nodes without the provider and says why.",
]

# --------------------------------------------------------------------------- §11 security, demo, rollback, status

SECURITY_REVIEW = {
    "intro": ("Run on 2026-10-20 against the new pipeline code using the organisation's example-secrets, example-cloud-and-containers and "
              "example-auth enterprise skills, before the first commit. It is a self-review, not a SecOps or EIP approval; treat "
              "every item as something to raise with EIP, not as cleared."),
    "rows": [
        {"id": "secrets-source", "area": "Secrets source", "finding": "The Artifactory token comes from Azure Key Vault (AzureKeyVault@2), not HashiCorp Vault, which the organisation's secrets standard names as the endorsed service. This matches the existing pilot pipeline's pattern (argocd-utility.yml); it was not introduced here", "status": "Flag for EIP: confirm Azure Key Vault is an accepted path for this secret, or migrate both pipelines to HashiCorp Vault", "prd_gate": False},
        {"id": "aad-preference", "area": "AAD preference", "finding": "Option B deliberately has no Azure AD app registration; the cluster's own OIDC issuer federates directly to Artifactory. the organisation's workforce IAM standard states a preference for AAD as the primary identity provider for federated auth. This is JFrog's own documented pattern and the one the pilot validated, not an oversight, but it is a real deviation worth a documented decision", "status": "Flag for EIP: get an explicit decision on file (Option B accepted, or move to Option A with an AAD app registration) rather than letting the deviation go unrecorded", "prd_gate": True},
        {"id": "sourcing", "area": "Image and chart sourcing", "finding": "The diagnostic/verify pods use mcr.microsoft.com/azurelinux/base/core:3.0 (Microsoft Container Registry), and the Helm chart comes from https://charts.jfrog.io directly. Neither is the organisation's approved artifact repository. The probe image matches what the pilot already used; the chart source was already an open item", "status": "Flag for EIP: mirror both through the organisation Artifactory, the same way the plugin binary already is. The console makes both overridable (DiagnosticImage, ChartRepo)", "prd_gate": True},
        {"id": "privileged-pods", "area": "Privileged pods", "finding": "The probe and node-reset pods run privileged: true with a hostPath mount of /, read-only except the guarded reset path. This is inherent to inspecting and fixing kubelet's on-disk config; the pilot found no less-privileged way to do it", "status": "Flag for EIP: confirm cluster admission policy restricts who can create privileged pods in kube-system, since this script does not and cannot enforce that from outside the cluster", "prd_gate": False},
        {"id": "network-policy", "area": "Network policy", "finding": "Per-pod network policy is required by the cloud standard. The probe, verify and reset pods have none", "status": "Open item: add a policy, or confirm the cluster's default-deny policy already covers kube-system short-lived pods", "prd_gate": False},
        {"id": "iac-scanning", "area": "IaC scanning", "finding": "The standard calls for scanning IaC templates before deployment. Only a structural lint (YAML indentation, tab characters, balanced template expressions) was run on these files, not a misconfiguration scanner", "status": "Open item: run the org's IaC scanner against .fdsecure/jfrog-kcp-cluster.yml and the templates before first real use", "prd_gate": False},
        {"id": "mapping-lifecycle", "area": "Mapping lifecycle", "finding": "Mappings are reviewed through a pull request, which gives an approval trail, and token lifetime is capped at 3600 seconds. There is no automatic detection of a mapping whose namespace or service account no longer exists in the cluster, and Prune is manual and defaults to false", "status": "Open item: consider a periodic check that flags mappings whose subject no longer exists. The console implements this as a read-only \"orphaned mappings\" check", "prd_gate": False},
        {"id": "least-privilege", "area": "Least privilege", "finding": "The created ServiceAccount carries no Kubernetes RBAC grants of its own; the chart's own rbac.create: true resources have not been audited for scope", "status": "Open item: review what the chart's Role/RoleBinding actually grants. The console renders the chart's RBAC objects from helm get manifest and lists every verb and resource", "prd_gate": False},
    ],
    "statuses": ["open", "raised with EIP", "decided"],
    "outro": ("None of this blocks the sandbox pilot, which already runs this pattern informally. It does mean the template must not "
              "be presented as pre-approved for other clusters, especially PRD, until EIP has seen the AAD-preference deviation and "
              "the artifact-sourcing items."),
    "prd_gate": "For Environment=PRD the console shows a blocking banner until the AAD-preference and sourcing rows are marked \"decided\".",
}

DEMO = {
    "audience": "Engineers and platform owners who know imagePullSecrets and want to see what replaces them.",
    "duration": "15 to 20 minutes",
    "how": ("Everything runs through the argocd-utility pipeline, because personal accounts cannot run kubectl against this "
            "cluster. Steps marked \"not yet tried\" have not been run and should be rehearsed first."),
    "rehearsal": [
        {"n": 1, "id": "audit", "required": True, "text": "Run the Artifactory audit-log check from \"Reading the logs\" and confirm the pull from 2026-10-16 is attributed to kcp-pull-user. Do not demo the claim \"no static account\" until this is done."},
        {"n": 2, "id": "kubelet", "required": True, "text": "Run CHECK_KUBELET_PROVIDER and confirm the footer shows one JFrog entry with a bare artifactory_url on every node that will run the pod."},
        {"n": 3, "id": "test-pod", "required": True, "text": "Run CREATE_TEST_POD once and confirm Running. Keep the output and screenshots as a fallback if the live run misbehaves. The console stores that run as the fallback evidence."},
        {"n": 4, "id": "fresh", "required": False, "text": "Decide whether the demo needs a fresh exchange or just a working secretless pull. For a fresh pull, promote a second tag into aks-sandbox-test-docker-local (Phase 3b) that no node has pulled, and plan to run CREATE_TEST_POD with TEST_POD_IMAGE set to it."},
        {"n": 5, "id": "no-restart", "required": False, "text": "Do not run INSTALL_JFROG_PROVIDER, RESET_NODE_JFROG_ENTRY or CHECK_JFROG_POD_CONFIG during the demo. All three restart the DaemonSet, and the setup step rewrites the node config and restarts kubelet. CHECK_JFROG_POD_CONFIG restarts it as a side effect even though it looks like a read-only check. In demo mode the console disables these three."},
    ],
    "gate": "Demo mode stays locked until rehearsal items 1 to 3 are done.",
    "run_of_show": [
        {"n": 1, "title": "The old way", "show": "The static approach this replaces: pull secrets such as docker-artifactory listed under imagePullSecrets in src/argocd/values.yaml", "how": "Open the file in the repo", "say": "Every pull secret is a stored credential that has to be created, rotated and kept out of logs"},
        {"n": 2, "title": "The trust chain", "show": "The three objects that tie a pod to a JFrog identity: the OIDC provider aks-sandbox-oidc, the identity mapping (sub: system:serviceaccount:jfrog:jfrog-provider-sa to kcp-pull-user), and the service account annotation JFrogExchange: \"true\"", "how": "Artifactory UI for the first two; the annotation from the saved CHECK_JFROG_POD_CONFIG output (do not rerun it live)", "say": "Identity comes from the cluster's own token, not from anything we store. The mapping is the only place that grants access"},
        {"n": 3, "title": "The wiring on the node", "show": "Two providers side by side in kubelet's config: acr-credential-provider (Microsoft's) and jfrog-credentials-provider, with matchImages set to the repo's host and a bare artifactory_url", "how": "Run CHECK_KUBELET_PROVIDER; show the QUICK SUMMARY footer", "say": "AKS already runs this mechanism for ACR. We register a second implementation for JFrog", "scenario": "CHECK_KUBELET_PROVIDER"},
        {"n": 4, "title": "The pod", "show": "serviceAccountName: jfrog-provider-sa and imagePullSecrets: []", "how": "Show the pod spec from the CREATE_TEST_POD section of argocd-utility.sh or the YAML in Phase 5", "say": "There is nothing for the pull to authenticate with except the pod's identity"},
        {"n": 5, "title": "The live pull", "show": "The pod reaches Running and the events show Successfully pulled image", "how": "Run CREATE_TEST_POD", "say": "The image came from a private repo with no stored secret", "scenario": "CREATE_TEST_POD"},
        {"n": 6, "title": "The evidence", "show": "The plugin's own log lines for the exchange, and Artifactory's audit log naming the mapped identity", "how": "The plugin log section of CHECK_KUBELET_PROVIDER; the Artifactory audit log", "say": "This is proof it was the exchange, not a cached or anonymous pull"},
        {"n": 7, "title": "The control", "show": "The same image pulled by a pod using the namespace's default service account fails", "how": "Needs a small change: CREATE_TEST_POD currently hardcodes jfrog-provider-sa. Add an override and rehearse. The console implements this as CREATE_CONTROL_POD (TEST_POD_SA override) and produces the argocd-utility.sh diff for review", "say": "Access is tied to the identity, not to the node: the same node refuses a pod without the mapped identity", "not_yet_tried": True, "scenario": "CREATE_CONTROL_POD"},
    ],
    "rerun_caveats": [
        "If the pod lands on a node that already has the image, the events say `already present on machine` and no registry call is made. That run proves nothing about authentication.",
        "Kubelet caches the plugin's result for defaultCacheDuration (5 hours in this setup). Within that window kubelet may reuse the cached token and not call the plugin again, so the plugin log can stay silent even though the pull works. Whether a kubelet restart clears this cache was not tested.",
        "A clean demonstration of a live exchange therefore needs an image no node has pulled and a token that is not cached. Either wait out the cache, or temporarily lower defaultCacheDuration (AZURE.md's example is 5 minutes) and reinstall before the demo, not during it.",
    ],
    "faq": [
        {"q": "Where is the credential?", "a": "There is no stored one. The plugin sends the pod's projected ServiceAccount token to Artifactory and receives a short-lived token back (the mapping sets expires_in: 3600).", "confidence": "Observed in the log: the plugin obtaining the ServiceAccount token and calling the OIDC endpoint"},
        {"q": "Does this need an Azure AD app?", "a": "No. This is Option B in AZURE.md.", "confidence": "Observed: no app registration was created"},
        {"q": "What if the node is replaced?", "a": "The DaemonSet writes the entry on the new node.", "confidence": "Not tested: a fresh-node run has not been done; only a migration of an existing entry has"},
        {"q": "What happens if the identity mapping is wrong?", "a": "The exchange should be refused and the pull fails.", "confidence": "Not observed in this pilot, because the failure was earlier in the chain"},
        {"q": "Is the plugin binary trusted?", "a": "It is the upstream JFrog binary, version 1.4.0, mirrored into our own Artifactory. The console computes the mirrored artifact's sha256 via the storage API and compares it with the upstream release's published checksum when one is reachable; otherwise it records \"unverified\".", "confidence": "No signature or checksum verification was performed in the pilot"},
        {"q": "Does this apply to every node?", "a": "Only to nodes labelled credentialsProviderEnabled=true, because the values file restricts the DaemonSet with node affinity.", "confidence": "Observed: DaemonSet 2 of 2 on the labelled nodes"},
    ],
    "after": "Delete the demo pod through the pipeline, and note anything that went differently from this script so the script can be corrected. The console prompts for these notes and appends them to the run report.",
    "blocked_scenarios": ["INSTALL_JFROG_PROVIDER", "RESET_NODE_JFROG_ENTRY", "CHECK_JFROG_POD_CONFIG"],
}

ROLLBACK = [
    {"id": "test-pod", "title": "Test pod", "command": "kubectl delete pod secretless-pull-test -n jfrog", "scenario": "DELETE_TEST_POD", "params": {"TEST_POD_NAME": "secretless-pull-test"},
     "text": "Through a pipeline scenario, since personal accounts cannot delete."},
    {"id": "stale-pod", "title": "Stale pilot-test-pod", "command": "kubectl delete pod pilot-test-pod -n jfrog", "scenario": "DELETE_TEST_POD", "params": {"TEST_POD_NAME": "pilot-test-pod"},
     "text": "Delete it the same way."},
    {"id": "provider", "title": "Provider", "command": "helm uninstall jfrog-cred-provider -n jfrog", "scenario": "UNINSTALL_JFROG_PROVIDER",
     "text": "Whether uninstall removes the JFrog entry from each node's credential-provider-config.yaml was not tested. Confirm with CHECK_KUBELET_PROVIDER afterwards, and use the backup file or RESET_NODE_JFROG_ENTRY if an entry remains. The console runs that check automatically after an uninstall.", "untested": True, "follow_up": "CHECK_KUBELET_PROVIDER"},
    {"id": "node-label", "title": "Node label", "command": "kubectl label nodes --all credentialsProviderEnabled-", "scenario": "REMOVE_NODE_LABEL",
     "text": "credentialsProviderEnabled=true is added to all nodes by INSTALL_JFROG_PROVIDER and is not removed by uninstall."},
    {"id": "artifactory", "title": "Artifactory", "command": "DELETE /access/api/v1/oidc/aks-sandbox-oidc (and its identity mapping)", "jfrog_write": True,
     "text": "The OIDC provider aks-sandbox-oidc and its identity mapping can be removed through the Access API if the pilot is abandoned. Do not touch aks-other-cluster-oidc: the console hard-blocks any delete of a protected provider."},
]

WHERE_THINGS_STAND = ("The provider works end to end on sandbox: the OIDC exchange succeeds, the pod pulls from the scoped repo with no "
                      "imagePullSecret, and it runs.")

OPEN_ITEMS = [
    {"id": "attribution", "title": "Confirm attribution in Artifactory", "text": "Check the audit log for the pull and confirm it is attributed to kcp-pull-user rather than a static account. This is the last piece of the original goal.", "check": "audit_attribution"},
    {"id": "fresh-node", "title": "Test a fresh node", "text": "The fix migrated an existing entry. Replace or add a node and confirm the DaemonSet writes a correct entry from scratch and kubelet loads it. The console's \"fresh node\" check detects nodes younger than the last install and runs the three-layer diff on them.", "check": "fresh_node"},
    {"id": "cache-duration", "title": "Decide whether to keep defaultCacheDuration: 5h", "text": "AZURE.md's example is 5 minutes. Longer means fewer exchanges and longer exposure of a cached token. The console shows that trade-off."},
    {"id": "upgrade", "title": "Upgrade behaviour", "text": "Any future change to the spelling of artifactoryUrl needs the node entry reset. The console's three-layer diff is the automatic check that compares the release value with each node's entry; run it after every install.", "check": "three_layers"},
    {"id": "download-token", "title": "JFROG_DOWNLOAD_TOKEN is the admin-scoped Artifactory token", "text": "It was a stopgap after hitting a permission wall creating a scoped read-only group: FORBIDDEN on /access/api/v2/groups, because the identity behind $ARTIFACTORY_ADMIN_TOKEN has Artifactory-admin but not Access-service group-management rights. Tighten it once someone with those rights can create the group, or make the mirror repo anonymous-read and drop the token. Also rotate $ARTIFACTORY_ADMIN_TOKEN: it was typed into several pipeline variable fields during debugging, and masking in logs was not confirmed for all of them. The console shows a token-scope check and scans stored run logs for unmasked token patterns (any hit is critical).", "check": "token_scope"},
    {"id": "stale-pod", "title": "The stale pilot-test-pod in jfrog still needs deleting", "text": "Offered on the Rollback tab.", "check": None},
    {"id": "forbidden-user", "title": "Loose thread, not root-caused", "text": "One CREATE_TEST_POD run once failed with Forbidden attributed to a human identity (operator@example.com) rather than the service principal. Watch for it recurring: it would mean that pipeline stage is not authenticating the way we assume. The console parses Forbidden lines in every pipeline log and alerts when the identity is a user principal rather than the service principal.", "check": "forbidden_principal"},
    {"id": "docker-remote", "title": "Docker Hub remote repo", "text": "example-docker-remote.jfrog.io is dormant. If anyone needs it, an Artifactory admin has to reactivate it.", "check": None},
]

TESTING_PERFORMED = [
    "Node config inspected on vmss00000e and vmss00000g: provider entries, matchImages, artifactory_url, kubelet start time, file modification time.",
    "Rendered release inspected (helm get values -a, helm get manifest, chart 1.4.0, revision 15).",
    "Plugin log file read directly; both the doubled URL and the later single-scheme URL were observed.",
    "Service account annotation JFrogExchange: \"true\" observed.",
    "Pod secretless-pull-test pulled the image and reached Running with imagePullSecrets: [].",
]
TESTING_NOT_PERFORMED = [
    {"id": "audit", "text": "Artifactory audit log check for the identity used on the pull."},
    {"id": "fresh_node", "text": "Fresh-node provisioning test."},
    {"id": "cache_expiry", "text": "A pull after the 5 h token cache expires, and a forced re-exchange with an uncached image."},
    {"id": "rollback", "text": "Rollback from the .bak-before-jfrog-reset file; helm uninstall cleanup behaviour."},
    {"id": "promote_body", "text": "Verification of the Promote request body against current JFrog documentation (shown as the documented shape)."},
    {"id": "security_review", "text": "Any security review. The runbook records a sandbox pilot only and states nothing about review or approval status, which is unknown."},
]


def all_scenarios() -> list:
    return PILOT_SCENARIOS + CONSOLE_SCENARIOS


def scenario(name: str):
    return next((s for s in all_scenarios() if s["name"] == name), None)


def as_dict() -> dict:
    """Everything the UI renders, in one payload."""
    return {k: v for k, v in globals().items() if k.isupper() and not k.startswith("_")}
