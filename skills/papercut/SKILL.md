---
name: papercut
description: Log a small, non-blocking friction you just hit — a confusing or undocumented setup step, a misleading error, a stale cache, a command that succeeded but did the wrong thing, a gotcha you had to work around. Use in the moment, even though it is not blocking. Also use to review the accumulated friction log (`papercut list`) or to mine the current session for friction you pushed through without recording.
---

# Log a papercut

Friction you route around silently is friction nobody ever fixes. This is the
cheap write path for it.

**Hard tool failures are already captured automatically** by the
`PostToolUseFailure` hook (`hooks/PostToolUseFailure/papercut-log.js`) — nonzero
exits, missing commands, timeouts, guard blocks, ENOENT. Do **not** self-report
those; it is duplicate work and costs tokens the hook does not.

Use this skill for the class that has **no error signature**, which is the only
class automation cannot see:

- documentation or a setup step that was wrong, missing, or misleading
- a command that **succeeded** but did the wrong thing, or silently truncated output
- a stale cache, a stale index, a config that looked live but wasn't
- an error message that pointed at the wrong cause
- a non-obvious gotcha you had to discover by experiment
- a tool that technically works but is hostile to call correctly

## Log one

```bash
papercut add -m "what you were doing → what got in the way (+ a guess at cause/fix)"
```

One or two sentences. A guess at the cause or fix is a bonus, not required.
Example:

```bash
papercut add -m "Ran a-cli query --dim page,query to get a page's search terms; totals were 22 impressions against 13,834 on the page. Row limit truncates server-side without warning — --page returns the real 285 queries."
```

Log it **in the moment and keep working**. It is not blocking, it does not need
permission, and it should not derail what you were doing. Duplicates are fine and
in fact wanted — always log one even if you suspect it's already there — since
the rollup ranks by how many distinct sessions hit a signature, so repetition is
exactly how something gets prioritized.

**Reuse a signature when one already fits.** Without `--sig`, the signature is
slugged from your wording, so two agents describing the same friction differently
never aggregate and neither ever crosses the rollup threshold. If
`papercut list --days 30` already shows a signature that matches, pass it:

```bash
papercut add --sig guard_blocked:a-command-guard -m "a-command-guard blocked a truncating redirect into \$A_JOB_DIR/tmp, which the background-job instructions tell agents to use"
```

That one flag is the difference between a note nobody reads and a ranked issue.

## What this is *not*

| Use | For |
|---|---|
| `papercut` | small non-blocking friction; the repo needs sanding down |
| memory note (`/memory-mine`) | a durable fact worth recalling later |
| `ce-compound` | a solved, verified, non-trivial problem worth a written learning |
| gh issue | a real tracked bug or a piece of planned work |

If it is blocking, it is not a papercut — fix it or raise it.

## Check before you rediscover

Before unfamiliar or risky work in a repo, spend one command:

```bash
papercut list --cwd "$PWD" --days 30
```

The log is a cache, not just a report — someone (possibly you, last week) may
already have paid for the answer. This is the cheapest use of the whole system.

## Subagents

The automatic hook captures subagent hard failures at full fidelity, attributed
to the parent session — verified by probe. Only this **voluntary** path is blind
there: a subagent will not self-report unless you tell it to. If you dispatch
subagents into unfamiliar territory and want their judgment-class friction
captured, say so in the prompt.

## Review what has accumulated

```bash
papercut list --days 7 -v          # ranked by distinct sessions
papercut list --quarantined        # junk fingerprints (needs fingerprinting, not fixes)
papercut show <signature>          # every occurrence of one signature
papercut rollup --days 7           # what is over threshold (report only)
papercut rollup --days 7 --apply   # legacy route: file/update unassigned raw-signature issues
papercut resolve <signature>       # fixed — hide it until it recurs
papercut staleness                 # is capture still alive?
```

`resolve` is not permanent silence: if a resolved signature happens again after
the fix, that is a regression and it comes straight back. Use it freely.

`a weekly scheduled run` already runs the report-only rollup every Monday and nags via
the Windows toast when anything is over threshold, so the log cannot quietly
become write-only. `--apply` stays a deliberate legacy operator step for
unassigned raw signatures; once a family exists, use the clinic below.

## Run the clinic

**Move a folded family through a dossier; do not promote a raw signature as if
it were the work.** A raw signature is an immutable capture address. A family
folds related signatures into the one judgment and remedy they share, so you
can track the cause instead of filing near-duplicate issues.

Create and assign the family before triage. Use one family for the causal
cluster, not one family per spelling of its raw signature.

```bash
papercut family create <family>               # a short causal name, e.g. worktree-isolation
papercut family assign <family> <signature>   # repeat per member; membership is reversible
papercut family show <family>                 # what is folded in right now
```

Run triage, write the judgment half of each selected dossier, then adopt one
completed family.

```bash
papercut triage --days 30       # write dossier drafts for flagged families (3 by default)
papercut adopt <family>         # validate the completed dossier and file it, once
papercut adopt <family> --json  # same, emitting the confirmed locator
```

Triage also lists the top unfamilied flagged signatures — high-volume raw
material nobody has sorted into a family yet — each with a paste-ready
`papercut family assign` command (`--unfamilied-limit` widens the list; any cut
is named). It only proposes: grouping signatures under one cause is the
judgment step the clinic reserves for you, and nothing joins a family until you
run the assign yourself.

Triage produces local candidate dossier drafts in
`${PAPERCUT_STORE:-~/.claude/papercuts}/state/dossiers/`. It regenerates the
machine-owned evidence half; do not edit it. It byte-preserves your judgment
half, which must contain a non-empty value under every heading below or adopt
exits 2 and names what is missing:

- `## Causal hypothesis`
- `## Strongest counterexample`
- `## Owner class`
- `## Destination repository`
- `## Destination justification`
- `## Cheapest remedy`
- `## Acceptance Criteria`
- `## Pre-registered success measure`
- `## Planted negative`
- `## No-Claim Boundary`
- `## For humans`

`## For humans` is the one section a non-operator reads: one short plain-language
paragraph — what keeps going wrong and why it matters, no signatures, paths, or
jargon. Adopt renders it at the top of the filed issue and collapses the full
dossier into a details block below it, so write it for someone who has never
seen this tool.

Adopt never applies `the dispatch-ready label` — filing a work item and admitting it to the
an autonomous queue's autonomous lane are two separate acts. The label itself is
human-admitted: the operator's tag, or a session tagging specific work the
operator explicitly approved in that same session (amended 2026-08-28; the
full rule lives in `docs/your queue's documentation`).

### Adopt local work; escalate upstream work

Route from the dossier's owner class. For `local-defect` or `target-repo`, use
`papercut adopt` so the remedy enters our work queue. For `upstream`, file the
report yourself and record its URL instead:

```bash
papercut family escalate <family> --to https://upstream.example/issues/123
papercut family escalate <family> --to https://upstream.example/issues/123 \
    -n "optional local context"
```

Escalate makes no GitHub calls: it records the supplied HTTPS locator verbatim.
The family leaves the actionable/flagged lane, while every rollup keeps showing
its live window volume under `escalated upstream`. Recurrence is expected and
never comments on or reopens the upstream report. Use `family reopen` to undo
the escalation, or `family dispose` to retire it finally.

**After adopt, the handoff is the operator's.** Tagging a filed work-spec
issue `the dispatch-ready label` is the one act that puts it in the an autonomous queue's intake
filter. Adopt says so on success, and the weekly rollup's `dispatch handoff:`
section shows where adopted-open items stand — awaiting the tag, tagged,
claimed by a session, or blocked. Every state is a label fact read live
during `rollup --refresh`, never stored, and never an inferred outcome:
final readiness (native dependency blockers included) is `an external readiness check`'s
call, `claimed` is the shared cross-session claim signal rather than proof
of a an autonomous queue run, and the trivial route's pull request sits outside the
intake entirely — an external readiness check reads issues only. The section covers up to the
refresh read cap (`--limit`, default 10) per run; the quiet tail rotates
oldest-checked first, so anything past the cap surfaces on a later run.
Whether ticks fire by hand or on the timer is the an autonomous queue's own ratchet
ladder (`docs/an autonomous queue-ratchets.md`), not this tool's.

**Keep the queue at the operator's keep-rate, not the backlog's size.** The
cap defaults to 3; pass `--cap N` to override it. It counts all open
papercut-originated work items: the loop's recorded adoption locators that are
still open, unioned with open issues filed by legacy `rollup --apply`,
deduplicated by locator, across every destination repository.

Use the exit taxonomy:

| Exit | Meaning |
|---|---|
| `0` | success or no candidates |
| `2` | incomplete dossier, missing fields named |
| `3` | policy refusal: cap, stale snapshot, already adopted or disposed, an open legacy per-signature issue covering a member, a destination label that cannot be provisioned, or a redaction that would alter the judgment half |
| `4` | unconfirmed remote state: the artifact may exist remotely; retry reconciles via the marker rather than filing a second one |

When the honest answer is no remedy, dispose rather than inventing a work item.
Fill the causal hypothesis, strongest counterexample, owner class, and
No-Claim Boundary, then record the verdict. Reopen reverses that disposition.

```bash
papercut family dispose <family> --verdict intended-policy
#   verdicts: intended-policy | insufficient-evidence | upstream-reported
papercut family reopen <family>   # reverses it, restoring the retained dossier
```

Dispose retains a redacted copy of the dossier in its own event, and reopen
writes that copy back if no draft is present — so reversing a disposition does
not cost you the judgment you authored. Two limits are worth knowing rather
than discovering: a draft you re-authored after disposing is never clobbered by
the retained copy, and the retained copy is redacted, so it can differ from the
bytes the `dossier_digest` authenticates. A family is refused, not silently
absorbed, if you try to assign into it or dispose it while it is already
adopted or disposed; reopen it first.

### Close the loop

Adoption is not the end of the family. Record what upstream did with the work
item, and the weekly rollup reports a recurrence instead of filing a duplicate.

```bash
papercut family unassign <family> <signature>   # reversible; leaves an audit event
papercut family close-observed <family> \
    --repo owner/repo --kind issue --number 123 \
    --url https://github.com/owner/repo/issues/123 --state closed
papercut family recur-comment <family>          # comment once on a closed recurrence
```

`close-observed` records a local observation of one work item's state; it is
refused if it would point an adopted family at a *different* item, because
disposition, recurrence and the open-work cap all read that locator back. A
`--state open` observation ends the disposition epoch, so a later close starts
a fresh one.

Once a family is closed upstream and member signatures keep arriving, that is a
recurrence: the remedy did not hold. `rollup --apply` and `recur-comment` share
one decision, so neither comments unless a member record postdates both the
closure and the last comment. The comment carries a tool-owned marker keyed to
the closure, and both paths read the item's existing comments first, so a
crashed run reconciles on retry instead of posting a duplicate. If those
comments cannot be read, the run skips that family rather than risk one.

`rollup --apply` also honours a global open-work cap, shared with `adopt`:

```bash
papercut rollup --apply --cap 3   # default; --cap -1 disables the check
```

The cap counts open labeled issues *and* pull requests across every repository
the store can enumerate, including ones recorded in the filing registry whose
capture directories have since been swept. It exists so a backlog of open
papercut work is not compounded by a weekly run that files more.

### Did the fix actually hold?

A closed work item is a claim, not a result. Once a family has a closed
observation, `rollup` and `family show` both carry a verification stage,
recomputed from the store on every read:

```bash
papercut rollup --days 7                    # the stage rides the weekly report
papercut family show                        # every family's stage at a glance
papercut family show <family> --window 60   # widen the verification window
```

| stage | what it means |
|---|---|
| `regressed` | a member signature recurred after the closure, or a recurrence comment was already posted |
| `verifying` | the closure is younger than the window; days remaining are printed |
| `verified` | window elapsed, no recurrence, and capture was demonstrably alive |
| `provisional` | window elapsed and quiet, but too little capture happened to conclude anything |

The distinction that makes this worth having is `verified` versus
`provisional`. Silence has two causes — the fix worked, or nobody was around to
hit the problem — so the stage never rests on silence alone. Exposure is
measured as distinct capture sessions store-wide in the window after the
closure, and must clear a floor of three sessions or half the equal-length
pre-closure baseline, whichever is larger. Both measurements and the floor are
printed, never a bare verdict — including while a family is still `verifying`,
where the count so far shows whether it is on track to clear the floor or
heading for `provisional`.

A recurrence counts only when the rollup would act on it. Member signatures
that are quarantined as junk fingerprints, or that were individually resolved
and have been quiet since, are already excluded from the rollup's recurrence
lane — so the stage excludes them too. Otherwise a family could read
`regressed` forever over a record the rollup deliberately ignores. The window
must be a positive number of days; `--window 0` and negative values are
refused rather than quietly replaced with the default.

Be honest about what that proves. Store-wide capture liveness is a proxy: it
says agents were working, not that the fixed mechanism was exercised. So
`verified` means "no recurrence while capture was alive", and a low-traffic
family can sit `provisional` indefinitely — that is the correct answer, not a
gap to paper over. Nothing here writes: no event is appended, no verdict is
stored, and GitHub is never contacted, so a regression shows up the next time
anyone looks rather than being frozen into a stale verdict.

**Pick member signatures whose silence is the success signal.** Recurrence
and verification read member records, so a member signature that fires when
the mechanism works *as intended* can never validate a fix — it reads
`regressed` forever no matter how good the fix was. Live example
(2026-08-28): the a-vcs-guard doc-gap family's one member was
`guard_blocked:a-vcs-guard`, which fires every time the guard correctly blocks
something; the fix was a documentation change whose real regression test
lives in CI. For a family like that, close the loop with `family dispose`
(verdict `intended-policy`) once the fix ships and its planted negative is
enforced elsewhere — record-based verification is category-inapplicable, and
pretending otherwise manufactures a permanent false `regressed`.

**Dossier directory Ceremony Test**

- consumer — `adopt`, immediately before it files or takes over the work item.
- gate — `an external work-item validator`; non-zero exit means do not file it.
- defect — OBSERVED: the papercut pipeline dead-ended at a weekly report; signatures were ranked and reported, and nothing carried judgment forward into tracked work.
- delete-when — its draft is deleted when the family is adopted (the body lives in the filed item) or disposed (the verdict event carries the digest, plus a redacted copy of the body for reopen). Adopt and dispose each delete only after their own event is durably appended, so a crash loses the draft only once the record that replaces it is safe.

`a weekly scheduled run` invokes `$HOME/.claude/bin/papercut.py`, not a worktree
copy. The clinic is not live for the weekly report or for an ambient
`papercut` invocation until `your config sync push` installs it — so run it from a
worktree as `python3 claude/bin/papercut.py` until then.

## Mining a whole session

If the user asks you to sweep this session for friction you pushed through
without recording, re-read the session, and for each distinct piece of friction
that fits the "no error signature" classes above, run one `papercut add`. Skip
anything the hook would already have caught. **Only do this when asked** — it is
not something to run on your own initiative at the end of a session.
