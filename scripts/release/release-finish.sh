#!/usr/bin/env bash
# papercut release, step 2 of 2: wait for the release PR to merge, tag the merged
# head, draft the GitHub release from the notes file, repin the marketplace card
# (four-field diff on origin/main bytes), publish, and prove the announce from the
# release's OWN Release-train run.
#
#   scripts/release/release-finish.sh <version> <notes-file> <marketplace-checkout>
#
# <marketplace-checkout> is a clone of the marketplace repository that carries
# this plugin's card. Idempotent: every step checks whether it already happened,
# so a stopped run is resumed by re-running the same command. The announce run
# is selected by its display title and creation time, never "newest completed"
# (a previous finisher printed the prior release's receipt that way), and the
# GitHub CLI's --jq takes no --arg, so the title is inlined and createdAt is
# compared in bash (a finisher that passed --arg looped forever on the error).
set -uo pipefail
VERSION=${1:?version, e.g. 0.1.19}
NOTES=${2:?notes file}
MARKET=${3:?marketplace checkout}
TAG="v$VERSION"
BRANCH="release-${VERSION//./-}"
REPO=StartupBros-com/papercut
CARD_BRANCH="papercut-${VERSION//./-}-repin"
CARD_WT="/tmp/$CARD_BRANCH"

until STATE="$(gh pr view "$BRANCH" --repo "$REPO" --json state --jq .state 2>/dev/null)" && [ -n "$STATE" ] && [ "$STATE" != "OPEN" ]; do sleep 30; done
echo "release PR $BRANCH: $STATE"
[ "$STATE" = "MERGED" ] || exit 1

set -e
cd "$(git rev-parse --show-toplevel)"
git checkout -q main
git fetch origin --quiet
git pull -q --ff-only origin main
SHA="$(git rev-parse HEAD)"
grep -q "^$VERSION\$" VERSION || { echo "main does not carry the $VERSION bump" >&2; exit 1; }
if ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then git tag "$TAG" "$SHA"; git push -q origin "$TAG"; fi
if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release create "$TAG" --repo "$REPO" --target main --draft --title "papercut $TAG" --notes-file "$NOTES"
fi
RELEASE_ID=""
for _ in 1 2 3 4 5 6; do
  RELEASE_ID="$(gh api "repos/$REPO/releases" --jq ".[] | select(.tag_name==\"$TAG\") | .id" | head -1)"
  [ -n "$RELEASE_ID" ] && break
  sleep 5
done
[ -n "$RELEASE_ID" ] || { echo "no release id for $TAG" >&2; exit 1; }
echo "TUPLE sha=$SHA releaseId=$RELEASE_ID"

cd "$MARKET"
git fetch origin --quiet
if ! git show origin/main:.claude-plugin/marketplace.json | grep -q "\"releaseTag\": \"$TAG\""; then
  [ -d "$CARD_WT" ] || git worktree add -q "$CARD_WT" -b "$CARD_BRANCH" origin/main
  cd "$CARD_WT"
  CARD_SHA="$SHA" CARD_RELEASE_ID="$RELEASE_ID" CARD_VERSION="$VERSION" CARD_WT="$CARD_WT" python3 - <<'PY'
import os, pathlib, re, subprocess, sys
wt = pathlib.Path(os.environ["CARD_WT"])
s = subprocess.run(["git", "-C", str(wt), "show", "origin/main:.claude-plugin/marketplace.json"], capture_output=True, check=True).stdout.decode("utf-8")
i = s.find('"name": "papercut"')
j = s.find('"name": "', i + 10)
block = s[i:] if j < 0 else s[i:j]
v = os.environ["CARD_VERSION"]
new = block
new, n1 = re.subn(r'"sha": "[0-9a-f]{40}"', '"sha": "%s"' % os.environ["CARD_SHA"], new, count=1)
new, n2 = re.subn(r'"version": "[0-9.]+"', '"version": "%s"' % v, new, count=1)
new, n3 = re.subn(r'"releaseId": \d+', '"releaseId": %s' % os.environ["CARD_RELEASE_ID"], new, count=1)
new, n4 = re.subn(r'"releaseTag": "v[0-9.]+"', '"releaseTag": "v%s"' % v, new, count=1)
if (n1, n2, n3, n4) != (1, 1, 1, 1):
    sys.exit("substitution counts %r" % ((n1, n2, n3, n4),))
(wt / ".claude-plugin/marketplace.json").write_bytes((s[:i] + new + ("" if j < 0 else s[j:])).encode("utf-8"))
print("minimal repin applied")
PY
  bash scripts/validate-marketplace.sh || { echo "validator FAILED" >&2; exit 1; }
  git add .claude-plugin/marketplace.json
  git commit -q -m "chore(papercut): repin marketplace card to $TAG"
  git push -q -u origin "$CARD_BRANCH"
  gh pr view "$CARD_BRANCH" --json number >/dev/null 2>&1 || gh pr create --title "chore(papercut): repin marketplace card to $TAG" --body "sha $SHA, releaseId $RELEASE_ID (draft), tag at the merged head. Four-field diff on origin/main bytes."
  sleep 8
  gh pr merge "$CARD_BRANCH" --auto --squash
  set +e
  until STATE="$(gh pr view "$CARD_BRANCH" --repo StartupBros-com/hov-marketplace --json state --jq .state 2>/dev/null)" && [ -n "$STATE" ] && [ "$STATE" != "OPEN" ]; do sleep 30; done
  echo "card PR: $STATE"
  [ "$STATE" = "MERGED" ] || exit 1
  sleep 45
fi
set +e
PUBLISH_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ "$(gh release view "$TAG" --repo "$REPO" --json isDraft --jq .isDraft)" = "true" ]; then
  gh release edit "$TAG" --repo "$REPO" --draft=false
else
  PUBLISH_AT="$(gh release view "$TAG" --repo "$REPO" --json publishedAt --jq .publishedAt)"
fi
gh release view "$TAG" --repo "$REPO" --json isDraft,publishedAt --jq '"draft=\(.isDraft) publishedAt=\(.publishedAt)"'
while true; do
  RUN="$(gh run list --repo "$REPO" --workflow "Release train" --limit 8 --json databaseId,status,displayTitle,createdAt --jq ".[] | select(.displayTitle==\"papercut $TAG\") | \"\(.databaseId) \(.status) \(.createdAt)\"" 2>/dev/null | head -1)" || { sleep 20; continue; }
  ID="$(printf '%s' "$RUN" | cut -d' ' -f1)"; STATUS="$(printf '%s' "$RUN" | cut -d' ' -f2)"; CREATED="$(printf '%s' "$RUN" | cut -d' ' -f3)"
  if [ -n "$ID" ] && [ "$STATUS" = "completed" ] && { [ "$CREATED" \> "$PUBLISH_AT" ] || [ "$CREATED" = "$PUBLISH_AT" ]; }; then break; fi
  sleep 20
done
echo "run $ID (papercut $TAG): $(gh run view "$ID" --repo "$REPO" --json conclusion --jq .conclusion)"
gh run view "$ID" --repo "$REPO" --log 2>&1 | grep -E '"status":"announced"|does not yet list|403' | tail -1
cd "$MARKET" && git worktree remove --force "$CARD_WT" 2>/dev/null; git branch -D "$CARD_BRANCH" 2>/dev/null; git worktree prune; true
