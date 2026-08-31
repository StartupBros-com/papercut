# papercut

[![CI](https://github.com/StartupBros-com/papercut/actions/workflows/ci.yml/badge.svg)](https://github.com/StartupBros-com/papercut/actions/workflows/ci.yml)

Records the small failures your agents route around silently, and mines them
into ranked, tracked, verified fixes.

```
/plugin marketplace add https://github.com/StartupBros-com/hov-marketplace.git
/plugin install papercut@hov
```

## The problem

Agents hit dozens of snags a day — a misleading error, a file that is always
too big to read, a config probe that fails on every run — and they work around
all of it without telling you. Each workaround costs tokens and time, every
session, forever. Friction nobody records is friction nobody fixes.

## The solution

papercut writes the failures down automatically, then gives you the machinery
to turn the pile into fixes:

| | |
|---|---|
| Capture | A `PostToolUseFailure` hook logs every hard tool failure — no agent cooperation needed, no tokens spent |
| Rank | Signatures sort by **distinct sessions hit**, so repetition is the priority signal, not noise |
| Fold | Related signatures group into a causal **family**; you fix the cause, not each spelling of it |
| File | One family at a time carries an evidence dossier into a filed, tracked work item |
| Verify | A fix only reads **verified** after enough real traffic has passed to make silence meaningful |

> **Verification status, stated exactly:** the refuse-verified-on-silence rule
> is enforced by tests; regression detection has fired on live data; the first
> positive verified-fix cycle is in progress in the source harness (exposure
> 71/187 at publication, window completing 2026-09-28).

## Quick example

```bash
# after a few days of normal agent work:
python3 -m papercut list --days 7          # ranked: which friction, how many sessions
python3 -m papercut show "no_such_file:config.yaml"   # every occurrence of one signature
python3 -m papercut add -m "the export flag silently truncates at 1000 rows; the docs never say so"
python3 -m papercut rollup --days 7        # report: what is over threshold, verification state
python3 -m papercut triage --days 30       # draft evidence dossiers for the worst families
python3 -m papercut adopt my-family        # validate the dossier, file the work item
```

## Design philosophy

- **Silence has two causes.** A quiet signature means the fix worked — or
  nobody was around to hit the problem. Verification measures exposure
  (distinct capture sessions since the fix) against a floor before quiet
  counts as anything.
- **Capture costs nothing.** The hook exits 0 with no stdout, so records never
  enter the transcript. One deliberate exception: an oversized-`.jsonl` read
  failure returns a one-line hint naming a better tool.
- **Duplicates are wanted.** Ranking is by distinct sessions, so logging the
  same friction again is exactly how it gets prioritized. Never deduplicate
  before logging.
- **Quarantine defers, never deletes.** Junk fingerprints leave the ranking
  but stay on disk and visible (`list --quarantined`); the fix for a bad
  fingerprint is better fingerprinting, not deletion.
- **A closed work item is a claim, not a result.** Families carry a derived
  verification stage — `verifying`, `verified`, `provisional`, `regressed` —
  recomputed from the store on every read. Nothing stores a verdict that could
  go stale.

## Installation

**As a Claude Code plugin** (registers the capture hook automatically):

```
/plugin marketplace add https://github.com/StartupBros-com/hov-marketplace.git
/plugin install papercut@hov
```

**Standalone CLI** (stdlib-only Python, no dependencies):

```bash
git clone https://github.com/StartupBros-com/papercut.git
cd papercut
python3 -m papercut --help
```

Standalone use captures nothing until the hook is registered. The plugin's
`hooks/hooks.json` does this for you; the equivalent manual entry in
`settings.json` is:

```json
{
  "hooks": {
    "PostToolUseFailure": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "node \"/path/to/papercut/hooks/papercut-log.js\"" }
      ]}
    ]
  }
}
```

## Quick start

1. Install the plugin (above). New sessions start capturing immediately.
2. Work normally for a few days. Optionally tell your agents about the
   voluntary path — one line in your agent instructions: *"non-blocking
   friction you worked around? `papercut add -m \"...\"` and carry on."*
3. Run `python3 -m papercut list --days 7`. The ranking is the answer to
   "what keeps hurting."
4. When something crosses threshold (3+ distinct sessions, 3+ hits), run
   `triage`, complete the dossier's judgment sections, and `adopt` it into a
   filed work item.
5. After the fix ships and the item closes, `rollup` reports whether it held.

## Commands

| Command | What it does |
|---|---|
| `add -m "..."` | Log a self-reported papercut (the no-error-signature class) |
| `list [--days N] [-v] [--quarantined]` | Ranked signatures in the window |
| `show <signature>` | Every occurrence of one signature |
| `resolve <signature>` | Mark fixed; hidden until it recurs, then it returns |
| `rollup [--days N] [--apply]` | Rank plus verification report; `--apply` files/updates issues over threshold |
| `triage [--days N]` | Draft evidence dossiers for flagged families |
| `adopt <family>` | Validate a completed dossier and file it, once |
| `family <create\|assign\|show\|escalate\|dispose\|reopen\|...>` | Append-only family lifecycle |
| `staleness` | Is capture still alive? (for a weekly scheduled check) |

Run any command with `-h` for its full flags.

## Configuration

Optional file at `${CLAUDE_CONFIG_DIR:-~/.claude}/papercut.json`, or
environment variables — env wins. Defaults are chosen so an empty config is a
working install.

| Variable | Purpose |
|---|---|
| `PAPERCUT_STORE` | Store directory (default `${CLAUDE_CONFIG_DIR:-~/.claude}/papercuts`) |
| `PAPERCUT_ISSUE_LABEL` | Label applied to filed issues (default `papercut`) |
| `PAPERCUT_WORK_SPEC_LABEL` | Extra label for adopted work items |
| `PAPERCUT_WORK_SPEC_SECTIONS` | Required headings in a filed body (default empty: gate off) |
| `PAPERCUT_DISPATCH_READY_LABEL` / `PAPERCUT_DISPATCH_DOCS_REF` | Wire adopt's guidance to your own dispatch queue (default unset: no guidance printed) |
| `PAPERCUT_KNOWN_GUARDS` | Guard names, so differently-worded reports of one guard share a signature |
| `PAPERCUT_GH_LIST_LIMIT`, `PAPERCUT_VERIFY_WINDOW_DAYS`, `PAPERCUT_VERIFY_EXPOSURE_FLOOR`, `PAPERCUT_TRIAGE_UNFAMILIED_LIMIT`, `PAPERCUT_DOSSIER_PROJECT_CAP` | Tuning; see the defaults in `papercut/cli.py` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| No records after installing | The hook fires in sessions started after install. Check liveness with `python3 -m papercut staleness` |
| `list` shows nothing actionable | Thresholds are 3+ distinct sessions and 3+ hits; `list -v` shows everything below them |
| `rollup --apply` or `adopt` errors mentioning `gh` | Filing needs an authenticated GitHub CLI. Capture and reporting work without it |
| Records landing somewhere unexpected | The store follows `CLAUDE_CONFIG_DIR`, then `PAPERCUT_STORE` |
| A junk signature ranks high | `list --quarantined` shows what is already filtered; a new junk shape is a fingerprinting bug — issues welcome |

## Limitations

- Claude Code only: capture rides the `PostToolUseFailure` hook event.
- The store is per-machine. There is no team aggregation.
- Verification exposure is a proxy: store-wide session liveness says agents
  were working, not that the fixed path was exercised. Low-traffic fixes can
  sit `provisional` indefinitely — that is the correct answer, not a bug.
- Filing (`rollup --apply`, `adopt`) requires the `gh` CLI; everything else is
  stdlib-only Python plus Node for the hook.
- The voluntary `add` path only happens if your agents are told to use it.

## FAQ

**What gets recorded, exactly?** For each hard tool failure: timestamp,
signature, tool name, the error text, the failing command or target, working
directory, and a session id suffix. Credential-shaped values (bearer tokens,
key patterns, connection-string passwords) are redacted before anything is
written, and the redaction corpus is pinned by tests on both the hook and CLI
sides.

**Does anything leave my machine?** Not from capture. Records are local files.
The only network calls are the ones you invoke: `rollup --apply` and `adopt`
file GitHub issues via `gh`; `family escalate` records a URL you provide.

**What does capture cost per session?** Nothing in tokens: the hook exits 0
with no stdout. The one exception is documented under Design philosophy.

**How do I turn it off?** Uninstall the plugin (or remove the hook entry).
The store remains; delete it if you want the history gone.

**Where did this code come from?** It is extracted from a working private
harness by a re-runnable sanitizing transform — comments citing evidence are
translated rather than stripped, output is verified to parse before it is
verified clean, and `scripts/check_no_private_refs.py` holds the private
reference count at zero (it was 183 before the transform existed). See
[docs/EXTRACTION-DESIGN.md](docs/EXTRACTION-DESIGN.md) for the design record.

```bash
python3 scripts/check_stdlib_only.py      # dependency contract
python3 scripts/check_no_private_refs.py  # sanitization, as a number
```

## Contributing

Issues and pull requests are welcome. For anything non-trivial, open an issue
first so we can agree on the approach before you spend time on it.

See [LICENSE](LICENSE) for terms.
