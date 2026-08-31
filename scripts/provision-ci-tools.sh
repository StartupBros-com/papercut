#!/usr/bin/env bash
# sha256-pinned CI tool provisioning (token-eater's stanza, trimmed to what
# papercut's release-policy smoke actually needs: jq and yq; git ships on the
# runner image).
set -euo pipefail

bin_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/hov-ci-bin"
mkdir -p "$bin_dir"

install_asset() {
  local command_name="$1" url="$2" sha="$3"
  local target="$bin_dir/$command_name"
  curl --fail --location --silent --show-error "$url" --output "$target"
  printf '%s  %s\n' "$sha" "$target" | sha256sum --check --status
  chmod +x "$target"
}

install_asset jq \
  'https://github.com/jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64' \
  '020468de7539ce70ef1bceaf7cde2e8c4f2ca6c3afb84642aabc5c97d9fc2a0d'
install_asset yq \
  'https://github.com/mikefarah/yq/releases/download/v4.50.1/yq_linux_amd64' \
  'c7a1278e6bbc4924f41b56db838086c39d13ee25dcb22089e7fbf16ac901f0d4'

printf '%s\n' "$bin_dir" >> "${GITHUB_PATH:?GITHUB_PATH is required}"
