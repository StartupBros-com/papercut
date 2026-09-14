#!/usr/bin/env bash
# Clean-install smoke test over the CUSTOMER artifact.
#
# Every other test in this suite runs against files sitting in a workstation
# checkout where papercut is already configured, built, and on PATH. None of
# that proves the thing a customer actually installs works: a plugin checkout
# with nothing pre-configured, on a machine that has never heard of papercut.
#
# This script builds a hermetic HOME + CLAUDE_CONFIG_DIR, strips any
# pre-existing `papercut` off PATH, and walks the documented install-and-
# first-use path from an unrelated working directory: bundled launcher ->
# hook capture -> CLI read-back -- then proves neither profile leaked into
# the real store or into each other.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# PAPERCUT_STORE outranks BOTH CLAUDE_CONFIG_DIR and HOME in the CLI's store
# resolution, so overriding only the latter two does not isolate anything: a
# caller who exports PAPERCUT_STORE sends this test's synthetic capture into
# THEIR store, and the containment check below then reports the leak it just
# caused rather than preventing it. Resolve the caller's store once, for the
# containment assertion, then remove the variable so no child inherits it.
CALLER_STORE="${PAPERCUT_STORE:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/papercuts}"
CALLER_STORE_WAS_SET="${PAPERCUT_STORE:+yes}"
unset PAPERCUT_STORE

pass() { printf 'PASS - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required (the CLI runs as python3 -m papercut)"
command -v node >/dev/null 2>&1 || fail "node is required (the hook is a node script)"

# --- Hermetic fixture -------------------------------------------------------
# Two independent fake profiles, plus a working directory that is NOT this
# checkout: the point is to prove the shipped artifact, not a script that
# happens to run from inside its own source tree.
PROFILE1_HOME="$SCRATCH/profile1-home"
PROFILE1_CFG="$SCRATCH/profile1-home/.claude-config"
PROFILE2_HOME="$SCRATCH/profile2-home"
PROFILE2_CFG="$SCRATCH/profile2-home/.claude-config"
WORKDIR="$SCRATCH/customer-project"
mkdir -p "$PROFILE1_HOME" "$PROFILE1_CFG" "$PROFILE2_HOME" "$PROFILE2_CFG" "$WORKDIR"

# A PATH carrying only the interpreters the artifact declares it needs
# (python3, node) -- deliberately excluding every directory a workstation
# might already have papercut installed into (~/.local/bin, a venv, this
# checkout's own bin/). If a pre-existing install leaked in here, every
# assertion below would silently pass against the WRONG artifact.
PY_DIR="$(dirname "$(command -v python3)")"
NODE_DIR="$(dirname "$(command -v node)")"
if [ "$PY_DIR" = "$NODE_DIR" ]; then
  MIN_PATH="$PY_DIR"
else
  MIN_PATH="$PY_DIR:$NODE_DIR"
fi

# 0. The isolation itself: with the constrained PATH, no `papercut` may
#    resolve. Every assertion below is meaningless if this is wrong, since
#    the test could then pass by shelling out to the workstation's own install.
if PATH="$MIN_PATH" command -v papercut >/dev/null 2>&1; then
  fail "papercut resolves on the constrained PATH -- isolation is broken, the remaining checks would prove nothing"
fi
pass "constrained PATH has no pre-existing papercut"

# ---------------------------------------------------------------------------
# 1. The bundled launcher is reachable and runs.
# ---------------------------------------------------------------------------
launcher="$ROOT/scripts/papercut"
[ -f "$launcher" ] || fail "bundled launcher missing: $launcher"
[ -x "$launcher" ] || fail "bundled launcher is not executable: $launcher"
pass "bundled launcher exists at scripts/papercut and is executable"

launcher_script="$SCRATCH/run-launcher.sh"
cat > "$launcher_script" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
unset PAPERCUT_STORE
export PATH="$MIN_PATH"
export HOME="$PROFILE1_HOME"
export CLAUDE_CONFIG_DIR="$PROFILE1_CFG"
cd "$WORKDIR"
exec "$launcher" list --json --days 1 --cwd "$WORKDIR"
SCRIPT
chmod +x "$launcher_script"

launcher_out="$(bash "$launcher_script" 2>&1)" || fail "bundled launcher did not run cleanly under a hermetic profile with no papercut on PATH: $launcher_out"
case "$launcher_out" in
  "["*)
    pass "bundled launcher runs from an unrelated cwd, with no papercut on PATH, and returns JSON"
    ;;
  *)
    fail "bundled launcher ran but did not return the expected JSON list: $launcher_out"
    ;;
esac

# ---------------------------------------------------------------------------
# 2. hooks/hooks.json is well-formed, and every command it declares resolves
#    to a real file in the package.
# ---------------------------------------------------------------------------
hooks_json="$ROOT/hooks/hooks.json"
[ -f "$hooks_json" ] || fail "hooks/hooks.json is missing"

hooks_check="$SCRATCH/check-hooks-json.py"
cat > "$hooks_check" <<'PY'
import json, os, re, sys

root, hooks_path = sys.argv[1], sys.argv[2]
with open(hooks_path, encoding="utf-8") as fh:
    text = fh.read()
try:
    data = json.loads(text)
except json.JSONDecodeError as exc:
    print(f"JSON_ERROR: {exc}")
    sys.exit(1)

commands = []


def walk(node):
    if isinstance(node, dict):
        if isinstance(node.get("command"), str):
            commands.append(node["command"])
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)


walk(data)

if not commands:
    print("NO_COMMANDS: hooks.json declares zero commands -- nothing to verify")
    sys.exit(1)

missing = []
for cmd in commands:
    m = re.search(r'\$\{CLAUDE_PLUGIN_ROOT\}([^"]+)', cmd)
    if not m:
        print(f"NO_PLUGIN_ROOT_REF: {cmd!r}")
        sys.exit(1)
    target = root + m.group(1)
    if not os.path.isfile(target):
        missing.append(target)

if missing:
    for p in missing:
        print(f"MISSING_FILE: {p}")
    sys.exit(1)

print(f"{len(commands)} command(s) resolve to real files")
PY

hooks_out="$(python3 "$hooks_check" "$ROOT" "$hooks_json" 2>&1)" && hooks_status=0 || hooks_status=$?
if [ "$hooks_status" -ne 0 ]; then
  fail "hooks/hooks.json invalid or references a missing file: $hooks_out"
fi
pass "hooks/hooks.json is well-formed JSON"
pass "every command hooks.json declares resolves to a real file ($hooks_out)"

# ---------------------------------------------------------------------------
# 3 & 4. A real PostToolUseFailure payload, fed to the capture hook, produces
#    a record -- and the CLI reads back the SAME store (the store-path
#    agreement that silently breaks when hook and CLI resolve the profile
#    differently).
# ---------------------------------------------------------------------------
SIG_CMD="papercut-install-smoke-missing-cmd"
expected_sig="command_not_found:$SIG_CMD"

payload_file="$SCRATCH/payload.json"
payload_script="$SCRATCH/make-payload.py"
cat > "$payload_script" <<PY
import json
print(json.dumps({
    "tool_name": "Bash",
    "tool_input": {"command": "$SIG_CMD --flag"},
    "tool_response": {"stderr": "bash: $SIG_CMD: command not found"},
    "cwd": "$WORKDIR",
    "session_id": "install-smoke-session",
    "duration_ms": 7,
}))
PY
python3 "$payload_script" > "$payload_file"

hook_script="$SCRATCH/run-hook.sh"
cat > "$hook_script" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
unset PAPERCUT_STORE
export PATH="$MIN_PATH"
export HOME="$PROFILE1_HOME"
export CLAUDE_CONFIG_DIR="$PROFILE1_CFG"
exec node "$ROOT/hooks/papercut-log.js" < "$payload_file"
SCRIPT
chmod +x "$hook_script"

hook_out="$(bash "$hook_script" 2>&1)" || fail "hooks/papercut-log.js exited non-zero on a real failure payload: $hook_out"
pass "PostToolUseFailure hook accepted a real failure payload without erroring"

sig_pattern="\"sig\":\"$expected_sig\""
store_hits="$(find "$PROFILE1_CFG/papercuts" -type f -name '*.jsonl' -print0 2>/dev/null | xargs -0 -r grep -Fl "$sig_pattern" 2>/dev/null || true)"
[ -n "$store_hits" ] || fail "hook did not write a record carrying sig $expected_sig under $PROFILE1_CFG/papercuts"
pass "hook wrote a record with the expected signature ($expected_sig)"

readback_script="$SCRATCH/run-readback.sh"
cat > "$readback_script" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
unset PAPERCUT_STORE
export PATH="$MIN_PATH"
export HOME="$PROFILE1_HOME"
export CLAUDE_CONFIG_DIR="$PROFILE1_CFG"
exec "$launcher" list --json --days 1 --cwd "$WORKDIR"
SCRIPT
chmod +x "$readback_script"

readback_out="$(bash "$readback_script" 2>&1)" || fail "CLI list failed to read back the profile the hook just wrote to: $readback_out"

readback_check="$SCRATCH/check-readback.py"
cat > "$readback_check" <<'PY'
import json
import sys

expected = sys.argv[1]
rows = json.loads(sys.argv[2])
sigs = [r.get("sig") for r in rows]
if expected not in sigs:
    print(f"NOT_FOUND: expected sig {expected!r} not in {sigs!r}")
    sys.exit(1)
match = next(r for r in rows if r["sig"] == expected)
if not match.get("count", 0) >= 1:
    print(f"BAD_COUNT: count was {match.get('count')!r}, expected >= 1")
    sys.exit(1)
print(f"count={match['count']}")
PY
readback_check_out="$(python3 "$readback_check" "$expected_sig" "$readback_out" 2>&1)" || fail "CLI read a different store than the hook wrote (store-path agreement broke): $readback_check_out / raw output: $readback_out"
pass "CLI reads back the SAME store the hook wrote to ($readback_check_out)"

# ---------------------------------------------------------------------------
# 5. Nothing leaked: the real HOME/store never saw this test's records, and a
#    second, independent temp profile sees none of the first profile's
#    records either.
#
# A full before/after snapshot of the real store is not used here: this is a
# multi-session workstation, and another live session's own papercut hook can
# legitimately append to the real store while this test runs, which would
# make a snapshot diff flaky for reasons that have nothing to do with a real
# leak. Instead this checks containment directly -- every record this test
# produces carries WORKDIR (a freshly minted, globally unique tmp path) as its
# cwd, so the real store must never mention it, independent of any unrelated
# concurrent activity.
# Resolved at the top, BEFORE the override was stripped -- re-resolving here
# would silently point at the hermetic profile and assert nothing.
real_store="$CALLER_STORE"
if [ -d "$real_store" ] && grep -Frq "$WORKDIR" "$real_store" 2>/dev/null; then
  fail "the simulated failure leaked into the REAL papercut store ($real_store) -- it mentions this test's tmp cwd"
fi
pass "the real papercut store ($real_store) carries no trace of this test's records"

second_readback_script="$SCRATCH/run-second-readback.sh"
cat > "$second_readback_script" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
unset PAPERCUT_STORE
export PATH="$MIN_PATH"
export HOME="$PROFILE2_HOME"
export CLAUDE_CONFIG_DIR="$PROFILE2_CFG"
exec "$launcher" list --json --days 1 --cwd "$WORKDIR"
SCRIPT
chmod +x "$second_readback_script"

second_out="$(bash "$second_readback_script" 2>&1)" || fail "CLI failed against a second, independent profile: $second_out"
case "$second_out" in
  *"$expected_sig"*)
    fail "a second, independent profile saw the FIRST profile's record -- profiles are not isolated: $second_out"
    ;;
esac
[ ! -d "$PROFILE2_CFG/papercuts" ] || fail "a store directory exists under the second profile even though nothing ever wrote to it: $PROFILE2_CFG/papercuts"
pass "a second, independent profile sees none of the first profile's records"


# ---------------------------------------------------------------------------
# 6. Regression: a caller-exported PAPERCUT_STORE must not receive this test's
#    records. Guarded by a sentinel so the re-invocation runs exactly once.
# ---------------------------------------------------------------------------
if [ -z "${PAPERCUT_SMOKE_INNER:-}" ]; then
  caller_store="$SCRATCH/caller-store"
  mkdir -p "$caller_store"
  if PAPERCUT_SMOKE_INNER=1 PAPERCUT_STORE="$caller_store" \
      bash "${BASH_SOURCE[0]}" >"$SCRATCH/inner.log" 2>&1; then
    if find "$caller_store" -type f -name '*.jsonl' | read -r _; then
      fail "a caller-exported PAPERCUT_STORE received this test's records -- the hermetic profile did not override it"
    fi
    pass "a caller-exported PAPERCUT_STORE is left untouched"
  else
    sed 's/^/    /' "$SCRATCH/inner.log" >&2
    fail "the smoke test failed when run with a caller-exported PAPERCUT_STORE (output above)"
  fi
fi

printf 'install-smoke: all checks pass\n'
