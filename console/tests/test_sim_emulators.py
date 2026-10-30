"""Emulator fidelity (spec §7.2): the behaviours that decide simulation outcomes."""

import base64
import json
import unittest
import urllib.error
import urllib.parse
import urllib.request

from console.sim.clock import VirtualClock
from console.sim.emulators import jwt
from console.sim.emulators.azure import AzureEmulator
from console.sim.emulators.gcp import GcpEmulator
from console.sim.emulators.jfrog import JFrogEmulator
from console.sim.emulators.server import EmulatorServer

T0 = 1_767_225_600.0  # 2026-01-01T00:00:00Z
GOOGLE = "https://accounts.google.com"
TENANT = "tenant-0000"
V2 = f"https://login.microsoftonline.com/{TENANT}/v2.0"
V1 = f"https://sts.windows.net/{TENANT}/"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with _opener.open(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()


def j(raw):
    return json.loads(raw)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock(T0)
        self.gcp = GcpEmulator(self.clock, key=b"g")
        self.azure = AzureEmulator(self.clock, key=b"a", tenant_id=TENANT)
        self.jfrog = JFrogEmulator(self.clock, trust={GOOGLE: b"g", V2: b"a", V1: b"a"})
        self.srv = EmulatorServer({"/jfrog": self.jfrog, "/gcp": self.gcp, "/azure": self.azure})
        self.base = self.srv.start()
        self.addCleanup(self.srv.stop)
        self.jfrog.add_integration("gcp-cloudrun", issuer=GOOGLE, audience="jfrog-gcp")
        self.jfrog.add_integration("azure-entra", issuer=V2, audience="app-1")
        self.jfrog.grant("serverless-readers", ["generic-local", "docker-virtual"])

    def gcp_identity(self, workload="runtime", audience="jfrog-gcp", fmt="full"):
        q = urllib.parse.urlencode({"audience": audience, "format": fmt})
        st, raw = call(f"{self.base}/gcp/md/{workload}/computeMetadata/v1/instance/service-accounts/default/identity?{q}",
                       headers={"Metadata-Flavor": "Google"})
        return st, raw.decode()

    def exchange(self, subject, provider="gcp-cloudrun"):
        st, raw = call(f"{self.base}/jfrog/access/api/v1/oidc/token", "POST", body={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
            "subject_token": subject, "provider_name": provider})
        return st, j(raw)


class JwtTests(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        tok = jwt.encode({"iss": "x", "sub": "1"}, b"k")
        self.assertEqual(jwt.decode(tok, b"k")["sub"], "1")
        with self.assertRaises(jwt.JwtError):
            jwt.decode(tok, b"other")
        self.assertEqual(jwt.claims(tok)["iss"], "x")


class GcpTests(Base):
    def test_identity_email_only_with_full_format(self):
        self.gcp.add_account("runtime", "run@p.iam.gserviceaccount.com", "111")
        st, tok = self.gcp_identity(fmt="full")
        self.assertEqual(st, 200)
        c = jwt.claims(tok)
        self.assertEqual((c["iss"], c["aud"], c["sub"], c["email"]), (GOOGLE, "jfrog-gcp", "111", "run@p.iam.gserviceaccount.com"))
        self.assertEqual(c["exp"], T0 + 3600)
        st, tok = self.gcp_identity(fmt="standard")
        self.assertNotIn("email", jwt.claims(tok))

    def test_metadata_flavor_required(self):
        self.gcp.add_account("runtime", "e", "111")
        st, _ = call(f"{self.base}/gcp/md/runtime/computeMetadata/v1/instance/service-accounts/default/identity?audience=a")
        self.assertEqual(st, 403)

    def test_identity_down(self):
        self.gcp.add_account("rotator", "e", "222")
        self.gcp.set_identity_down("rotator", True)
        self.assertEqual(self.gcp_identity("rotator")[0], 503)

    def test_secret_versions_and_ar_update_mask(self):
        self.gcp.add_account("rotator", "e", "222")
        self.gcp.add_secret("jfrog-upstream", "bootstrap-placeholder")
        self.gcp.add_remote_repo("us-central1", "jfrog-docker", "svc", "projects/123456789012/secrets/jfrog-upstream/versions/1")
        st, raw = call(f"{self.base}/gcp/md/rotator/computeMetadata/v1/instance/service-accounts/default/token",
                       headers={"Metadata-Flavor": "Google"})
        bearer = {"Authorization": f"Bearer {j(raw)['access_token']}"}
        sm = f"{self.base}/gcp/sm/v1/projects/sim-project/secrets/jfrog-upstream"
        self.assertEqual(call(f"{sm}:addVersion", "POST", body={"payload": {"data": "eA=="}})[0], 401)
        st, raw = call(f"{sm}:addVersion", "POST", bearer, {"payload": {"data": base64.b64encode(b"tok").decode()}})
        self.assertEqual(j(raw)["name"], "projects/123456789012/secrets/jfrog-upstream/versions/2")
        st, raw = call(f"{self.base}/gcp/sm/v1/projects/123456789012/secrets/jfrog-upstream/versions/1:disable", "POST", bearer, {})
        self.assertEqual(j(raw)["state"], "DISABLED")
        st, raw = call(f"{sm}/versions?filter=state:ENABLED", headers=bearer)
        self.assertEqual([v["name"].rsplit("/", 1)[1] for v in j(raw)["versions"]], ["2"])

        repo = f"{self.base}/gcp/ar/v1/projects/sim-project/locations/us-central1/repositories/jfrog-docker"
        mask = ("remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.username,"
                "remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.passwordSecretVersion")
        body = {"remoteRepositoryConfig": {"upstreamCredentials": {"usernamePasswordCredentials": {
            "username": "new-user", "passwordSecretVersion": "projects/123456789012/secrets/jfrog-upstream/versions/2"}}}}
        st, _ = call(f"{repo}?updateMask=remoteRepositoryConfig.bogus", "PATCH", bearer, body)
        self.assertEqual(st, 400)
        # Only fields in the mask change.
        call(f"{repo}?updateMask=remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.passwordSecretVersion",
             "PATCH", bearer, body)
        creds = self.gcp.repo("jfrog-docker")
        self.assertEqual(creds["username"], "svc")
        self.assertTrue(creds["passwordSecretVersion"].endswith("/versions/2"))
        call(f"{repo}?updateMask={mask}", "PATCH", bearer, body)
        self.assertEqual(self.gcp.repo("jfrog-docker")["username"], "new-user")
        self.assertEqual(self.gcp.version("projects/123456789012/secrets/jfrog-upstream/versions/2")["data"], b"tok")


class AzureTests(Base):
    def mi(self, workload="runtime", resource="api://app-1", header="h1", client_id="c1"):
        q = urllib.parse.urlencode({"resource": resource, "client_id": client_id, "api-version": "2019-08-01"})
        return call(f"{self.base}/azure/mi/{workload}?{q}", headers={"X-IDENTITY-HEADER": header})

    def setUp(self):
        super().setUp()
        self.azure.add_workload("runtime", header="h1", identities={"c1": "p1"})
        self.azure.set_app("app-1", version=2)

    def test_v2_token(self):
        st, raw = self.mi()
        c = jwt.claims(j(raw)["access_token"])
        self.assertEqual((c["iss"], c["aud"], c["oid"], c["tid"], c["ver"]), (V2, "app-1", "p1", TENANT, "2.0"))

    def test_v1_token(self):
        self.azure.set_app("app-1", version=1)
        c = jwt.claims(j(self.mi()[1])["access_token"])
        self.assertEqual((c["iss"], c["aud"], c["ver"]), (V1, "api://app-1", "1.0"))

    def test_header_and_client_id(self):
        self.assertEqual(self.mi(header="wrong")[0], 401)
        self.assertEqual(self.mi(client_id="not-attached")[0], 400)

    def test_app_role_gate(self):
        self.azure.set_app("app-1", version=2, role_gate=True, assigned=set())
        st, raw = self.mi()
        self.assertEqual(st, 400)
        self.assertIn("AADSTS501051", raw.decode())

    def test_key_vault_versions_and_arm_refresh(self):
        self.azure.add_secret("kv-sim", "jfrog-registry-token", "bootstrap-placeholder")
        kv = j(self.mi(resource="https://vault.azure.net")[1])["access_token"]
        url = f"{self.base}/azure/kv/kv-sim/secrets/jfrog-registry-token?api-version=7.4"
        st, raw = call(url, "PUT", {"Authorization": f"Bearer {kv}"}, {"value": "t2", "attributes": {"enabled": True, "exp": int(T0 + 100)}})
        self.assertEqual(st, 200)
        self.assertNotIn("t2", raw.decode())  # never echo the value
        latest = self.azure.latest("kv-sim", "jfrog-registry-token")
        self.assertEqual((latest["value"], latest["exp"], latest["label"]), ("t2", T0 + 100, "v2"))

        arm = j(self.mi(resource="https://management.azure.com/")[1])["access_token"]
        app = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/f"
        refresh = f"{self.base}/azure/arm{app}/config/configreferences/appsettings/refresh?api-version=2022-03-01"
        self.assertEqual(call(refresh, "POST", {"Authorization": f"Bearer {arm}"}, {})[0], 200)
        self.assertEqual(self.azure.refreshes[-1]["app"], app)
        self.azure.refresh_forbidden = True
        self.assertEqual(call(refresh, "POST", {"Authorization": f"Bearer {arm}"}, {})[0], 403)


class JFrogTests(Base):
    def setUp(self):
        super().setUp()
        self.gcp.add_account("runtime", "run@p", "111")
        self.gcp.add_account("rotator", "rot@p", "222")
        self.jfrog.add_mapping("t-runtime", "gcp-cloudrun", {"sub": "111"}, priority=10, username="svc-run",
                               expires_in=3600, groups=["serverless-readers"])
        self.jfrog.add_mapping("t-pull", "gcp-cloudrun", {"sub": "222"}, priority=20, username="svc-pull",
                               expires_in=14400, groups=["serverless-readers"])

    def test_exchange_mints_jwt_with_expiry(self):
        st, body = self.exchange(self.gcp_identity()[1])
        self.assertEqual(st, 200)
        self.assertEqual(body["username"], "svc-run")
        c = jwt.claims(body["access_token"])
        self.assertEqual(c["sub"], "jfac@sim/users/svc-run")
        self.assertEqual(c["exp"], T0 + 3600)

    def test_lowest_priority_number_wins(self):
        self.jfrog.add_mapping("broad", "gcp-cloudrun", {}, priority=1, username="svc-broad", expires_in=600, groups=[])
        self.assertEqual(self.exchange(self.gcp_identity()[1])[1]["username"], "svc-broad")

    def test_no_match_is_401(self):
        self.gcp.add_account("other", "o@p", "999")
        st, body = self.exchange(self.gcp_identity("other")[1])
        self.assertEqual(st, 401)
        self.assertIn("no identity mapping matched", json.dumps(body))

    def test_issuer_and_audience_checked(self):
        st, body = self.exchange(self.gcp_identity(audience="jfrog-gpc")[1])
        self.assertEqual(st, 401)
        self.assertIn("audience mismatch", json.dumps(body))
        self.azure.add_workload("runtime", header="h", identities={"c": "p"})
        self.azure.set_app("app-1", version=1)
        q = urllib.parse.urlencode({"resource": "api://app-1", "client_id": "c"})
        tok = j(call(f"{self.base}/azure/mi/runtime?{q}", headers={"X-IDENTITY-HEADER": "h"})[1])["access_token"]
        st, body = self.exchange(tok, "azure-entra")
        self.assertEqual(st, 401)
        self.assertIn("issuer mismatch", json.dumps(body))
        self.assertIn("sts.windows.net", json.dumps(body))

    def test_expired_subject_rejected(self):
        tok = self.gcp_identity()[1]
        self.clock.advance_to(T0 + 3601)
        self.assertIn("expired", json.dumps(self.exchange(tok)[1]))

    def test_unknown_provider(self):
        self.assertEqual(self.exchange(self.gcp_identity()[1], "nope")[0], 401)

    def test_revoked_mapping(self):
        self.jfrog.revoke_mapping("t-runtime")
        self.assertEqual(self.exchange(self.gcp_identity()[1])[0], 401)

    def test_artifact_reads_and_scope(self):
        access = self.exchange(self.gcp_identity()[1])[1]["access_token"]
        bearer = {"Authorization": f"Bearer {access}"}
        self.assertEqual(call(f"{self.base}/jfrog/artifactory/api/storage/generic-local/healthcheck", headers=bearer)[0], 200)
        self.assertEqual(call(f"{self.base}/jfrog/artifactory/generic-local/healthcheck/ping.txt", headers=bearer)[0], 200)
        self.assertEqual(call(f"{self.base}/jfrog/artifactory/secret-local/x", headers=bearer)[0], 403)
        self.assertEqual(call(f"{self.base}/jfrog/artifactory/generic-local/x")[0], 401)
        self.clock.advance_to(T0 + 3601)
        self.assertEqual(call(f"{self.base}/jfrog/artifactory/generic-local/x", headers=bearer)[0], 401)

    def test_registry_basic_auth(self):
        self.gcp_identity("rotator")
        access = self.exchange(self.gcp_identity("rotator")[1])[1]["access_token"]
        url = f"{self.base}/jfrog/v2/docker-virtual/app/manifests/1.0"

        def basic(u, p):
            return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}

        self.assertEqual(call(url, headers=basic("svc-pull", access))[0], 200)
        self.assertEqual(call(url, headers=basic("someone-else", access))[0], 401)
        self.assertEqual(call(url, headers=basic("svc-pull", "bootstrap-placeholder"))[0], 401)
        self.clock.advance_to(T0 + 14401)
        self.assertEqual(call(url, headers=basic("svc-pull", access))[0], 401)

    def test_outage_per_service(self):
        self.jfrog.set_down({"access", "registry"})
        self.assertEqual(self.exchange(self.gcp_identity()[1])[0], 503)
        self.assertEqual(call(f"{self.base}/jfrog/v2/")[0], 503)
        self.jfrog.set_down(set())
        self.assertEqual(self.exchange(self.gcp_identity()[1])[0], 200)


if __name__ == "__main__":
    unittest.main()
