"""python -m setup_console.oidc status|down|up [--cloud gcp|azure] [--only STACK ...]

The JFrog OIDC stacks (terraform/stacks/oidc/<cloud>/<stack>), on their own: each cloud's pipelines'
JFrog trust and aks-jfrog-trust (the AKS cluster's issuer and kubelet credential provider). Separate
from infrastructure (python -m setup_console.costs): OIDC stands on infrastructure, reads its outputs
and is brought up after it and taken down before it; infrastructure never runs any of this.

up    deploys each from its saved inputs, in catalogue order; one whose infrastructure is not
      deployed is skipped (bring it up with costs up first).
down  destroys each, in reverse order (already deleted is fine).

Every change goes through the Setup console's own gates (Jobs, setup_console/runner.py). Stacks with
a stack-setup pipeline (aks-jfrog-trust) run in Azure DevOps; the others run here with the operator's
short-lived JFrog token.
"""

from __future__ import annotations

import argparse
import sys

from . import stacks
from .runner import RunError, Runner

CLOUD = {"gcp": "Google Cloud", "azure": "Azure"}
OIDC = tuple(s["id"] for s in stacks.CATALOGUE if s["kind"] == "JFrog OIDC")


class OidcError(RunError):
    pass


class OidcRunner(Runner):
    error = OidcError

    def up(self, sid) -> str:
        gaps = stacks.missing(sid, self.env, self.root, self.state_root)
        if gaps:
            raise OidcError(f"{sid}: inputs not set: {', '.join(gaps)} (fill them in JFrog OIDC)")
        base = stacks.get(sid).get("after")
        if base and not self.deployed(base):
            return f"skipped: {base} is not deployed (python -m setup_console.costs up first)"
        return self.deploy(sid)

    def down(self, sid) -> str:
        return self.destroy(sid)


def select(cloud=None, only=None) -> list:
    picked = [s for s in OIDC if not only or s in only]
    return [s for s in picked if not cloud or stacks.get(s)["group"] == CLOUD[cloud]]


def run_all(runner: OidcRunner, action: str, picked: list) -> dict:
    out = {}
    for sid in (reversed(picked) if action == "down" else picked):   # one at a time: they share ADO trust
        try:
            if action == "status":
                res = "deployed" if runner.deployed(sid) else "not deployed"
            else:
                res = runner.down(sid) if action == "down" else runner.up(sid)
        except Exception as exc:   # one stack failing never stops the others
            res = f"failed: {exc}"
        runner.say(sid, res)
        out[sid] = res
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m setup_console.oidc", description=__doc__.split("\n\n")[0])
    p.add_argument("action", choices=("status", "down", "up"))
    p.add_argument("--cloud", choices=tuple(CLOUD))
    p.add_argument("--only", nargs="+", choices=OIDC, metavar="STACK")
    args = p.parse_args(argv)

    from console import deliver
    from console.shell import Shell
    from kcp_console.jfrog import OperatorToken   # the operator's short-lived JFrog token, as the portal gets it
    from .ado import Ado
    from .jobs import Jobs
    delivery = deliver.load_settings(stacks.REPO_ROOT / "pipelines" / "delivery.json")
    env = delivery.get("environment") or "test"
    a = delivery["ado"]
    ado = Ado(Shell(mode="live", log=lambda line: None), a["organization"], a["project"],
              branch=a.get("target_branch") or "main", repo=a.get("repository") or "")
    runner = OidcRunner(Jobs(env_name=env, ado=ado, token_source=OperatorToken().get), ado, env)
    picked = select(args.cloud, args.only)
    results = run_all(runner, args.action, picked)
    print()
    for sid in picked:
        print(f"{sid:<28} {results.get(sid)}")
    return 1 if any(str(r).startswith("failed") for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
