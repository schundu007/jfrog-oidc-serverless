"""Emulated Kubernetes projected service-account tokens (Knative services).

The kubelet writes the token to a file in the pod and refreshes it when 80%
of its lifetime has passed (or after 24 h); the workload's FileTokenSource
reads the file on every exchange. This emulator plays the kubelet: it writes
an HS256 JWT (the JFrog emulator trusts the key) with the cluster issuer, the
projected audience and sub system:serviceaccount:<ns>:<sa>.
"""

from __future__ import annotations

import os
import secrets

from . import jwt

ISSUER = "https://oidc.sim-cluster.example.com"
REFRESH_AT = 0.8


class K8sEmulator:
    def __init__(self, clock, key: bytes, path: str, issuer: str = ISSUER):
        self.clock = clock
        self.key = key
        self.path = path
        self.issuer = issuer
        self.namespace = ""
        self.service_account = ""
        self.audience = ""
        self.seconds = 3600
        self.uid = secrets.token_hex(8)
        self._written = None
        self.refreshes = 0

    def project(self, namespace: str, service_account: str, audience: str, seconds: int) -> None:
        self.namespace, self.service_account, self.audience, self.seconds = namespace, service_account, audience, seconds
        self._written = None

    def recreate_service_account(self) -> None:
        """Same name, new UID: the sub (system:serviceaccount:<ns>:<sa>) does not change."""
        self.uid = secrets.token_hex(8)
        self._written = None

    def tick(self) -> None:
        """Refresh the token file the way the kubelet does."""
        now = self.clock.now()
        if self._written is not None and now - self._written < REFRESH_AT * self.seconds:
            return
        sub = f"system:serviceaccount:{self.namespace}:{self.service_account}"
        claims = {"iss": self.issuer, "sub": sub, "aud": [self.audience], "iat": int(now), "nbf": int(now),
                  "exp": int(now) + self.seconds,
                  "kubernetes.io": {"namespace": self.namespace,
                                    "serviceaccount": {"name": self.service_account, "uid": self.uid}}}
        tmp = f"{self.path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(jwt.encode(claims, self.key))
        os.replace(tmp, self.path)
        self._written = now
        self.refreshes += 1
