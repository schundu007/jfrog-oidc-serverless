"""Job (Cloud Run or Container Apps): read a JFrog artifact with a short-lived OIDC token.

The job's Google identity token is exchanged for a JFrog access token
in-process; nothing is configured beyond the env vars in the client docstring
and JFROG_ARTIFACT_PATH (<repo>/<path>). The token itself is never printed.
"""

import os
import sys

from jfrog_oidc import JFrogOidcError, JFrogTokenProvider, download


def main() -> int:
    artifact = os.environ.get("JFROG_ARTIFACT_PATH", "")
    if not artifact:
        print("FAILED: JFROG_ARTIFACT_PATH is not set (<repo>/<path>)", file=sys.stderr)
        return 1
    try:
        jfrog = JFrogTokenProvider.from_env()
        token = jfrog.token()
        print(f"exchanged identity for a JFrog token valid {int(token.remaining())}s")
        path = download(jfrog, artifact, "/tmp/artifact")
    except JFrogOidcError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8", errors="replace") as f:
        print(f"read {artifact}: {f.read(500)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
