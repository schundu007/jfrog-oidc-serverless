# Building the console with Claude Code

## One-time setup

```bash
unzip jfrog-oidc-serverless.zip && cd jfrog-oidc-serverless   # or git clone
python3 --version            # 3.11+
terraform version            # 1.6+
gcloud auth application-default login && gcloud config set project <nonprod-project>
az login && az account set --subscription <nonprod-subscription>
export JFROG_ACCESS_TOKEN=$(jf eot <provider> <oidc-token> ...)  # or a short-lived admin token for non-prod
claude
```

Optional: review `docs/claude-settings.example.json` and, if it suits you, copy
it to `.claude/settings.json`. It auto-allows tests, `terraform fmt`/`validate`
and read-only git. It asks before plan/apply or any gcloud/az change, and denies
commands that would print secret values.

## First prompt

Paste this into Claude Code:

> Read CLAUDE.md and docs/console-spec.md. Then run the existing client and rotator
> tests to confirm a green baseline, run `terraform init -backend=false` and
> `terraform validate` on terraform/stacks/all-targets, and fix whatever the real
> providers reject in the modules (keep behaviour the same; note each fix in
> docs/progress.md). After that, start `/milestone M0`.

## Then, one milestone per session

```
/milestone M1     # simulation engine, headless, all scenarios green
/sim all          # look at verdicts and causal chains
/milestone M2     # server + Simulate tab (open the printed URL in a browser)
/milestone M3     # preflight + discover; then:
/verify-target orders-nonprod
/milestone M4     # terraform plan/apply + wire, behind approval gates
/milestone M5     # first rotation, live probes, ready report
/milestone M6     # hardening
```

Clear the context between milestones (`/clear`). The spec and `docs/progress.md`
carry the state from one session to the next.

## Before using it on production

- Run M5 end to end on non-prod for one GCP function and one Container App.
- Run `python -m console.sim run .console/configs/<prod-config>.json` and confirm
  the verdict is `pass` with your real TTLs and cron.
- Point `state_backend` at your GCS or azurerm backend; don't use local state.
