#!/usr/bin/env bash
# Install a pinned Terraform, verified against HashiCorp's SHA256SUMS.
set -euo pipefail
version=${TERRAFORM_VERSION:?}
dest="${AGENT_TOOLSDIRECTORY:-$HOME/.tools}/terraform/${version}"
if [ ! -x "${dest}/terraform" ]; then
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  zip="terraform_${version}_linux_amd64.zip"
  curl -fsSL -o "${work}/${zip}" "https://releases.hashicorp.com/terraform/${version}/${zip}"
  curl -fsSL -o "${work}/sums" "https://releases.hashicorp.com/terraform/${version}/terraform_${version}_SHA256SUMS"
  (cd "$work" && grep " ${zip}\$" sums | sha256sum --check --strict -)
  mkdir -p "$dest"
  unzip -q -o "${work}/${zip}" terraform -d "$dest"
fi
echo "##vso[task.prependpath]${dest}"
"${dest}/terraform" version
