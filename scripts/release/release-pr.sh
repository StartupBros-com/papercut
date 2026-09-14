#!/usr/bin/env bash
# papercut release, step 1 of 2: re-vendor from the source harness, run the gates,
# bump the version, open the release PR and arm auto-merge.
#
#   scripts/release/release-pr.sh <version> <notes-file> <pr-body-file> <source-checkout>
#
# <version> is the bare semver (0.1.19). <notes-file> becomes the GitHub release
# notes in step 2; <pr-body-file> is the PR body. <source-checkout> is the
# checkout of the source harness this plugin is vendored from; it must already
# carry everything this release should vendor (pull it first), and its vendor
# script is invoked from there.
#
# Every step is a plain command so a stopped run can be re-run from the top:
# an existing release branch is reused, an existing PR is left alone.
set -euo pipefail
VERSION=${1:?version, e.g. 0.1.19}
NOTES=${2:?notes file}
BODY=${3:?pr body file}
SOURCE=${4:?source harness checkout}
[ -f "$NOTES" ] || { echo "notes file missing: $NOTES" >&2; exit 1; }
[ -f "$BODY" ] || { echo "pr body file missing: $BODY" >&2; exit 1; }
VENDOR="$SOURCE/claude/scripts/papercut-vendor.py"
[ -f "$VENDOR" ] || { echo "vendor script missing at $VENDOR" >&2; exit 1; }
BRANCH="release-${VERSION//./-}"

cd "$(git rev-parse --show-toplevel)"
git fetch origin --quiet
git checkout -q main
git pull -q --ff-only origin main
if git rev-parse -q --verify "$BRANCH" >/dev/null; then git checkout -q "$BRANCH"; else git checkout -q -b "$BRANCH"; fi

python3 "$VENDOR" | tail -3
for hook in hooks/*.js; do node --check "$hook"; done
grep -q "require('./papercut-log.js')" hooks/read-ceiling-chunk.js || { echo "sibling require rewrite missing" >&2; exit 1; }
git status --porcelain

python3 -m unittest discover -s tests -q 2>&1 | grep -E '^Ran |^OK|FAILED'
python3 -m unittest discover -s tests -q >/dev/null 2>&1 || { echo "suite FAILED" >&2; exit 1; }
python3 scripts/check_no_private_refs.py || { echo "sanitizer FAILED" >&2; exit 1; }
python3 scripts/check_stdlib_only.py || { echo "stdlib check FAILED" >&2; exit 1; }

printf '%s\n' "$VERSION" > VERSION
# All FOUR version sites, not two. check_version_consistency.py treats VERSION
# as the authority and fails on any disagreement, so bumping a subset produced a
# release branch that could never go green -- the guard and the tool that feeds
# it disagreed about how many files exist.
python3 - "$VERSION" <<'PY'
import json, pathlib, re, sys
new = sys.argv[1]

p = pathlib.Path(".claude-plugin/plugin.json")
d = json.loads(p.read_text())
d["version"] = new
p.write_text(json.dumps(d, indent=2) + "\n")

p = pathlib.Path("pyproject.toml")
t = p.read_text()
t2, n = re.subn(r'^version\s*=\s*"[^"]+"', 'version = "%s"' % new, t, count=1, flags=re.M)
if n != 1:
    sys.exit("pyproject.toml: no version line to bump")
p.write_text(t2)

p = pathlib.Path("papercut/__init__.py")
t = p.read_text()
t2, n = re.subn(r'^__version__\s*=\s*"[^"]+"', '__version__ = "%s"' % new, t, count=1, flags=re.M)
if n != 1:
    sys.exit("papercut/__init__.py: no __version__ line to bump")
p.write_text(t2)

print("bumped ->", new)
PY

# Fail here rather than in CI: a release branch that cannot go green is worse
# than no release branch.
python3 scripts/check_version_consistency.py || {
  echo "version sites disagree after the bump" >&2; exit 1; }

git add -A hooks VERSION .claude-plugin/plugin.json pyproject.toml papercut tests skills scripts README.md
if git diff --cached --quiet; then echo "nothing to commit (already released?)" >&2; exit 1; fi
git commit -q -m "release: v$VERSION" -F "$BODY"
git push -q -u origin "$BRANCH"
if ! gh pr view "$BRANCH" --json number >/dev/null 2>&1; then
  gh pr create --title "release: v$VERSION" --body-file "$BODY"
  sleep 8
fi
gh pr merge "$BRANCH" --auto --squash
gh pr view "$BRANCH" --json number --jq .number
