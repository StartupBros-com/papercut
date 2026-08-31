# papercut: extraction design for a marketplace plugin

Status: **design only.** Nothing is published. Publication stays gated on the
proofs listed under "Gates" below.

Source of record: a five-dimension audit (coupling, GitHub-dependency,
sanitization, capture-hook portability, marketplace precedent) run 2026-08-28,
plus direct probes recorded inline. Where a claim below was proved by running
something, the probe is named. Where it was not, it says so — the audit
produced 51 findings but its verifier matched claims by title and most titles
did not align, so **audit findings are leads, not verdicts**.

Precedent: `~/SITES/memory-dream` is a shipped extraction of a stdlib-only
Python CLI out of this same harness. Its `docs/EXTRACTION-DESIGN.md` is the
template this document follows.

## What is already true (probed, not assumed)

- **Stdlib-only.** An AST walk over `claude/bin/papercut.py` found 16 imports
  and zero third-party packages. There is no dependency story to solve.
- **One GitHub chokepoint.** `gh()` is the single subprocess boundary, with 19
  call sites and no caller passing `check=True`.
- **Local commands already survive a machine with no `gh`.** Probed by
  rebuilding PATH without it: `add`, `list`, `triage`, `staleness`,
  `family show`, `rollup`, and `rollup --refresh` all exit 0 with correct
  output and no traceback, both with an empty store and with an adopted family
  present. The mutating paths fail cleanly (exit 2/4), not with a traceback.

  This refutes the earlier working assumption that ~20 call sites needed
  degradation work before extraction. They did not.

## Defects this audit surfaced in the *live* tool

Both were fixed here rather than deferred to the port, because they are bugs
today, not just portability problems.

1. **The hook ignored `PAPERCUT_STORE`** while the CLI honored it
   (`papercut.py:68`), so setting it split the corpus: the hook wrote records
   no read path would ever see. Fixed at the hook's own resolution point, with
   `PAPERCUT_STORE` added to the hook-test harness `NEUTRALIZE` list so the
   suite cannot write into a real store. (PR #593.)

2. **A missing `gh` was reported as a missing repository — and as an empty work
   queue.** `str(FileNotFoundError)` is `"[Errno 2] No such file or directory:
   'gh'"`, and `REMOTE_MISSING_RE` matches on `no such`. So `repo_exists()`
   returned `False` ("this repository does not exist") and
   `list_items_or_refuse()` returned `[]` ("this queue is empty") when nothing
   had been read at all. The empty list is what the global open-work cap counts
   and what the duplicate-marker search trusts, so a machine without `gh` could
   file past the cap and refile an item that already existed.

   Fixed at the `gh()` chokepoint — one guard in the shared function — by
   catching `FileNotFoundError` ahead of `OSError` and returning a message
   deliberately worded to share no substring with that regex, plus a
   deduplicated one-line diagnostic naming the real cause. Authentication
   failures get the same treatment while keeping their non-zero return, so
   every fail-closed caller still fails closed.

## The split, and the plan to get there

**The governing rule: the operator's harness keeps today's behavior by keeping
today's defaults.** Generalization happens in this repo's copy first, with every
default set to what it already does, and the *plugin* ships different defaults.
Nothing is forked away and re-shed. The operator needs no config file at all.

That is not a preference; it is the lowest-risk option and it is testable. The
proof obligation for every stage below is the same: **the existing
`test_papercut.py` suite passes unmodified, with no config file present.** A
change that needs a test edited to stay green is a change that moved behavior.

### Mechanism

Follow `~/SITES/memory-dream/memory_dream/config.py`: keep module-level
constants and mutate them in place (`globals()[name] = ...`), file first then
env on top, loaded once from `main()` (`papercut.py:3332`). Do **not** introduce
a `Config` object — the suite rebinds 7 module constants in 20 places
(`PC.RESOLVED`, `PC.FAMILIES`, `PC.DOSSIERS`, `PC.STORE`, `PC.FILINGS`,
`PC.GH_LIST_LIMIT`, `PC._GH_DIAGNOSED`), and an indirection layer churns every
one of them for no gain.

**The derived-path trap.** `RESOLVED`, `FAMILIES`, `FILINGS` and `DOSSIERS` are
computed from `STORE` at import (`papercut.py:256-267`) and then freeze. Probed:
setting `PC.STORE` to a new path left `PC.RESOLVED` pointing at the old store.
Any override of `STORE` must be followed by a `recompute_paths()` step, or the
override half-applies and the tool reads one store while writing another — the
same class of split-brain the hook had before PR #593.

### Stage 1 — the override layer (DONE 2026-08-29)

Shipped inert. Module constants stay the interface and are mutated in place;
`load_config()` runs once from `main()`, so nothing moves at import.
`recompute_derived()` re-derives the five constants that freeze at import --
the four state paths from `STORE`, and `ADOPT_LABELS` from the two label names,
which is the same trap in different clothes and was nearly missed.

The zero-behavior-change claim was proved rather than asserted: no `PAPERCUT_*`
var is set and no config file exists; a test pins every constant unchanged
after loading with neither present; the test file diff is 106 insertions and
**zero deletions**, so no existing test was bent to fit; and the same read-only
rollup through the previous binary and this one, against the same store, is
byte-identical across all 26 lines.

Landed before Monday's unattended run after an earlier decision to hold it.
That deferral was aimed at a path-resolution refactor; what this turned out to
be is a loader that does nothing until configured, and the harness configures
nothing. Recorded because the reasoning matters more than the outcome: hold a
risk when it is real, and update when the built thing is not the imagined one.

### Stage 2 — the four portability defects (DONE 2026-08-29)

All four closed. Three were real defects here, not only in a packaged copy.

1. **`adopt` failed 100% of the time in a plugin layout.** `work_spec_gate`
   shelled out to `Path(__file__).parents[1]/"scripts"/"work-spec-check.sh"`,
   which does not exist there, so every adoption hit the OSError branch and
   failed with an errno — a perfect dossier included. Now checked with
   papercut's own `markdown_section`, which also closed a real gap here: the
   script validated with a different regex than the renderer uses, so the gate
   could pass text the renderer read as missing. The script is untouched and
   keeps its own consumers.
2. **The legacy filing path restyled existing labels.** `ensure_label` used
   `gh label create --force`, which rewrites colour and description.
   `ensure_adoption_labels` had already fixed exactly this on the adopt path
   after it was measured on 2026-08-26 against this org's curated `work-spec`
   label — the fix simply never reached the second caller. Now
   verify-then-create, failing closed on an unreadable label list.
3. **`adopt` named this harness's queue unconditionally.** Every successful
   issue filing printed "tag it loop-ok" plus a doc reference, so a stranger
   got instructions for machinery they do not have. Now gated on
   `DISPATCH_READY_LABEL` / `DISPATCH_DOCS_REF` through the Stage 1 layer,
   defaulting to today's exact text; empty label omits the line rather than
   rewording it.
4. **Guard self-report fragmentation** — withdrawn, see Stage 3. Measured at
   zero instances in 560 self-reports.

Every default equals today's behavior, so this harness is unchanged. Remaining
dispatcher coupling is `dispatch_handoff_line`'s label reads, which sit behind
`--refresh` and are a separate item rather than part of this stage.

### Stage 3 — guards: nothing to build (measured 2026-08-29)

**This stage was planned, then measured away.** The mechanism finding below is
correct and worth keeping. The prescription that followed it was not, and is
withdrawn.

**Correct, and load-bearing.** `KNOWN_GUARDS` (`papercut.py:185`) is not the
operator's high-value signal. `guard_blocked:git-guard` is produced entirely by
the **JS hook**: each guard names itself via `setDenyContext({guard: '...'})`
and `papercut-log.js` builds `guard_blocked:<name>` from that string. The hook
never imports the Python list, so emptying it costs the automatic path nothing.
In-tree proof: `_FIXTURE_GUARD_SIG` names a guard absent from the list and
flows through rank, rollup and show with no special casing. The list affects
exactly one path — a free-text `papercut add` with `--sig` omitted
(`papercut.py:1270`).

**Withdrawn.** This document previously argued the list is "already stale", so
"prose self-reports about them fragment today, on this machine", and prescribed
learning the vocabulary from the store. Measured across all **560** self-reported
records: **zero** have ever named `whatsapp-guard`, `grep-cost-guard`,
`heavy-job-admission` or `canonical-worktree-route-guard`. The only fragmentation
on record is 21 records dated 2026-08-06 to 08-07 — before `guard_in_message`
existed — and nothing since. The staleness is real; its consequence was not.

**So there is nothing to build here.** The harness keeps its six-name list,
which costs nothing and is exactly today's behavior. The plugin ships an empty
one, which costs a stranger nothing either, because the automatic path never
consults it. If a prose self-report about an unlisted guard ever appears, the
fix is `--sig`, which SKILL.md already documents as the escape hatch.

The general lesson, since this document exists to be followed: a stale-looking
data structure is not a defect until someone measures what its staleness costs.
This one cost nothing, and building the "strictly better" mechanism would have
been pure machinery.

### Stage 4 — the one true deletion: the trivial-head-ref route (DONE 2026-08-29)

`open_pr_on_head` / `trivial_head_ref` / `head_ref_exists`, the `elif head:`
fork in `cmd_adopt`, its four dedicated tests, its SKILL.md paragraph and its
`judgment_redaction_field` entry are gone: 173 deletions against 6 insertions.

The evidence for deleting rather than defaulting: **8 adoptions in the tool's
entire history, every one of them `kind: issue`, zero via this route.** The safe
stranger default was "off" and the operator's measured behavior was also
"never", so a config knob would have encoded a distinction that does not exist.

Scoped to the CREATION path only. `open_papercut_count`'s issue+pr queue loop,
`locator_fields`' `pr` validation, `dispatch_handoff_line`'s `kind == "pr"`
branch and `family close-observed --kind pr` all remain: they READ locators, and
a legacy PR adoption must stay countable against the cap. Removing those would
have narrowed a compat surface rather than deleting dead code.

### Stage 5 — sanitize, then re-measure

Only now. Much of the 172-hit debt lives in code Stages 3 and 4 remove or
rewrite; sanitizing first is work thrown away.

### The knob table

Stranger default on the left, the value that preserves today's behavior on the
right. **Every "harness value" below is what the code already does**, which is
why the operator needs no config file.

| knob | stranger default | harness value (= today) |
|---|---|---|
| `store_root` | `${CLAUDE_CONFIG_DIR:-~/.claude}/papercuts` | unchanged (already equal) |
| `adopt_labels` | `["papercut"]` | `["papercut", "work-spec"]` |
| `work_spec_sections` | `[]` (gate is a no-op) | `Acceptance Criteria, Planted negative, No-Claim Boundary` |
| `dispatch_labels` | all unset — no handoff line, no loop-ok prose | `ready=loop-ok, claimed=claimed, blocked=blocked` |
| `legacy_label_force` | `false` (verify-then-create) | `true` |
| `known_guards` | `[]` (nothing consults it on the automatic path) | today's six names, unchanged |
| thresholds (`verify_window_days`, `verify_exposure_floor`, rollup defaults) | unchanged | unchanged |

### Does the operator lose anything?

Itemized, because the answer has to survive scrutiny rather than reassure.

- **Nothing changes for every row above** — the harness value *is* the current
  hardcoded value, and no config file is introduced on this machine.
- **Guards are untouched.** The list stays as it is. The fragmentation this
  document once claimed it caused was measured at zero instances.
- **The trivial-head route disappears** — measured zero uses in 30 days.
- **`dispatch handoff` and the work-spec gate keep working**, because their
  harness values stay on. Turning them off is the *plugin's* default, not this
  repo's.

The one real cost is not capability, it is care: from Stage 1 on, every default
is load-bearing, so a future change that edits a default is a behavior change
and must be reviewed as one.

## Sanitization contract

Model on `~/SITES/memory-dream/scripts/check_no_private_refs.py`: a CI script
that fails on any shipped file containing personal paths, this org's and
repo's names, private issue/PR numbers, private script names
(`sync.sh`, `wt-new.sh`, `harness-weekly.sh`, `gh-ready.sh`), private project
codenames, and internal doctrine shorthand (the `KTD<n>` and `R<n>` gate codes,
which the public docs must spell out as named invariants instead).

## Skeleton: what a build actually measured

A skeleton was built at `~/SITES/papercut` (17 files, memory-dream's layout)
with the CLI, hook, skill, tests and validator **vendored verbatim** — no
sanitizing, no generalizing. The point was to turn "how much work is the port?"
into a number instead of an estimate. Nothing is published and no repository
was created.

Two results were better than expected:

- **The package layout works with zero code changes.** `python3 -m papercut
  --help` exits 0 and prints the correct usage with `papercut.py` copied
  straight to `papercut/cli.py`. No import rewrite was needed to make it run.
- **The dependency contract already holds.** `scripts/check_stdlib_only.py`
  passes on the vendored CLI.

The debt is concentrated and countable. `scripts/check_no_private_refs.py`
reports **172 hits**:

| file | hits |
|---|---|
| `tests/test_papercut.py` | 81 |
| `papercut/cli.py` | 54 |
| `skills/papercut/SKILL.md` | 18 |
| `hooks/papercut-log.js` | 12 |
| `scripts/work-spec-check.sh` | 7 |

By kind, the largest classes are private harness script names (37), the
private queue label `loop-ok` (29), internal doctrine shorthand such as `KTD6`
(20), private issue references (17), and this fleet's guard names (15).

The guard deliberately does **not** ban the publishing org or repository URL:
those are identity, not leakage, and the memory-dream guard makes the same
call. An earlier over-broad version counted 195 by flagging them.

Two further facts the build settled:

- The vendored tests resolve the CLI as `Path(__file__).parents[1] / "bin" /
  "papercut.py"`. They must be ported to package imports; they do not run
  as-is in this layout.
- **No existing hov plugin ships a `hooks.json`.** papercut's automatic
  capture would make it the first hook-bearing plugin in the marketplace, so
  hook registration has no in-catalog precedent to copy and needs its own
  install smoke test.

## Gates before anything is published

1. One "did the fix hold?" cycle closes — the 7-day telemetry gates land next
   week. The skill's core promise is measured improvement; shipping before a
   single measurement closes would sell the one thing not yet witnessed.
2. Monday's first fully unattended weekly run comes back clean.
3. The publishing sequence itself (from the marketplace validator's own
   source): a new public repo in the memory-dream shape, a draft release, the
   marketplace card SHA-pinned to a 40-character commit, **and** a new
   `papercut)` arm added to `expected_source_url()` in
   `scripts/validate-marketplace.sh` — the validator rejects any slug not
   explicitly allowlisted there.
