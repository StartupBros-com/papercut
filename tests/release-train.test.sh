#!/usr/bin/env bash
# Release-policy smoke for papercut's announce-only train.
#
# Ported from token-eater's release-train.test.sh, scoped to what THIS train
# claims: papercut has no promote/marketplace-mutation machinery, so the
# surface under test is (1) the workflow's declared policy, (2) the verify
# job's ancestry rule, exercised for real in a throwaway repo, and (3) a
# mutation probe proving the policy check can fail.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

HARDENED_SHA='08f7d22f3a5b59b1658ab2e96a20d0d3c352869c'
RETIRED_SHA='c981b872ebf650805200ad72c8b7142232f8b3f6'
ANNOUNCE_WORKFLOW='StartupBros-com/hov-marketplace/.github/workflows/hov-tool-drop-announce.yml'
ANNOUNCE_IF="github.event.release.draft == false && github.event.release.prerelease == false && needs.verify.result == 'success'"
WF="$ROOT/.github/workflows/release-train.yml"

pass() { printf 'ok - %s\n' "$1"; }
fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }

command -v yq >/dev/null || fail "yq is required (policy checks parse the workflow)"
command -v jq >/dev/null || fail "jq is required"

validate_release_policy() {
  local workflow="$1" json
  if grep -Eq 'TOOL_RELEASE_ANNOUNCE_(SECRET|URL)|ANNOUNCE_(SECRET|URL)|x-tool-release-announce-secret|/api/internal/ops/tool-releases|(^|[[:space:]])curl([[:space:]]|$)' "$workflow"; then
    printf 'direct Tool Drop delivery surface is forbidden\n' >&2
    return 1
  fi
  json="$(yq -o=json '.' "$workflow")" || return 1
  jq -e '.on.release.types == ["published", "edited"]' <<<"$json" >/dev/null || {
    printf 'release events must be exactly published and edited\n' >&2
    return 1
  }
  jq -e --arg u "$ANNOUNCE_WORKFLOW@$HARDENED_SHA" \
    '.jobs.announce.uses == $u' <<<"$json" >/dev/null || {
    printf 'announce must use the hardened shared workflow pin\n' >&2
    return 1
  }
  jq -e --arg i "$ANNOUNCE_IF" '.jobs.announce.if == $i' <<<"$json" >/dev/null || {
    printf 'announce must be gated on non-draft, non-prerelease, green verify\n' >&2
    return 1
  }
  jq -e '.jobs.announce.permissions == {"contents": "read", "id-token": "write"}' <<<"$json" >/dev/null || {
    printf 'announce permissions must be exactly contents:read id-token:write\n' >&2
    return 1
  }
  jq -e '.concurrency["cancel-in-progress"] == false' <<<"$json" >/dev/null || {
    printf 'train concurrency must not cancel in-progress announces\n' >&2
    return 1
  }
  ! grep -Fq "$RETIRED_SHA" "$workflow" || {
    printf 'retired announce pin is forbidden\n' >&2
    return 1
  }
}

# 1. The shipped workflow satisfies the policy.
validate_release_policy "$WF" || fail "shipped workflow violates release policy"
pass "shipped workflow satisfies release policy"

# 2. Mutation probe: the retired pin must FAIL the policy check.
sed "s/$HARDENED_SHA/$RETIRED_SHA/" "$WF" > "$TMP/mutated.yml"
if validate_release_policy "$TMP/mutated.yml" 2>/dev/null; then
  fail "policy check passed a retired-pin mutation — the check is vacuous"
fi
pass "retired-pin mutation fails the policy check"

# 3. Ancestry rule, exercised for real: a tag on the default branch passes,
#    a tag not merged into it fails — the exact command the verify job runs.
REPO="$TMP/repo"
git init -q -b main "$REPO"
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
git -C "$REPO" tag v-good
git -C "$REPO" checkout -q -b side
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q --allow-empty -m stray
git -C "$REPO" tag v-bad
git -C "$REPO" checkout -q main

good_sha="$(git -C "$REPO" rev-list -n 1 v-good)"
bad_sha="$(git -C "$REPO" rev-list -n 1 v-bad)"
git -C "$REPO" merge-base --is-ancestor "$good_sha" main \
  || fail "ancestry rule rejected a tag that is on the default branch"
pass "tag on the default branch passes the ancestry rule"
if git -C "$REPO" merge-base --is-ancestor "$bad_sha" main; then
  fail "ancestry rule accepted a tag that is not merged into the default branch"
fi
pass "unmerged tag fails the ancestry rule"

printf 'release-train smoke: all checks pass\n'
