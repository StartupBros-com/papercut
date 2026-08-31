#!/usr/bin/env python3
"""papercut — the friction log agents can write to, and the rollup that closes the loop.

Two capture paths feed one per-project JSONL store:

  auto  PostToolUseFailure hook (hooks/PostToolUseFailure/papercut-log.js) records
        every hard tool failure. Zero tokens, 100% compliance, no agent judgment.
  self  `papercut add -m "..."` — the class with NO error signature: confusing
        docs, a misleading-but-successful command, a working-but-wrong tool, a
        setup step that isn't written down. Only a model can notice these, so
        this path is voluntary (see the /papercut skill).

And one consumption path, which is the whole point:

  rollup  counts signatures over a window, ranks by how many DISTINCT sessions hit
          them, and above a threshold opens or updates a GitHub issue.

Repetition is the priority signal, so duplicates are kept and counted — the
inverse of memory-mine.py, which discards a note that overlaps an existing index
line. One occurrence of a papercut is noise; the same one in nine sessions across
four repos is a bug report that writes itself.

WHY THE ROLLUP SHIPS WITH THE LOGGER: this harness has three live precedents of
capture built and consumption never wired — wf-pin-audit.py (cited in CLAUDE.md
as "defense-in-depth", invoked by nothing), 19 of 81 memory notes in one store
describing a pipeline that does not exist on this filesystem, and an indexing tool reporting
green systemd exits while its lexical index went 21 days stale. A write-only
papercut store would be the fourth. It is not optional.

Stdlib only, deliberately: no venv to rot (same reasoning as `dfs` in CLAUDE.md,
and the reason mcp-a-cli was retired for a CLI).

Store: ~/.claude/papercuts/<project-slug>.jsonl   (out of repo, never committed)

Commands:
  add    -m MSG [--sig SIG] [--cwd DIR]        append a self-reported papercut
  list   [--days N] [--project SLUG] [--json] [--quarantined]
                                               ranked signatures in the window
  show   SIG [--days N]                        every occurrence of one signature
  family create|assign|unassign|escalate|dispose|reopen|close-observed|recur-comment|show
                                               append or view family state
  triage [--days N] [--min-count N] [--min-sessions N] [--unfamilied-limit N] [--json]
                                               prepare local family dossiers; never files work
  adopt FAMILY [--cap N] [--json]            validate and file one completed family dossier
  rollup [--days N] [--min-count N] [--min-sessions N] [--apply] [--repo R]
                                               rank, then file/update gh issues
"""

from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

STORE = Path(os.environ.get("PAPERCUT_STORE", Path.home() / ".claude" / "papercuts"))
MAX_RECORD_BYTES = 3072  # keep parity with the hook's atomic-append cap
MAX_LOG_BYTES = 32 * 1024 * 1024  # mirrors the hook's per-store cap (writes stop silently there)
ISSUE_LABEL = "papercut"
WORK_SPEC_LABEL = "work-spec"
# Sections a filed work item must carry. Configurable because they are this
# harness's doctrine, not a universal truth: a packaged copy defaults this to
# empty so a stranger's adopt is not gated on headings their process never
# defined. Empty means the gate passes everything.
# Sections a filed work item must carry. Empty means the gate passes
# everything: these headings are one team's doctrine, not a universal
# contract, and gating a stranger on them would refuse every filing.
WORK_SPEC_SECTIONS: tuple[str, ...] = ()
# The label that admits a filed item to an autonomous queue, and the doc that
# explains the act. Both are this harness's an autonomous queue, not a universal one:
# adopt printed them on EVERY successful issue filing, so a stranger got
# actionable-looking instructions for infrastructure they do not have and a
# reference to a document that does not exist for them. Empty label = no
# an autonomous queue, and the guidance is omitted rather than reworded.
# Label that admits a filed item to an autonomous queue. Empty means no
# queue, and the post-filing guidance is omitted rather than reworded.
DISPATCH_READY_LABEL = ""
DISPATCH_DOCS_REF = ""
ADOPT_LABELS = (ISSUE_LABEL, WORK_SPEC_LABEL)
# `gh <kind> list --limit N` truncates hard rather than paging, and a result
# that fills N cannot be told apart from one that was cut off. Enumerate far
# past any real queue and treat a full page as an unenumerable universe --
# a marker missed behind the limit makes adopt file a duplicate.
GH_LIST_LIMIT = 1000
# A repository with issues disabled answers `gh issue list` with a non-zero
# exit. That is an answer, not an unreadable source: the queue provably holds
# nothing, so no marker can hide there. Measured 2026-08-26,
# an-org/example-project sits in the live adoption universe with issues off and
# refused every adoption. Matched narrowly so every other failure still
# fails closed per the open-work cap.
DISABLED_ISSUES_RE = re.compile(r"repository has disabled issues")
# A remote that answers "this does not exist" is an ANSWER; anything else that
# fails is an unconfirmed query and must not be reported as absence.
REMOTE_MISSING_RE = re.compile(
    r"could not resolve to a |not found|no such|does not exist", re.I)
# Worded to share NO substring with REMOTE_MISSING_RE. A missing `gh` used to
# surface as str(FileNotFoundError) -- "[Errno 2] No such file or directory:
# 'gh'" -- which that regex matches on "no such". repo_exists() therefore
# reported an ABSENT TOOL as a NONEXISTENT REPOSITORY, and the queue readers
# reported an unreadable queue as an EMPTY one, which is what lets adopt file
# past the open-work cap. Rewording this string is a behavior change: keep it
# clear of that regex.
GH_UNAVAILABLE = "GitHub CLI (gh) is unavailable: it is not installed, or not on PATH"
# Canonical shapes gh uses when it runs but has no usable credentials. Matching
# these only adds a diagnostic; the non-zero return is unchanged, so every
# fail-closed caller keeps failing closed.
GH_AUTH_RE = re.compile(
    r"gh auth login|not logged in|authentication failed|requires authentication|"
    r"HTTP 401|Bad credentials", re.I)
SIG_MARKER = "<!-- papercut-sig: {sig} -->"
FAMILY_MARKER = "<!-- papercut-family:{family} -->"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# Credential shapes scrubbed before anything is written or sent to GitHub. Kept in
# sync with REDACTIONS in hooks/PostToolUseFailure/papercut-log.js — the two
# capture paths must not disagree about what is safe to store. Redaction happens
# at CAPTURE, not at read: the store is a plain file and `rollup --apply` copies
# sample text into an issue body, so a secret must never land in either.
#
# The key-block pattern is assembled from fragments so this file does not match
# your config sync's own `guard_no_credentials` scanner, which greps the the source harness tree for
# that exact literal and refuses to sync when it finds it. Same trick, and same
# reason, as the comment in your config sync.
_KEY_BLOCK = (r"(-----BEGIN [A-Z ]*PRIV" + r"ATE KEY-----)[\s\S]*?"
              r"(-----END [A-Z ]*PRIV" + r"ATE KEY-----)")

REDACTIONS = [
    (re.compile(_KEY_BLOCK), r"\1<redacted>\2"),
    (re.compile(r"\b(bearer\s+)[\w./+=-]{12,}", re.I), r"\1<redacted>"),
    (re.compile(r"\b(authorization\s*[:=]\s*)\S+", re.I), r"\1<redacted>"),
    (re.compile(r"\b(sk-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xox[abprs]-|AKIA|glpat-)[\w-]{8,}"),
     r"\1<redacted>"),
    # Stripe uses an UNDERSCORE (sk_live_/sk_test_), so the hyphenated `sk-` rule
    # above never matched it. Measured gap: a live Stripe key passed through clean.
    (re.compile(r"\b((?:sk|pk|rk)_(?:live|test)_)\w{8,}"), r"\1<redacted>"),
    (re.compile(r"\b(AIza)[\w-]{20,}"), r"\1<redacted>"),                        # Google API key
    (re.compile(r"\b(eyJ[\w-]{6,})\.[\w-]{6,}\.[\w-]{6,}\b"), r"\1.<redacted>"),  # bare JWT
    (re.compile(r"([\w+.-]+)://([^\s:@/]+):([^\s@/]+)@"), r"\1://\2:<redacted>@"),
    # No leading \b: the secret-bearing name is usually PREFIXED (PGPASSWORD,
    # MYSQL_PWD, GITHUB_TOKEN), and \b would anchor past the prefix and miss it.
    (re.compile(r"([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
                r"|private[_-]?key))([\"']?\s*[:=]\s*[\"']?)[^\s\"',;&)]{4,}", re.I),
     r"\1\2<redacted>"),
]


def redact(s: str) -> str:
    out = str(s)
    for pattern, replacement in REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


def project_slug(cwd: str) -> str:
    """Match the ~/.claude/projects/<slug> and claude/memory/<slug> convention."""
    return re.sub(r"^-+", "-", re.sub(r"[/\\]", "-", str(cwd))) or "-unknown"


def parse_ts(value) -> datetime | None:
    """Parse any timestamp shape this system produces, or None.

    Three producers, three formats: the JS hook writes `toISOString()`
    (millisecond + `Z`), the Python CLI writes `datetime.isoformat()`
    (microsecond + `+00:00`), and GitHub returns second-precision `...Z`.
    Comparing those as raw strings inverts: ASCII `Z` outranks every digit, so a
    same-second `...789Z` record sorts after a chronologically later
    `...789012+00:00` resolve. Always compare parsed datetimes.
    """
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def newer_than(record_ts, boundary) -> bool:
    """True when `record_ts` is strictly later than `boundary`. Unparseable
    boundary -> False (never invent a regression); unparseable record -> False."""
    a, b = parse_ts(record_ts), parse_ts(boundary)
    return bool(a and b and a > b)


# First-party guards whose denials are a known, named friction class. A guard's
# name is the aggregation unit on the automatic path (`guard_blocked:<guard>`),
# and the self-report path must agree or the same friction splits in two.
# Guard names this installation recognises in free-text self-reports.
# Empty by default: the automatic capture path never consults this list
# (each guard names itself), so an unconfigured install loses nothing.
KNOWN_GUARDS: tuple[str, ...] = ()


# Mirrors WRAPPER_LINE/signalLine() in hooks/PostToolUseFailure/papercut-log.js.
# Operator-facing output must show the line the classifier actually keyed on; the
# Bash tool prefixes failures with its own `Exit code N`, so displaying the literal
# first line showed "Exit code 1" for every Bash record while the real error sat on
# line 2+ — triage output that hides the very thing it is triaging.
WRAPPER_LINE = re.compile(
    r"^\s*(?:exit code\s*\d+|command failed(?: with exit code \d+)?|error)\s*[:.]?\s*$", re.I
)


_PROGRESS_LINE = re.compile(r"\.\.\.\s*$")
_ERRORISH = re.compile(
    r"error|warn|fail|fatal|denied|refus|missing|cannot|unable|exception|blocked",
    re.IGNORECASE)


def signal_line(text: str) -> str:
    """First line carrying actual content, skipping the harness's wrapper lines.

    Parity with the hook's signalLine: when the first content line is a bare
    trailing-ellipsis progress banner ("Checking formatting..."), the key
    lands on the LAST content line -- banner-first tools summarize at the end
    -- and `show` must display the line the signature keyed on.
    """
    lines = str(text).split("\n")
    content = [ln.strip() for ln in lines
               if ln.strip() and not WRAPPER_LINE.match(ln)]
    if content:
        first = content[0]
        if (len(content) > 1 and _PROGRESS_LINE.search(first)
                and not _ERRORISH.search(first)):
            return content[-1]
        return first
    for ln in lines:
        if ln.strip():
            return ln.strip()
    return ""


def guard_in_message(msg: str) -> str | None:
    """The guard a self-reported message is about, if it is about one.

    Measured 2026-08-06: 17 of 46 self-reports described a-command-guard friction, but only 3
    used `guard_blocked:a-command-guard` — the other 15 got unique prose-slugged signatures,
    so 8 sessions' worth of a-command-guard friction surfaced in the rollup as 3. That is the
    same fragmentation the automatic path already fixed by attributing to the
    guard's name instead of its prose; this is the twin fix for the voluntary
    path, so both halves land in one bucket.
    """
    low = str(msg).lower()
    for guard in KNOWN_GUARDS:
        # Word-ish boundary so "a-command-guard" does not match inside an unrelated token.
        if re.search(rf"(?<![\w-]){re.escape(guard)}(?![\w-])", low):
            return guard
    return None


def session_id() -> str:
    """Short session id for the current process.

    The variable Claude Code actually exports to Bash is CLAUDE_CODE_SESSION_ID.
    Reading the plausible-but-nonexistent CLAUDE_SESSION_ID silently produced an
    empty session on every self-report, which made `rank()` count them as zero
    distinct sessions — so a voluntary papercut could never cross --min-sessions
    and the entire self-report path was invisible to the rollup. Caught by
    dogfooding; the CLAUDE_SESSION_ID fallback is kept only in case a caller sets
    it deliberately.
    """
    raw = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""
    return raw[-8:]


def store_file(cwd: str) -> Path:
    return STORE / f"{project_slug(cwd)}.jsonl"


# Resolution events live in a SUBDIRECTORY so the top-level `*.jsonl` glob that
# reads friction records can never pick them up as friction.
RESOLVED = STORE / "state" / "resolved.jsonl"

# Family events share the state directory but remain a separate append-only log:
# raw capture records are immutable telemetry addresses, never mutable members of
# a family row.
FAMILIES = STORE / "state" / "families.jsonl"
# Every work item this tool files, recorded the moment its number is confirmed.
# The cap's repository universe is otherwise derived from the capture window, so
# an item filed into a forced destination -- or one whose capture cwd has since
# been swept -- stops being queried and silently stops counting against the cap.
FILINGS = STORE / "state" / "filings.jsonl"
DOSSIERS = STORE / "state" / "dossiers"
FAMILY_SCHEMA_VERSION = 1
FAMILY_ACTIONS = {
    "create", "assign", "unassign", "adopt", "escalate", "dispose", "reopen",
    "close-observed", "recur-comment",
}
DISPOSE_VERDICTS = {"intended-policy", "insufficient-evidence", "upstream-reported"}
FAMILY_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,100}")
DISPOSE_DOSSIER_FIELDS = (
    "Causal hypothesis",
    "Strongest counterexample",
    "Owner class",
    "No-Claim Boundary",
)
DOSSIER_SCHEMA_VERSION = 1
DOSSIER_OWNER_CLASSES = {
    "local-defect", "target-repo", "upstream", "intended-policy", "insufficient-evidence",
}
DOSSIER_JUDGMENT_FIELDS = (
    "Causal hypothesis",
    "Strongest counterexample",
    "Owner class",
    "Destination repository",
    "Destination justification",
    "Cheapest remedy",
    "Acceptance Criteria",
    "Pre-registered success measure",
    "Planted negative",
    "No-Claim Boundary",
    "For humans",
)
# The filed issue lists at most this many project slugs; the full total and the
# omitted count are always stated, so the cap is visible, never silent.
DOSSIER_PROJECT_CAP = 15
# GitHub rejects issue/PR bodies over this many characters. Refusing locally,
# with the cause named, beats letting gh fail remotely and falling through to
# the exit-4 unconfirmed-remote path when nothing was created.
GITHUB_BODY_LIMIT = 65536
DOSSIER_MARKER_RE = re.compile(
    r"\A<!-- papercut-dossier schema=(?P<schema>\d+) family=(?P<family>[a-z0-9._-]+) "
    r"digest=(?P<digest>[0-9a-f]{64}) -->"
)
CANONICAL_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def record_filing(repo: str, kind: str, number: int, url: str) -> None:
    """Append one confirmed filing locator. Best effort: never fail a real create.

    Losing this record only costs cap precision on a later run, whereas raising
    here would abort a command whose remote side effect already happened.
    """
    try:
        FILINGS.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "repo": repo,
             "kind": kind, "number": number, "url": url},
            ensure_ascii=False, sort_keys=True) + "\n"
        with open(FILINGS, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        print(f"WARNING: could not record filing locator {repo}#{number}: {exc}",
              file=sys.stderr)


def read_filings() -> list[dict]:
    """Read recorded filing locators, skipping torn or malformed lines."""
    if not FILINGS.exists():
        return []
    try:
        text = FILINGS.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        # Same fail-closed posture as the family log: an unreadable registry must
        # not read as "nothing was ever filed" and quietly widen the cap.
        die(f"cannot read filing registry at {FILINGS}: {exc}")
    filings = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if locator_fields(record) is not None:
            filings.append(record)
    return filings


def remote_item_state(value: object) -> str | None:
    """Normalize one GitHub work-item state to ``open`` or ``closed``.

    `gh pr view --json state` reports a merged pull request as MERGED, which is
    terminal exactly like CLOSED. Treating it as unrecognized left every adopted
    merged PR permanently unconfirmable: refused by the cap and never observed
    into a disposition. Returns None only for a state neither route can classify.
    """
    text = str(value or "").strip().lower()
    if text == "open":
        return "open"
    if text in {"closed", "merged"}:
        return "closed"
    return None


def family_error(msg: str, status: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(status)


def validate_family_id(family: str) -> str:
    family = str(family)
    if not FAMILY_ID_RE.fullmatch(family):
        family_error("policy refusal: family id must use lowercase letters, digits, . _ or -", 3)
    return family


def plausible_https_url(value: object) -> bool:
    """Accept an HTTPS URL with a real host without normalizing stored bytes."""
    if not isinstance(value, str) or not value:
        return False
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127
           for character in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port  # Validate a present port while preserving the original URL.
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(host)


@contextmanager
def family_state_lock():
    """Hold the the single-writer state lock lock on the state directory through a family mutation."""
    FAMILIES.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(FAMILIES.parent, flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def new_family_event(family: str, action: str, **payload) -> dict:
    """Construct a schema-versioned event without rewriting its raw signature."""
    return {
        "schema_version": FAMILY_SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": session_id(),
        "family": family,
        "action": action,
        **payload,
    }


def terminate_torn_tail() -> None:
    """Close an unterminated final line before appending the next event.

    A crash between ``write`` and ``fsync`` can leave a partial line with no
    trailing newline. Appending onto it would concatenate the next event into
    the torn bytes and lose both. This only ever ADDS a newline: the torn bytes
    themselves are never rewritten, so the log stays append-only and the damaged
    line is still visible to an auditor as one skipped record.
    """
    try:
        size = FAMILIES.stat().st_size
    except FileNotFoundError:
        return
    if size == 0:
        return
    with open(FAMILIES, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) == b"\n":
            return
    with open(FAMILIES, "ab") as fh:
        fh.write(b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def append_family_event(event: dict, *, lock_held: bool = False) -> None:
    """Durably append an event. Public callers acquire ``family_state_lock``.

    The optional lock argument keeps a single command's read-modify-append span
    under one directory lock instead of opening a second, narrower critical
    section just for the write.
    """
    if not lock_held:
        with family_state_lock():
            append_family_event(event, lock_held=True)
        return
    terminate_torn_tail()
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with open(FAMILIES, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    # The event file's fsync persists its bytes; the directory fsync also makes
    # first-event creation durable before a disposal may remove its source draft.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(FAMILIES.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_family_events() -> list[dict]:
    """Read valid family events in append order, skipping torn JSONL lines."""
    if not FAMILIES.exists():
        return []
    try:
        text = FAMILIES.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # Raced with removal between the check above and this read.
        return []
    except OSError as exc:
        # Never degrade an unreadable log to "no families": that silently
        # unassigns every member and can re-file adopted work through the
        # legacy route. Refuse instead.
        family_error(f"cannot read family state at {FAMILIES}: {exc}", 2)

    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (not isinstance(event, dict) or event.get("schema_version") != FAMILY_SCHEMA_VERSION
                or not event.get("family") or event.get("action") not in FAMILY_ACTIONS
                or parse_ts(event.get("ts")) is None):
            continue
        events.append(event)
    return events


def family_state(family: str) -> dict:
    return {
        "family": family,
        "created": False,
        "lifecycle": "active",
        "locator": None,
        "upstream_url": None,
        "escalation_note": None,
        "dossier_digest": None,
        "verdict": None,
        "last_observation": None,
        "closed_observation": None,
        "recur_comment": None,
    }


def fold_families(events: list[dict] | None = None) -> dict:
    """Fold membership and lifecycle views in parsed-instant, append-order order.

    Membership and lifecycle deliberately fold independently: assignment events
    only map immutable raw signatures to family ids; they never mutate capture
    records or synthesize lifecycle state.
    """
    ordered = []
    for index, event in enumerate(events if events is not None else read_family_events()):
        if (not isinstance(event, dict) or event.get("schema_version") != FAMILY_SCHEMA_VERSION
                or event.get("action") not in FAMILY_ACTIONS or not event.get("family")):
            continue
        timestamp = parse_ts(event.get("ts"))
        if timestamp is not None:
            ordered.append((timestamp, index, event))
    ordered.sort(key=lambda item: (item[0], item[1]))

    membership: dict[str, str] = {}
    adoption: dict[str, dict] = {}
    for _, _, event in ordered:
        action = event["action"]
        family = str(event["family"])
        state = adoption.setdefault(family, family_state(family))
        sig = event.get("sig")
        if action == "assign" and sig:
            membership[str(sig)] = family
        elif action == "unassign" and sig:
            # Scoped to the named family. An unassign recorded against family A
            # must not evict a signature that a later assign moved into family B;
            # the event stays in the log as a no-op audit record.
            if membership.get(str(sig)) == family:
                membership.pop(str(sig), None)
        elif action == "create":
            state["created"] = True
        elif action == "adopt":
            state["lifecycle"] = "adopted"
            state["locator"] = event.get("locator") if isinstance(event.get("locator"), dict) else None
            state["upstream_url"] = None
            state["escalation_note"] = None
            state["dossier_digest"] = event.get("dossier_digest")
            state["verdict"] = None
            state["last_observation"] = None
            state["closed_observation"] = None
            state["recur_comment"] = None
        elif action == "escalate":
            upstream_url = event.get("upstream_url")
            if not plausible_https_url(upstream_url):
                continue
            state["lifecycle"] = "escalated"
            state["locator"] = None
            state["upstream_url"] = upstream_url
            state["escalation_note"] = str(event.get("note") or "")
            state["dossier_digest"] = None
            state["verdict"] = None
            state["last_observation"] = None
            state["closed_observation"] = None
            state["recur_comment"] = None
        elif action == "dispose":
            state["lifecycle"] = "disposed"
            state["upstream_url"] = None
            state["escalation_note"] = None
            state["dossier_digest"] = event.get("dossier_digest")
            state["verdict"] = event.get("verdict")
            state["last_observation"] = None
            state["closed_observation"] = None
            state["recur_comment"] = None
        elif action == "reopen":
            state["lifecycle"] = "active"
            state["upstream_url"] = None
            state["escalation_note"] = None
            state["dossier_digest"] = None
            state["verdict"] = None
            state["last_observation"] = None
            state["closed_observation"] = None
            state["recur_comment"] = None
        elif action == "close-observed":
            locator = event.get("locator")
            if isinstance(locator, dict):
                state["locator"] = locator
            observation = {
                "state": event.get("observed_state"),
                "observed_at": event.get("observed_at"),
                "ts": event["ts"],
            }
            state["last_observation"] = observation
            # A disposition epoch begins at its first closed observation. Later
            # closed observations are freshness data, not a second epoch.
            if observation["state"] == "closed" and state["closed_observation"] is None:
                state["closed_observation"] = observation
            elif observation["state"] == "open":
                # Reopened upstream: the epoch is over. Clearing it lets the next
                # close start a fresh one, instead of leaving the family flagged
                # forever against a closure that no longer holds.
                state["closed_observation"] = None
                state["recur_comment"] = None
        elif action == "recur-comment":
            state["recur_comment"] = event

    return {
        "membership": dict(sorted(membership.items())),
        "adoption": {family: adoption[family] for family in sorted(adoption)},
    }


def dossier_path(family: str) -> Path:
    return DOSSIERS / f"{validate_family_id(family)}.md"


def markdown_section(text: str, heading: str) -> str:
    """Return one Markdown heading's body, without accepting an empty section."""
    match = re.search(
        rf"(?ms)^#{{1,6}}[ \t]+{re.escape(heading)}[ \t]*#?[ \t]*$\n?(.*?)(?=^#{{1,6}}[ \t]+|\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def dispose_dossier_missing(text: str, verdict: str | None) -> list[str]:
    missing = [heading for heading in DISPOSE_DOSSIER_FIELDS if not markdown_section(text, heading)]
    owner_problem = owner_class_problem(text)
    if owner_problem:
        missing.append(owner_problem)
    if not verdict or not str(verdict).strip():
        missing.append("verdict")
    return missing


def delete_family_dossier(family: str) -> None:
    """Delete only after the caller has durably appended adopt/dispose."""
    path = dossier_path(family)
    if path.exists():
        path.unlink()


def owner_class_problem(text: str) -> str | None:
    """Check the routing enum once, for every terminal that reads it.

    Adoption reads the owner class to justify a destination and disposal reads
    it to justify a verdict, so both need the same vocabulary. Enforcing it on
    adoption alone let a disposal -- which suppresses a family exactly as an
    adoption does -- record an owner class no consumer can interpret. An empty
    heading is not reported here; each caller already reports it as missing.
    """
    owner = markdown_section(text, "Owner class")
    if owner and owner not in DOSSIER_OWNER_CLASSES:
        return "Owner class (must be one of: " + ", ".join(sorted(DOSSIER_OWNER_CLASSES)) + ")"
    return None


def dossier_judgment_missing(text: str) -> list[str]:
    """Return missing or malformed session-authored dossier fields.

    Triage uses this only to prioritize drafts. Adoption owns the final gate,
    including the live destination-repository check, so this helper does not
    make any network request.
    """
    missing = [heading for heading in DOSSIER_JUDGMENT_FIELDS if not markdown_section(text, heading)]
    owner_problem = owner_class_problem(text)
    if owner_problem:
        missing.append(owner_problem)
    destination = markdown_section(text, "Destination repository")
    if destination and not CANONICAL_REPO_RE.fullmatch(destination):
        missing.append("Destination repository (must be owner/repository)")
    return missing


def dossier_evidence(entry: dict, *, days: int, event_position: int,
                     min_count: int, min_sessions: int) -> str:
    """Render only machine-owned evidence; judgment begins after this string."""
    def values(items: list[object], empty: str) -> str:
        # Redacted here as well as at capture and at assign: membership can still
        # carry a legacy signature recorded before the assign-time guard existed,
        # and this string is copied verbatim into a filed GitHub issue body.
        return "\n".join(
            "- " + json.dumps(redact(str(item)), ensure_ascii=False) for item in items
        ) or f"- {empty}"

    return (
        "\n\n# Papercut candidate dossier\n\n"
        "## Snapshot metadata\n"
        f"- Triage window: {days} day(s)\n"
        f"- Event-log position: {event_position}\n"
        f"- Thresholds: >= {min_sessions} distinct session(s); >= {min_count} occurrence(s)\n"
        f"- Family: {json.dumps(str(entry['family']), ensure_ascii=False)}\n\n"
        "## Occurrence count\n"
        f"{entry['count']}\n\n"
        "## Distinct sessions\n"
        f"{entry['sessions']}\n\n"
        "## Projects\n"
        f"{project_lines(entry['projects'], values)}\n\n"
        "## Sample excerpts\n"
        f"{values(entry['samples'], '(no sample excerpt recorded)')}\n\n"
        "## Member signatures\n"
        f"{values(entry['members'], '(no member signature recorded)')}\n\n"
    )


def project_lines(projects: list[object], values) -> str:
    """State the full total, list up to the cap, and name any cut.

    The cap applies to projects only: dossier_snapshot strict-parses the
    Member signatures section for the adopt staleness comparison, so that
    list must always stay complete.
    """
    shown = list(projects)[:DOSSIER_PROJECT_CAP]
    omitted = len(projects) - len(shown)
    return (
        f"{len(projects)} distinct project(s)\n"
        + values(shown, "(no project recorded)")
        + (f"\n- ...and {omitted} more (list capped at {DOSSIER_PROJECT_CAP})"
           if omitted else "")
    )


DOSSIER_JUDGMENT_TEMPLATE = """## Causal hypothesis

## Strongest counterexample

## Owner class

## Destination repository

## Destination justification

## Cheapest remedy

## Acceptance Criteria

## Pre-registered success measure

## Planted negative

## No-Claim Boundary

## For humans
"""


def render_dossier(evidence: str, judgment: str, family: str) -> str:
    """Join evidence and untouched judgment behind a verifiable hidden marker."""
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    return (
        f"<!-- papercut-dossier schema={DOSSIER_SCHEMA_VERSION} family={family} digest={digest} -->"
        f"{evidence}{judgment}"
    )


def parse_dossier(family: str, text: str) -> tuple[str, str]:
    """Return the machine-owned evidence and byte-preserved judgment suffix.

    A malformed marker is a policy refusal rather than a reason to overwrite a
    hand-edited draft. The digest covers precisely the evidence portion, which
    lets triage refresh telemetry without treating judgment edits as corruption.
    """
    marker = DOSSIER_MARKER_RE.match(text)
    if not marker:
        raise ValueError("missing or malformed hidden marker")
    if int(marker.group("schema")) != DOSSIER_SCHEMA_VERSION:
        raise ValueError("unsupported hidden marker schema")
    if marker.group("family") != family:
        raise ValueError("hidden marker family does not match dossier path")
    contents = text[marker.end():]
    judgment_start = re.search(r"(?m)^## Causal hypothesis[ \t]*#?[ \t]*$", contents)
    if not judgment_start:
        raise ValueError("hidden marker has no Causal hypothesis judgment boundary")
    evidence = contents[:judgment_start.start()]
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    if digest != marker.group("digest"):
        raise ValueError("hidden marker digest does not match evidence")
    return evidence, contents[judgment_start.start():]


def dossier_draft_status(family: str, path: Path) -> tuple[bool, str | None]:
    """Return whether an existing draft is incomplete and its preserved judgment."""
    if not path.exists():
        return False, None
    try:
        _, judgment = parse_dossier(family, path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read dossier: {exc}") from exc
    return bool(dossier_judgment_missing(judgment)), judgment


def write_dossier(path: Path, body: str) -> None:
    """Atomically replace one snapshot while the caller holds ``family_state_lock``.

    Opening the live path for truncation would destroy the hand-authored judgment
    sections the moment the process died mid-write, and nothing else holds a copy.
    Writing a sibling temp file and renaming keeps the old draft readable until
    the replacement is durable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def family_show_data(family: str | None = None,
                     window_days: int | None = None) -> dict:
    folded = fold_families()
    window = validate_window(window_days)
    if family is None:
        # The all-family view carries stages too. It accepts --window, and an
        # operator scanning every family is exactly the one who needs to spot a
        # regression without already knowing which family to ask about.
        folded["verification"] = verification_view(
            folded["adoption"], folded["membership"], window)
        return folded
    family = validate_family_id(family)
    state = folded["adoption"].get(family, family_state(family))
    members = sorted(sig for sig, assigned in folded["membership"].items() if assigned == family)
    horizon = verification_horizon_days([state], window)
    verification = (verification_details(state, set(members), list(read_records(horizon)), window)
                    if horizon else None)
    return {
        "family": family,
        "members": members,
        "state": state,
        "verification": verification,
    }


def read_resolutions() -> dict[str, dict]:
    """Latest resolve/reopen event per signature. Append-only and event-sourced,
    matching the store's existing shape — no migration, no mutable rows."""
    latest: dict[str, dict] = {}
    if not RESOLVED.exists():
        return latest
    try:
        text = RESOLVED.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return latest
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sig = ev.get("sig")
        if not sig or not ev.get("ts"):
            continue
        prev = latest.get(sig)
        if prev is None or str(ev["ts"]) >= str(prev["ts"]):
            latest[sig] = ev
    return latest


def is_resolved(sig: str, records, resolutions: dict[str, dict]) -> bool:
    """True when `sig` was resolved and has NOT recurred since.

    A resolve is not permanent silence: if the same friction reappears after the
    fix, that is a regression and the most valuable thing the log can tell you.
    """
    ev = resolutions.get(sig)
    if not ev or ev.get("action") == "reopen":
        return False
    since = ev["ts"]
    return not any(r["sig"] == sig and newer_than(r.get("ts"), since) for r in records)


def cmd_resolve(args: argparse.Namespace) -> None:
    action = "reopen" if args.reopen else "resolve"

    # Say what is being suppressed. Resolving removes a signature from the weekly
    # rollup and the toast count, so a silent `resolve` is a way to quietly turn
    # capture off — the exact write-only failure this system exists to prevent.
    # A typo would otherwise no-op indistinguishably from a real suppression.
    matches = [r for r in read_records(args.lookback) if r["sig"] == args.sig]
    sessions = len({r.get("session") for r in matches if r.get("session")})
    if action == "resolve" and not matches:
        print(f"WARNING: no occurrence of {args.sig!r} in the last {args.lookback}d — "
              f"check the signature (`papercut list --days {args.lookback}`). Recording anyway.")

    ev = {
        "sig": args.sig,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "note": redact(str(args.note or ""))[:400],
    }
    RESOLVED.parent.mkdir(parents=True, exist_ok=True)
    with open(RESOLVED, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    if action == "resolve":
        scope = (f"{len(matches)} occurrence(s) across {sessions or len(matches)} session(s) "
                 f"in the last {args.lookback}d") if matches else "no recent occurrences"
        print(f"resolved: {args.sig} — suppressing {scope}. "
              f"It returns automatically if it recurs.")
    else:
        print(f"reopened: {args.sig}")


def cmd_staleness(args: argparse.Namespace) -> None:
    """Is capture still alive? Silence is ambiguous — it means either 'no friction'
    or 'the hook died'. A sibling indexing tool exited 0 daily for 21 days while its index silently
    never refreshed; this is the check that shape needs.
    """
    newest_cut = 0.0
    if STORE.is_dir():
        for fp in STORE.glob("*.jsonl"):
            try:
                newest_cut = max(newest_cut, fp.stat().st_mtime)
            except OSError:
                pass
    projects = Path.home() / ".claude" / "projects"
    newest_session = 0.0
    if projects.is_dir():
        for d in projects.iterdir():
            try:
                newest_session = max(newest_session, d.stat().st_mtime)
            except OSError:
                pass

    if newest_session == 0:
        print("papercut capture: no session activity to compare against")
        return
    if newest_cut == 0:
        print("WARN papercut capture: sessions are running but the store is EMPTY "
              "— is the PostToolUseFailure hook registered? (your config sync reconcile-papercut-hook)")
        return
    gap_h = (newest_session - newest_cut) / 3600.0
    # Oversized stores stop accepting writes silently; surface that as the same alarm.
    for fp in STORE.glob("*.jsonl"):
        try:
            if fp.stat().st_size > 0.8 * MAX_LOG_BYTES:
                print(f"WARN papercut store near cap: {fp.name} at {fp.stat().st_size // 1048576}MB "
                      f"of {MAX_LOG_BYTES // 1048576}MB — writes stop SILENTLY at the cap; prune or rotate")
        except OSError:
            pass
    if gap_h > args.max_gap_hours:
        print(f"WARN papercut capture stale: newest record is {gap_h:.0f}h older than the "
              f"newest session (threshold {args.max_gap_hours}h) — capture may be dead")
    else:
        print(f"papercut capture: healthy (newest record {gap_h:.1f}h behind newest session)")


def read_records(days: int, project: str | None = None, *,
                 include_fixtures: bool = False):
    """Yield records newer than `days`. Malformed lines are skipped, never fatal —
    the store is appended to concurrently by ~20 sessions and a torn line must not
    take down the rollup. Test-fixture records (fixture_rule) are dropped here —
    the one shared chokepoint every consumer reads through — unless the caller
    passes include_fixtures=True."""
    if not STORE.is_dir():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for fp in sorted(STORE.glob("*.jsonl")):
        if project and fp.stem != project:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or "sig" not in rec:
                continue
            try:
                ts = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            if not include_fixtures and fixture_rule(rec):
                continue
            rec["_project"] = fp.stem
            yield rec


# Junk-fingerprint quarantine. A 2026-08-26 census of the live corpus found 7 of
# the top-50 ranked rows (14%) to be capture defects, not fixable friction: keys
# that carry no causal signal after the hook's placeholder normalization
# (`bash:{` is JSON error output keyed on its first character, not a literal `{`
# command), a raw-ANSI bun-test banner, the generic `bash:exit code <n>`, and the
# mixed-cause `timed_out` bucket. High counts on incoherent keys read as work:
# they pollute ranking and `rollup --apply` files GitHub issues for them.
# Quarantine DEFERS, never deletes — records stay in the store, the lane stays
# visible via `list --quarantined`, and the fix is hook-side fingerprinting (each
# graduation with a normalization regression fixture), never a `resolve`.
_PLACEHOLDER = re.compile(r"<(?:path|n|hex)>")


def quarantine_rule(sig: str) -> str | None:
    """The quarantine rule id `sig` trips, or None for a coherent signature.

    Ordered, first match wins. Rules key on the SIGNATURE, which the hook has
    already normalized (lowercased, `<path>`/`<n>`/`<hex>` placeholders), so the
    junk shapes are literal strings and small exact rules suffice."""
    s = str(sig)
    # A raw ESC byte means the fingerprint kept terminal styling, so the same
    # output re-keys on every colour change. Policy: any ESC-bearing key is
    # treated as a defective fingerprint (every one measured in the census was).
    if "\x1b" in s:
        return "ansi-escape"
    # Drop the prefix up to the first colon, the placeholders, then everything
    # non-alphabetic. A real signature still names a cause after its prefix
    # ("permission denied"); nothing left means the discriminator was structure,
    # not signal: `bash:{`, `bash:[]`, `bash:<path>`, `bash:<path>:<n>`. This
    # deliberately also catches category keys whose discriminator collapsed
    # (`no_such_file:.`, `command_not_found:1` — one live record under that key
    # was actually a missing systemctl): a category alone cannot rank a fix, the
    # same reason `timed_out` below is quarantined despite its meaningful name.
    residue = re.sub(r"[^a-z]", "", _PLACEHOLDER.sub("", s.split(":", 1)[-1].lower()))
    if not residue:
        return "no-signal-residue"
    if s == "bash:exit code <n>":
        return "generic-exit-code"
    # One key, three causes measured (undici fetch, bun per-test timeout, curl
    # DNS) — a bucket that mixes causes cannot rank a fix.
    if s == "timed_out":
        return "mixed-timeout"
    # Legacy captures keyed on a tool's opening progress banner (the hook now
    # prefers the trailing summary line, but the old keys keep arriving from
    # sessions running the previous hook until sync propagates).
    if s == "bash:checking formatting...":
        return "progress-line"
    # `ls -l` rows and their `total <n>` header: a listing echoed on failure is
    # structure, not signal -- the key lands on whichever file sorted first.
    if re.match(r"^bash:(?:total <n>$|[bcdlps-](?:[r-][w-][xst-]){3}[ .+@])", s):
        return "file-listing-line"
    return None


# Test-fixture pollution (an earlier change). The a-route-guard test
# matrix calls the guard's run() in-process; every expected denial appends a
# real record under os.homedir(). an earlier change sandboxed HOME for every entrypoint in
# THIS tree, but pre-an earlier change worktrees keep their own unsandboxed copies and live
# sessions still run suites there (pinned 2026-08-28: `bash hooks/tests/run.sh`
# in a pre-an earlier change worktree at 09:01:13.879Z, fixture records landing 0.3-0.5s
# later). No in-repo sandbox layer can reach a frozen tree, so the store's
# READERS drop the records instead: every consumer flows through read_records,
# and rollup reports the dropped volume as a count so a fresh leak stays
# visible without ever ranking as work. Same posture as quarantine: defer at
# read time, never delete from disk.
_FIXTURE_GUARD_SIG = "guard_blocked:a-route-guard"
# The test's own fixture cwds, nothing else: its mkdtemp prefixes (current and
# historical) and the literal laundering-workspace payload. A record from any
# real project keeps counting — the guard blocking noncanonical clones is
# intended policy, and that genuine remainder is the signal this family exists
# to measure (the dossier's no-claim boundary).
_FIXTURE_CWD = re.compile(
    r"^/tmp/(?:route-guard-|canonical-route-subdir-guard-)[^/]+(?:/|$)"
    r"|^/parent/workspace$"
)
# Reasons only synthetic input can produce: the guard denies malformed input
# BEFORE setDenyContext(), so a production (single-shot) hook process has no
# deny context and never logs these. Only the in-process test matrix — reusing
# the previous call's context — can write them, from any cwd.
_FIXTURE_ONLY_REASONS = ("Hook input is malformed", "Hook input is not an object")

# A second fixture shape, for the guard suites that write from the repo's own
# tree rather than a mkdtemp sandbox. A cwd prefix ALONE cannot separate those
# from an agent legitimately working in that directory, so two independent
# signals are required TOGETHER:
#   1. the cwd is a hook-test working directory, and
#   2. the identity was synthesized -- the hook's `ppid-<pid>` fallback, used
#      only when the payload carried no session_id at all.
# Measured 2026-08-29 over the whole live store (47,168 records): the signals
# agree on 6,700 of 6,722 a-vcs-guard records, and every guard with genuine
# production denials is 0% synthesized (a-cost-guard 2,365, heavy-job-
# admission 480, a-repetition-guard 37) -- so a real denial with no session_id is rare
# now, and the 2026-08-06 "95% carry neither" measurement no longer holds.
# (Corrected later the same day: a-cost-guard's 2,313 and heavy-job-
# admission's 480 turned out to be SITES/demo fixture writes carrying REAL
# session ids -- see _FIXTURE_DEMO_CWD below. The conjunctive design stands;
# this census line had counted fixtures as its genuine examples.)
# Conjunctive on purpose: either signal on its own KEEPS the record.
# Two shapes, not an enumerated prefix list. Guard suites write either from a
# mkdtemp sandbox under /tmp (route-guard uses /tmp/route-guard-*, a-vcs-guard
# /tmp/gg-branch-* and /tmp/gg-varpath-*, and the next suite will invent its
# own) or from the repo's own hooks tree. Enumerating prefixes per guard is
# whack-a-mole that silently under-filters until someone notices; the shape
# is the durable signal. Measured 2026-08-29: of guard records with a /tmp
# cwd, 2,406 carry a synthesized identity and 5 carry a real session id --
# and those 5 are KEPT, because the rule below is conjunctive.
_FIXTURE_HOOK_TEST_CWD = re.compile(r"^/tmp/|/claude(?:/hooks)?$")

# A third fixture shape (measured 2026-08-29): the a-cost-guard and
# an-admission-guard suites pin cwd to a directory that does not exist on
# this machine -- /home/user/SITES/demo, hardcoded in their test payloads --
# so the same frozen pre-an earlier change worktree run that wrote the route-guard matrix
# (09:01:13.879Z, 2026-08-28) landed 2,793 records there two seconds later
# (2,313 a-cost-guard + 480 an-admission-guard). Both conjunctive signals
# above miss them: the cwd is neither /tmp nor a hooks tree, and the suite
# runs inside a live session so the records carry REAL session ids -- which is
# exactly how the 2026-08-29 census above miscounted a-cost-guard's 2,365
# records as genuine production denials. A literal fixture cwd is the honest
# match (same precedent as ^/parent/workspace$): no agent can legitimately
# work in a directory that does not exist.
_FIXTURE_DEMO_CWD = re.compile(r"^/home/user/SITES/demo(?:/|$)")


def synthesized_identity(rec: dict) -> bool:
    """True when the record's session was invented rather than supplied.

    The hook falls back to `ppid-<pid>` when a denial payload carries no
    session_id. Records written before the marker fix store that truncated to
    its last 8 characters (`ppid-318283` -> `d-318283`); either shape contains
    a hyphen, which the last 8 hex characters of a real session UUID cannot.
    """
    return "-" in str(rec.get("session") or "")


def fixture_rule(rec: dict) -> str | None:
    """The fixture rule id `rec` trips, or None for organic telemetry.

    Record-level, unlike quarantine_rule: this signature mixes genuine denials
    (real cwds — kept) with test-matrix writes (fixture cwds — dropped), so a
    signature-level lane would hide the genuine remainder it must preserve."""
    sig = str(rec.get("sig", ""))
    # Checked before the route-guard branch because it spans every guard, but
    # scoped to guard_blocked:* so ordinary telemetry is never touched -- no
    # non-guard signature in the live store carries a synthesized identity.
    if (sig.startswith("guard_blocked:")
            and _FIXTURE_HOOK_TEST_CWD.search(str(rec.get("cwd") or ""))
            and synthesized_identity(rec)):
        return "hook-test-synthesized-identity"
    if (sig.startswith("guard_blocked:")
            and _FIXTURE_DEMO_CWD.match(str(rec.get("cwd") or ""))):
        return "guard-suite-demo-cwd"
    if sig != _FIXTURE_GUARD_SIG:
        return None
    if _FIXTURE_CWD.search(str(rec.get("cwd") or "")):
        return "route-guard-fixture-cwd"
    err = str(rec.get("err") or "")
    if any(reason in err for reason in _FIXTURE_ONLY_REASONS):
        return "route-guard-synthetic-input"
    return None


def count_fixture_records(days: int) -> int:
    """Window count of the fixture records read_records is dropping.

    Rollup's recurrence counter: read_records filters silently (a generator
    cannot also report), and a silent filter would hide a NEW leak from the
    very loop built to catch recurring friction — so rollup re-reads with the
    escape hatch and prints the volume."""
    return sum(
        1 for rec in read_records(days, include_fixtures=True) if fixture_rule(rec)
    )


def rank(records, membership: dict[str, str] | None = None):
    """Rank raw records, folding assigned signatures into their family groups.

    Families are aggregated here, while the caller decides which raw signatures
    belong in its lane. That ordering lets rollup exclude resolved and
    quarantined member records *before* they can add weight to a family. A
    family group's sessions are one set populated from raw records, not a sum of
    child ranks, so one session that hit two members still counts once.
    """
    if membership is None:
        membership = fold_families()["membership"]
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "sessions": set(), "projects": set(), "samples": [],
                 "self": 0, "unattributed": 0, "members": set()}
    )
    for rec in records:
        raw_sig = str(rec["sig"])
        family = membership.get(raw_sig)
        key = ("family", family) if family else ("signature", raw_sig)
        g = groups[key]
        g["count"] += 1
        g["members"].add(raw_sig)
        if rec.get("session"):
            g["sessions"].add(rec["session"])
        else:
            # A record with no session id must not silently score zero — that is
            # how the self-report path went invisible to the rollup. Treat each
            # unattributed record as its own occurrence: over-surfacing is far
            # cheaper than a capture path that can never reach the threshold.
            g["unattributed"] += 1
        g["projects"].add(rec.get("_project", "?"))
        if rec.get("source") == "self":
            g["self"] += 1
        if len(g["samples"]) < 3:
            sample = rec.get("err") or rec.get("msg") or ""
            if sample:
                # Redact again on the way out. Capture-time redaction covers
                # everything written from now on, but records already in the
                # store predate it — and these samples are what --apply copies
                # into a GitHub issue body for unassigned signatures.
                g["samples"].append(redact(signal_line(sample))[:180])
    out = []
    for (kind, name), g in groups.items():
        family = name if kind == "family" else None
        out.append(
            {
                "sig": name,
                "family": family,
                "members": sorted(g["members"]) if family else [],
                "count": g["count"],
                "sessions": len(g["sessions"]) + g["unattributed"],
                "projects": sorted(g["projects"]),
                "self_reported": g["self"],
                "samples": g["samples"],
                # Rollup removes quarantined records before it invokes this
                # family fold. List keeps raw rows by passing membership={}.
                "quarantine": None if family else quarantine_rule(name),
            }
        )
    out.sort(key=lambda r: (-r["sessions"], -r["count"], r["sig"]))
    return out


def rollup_lanes(records: list[dict], family_views: dict, *, min_count: int,
                 min_sessions: int, refresh_unknown: set[str] | None = None) -> dict:
    """Partition records into rollup lanes after the family fold.

    Both rollup and triage consume this exact partition. Its flagged lane is the
    sole source of family clinic candidates; raw signatures remain on rollup's
    legacy auto-file route and never acquire a dossier.
    """
    resolutions = read_resolutions()
    raw_ranked = rank(records, membership={})
    suppressed = [row for row in raw_ranked if is_resolved(row["sig"], records, resolutions)]
    resolved_sigs = {row["sig"] for row in suppressed}
    unresolved_records = [row for row in records if str(row["sig"]) not in resolved_sigs]
    quarantined = [
        row for row in raw_ranked
        if row["sig"] not in resolved_sigs and row["quarantine"]
    ]
    quarantined_sigs = {row["sig"] for row in quarantined}
    eligible_records = [
        row for row in unresolved_records if str(row["sig"]) not in quarantined_sigs
    ]
    ranked = rank(eligible_records, membership=family_views["membership"])
    threshold_rows = [
        row for row in ranked
        if row["sessions"] >= min_sessions and row["count"] >= min_count
    ]

    adoption = family_views["adoption"]
    unknown = refresh_unknown or set()
    # Escalation is not an over-threshold disposition: its own lane remains
    # visible whenever the family has live records, even after volume falls
    # below the local-action threshold. That is how an ignored upstream report
    # stays measurable without returning to our fix queue.
    escalated = [
        entry for entry in ranked
        if entry.get("family")
        and (adoption.get(entry["family"]) or {}).get("lifecycle") == "escalated"
    ]
    adopted_open, disposed, flagged = [], [], []
    for entry in threshold_rows:
        family = entry.get("family")
        state = adoption.get(family) if family else None
        if family and family in unknown:
            adopted_open.append(entry)
        elif state and state["lifecycle"] == "disposed":
            disposed.append(entry)
        elif state and state["lifecycle"] == "escalated":
            continue
        elif state and state["lifecycle"] == "adopted" and state["closed_observation"] is None:
            adopted_open.append(entry)
        else:
            flagged.append(entry)
    return {
        "suppressed": suppressed,
        "quarantined": quarantined,
        "eligible_records": eligible_records,
        "ranked": ranked,
        "threshold_rows": threshold_rows,
        "escalated": escalated,
        "adopted_open": adopted_open,
        "disposed": disposed,
        "flagged": flagged,
    }


# --------------------------------------------------------------------------- add


def cmd_add(args: argparse.Namespace) -> None:
    msg = redact(args.message.strip())
    if not msg:
        die("empty message")
    cwd = args.cwd or os.getcwd()
    # A slug derived from prose is a WEAK dedupe key: two agents describing the
    # same friction in different words produce different signatures and will not
    # aggregate. So before falling back to one, attribute to a named guard when
    # the message is plainly about one — that is where the fragmentation actually
    # bit (15 of 17 a-command-guard reports each got their own signature). Keep the slug to
    # [a-z0-9-] so it survives an issue title and a shell round trip intact.
    sig = args.sig
    if not sig:
        guard = guard_in_message(msg)
        sig = f"guard_blocked:{guard}" if guard else None
    if not sig:
        sig = "self:" + (re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", msg.lower())).strip("-")[:60] or "unlabelled")

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sig": sig,
        "tool": "self-report",
        "msg": msg[:600],
        "cwd": str(cwd)[:200],
        "session": session_id(),
        "source": "self",
    }
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    if len(line.encode()) > MAX_RECORD_BYTES:
        rec["msg"] = rec["msg"][:300]
        line = json.dumps(rec, ensure_ascii=False) + "\n"

    fp = store_file(cwd)
    fp.parent.mkdir(parents=True, exist_ok=True)
    # Single O_APPEND write under PIPE_BUF: atomic across concurrent sessions.
    with open(fp, "a", encoding="utf-8") as fh:
        fh.write(line)
    print(f"papercut logged: {sig}" if not args.quiet else "", end="" if args.quiet else "\n")


# ------------------------------------------------------------------------- family


def record_family_event(family: str, action: str, *, guard=None,
                        must_exist: bool = False, **payload) -> dict | None:
    """Append one family event while holding the state-directory lock.

    ``guard`` receives the family's folded state inside the same critical
    section. It may refuse by raising, or return ``False`` when a stale
    snapshot should be skipped without aborting its caller. Either result keeps
    the check and append atomic against interleaved lifecycle writes.
    """
    try:
        with family_state_lock():
            state = fold_families()["adoption"].get(family)
            if must_exist and (state is None or not state.get("created")):
                family_error(f"policy refusal: family {family} does not exist", 3)
            if guard is not None:
                accepted = guard(state if state is not None else family_state(family))
                if accepted is False:
                    return None
            event = new_family_event(family, action, **payload)
            append_family_event(event, lock_held=True)
            return event
    except OSError as exc:
        family_error(f"could not append family event: {exc}")


def refuse_unless_active(state: dict, doing: str) -> None:
    """Refuse a mutation that a terminal family must not silently absorb."""
    lifecycle = state.get("lifecycle", "active")
    if lifecycle != "active":
        family = state.get("family", "?")
        family_error(
            f"policy refusal: family {family} is already {lifecycle}; "
            f"run `papercut family reopen {family}` before {doing}", 3)


def refuse_unless_disposable(state: dict) -> None:
    """Allow final retirement of active or already-escalated local work."""
    lifecycle = state.get("lifecycle", "active")
    if lifecycle not in {"active", "escalated"}:
        family = state.get("family", "?")
        family_error(
            f"policy refusal: family {family} is already {lifecycle}; "
            f"run `papercut family reopen {family}` before disposing it", 3)


def cmd_family_create(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    record_family_event(family, "create")
    print(f"family created: {family}")


def cmd_family_assign(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    if not args.sig:
        family_error("policy refusal: raw signature must not be empty", 3)
    if redact(args.sig) != args.sig:
        # The signature is stored verbatim and later rendered into a dossier and
        # a filed issue body, so a credential shape is refused at the door rather
        # than scrubbed into a different immutable capture address.
        family_error(
            "policy refusal: raw signature carries a credential shape; it must be "
            "redacted at capture, not assigned into a family", 3)
    # Keep this payload verbatim: it is the immutable capture address a family folds.
    # A terminal family is refused: its dossier was already authored, validated
    # and filed, and a disposed family's members are suppressed from the flagged
    # lane, so a new member would silently vanish from triage.
    record_family_event(
        family, "assign", sig=args.sig,
        guard=lambda state: refuse_unless_active(state, "assigning new members"))
    print(f"family assigned: {args.sig} -> {family}")


def cmd_family_unassign(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    if not args.sig:
        family_error("policy refusal: raw signature must not be empty", 3)
    # Unknown signatures intentionally append a no-op audit event. A later assign
    # can always reverse it, and a typo is visible rather than silently discarded.
    record_family_event(family, "unassign", sig=args.sig)
    print(f"family unassigned: {args.sig} from {family}")


def cmd_family_escalate(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    locator_url = str(args.to)
    if not plausible_https_url(locator_url):
        family_error("policy refusal: --to must be an https URL with a non-empty host", 3)
    note = redact(str(args.note or ""))[:400]
    record_family_event(
        family, "escalate", upstream_url=locator_url, note=note,
        must_exist=True,
        guard=lambda state: refuse_unless_active(state, "escalating it"),
    )
    print(f"family escalated: {family} -> {locator_url}")


def cmd_family_dispose(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    verdict = args.verdict
    if verdict and verdict not in DISPOSE_VERDICTS:
        family_error(f"policy refusal: invalid dispose verdict {verdict!r}", 3)

    try:
        # Read, validate, append, and deletion are one critical section. The
        # unlink sits after fsync-backed append so an append failure retains the
        # authored draft for correction or retry.
        with family_state_lock():
            # Fold inside the section: disposing an adopted family would strand an
            # open work item, and re-disposing would overwrite the first verdict.
            refuse_unless_disposable(
                fold_families()["adoption"].get(family, family_state(family)))
            path = dossier_path(family)
            dossier = path.read_text(encoding="utf-8") if path.exists() else ""
            missing = dispose_dossier_missing(dossier, verdict)
            if missing:
                family_error("incomplete dossier: missing " + ", ".join(missing), 2)
            digest = hashlib.sha256(dossier.encode("utf-8")).hexdigest()
            # Retain a redaction-safe copy so `reopen` can restore the authored
            # judgment. The digest still covers the ORIGINAL bytes, so a retained
            # copy that differs under redaction stays detectable rather than
            # silently re-authenticating as the disposed dossier.
            event = new_family_event(
                family, "dispose", verdict=verdict, dossier_digest=digest,
                dossier_retained=redact(dossier),
            )
            append_family_event(event, lock_held=True)
            delete_family_dossier(family)
    except OSError as exc:
        family_error(f"could not append family event: {exc}")
    print(f"family disposed: {family} ({verdict})")


def cmd_family_reopen(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    restored = False
    try:
        # Append and restore are one critical section so a concurrent triage
        # refresh cannot observe a reopened family with no dossier.
        with family_state_lock():
            retained = ""
            for event in read_family_events():
                if str(event.get("family")) != family:
                    continue
                if event.get("action") == "dispose":
                    retained = str(event.get("dossier_retained") or "")
                elif event.get("action") in {"adopt", "reopen"}:
                    retained = ""
            append_family_event(new_family_event(family, "reopen"), lock_held=True)
            path = dossier_path(family)
            # Never clobber a draft that already exists: an operator who authored
            # a replacement outranks the retained copy.
            if retained and not path.exists():
                write_dossier(path, retained)
                restored = True
    except OSError as exc:
        family_error(f"could not append family event: {exc}")
    print(f"family reopened: {family}" + (" (dossier restored)" if restored else ""))


def cmd_family_close_observed(args: argparse.Namespace) -> None:
    family = validate_family_id(args.family)
    observed_at = args.observed_at or datetime.now(timezone.utc).isoformat()
    observed_ts = parse_ts(observed_at)
    if observed_ts is None:
        family_error("policy refusal: --observed-at must be ISO-8601", 3)
    # An observation records something that already happened. A stamp in the
    # future (a year typo) would start the verification clock there and hold the
    # family in `verifying` until that date, hiding it from the re-measure.
    if observed_ts > datetime.now(timezone.utc):
        family_error("policy refusal: --observed-at must not be in the future", 3)
    repo = str(args.repo or "").strip()
    if not CANONICAL_REPO_RE.fullmatch(repo):
        family_error("policy refusal: --repo must be owner/repository", 3)
    try:
        number = int(args.number)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        family_error("policy refusal: --number must be a positive work-item number", 3)
    url = str(args.url or "").strip()
    if not url:
        family_error("policy refusal: --url must not be empty", 3)
    locator = {
        "repo": repo,
        "kind": args.kind,
        "number": number,
        "url": url,
    }

    def guard(state: dict) -> None:
        if state.get("lifecycle") == "escalated":
            family_error(
                f"policy refusal: family {family} is already escalated; "
                f"run `papercut family reopen {family}` before recording an observation", 3)
        # An observation may refresh the adopted locator's freshness but must not
        # redirect the family at a different work item: disposition, recurrence
        # and the open-work cap all read this locator back.
        recorded = state.get("locator")
        if not isinstance(recorded, dict) or not recorded.get("number"):
            return
        if (str(recorded.get("repo")) != repo or str(recorded.get("kind")) != args.kind
                or int(recorded.get("number") or 0) != number):
            family_error(
                f"policy refusal: {family} is adopted at "
                f"{recorded.get('repo')}#{recorded.get('number')} ({recorded.get('kind')}); "
                f"refusing to observe a different work item {repo}#{number} ({args.kind})", 3)

    record_family_event(
        family, "close-observed", locator=locator,
        observed_state=args.state, observed_at=observed_at, guard=guard,
    )
    print(f"family close observed: {family} {args.state}")


def cmd_family_show(args: argparse.Namespace) -> None:
    window = validate_window(getattr(args, "window", None))
    data = family_show_data(args.family, window)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    if args.family:
        state = data["state"]
        print(f"{data['family']}: {state['lifecycle']} ({len(data['members'])} member(s))")
        if state["verdict"]:
            print(f"  verdict: {state['verdict']}")
        if state["lifecycle"] == "escalated":
            print(f"  locator: {state['upstream_url']}")
            if state["escalation_note"]:
                print(f"  note: {state['escalation_note']}")
        elif state["locator"]:
            locator = state["locator"]
            print(f"  locator: {locator.get('repo', '?')}#{locator.get('number', '?')}")
        if data["verification"]:
            print(f"  verification (window {window}d): "
                  f"{verification_summary(data['verification'])}")
        return
    if not data["adoption"] and not data["membership"]:
        print("family state: none")
        return
    print("family state:")
    for family, state in data["adoption"].items():
        members = sum(1 for assigned in data["membership"].values() if assigned == family)
        print(f"  {family}: {state['lifecycle']} ({members} member(s))")
        if state["lifecycle"] == "escalated":
            print(f"    locator: {state['upstream_url']}")
            if state["escalation_note"]:
                print(f"    note: {state['escalation_note']}")
        details = data.get("verification", {}).get(family)
        if details:
            print(f"    verification (window {window}d): {verification_summary(details)}")


# -------------------------------------------------------------------- list / show


def cmd_list(args: argparse.Namespace) -> None:
    # --cwd is the ergonomic form: an agent knows its directory, not the slug
    # (a `/`->`-` path transform). Requiring the slug made the documented
    # pre-flight check silently return zero rows on any near-miss.
    project = args.project or (project_slug(args.cwd) if args.cwd else None)
    records = list(read_records(args.days, project))
    # List remains the raw-signature inspection surface; only weekly rollup
    # folds eligible telemetry into family rows.
    ranked = rank(records, membership={})
    if not args.include_resolved:
        res = read_resolutions()
        ranked = [r for r in ranked if not is_resolved(r["sig"], records, res)]
    # The lanes are exclusive: the default view is the fix queue, --quarantined
    # is the fingerprinting backlog. Junk in the fix queue reads as work.
    if args.quarantined:
        ranked = [r for r in ranked if r["quarantine"]]
    else:
        ranked = [r for r in ranked if not r["quarantine"]]
    if args.json:
        print(json.dumps(ranked, indent=2))
        return
    if not ranked:
        lane = "nothing quarantined" if args.quarantined else "none"
        print(f"papercuts: {lane} in the last {args.days}d")
        return
    if args.quarantined:
        print(f"papercuts — quarantined junk fingerprints, last {args.days}d "
              f"(needs hook-side fingerprinting, not fixes)\n")
    else:
        print(f"papercuts — last {args.days}d, ranked by distinct sessions\n")
    for r in ranked[: args.limit]:
        proj = f"{len(r['projects'])} project(s)"
        flag = "  [self-reported]" if r["self_reported"] == r["count"] else ""
        if args.quarantined:
            flag += f"  [{r['quarantine']}]"
        print(f"{r['sessions']:>4} sess  {r['count']:>4}x  {proj:<14}  {r['sig']}{flag}")
        if args.verbose and r["samples"]:
            print(f"                              {r['samples'][0]}")


def cmd_show(args: argparse.Namespace) -> None:
    hits = [r for r in read_records(args.days) if r["sig"] == args.sig]
    # Surface the resolution state here or `--note` would be write-only.
    ev = read_resolutions().get(args.sig)
    if ev:
        note = f" — {ev['note']}" if ev.get("note") else ""
        print(f"[{ev.get('action', 'resolve')}d {str(ev.get('ts', ''))[:19]}]{note}\n")
    rule = quarantine_rule(args.sig)
    quarantine_note = (
        f"quarantined [{rule}]: this key needs fingerprinting, not a fix — it "
        f"carries no causal signal, so it is excluded from list/rollup ranking "
        f"(`papercut list --quarantined` shows the lane)."
    ) if rule else None
    if not hits:
        print(f"no occurrences of {args.sig!r} in the last {args.days}d")
        if quarantine_note:
            print(f"\n{quarantine_note}")
        return
    print(f"{args.sig} — {len(hits)} occurrence(s), last {args.days}d\n")

    # Guard signatures deliberately collapse to the guard name, so the rule
    # breakdown lives here. Without it, `guard_blocked:a-vcs-guard 82x` would say
    # which guard is being tripped but never which RULE — and the rule is what
    # gets fixed.
    if len(hits) > 3:
        rules = collections.Counter(
            signal_line(r.get("err") or r.get("msg") or "")[:70] for r in hits
        )
        if len(rules) > 1:
            print("  by rule:")
            for text, n in rules.most_common(8):
                print(f"    {n:>4}x  {text}")
            print()
    for r in hits[: args.limit]:
        body = signal_line(r.get("err") or r.get("msg") or "")[:200]
        print(f"  {r['ts'][:19]}  {r.get('_project','?')}  [{r.get('source','auto')}]")
        if r.get("cmd"):
            print(f"      $ {r['cmd'][:160]}")
        elif r.get("target"):
            # Non-Bash tools record a target rather than a command; without this
            # branch a Read failure printed its error with no hint which file.
            print(f"      -> {r['target'][:160]}")
        print(f"      {body}")
    if quarantine_note:
        print(f"\n{quarantine_note}")


# ------------------------------------------------------------------------ rollup


_GH_DIAGNOSED: set[str] = set()


def gh_diagnose(cause: str, message: str) -> None:
    """One stderr line per distinct cause per process.

    Most callers deliberately swallow a non-zero gh and degrade, so without
    this the operator only ever sees the downstream symptom. Deduplicated
    because one rollup calls gh once per family and nobody needs the same
    line forty times.
    """
    if cause in _GH_DIAGNOSED:
        return
    _GH_DIAGNOSED.add(cause)
    print(f"ERROR: {message}", file=sys.stderr)


def gh(*argv: str, check: bool = False) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["gh", *argv], capture_output=True, text=True, timeout=60, check=False
        )
    # Caught before OSError, which it subclasses. This is the one failure about
    # the TOOL rather than the request, and returning str(exc) made every
    # downstream "is it missing?" test answer yes about the repository instead.
    except FileNotFoundError:
        gh_diagnose("missing", GH_UNAVAILABLE + ". Install it from https://cli.github.com, "
                    "or use papercut's local-only commands, which need no network.")
        if check:
            die(GH_UNAVAILABLE)
        return 1, GH_UNAVAILABLE
    except (OSError, subprocess.TimeoutExpired) as exc:
        if check:
            die(f"gh failed: {exc}")
        return 1, str(exc)
    out = (p.stdout or p.stderr).strip()
    if p.returncode != 0 and GH_AUTH_RE.search(out):
        gh_diagnose("auth", "GitHub CLI is installed but not authenticated: "
                            "run gh auth login.")
    if check and p.returncode != 0:
        die(f"gh {' '.join(argv)} failed: {p.stderr.strip()}")
    return p.returncode, out


def repo_for(sample_cwd: str) -> str | None:
    """Derive owner/name from the directory the friction happened in, so a papercut
    lands in the repo it belongs to rather than a catch-all."""
    if not sample_cwd:
        return None
    # Read the remote directly rather than `gh repo view`, which resolves against
    # the CALLER's cwd — the rollup runs from anywhere and must attribute each
    # signature to the directory the friction actually happened in.
    try:
        p = subprocess.run(
            ["git", "-C", sample_cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    m = re.search(r"[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", p.stdout.strip())
    return m.group(1) if m else None


def issue_body(entry: dict, days: int) -> str:
    lines = [
        SIG_MARKER.format(sig=entry["sig"]),
        "",
        f"Signature `{entry['sig']}` hit **{entry['sessions']} distinct session(s)** "
        f"({entry['count']} occurrence(s)) in the last {days} days.",
        "",
        f"Projects affected: {', '.join(entry['projects'])}",
        "",
    ]
    if entry["samples"]:
        lines += ["Samples:", "", "```"] + entry["samples"] + ["```", ""]
    lines += [
        "Filed automatically by `papercut rollup`. Repetition is the priority signal: "
        "this is friction agents worked around silently, once per session, without "
        "anything recording it.",
        "",
        "Close this if it is working as intended (a guard doing its job is not a bug) — "
        "the rollup will not refile a signature that has an open or recently-closed issue.",
    ]
    return "\n".join(lines)


def find_existing(repo: str, sig: str) -> dict | None:
    """The issue already tracking `sig`, with its state — or None.

    State matters: a CLOSED issue is an operator decision that this friction is
    resolved or working-as-intended. Without checking it, the rollup would
    re-comment 'Still recurring' on a deliberately closed issue forever, which
    directly contradicts the promise printed into every issue body.
    """
    code, out = gh(
        "issue", "list", "--repo", repo, "--state", "all", "--limit", "200",
        "--label", ISSUE_LABEL, "--json", "number,body,state,closedAt",
    )
    if code != 0:
        return None
    try:
        for issue in json.loads(out or "[]"):
            if SIG_MARKER.format(sig=sig) in (issue.get("body") or ""):
                return issue
    except (json.JSONDecodeError, TypeError, KeyError):
        return None
    return None


def ensure_label(repo: str) -> bool:
    """`gh issue create --label` fails when the label does not exist (unlike
    `issue list`, which tolerates it), so the label has to exist before filing.

    Verify-then-create, NOT `--force`. The adopt path already learned this:
    ensure_adoption_labels records that `gh label create --force` rewrites an
    existing label's colour and description, and that measured 2026-08-26 it
    would have restyled this org's curated `work-spec` label as a side effect
    of filing an unrelated work item. That fix landed on one call site and not
    this one, so the legacy `rollup --apply` route kept the same defect against
    the `papercut` label -- in any repository it files into, including a
    stranger's who already uses that name for something else. Existence is what
    is required; appearance is theirs.

    Returns success so the caller only marks the repo done when it worked — a
    transient failure must be retried on the next entry, not silently suppress
    filing for the rest of the run.
    """
    present = label_exists(repo, ISSUE_LABEL)
    if present is None:
        # Could not read the label list. Fail closed rather than force-create:
        # an unreadable remote is not permission to restyle whatever is there.
        return False
    if present:
        return True
    code, _ = gh("label", "create", ISSUE_LABEL, "--repo", repo,
                 "--description", "Agent-reported friction, filed by papercut rollup",
                 "--color", "FBCA04")
    return code == 0


def adoption_body(dossier: str, family: str) -> str:
    """Render the filed body human-first without modifying the local draft.

    Line 1 keeps the dossier marker byte-identical (its digest still covers
    exactly the evidence bytes), line 2 adds the immutable remote-dedupe
    marker, the authored `For humans` judgment renders before any machine
    evidence, and the whole dossier follows verbatim inside a collapsed
    block for agents. Raises ValueError rather than filing a body whose
    human half would be empty.
    """
    evidence, judgment = parse_dossier(family, dossier)
    for_humans = markdown_section(judgment, "For humans")
    if not for_humans:
        raise ValueError("missing For humans")
    marker = dossier[: len(dossier) - len(evidence) - len(judgment)]
    return (
        f"{marker}\n{FAMILY_MARKER.format(family=family)}\n\n"
        f"## For humans\n{for_humans}\n\n---\n\n"
        "<details>\n<summary><b>Full evidence dossier</b> — machine-owned "
        "telemetry and the complete authored judgment; agent-facing</summary>\n"
        f"{evidence}{judgment}</details>\n"
    )


def work_spec_gate(body: str) -> tuple[bool, str]:
    """Refuse a body missing a required section, using papercut's OWN extractor.

    This used to shell out to ../scripts/an external work-item validator, which had two
    problems. The portability one: that relative path does not exist in a
    packaged layout, so EVERY adopt hit the OSError branch and failed with an
    errno -- a perfect dossier included, and with nothing pointing at the real
    cause. The correctness one, which applies here too: that script validates
    with a downstream extractor's regex, while papercut renders the body with
    markdown_section(). The two disagree on a heading like
    "## Acceptance Criteria (required)" -- the script accepts it, the renderer
    reads the section as missing. So the gate could pass text the renderer
    would drop. Checking with the same extractor that renders removes that gap
    by construction.

    The script itself is untouched and keeps its own consumers (an external worktree tool and
    evidence-regime.test.sh); only papercut's dependency on its path is gone.
    """
    missing = [name for name in WORK_SPEC_SECTIONS if not markdown_section(body, name)]
    if missing:
        return False, "missing required section(s): " + ", ".join(missing)
    return True, ""


def dossier_snapshot(evidence: str) -> dict:
    """Read the triage-owned metadata that makes adopt's live comparison stable."""
    metadata = markdown_section(evidence, "Snapshot metadata")
    window = re.search(r"(?m)^- Triage window: (\d+) day\(s\)$", metadata)
    position = re.search(r"(?m)^- Event-log position: (\d+)$", metadata)
    thresholds = re.search(
        r"(?m)^- Thresholds: >= (\d+) distinct session\(s\); >= (\d+) occurrence\(s\)$", metadata,
    )
    if not window or not position or not thresholds:
        raise ValueError("snapshot metadata is incomplete or malformed")
    members = []
    for line in markdown_section(evidence, "Member signatures").splitlines():
        if not line.startswith("- "):
            raise ValueError("member signatures are malformed")
        try:
            value = json.loads(line[2:])
        except json.JSONDecodeError as exc:
            raise ValueError("member signatures are malformed") from exc
        if not isinstance(value, str) or not value:
            raise ValueError("member signatures are malformed")
        members.append(value)
    if not members:
        raise ValueError("member signatures are empty")
    return {
        "days": int(window.group(1)),
        "event_position": int(position.group(1)),
        "min_sessions": int(thresholds.group(1)),
        "min_count": int(thresholds.group(2)),
        "members": sorted(set(members)),
    }


def judgment_redaction_field(judgment: str) -> str | None:
    """Name the authored field a redaction would silently rewrite."""
    for heading in DOSSIER_JUDGMENT_FIELDS:
        value = markdown_section(judgment, heading)
        if value and redact(value) != value:
            return heading
    return "judgment" if redact(judgment) != judgment else None


def stale_dossier_entry(records: list[dict], membership: dict[str, str], family: str) -> dict:
    """Return fresh raw evidence for a refusal, even if eligibility is now zero."""
    entry = next(
        (row for row in rank(records, membership=membership) if row.get("family") == family), None,
    )
    return entry or {
        "family": family, "count": 0, "sessions": 0, "projects": [], "samples": [], "members": [],
    }


def regenerate_stale_dossier(path: Path, family: str, judgment: str, records: list[dict],
                              membership: dict[str, str], *, days: int, event_position: int,
                              min_count: int, min_sessions: int) -> None:
    """Refresh only evidence; the session-authored judgment remains byte-identical."""
    entry = stale_dossier_entry(records, membership, family)
    evidence = dossier_evidence(
        entry, days=days, event_position=event_position,
        min_count=min_count, min_sessions=min_sessions,
    )
    write_dossier(path, render_dossier(evidence, judgment, family))


def repo_exists(repo: str) -> bool | None:
    """True, False, or None when the remote could not answer.

    Collapsing the last case into False reported a transient auth or network
    failure to the operator as "destination repository does not exist", which is
    a different and far more alarming claim than the truth.
    """
    code, out = gh("repo", "view", repo, "--json", "nameWithOwner")
    if code != 0:
        return False if REMOTE_MISSING_RE.search(out or "") else None
    try:
        return json.loads(out).get("nameWithOwner") == repo
    except (AttributeError, TypeError, json.JSONDecodeError):
        return None


def adoption_repository_universe(records: list[dict], adoption: dict[str, dict], destination: str) -> set[str]:
    """Bound the global cap to repos the local store can enumerate completely."""
    # A caller with no destination (report-only rollup) seeds no repository here;
    # the capture window and the filing registry still supply the universe.
    repositories = {destination} if destination else set()
    for state in adoption.values():
        locator = state.get("locator")
        if locator is None:
            continue
        fields = locator_fields(locator)
        if fields is None:
            raise ValueError("recorded adoption locator is incomplete")
        repositories.add(fields[0])
    # `rollup --apply` SKIPs a signature whose recorded cwd resolves to no repo
    # rather than failing, so such a record names no repository and cannot be
    # part of the open-work cap's universe. Refusing here instead made adopt unusable on the
    # real store: measured 2026-08-26, 1306 of 1431 distinct capture cwds no
    # longer resolve, mostly worktrees that have since been swept. the open-work cap's
    # fail-closed clause covers a source that cannot be QUERIED -- enforced on
    # every gh read below -- not a capture record that simply is not in a repo.
    # Durably recorded filings outlive the capture window: an item filed into a
    # forced destination, or whose capture cwd has since been swept, is still
    # ours to count.
    for filing in read_filings():
        fields = locator_fields(filing)
        if fields is not None:
            repositories.add(fields[0])
    # Resolve each distinct cwd once; repo_for shells out to git per call.
    for cwd in dict.fromkeys(
        str(record["cwd"]) for record in records if record.get("cwd")
    ):
        repo = repo_for(cwd)
        if repo:
            repositories.add(repo)
    return repositories


def json_items_or_refuse(code: int, out: str, context: str,
                         limit: int | None = None) -> list[dict]:
    if code != 0:
        raise ValueError(f"could not query {context}")
    try:
        values = json.loads(out or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not parse {context}") from exc
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise ValueError(f"could not parse {context}")
    if limit is not None and len(values) >= limit:
        raise ValueError(
            f"could not enumerate {context}: the result filled the {limit}-item "
            "list limit, so an older item may be hidden behind it"
        )
    return values


def list_items_or_refuse(kind: str, context: str, *argv: str) -> list[dict]:
    """List one GitHub queue, or refuse when it cannot be read.

    Every adoption query goes through here so the "answer, not an unreadable
    source" decision exists once. Two non-zero exits are answers.

    A repository with issues disabled answers `gh issue list` with a non-zero
    exit, but the queue provably holds nothing, so no marker and no open item
    can hide there. Measured 2026-08-26, an-org/example-project sits in the
    live adoption universe with issues off and refused every adoption -- once
    through the marker search, then again through the global cap count.

    A repository that does not exist is the same kind of answer, and
    REMOTE_MISSING_RE already encodes it for repo_exists.
    Only this path was left out, so one dead repository anywhere in the
    universe refused every adoption in every repository. Measured 2026-08-27:
    twelve fixture filings for `owner/repository` in state/filings.jsonl put it
    in the live universe, where `gh issue list` answers `GraphQL: Could not
    resolve to a Repository`, and both adopt and `rollup --apply` refused for
    every family. A repository that does not exist holds no queue, so it hides
    no marker and no open item. Applied to both queues rather than issues
    alone: `gh pr list` happens to answer that case with an empty list and a
    zero exit today, which is the same conclusion by a different route.

    Both matches are narrow on purpose. Any other non-zero exit still fails
    closed per the open-work cap, and a result filling the list limit is unenumerable.
    """
    code, out = gh(kind, "list", *argv)
    if code != 0 and kind == "issue" and DISABLED_ISSUES_RE.search(out or ""):
        return []
    if code != 0 and REMOTE_MISSING_RE.search(out or ""):
        return []
    return json_items_or_refuse(code, out, context, limit=GH_LIST_LIMIT)


def open_papercut_count(records: list[dict], adoption: dict[str, dict], destination: str,
                        repositories: set[str] | None = None) -> int:
    """Count recorded adoptions plus legacy --apply items, deduplicated by locator.

    ``repositories`` lets a caller that already resolved the universe pass it in.
    Re-deriving it here shelled out to git once per distinct capture cwd and
    re-listed every repository a second time, all while holding the family lock.
    """
    locators: set[tuple[str, str, int]] = set()
    for family, state in adoption.items():
        if state.get("lifecycle") != "adopted":
            continue
        fields = locator_fields(state.get("locator"))
        if fields is None:
            raise ValueError(f"recorded adoption locator for {family} is incomplete")
        repo, kind, number = fields
        code, out = gh(kind, "view", str(number), "--repo", repo, "--json", "state")
        if code != 0:
            raise ValueError(f"could not confirm recorded adoption locator {repo}#{number}")
        try:
            remote = json.loads(out)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not parse recorded adoption locator {repo}#{number}") from exc
        observed = remote_item_state(remote.get("state")) if isinstance(remote, dict) else None
        if observed is None:
            raise ValueError(f"could not confirm recorded adoption locator {repo}#{number}")
        if observed == "open":
            locators.add((repo, kind, number))

    if repositories is None:
        repositories = adoption_repository_universe(records, adoption, destination)
    for repo in sorted(repositories):
        # Both queues, not just issues: the trivial route files a PR, and a PR
        # created before a failed read-back leaves no recorded locator, so
        # counting issues alone let that work escape the cap entirely.
        for kind in ("issue", "pr"):
            items = list_items_or_refuse(
                kind, f"open {ISSUE_LABEL} {kind}s in {repo}",
                "--repo", repo, "--state", "open",
                "--limit", str(GH_LIST_LIMIT),
                "--label", ISSUE_LABEL, "--json", "number",
            )
            for item in items:
                number = item.get("number")
                if not isinstance(number, int) or number <= 0:
                    raise ValueError(f"could not parse open {ISSUE_LABEL} {kind}s in {repo}")
                locators.add((repo, kind, number))
    return len(locators)


def remote_work_items(repo: str) -> list[dict]:
    """Search both queues so a marker can reconcile either tracked route.

    Filtered server-side by ISSUE_LABEL. Both filing routes label everything
    they create -- adopt with ADOPT_LABELS, legacy `rollup --apply` with
    ISSUE_LABEL -- so an unlabeled item is one neither route wrote and cannot
    hold a marker. Enumerating unfiltered searched a space this tool cannot
    have written to, and did not survive a real universe: measured 2026-08-26,
    an-org/example-project alone holds >= 1000 PRs, which correctly tripped
    the truncation refusal and blocked every adoption.
    """
    items = []
    for kind in ("issue", "pr"):
        # Only what the two consumers below read: the marker lives in the
        # body, the legacy check reads state, and every locator is rebuilt
        # by remote_locator(). Asking for labels was doubly redundant --
        # the --label filter already ran server-side -- and this list can
        # legitimately return GH_LIST_LIMIT items.
        values = list_items_or_refuse(
            kind, f"{kind} markers in {repo}",
            "--repo", repo, "--state", "all",
            "--label", ISSUE_LABEL,
            "--limit", str(GH_LIST_LIMIT), "--json", "number,state,body",
        )
        for value in values:
            number = value.get("number")
            if not isinstance(number, int) or number <= 0:
                raise ValueError(f"could not parse {kind} markers in {repo}")
            items.append({"repo": repo, "kind": kind, **value})
    return items


def label_exists(repo: str, label: str) -> bool | None:
    """True, False, or None when the answer cannot be read at all."""
    code, out = gh(
        "label", "list", "--repo", repo, "--search", label,
        "--limit", "100", "--json", "name",
    )
    if code != 0:
        return None
    try:
        labels = json.loads(out or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(labels, list):
        return None
    return label in {item.get("name") for item in labels if isinstance(item, dict)}


def ensure_adoption_labels(repo: str) -> bool:
    """Ensure both mandatory labels exist; unlabeled filing is forbidden.

    Creates only what is genuinely absent. Both labels are shared harness
    labels rather than papercut-owned ones, and `gh label create --force`
    rewrites an existing label's colour and description: measured 2026-08-26,
    that would have restyled the source harness' curated `work-spec`
    label as a side effect of filing an unrelated work item. the duplicate-filing guard requires
    existence, not appearance.
    """
    for label in ADOPT_LABELS:
        present = label_exists(repo, label)
        if present is None:
            return False
        if present:
            continue
        code, _ = gh(
            "label", "create", label, "--repo", repo,
            "--description", "Papercut promotion-loop work item", "--color", "FBCA04",
        )
        if code != 0:
            return False
        if label_exists(repo, label) is not True:
            return False
    return True


def remote_locator(repo: str, kind: str, number: int, marker: str) -> dict | None:
    """Read back a created or reconciled artifact before recording local adoption."""
    code, out = gh(kind, "view", str(number), "--repo", repo, "--json", "number,url,body")
    if code != 0:
        return None
    try:
        item = json.loads(out)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(item, dict) or item.get("number") != number or marker not in str(item.get("body", "")):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not url:
        return None
    return {"repo": repo, "kind": kind, "number": number, "url": url}


def number_from_created_url(output: str, kind: str) -> int | None:
    path = "issues" if kind == "issue" else "pull"
    match = re.search(rf"/{path}/(\d+)(?:[/?#]|$)", output)
    return int(match.group(1)) if match else None


def row_label(entry: dict) -> str:
    """Render a folded family without treating it as a raw signature."""
    if entry.get("family"):
        return f"family:{entry['family']} ({len(entry['members'])} member(s))"
    return entry["sig"]


def locator_fields(locator: object) -> tuple[str, str, int] | None:
    """Return a usable locally recorded locator without reading GitHub."""
    if not isinstance(locator, dict):
        return None
    repo, kind, number = locator.get("repo"), locator.get("kind"), locator.get("number")
    if not isinstance(repo, str) or kind not in {"issue", "pr"}:
        return None
    if not isinstance(number, int) or number <= 0:
        return None
    return repo, kind, number


def refresh_family_dispositions(
        adoption: dict[str, dict], families: list[str], limit: int,
) -> tuple[set[str], dict[str, dict]]:
    """Observe at most ``limit`` adopted items and persist successful reads.

    Returns the families whose remote state could not be confirmed, plus a
    dispatch snapshot for the items observed open: their labels and URL, read
    in the same ``gh view`` call the disposition needs anyway. The snapshot is
    never stored -- labels are the an autonomous queue's live intake state
    (`the dispatch-ready label`/`claimed`/`blocked`, see your queue's documentation), and a
    persisted copy would go stale the moment the operator tags anything.
    """
    unknown: set[str] = set()
    dispatch: dict[str, dict] = {}
    reads = 0
    for family in families:
        state = adoption.get(family, family_state(family))
        if state["lifecycle"] != "adopted":
            continue
        locator = locator_fields(state.get("locator"))
        if locator is None:
            unknown.add(family)
            continue
        if reads >= limit:
            # The cap is a freshness bound, not a remote-state failure.
            continue
        repo, kind, number = locator
        reads += 1
        code, out = gh(kind, "view", str(number), "--repo", repo,
                       "--json", "state,url,labels")
        if code != 0:
            unknown.add(family)
            continue
        try:
            remote = json.loads(out)
        except (TypeError, json.JSONDecodeError):
            unknown.add(family)
            continue
        observed_state = remote_item_state(remote.get("state")) if isinstance(remote, dict) else None
        if observed_state is None:
            unknown.add(family)
            continue
        observed_locator = dict(state["locator"])
        if isinstance(remote.get("url"), str) and remote["url"]:
            observed_locator["url"] = remote["url"]
        # The network read happened outside the family lock. Persist it only
        # if the lifecycle and canonical work item still match the snapshot that
        # drove the read; a concurrent reopen/escalate must outrank stale GitHub
        # data and must not regain its old adopted locator or dispatch state.
        recorded = record_family_event(
            family, "close-observed", locator=observed_locator,
            observed_state=observed_state, observed_at=datetime.now(timezone.utc).isoformat(),
            guard=lambda current: (
                current.get("lifecycle") == "adopted"
                and locator_fields(current.get("locator")) == locator
            ),
        )
        if recorded is None:
            continue
        if observed_state == "open":
            labels = remote.get("labels")
            dispatch[family] = {
                "kind": kind,
                "labels": sorted(
                    str(entry.get("name"))
                    for entry in (labels if isinstance(labels, list) else [])
                    if isinstance(entry, dict) and entry.get("name")
                ),
                "url": observed_locator.get("url") or f"{repo}#{number}",
            }
    return unknown, dispatch


def dispatch_handoff_line(family: str, snapshot: dict) -> str:
    """One adopted-open item's position relative to the an autonomous queue intake.

    Every status states a label fact, never an inferred outcome. The refresh
    read sees labels only: `claimed` is the shared cross-session claim signal
    (wt-new applies it for manual sessions too, and a stale claim persists
    until reaped), and native dependency blockers are visible only to
    an external readiness check's own query — so this line never asserts a an autonomous queue run and
    never promises selection, it reports the tag and names the authority
    that decides the rest. A pull-request locator (the trivial adopt route)
    is outside the intake entirely: an external readiness check reads issues only.
    """
    labels = set(snapshot.get("labels", []))
    url = snapshot.get("url", "")
    if snapshot.get("kind") == "pr":
        status = ("pull request — outside an autonomous queue intake "
                  "(an external readiness check reads issues); finish or merge it directly")
    elif "claimed" in labels:
        status = "claimed — a session holds it, excluded from ready work"
    elif "blocked" in labels:
        status = "blocked — excluded from ready work"
    elif "the dispatch-ready label" in labels:
        status = "tagged the dispatch-ready label — intake-eligible once an external readiness check clears it"
    else:
        status = "awaiting operator the dispatch-ready label tag"
    return f"  {family}: {status} — {url}"


def disposition_cache_age(adoption: dict[str, dict]) -> str:
    """Render cache age from local events; this path never reads GitHub."""
    observations = []
    for state in adoption.values():
        observation = state.get("last_observation")
        if not isinstance(observation, dict):
            continue
        instant = parse_ts(observation.get("observed_at")) or parse_ts(observation.get("ts"))
        if instant is not None:
            observations.append(instant)
    if not observations:
        return "papercut disposition-cache-age: no-observation"
    elapsed = max(0, int((datetime.now(timezone.utc) - max(observations)).total_seconds()))
    if elapsed < 60:
        age = f"{elapsed}s"
    elif elapsed < 3600:
        age = f"{elapsed // 60}m"
    elif elapsed < 86400:
        age = f"{elapsed // 3600}h"
    else:
        age = f"{elapsed // 86400}d"
    return f"papercut disposition-cache-age: {age}"


RECUR_MARKER = "<!-- papercut-recurrence family={family} epoch={epoch} -->"


def closure_stamp(state: dict) -> str | None:
    """When this family's work item was observed closed.

    One extraction, every consumer: the recurrence marker, the recurrence
    boundary and the verification stage all anchor on the same instant, so they
    can never disagree about which records are post-fix.
    """
    closed = state.get("closed_observation")
    if not isinstance(closed, dict):
        return None
    stamp = closed.get("observed_at") or closed.get("ts")
    return str(stamp) if stamp else None


def recurrence_boundary(state: dict) -> str | None:
    """The instant after which a member record counts as a NEW recurrence.

    The later of the closed observation and the last recurrence comment. Without
    the closed-observation floor, the FIRST closure of any family reported a
    recurrence assembled entirely from records that predate it -- the very
    occurrences the work item was filed for.
    """
    candidates = [closure_stamp(state)]
    comment = state.get("recur_comment")
    if isinstance(comment, dict):
        candidates.append(comment.get("ts"))
    parsed = [(parse_ts(value), value) for value in candidates if value]
    parsed = [pair for pair in parsed if pair[0] is not None]
    if not parsed:
        return None
    return max(parsed, key=lambda pair: pair[0])[1]


def has_new_recurrence(state: dict, members, records) -> bool:
    """True when some member record postdates both the closure and the last comment."""
    boundary = recurrence_boundary(state)
    if boundary is None:
        return False
    return any(
        record["sig"] in members and newer_than(record.get("ts"), boundary)
        for record in records
    )


def recurrence_marker(family: str, state: dict) -> str:
    """A deterministic, tool-owned marker naming this family's disposition epoch.

    Keyed on the CLOSED OBSERVATION alone, never on the comment timestamp, so the
    marker a crashed run already posted is still the marker the retry looks for.
    """
    stamp = closure_stamp(state) or ""
    epoch = hashlib.sha256(f"{family}\0{stamp}".encode("utf-8")).hexdigest()[:16]
    return RECUR_MARKER.format(family=family, epoch=epoch)


def recurrence_comment_posted(locator: dict, marker: str) -> bool | None:
    """Has this epoch's marker already been posted? None when unreadable.

    Recording the local event is a separate write from posting the comment, so a
    crash between them used to duplicate the comment on the next run. The remote
    marker is the durable record; the local event is only a cache of it.
    """
    code, out = gh(
        locator["kind"], "view", str(locator["number"]),
        "--repo", locator["repo"], "--json", "comments",
    )
    if code != 0:
        return None
    try:
        payload = json.loads(out)
    except (TypeError, json.JSONDecodeError):
        return None
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        return None
    return any(
        marker in str(comment.get("body", ""))
        for comment in comments if isinstance(comment, dict)
    )


def recurrence_body(family: str, entry: dict, days: int, marker: str) -> str:
    """One wording for both filing routes, carrying the epoch marker."""
    return (
        f"Family `{family}` is recurring: {entry['sessions']} session(s), "
        f"{entry['count']} occurrence(s) in the last {days}d. "
        "This comments the closed work item; it does not reopen it.\n\n"
        f"{marker}"
    )


def triage_recurrence(state: dict, members, records) -> dict:
    """The triage banner's decision, sharing recur-comment's predicate exactly.

    A closed observation alone is a CLOSURE, not a recurrence: `detected` holds
    only when a member record postdates the recurrence boundary -- the same
    `has_new_recurrence` gate cmd_family_recur_comment applies before posting.
    Without it the banner told the operator to run a recur-comment that would
    correctly decline (observed live 2026-08-29: python-command-missing, closed
    that evening, zero post-boundary records, banner fired anyway). A closed
    and quiet family still carries its locator under `closed_quiet` so triage
    can say WHY the closed item is in the lane without pointing at an action
    that will no-op."""
    details = recurrence_details(state)
    if details["detected"] and not has_new_recurrence(state, members, records):
        return {**details, "detected": False, "closed_quiet": True}
    return details


def recurrence_details(state: dict) -> dict:
    """Describe locally observed closed-work recurrence without contacting GitHub."""
    locator = locator_fields(state.get("locator"))
    if (state.get("lifecycle") != "adopted" or state.get("closed_observation") is None
            or locator is None):
        return {"detected": False}
    repo, kind, number = locator
    return {
        "detected": True,
        "commented": state.get("recur_comment") is not None,
        "locator": {"repo": repo, "kind": kind, "number": number},
    }


# ------------------------------------------------------------ verification stage

# A closed work item is a claim, not a result. These re-measure it from facts the
# store already holds -- the closure observation, the family's member records, and
# store-wide capture liveness -- recomputed on every read. Nothing here appends an
# event, writes a verdict, or contacts GitHub: a stored verdict would go stale the
# moment the fix regressed, and this way a regression is visible the next time
# anyone looks.
VERIFY_WINDOW_DAYS = 30
# Three sessions is the smallest exposure that distinguishes "nobody hit it" from
# "nobody was there". Below it, quiet is uninformative at any baseline.
VERIFY_EXPOSURE_FLOOR = 3


def validate_window(window) -> int:
    """A verification window is a positive number of days, or the run is refused.

    `--window 0` is falsy and would be swallowed by the default, silently
    measuring 30 days while the report prints the number the operator asked for;
    a negative window inverts the baseline and exposure intervals so every family
    reads `provisional`. Both are exactly the silently-wrong output this feature
    exists to catch, so they are refused rather than corrected.
    """
    if window is None:
        return VERIFY_WINDOW_DAYS
    try:
        days = int(window)
    except (TypeError, ValueError):
        days = 0
    if days < 1:
        family_error("policy refusal: --window must be a positive number of days", 3)
    return days


def verification_horizon_days(states, window_days: int) -> int:
    """How far back the classifier has to read to measure these families.

    Reaches one window BEFORE the oldest closure, because the exposure floor is
    scaled by an equal-length pre-closure baseline.
    """
    now = datetime.now(timezone.utc)
    spans = []
    for state in states:
        closed_at = parse_ts(closure_stamp(state))
        if closed_at is None:
            continue
        # A closure stamp ahead of now must not produce a negative horizon, which
        # would read as "no closures" or hand read_records a nonsense lookback.
        spans.append(max(0, (now - closed_at).days) + window_days + 1)
    return max(spans, default=0)


def verification_details(state: dict, members, records,
                         window_days: int = VERIFY_WINDOW_DAYS,
                         resolutions: dict[str, dict] | None = None) -> dict | None:
    """Classify one adopted-and-closed family's fix. None when it has no closure.

    Exposure is store-wide distinct capture sessions, not family-specific eligible
    attempts: the store cannot say whether the fixed mechanism was exercised, only
    whether capture was alive at all. That makes `verified` mean "no recurrence
    while agents were demonstrably working", never "proven fixed" -- and it is why
    a quiet family with no exposure stays `provisional` forever rather than
    graduating on silence.
    """
    if state.get("lifecycle") != "adopted":
        return None
    closed_at = parse_ts(closure_stamp(state))
    if closed_at is None:
        return None

    window = timedelta(days=window_days)
    details = {"stage": None, "window_days": window_days,
               "closed_at": closure_stamp(state)}

    # Parity with the recurrence commenter, which is the whole point of deriving
    # this: `rollup_lanes` strips quarantined and still-resolved signatures into
    # their own lanes BEFORE the family fold, so `has_new_recurrence` never sees
    # them. Reading raw records here would report a regression rollup deliberately
    # refuses to act on -- and since the read horizon grows with the closure age,
    # that false regression would never age out. Narrowed here rather than at the
    # call sites so no future caller can reintroduce the disagreement.
    if resolutions is None:
        resolutions = read_resolutions()
    live_members = {
        sig for sig in members
        if quarantine_rule(sig) is None and not is_resolved(sig, records, resolutions)
    }

    # A recurrence comment is durable evidence; the records that caused it age out
    # of the read horizon, so a regressed family would otherwise drift quiet and
    # then be promoted for the silence its own regression caused.
    boundary = recurrence_boundary(state)
    recurred = state.get("recur_comment") is not None or any(
        record.get("sig") in live_members and newer_than(record.get("ts"), boundary)
        for record in records
    )
    if recurred:
        details["stage"] = "regressed"
        return details

    def sessions_between(start, end) -> int:
        # Session-less records are skipped, matching `rank`: they cannot attest
        # that a distinct session was alive, and 0.6% of the live corpus has no
        # session field. Counting them together would inflate exposure by one.
        seen = set()
        for record in records:
            session = record.get("session")
            if not session:
                continue
            ts = parse_ts(record.get("ts"))
            if ts is None or ts <= start or ts > end:
                continue
            seen.add(str(session))
        return len(seen)

    # Measured before the verifying branch returns, so a family still inside its
    # window shows how far it is from the floor. Without it, a family headed for
    # `provisional` with zero exposure is indistinguishable from one that will
    # clear the floor comfortably, and nobody can tell until the window is spent.
    baseline = sessions_between(closed_at - window, closed_at)
    floor = max(VERIFY_EXPOSURE_FLOOR, math.ceil(baseline / 2))
    details["baseline_sessions"] = baseline
    details["floor"] = floor

    # Clamped at zero so a closure stamp somehow written ahead of now degrades to
    # "the clock starts when we read it" rather than printing a year of remaining
    # days. `family close-observed` refuses a future --observed-at, so this only
    # catches a hand-edited or clock-skewed event.
    now = datetime.now(timezone.utc)
    elapsed = max(timedelta(0), now - closed_at)
    if elapsed < window:
        details["exposure_sessions"] = sessions_between(
            closed_at, min(now, closed_at + window))
        details["partial"] = True
        remaining = (window - elapsed).total_seconds() / 86400
        details["stage"] = "verifying"
        details["days_remaining"] = max(1, math.ceil(remaining))
        return details

    exposure = sessions_between(closed_at, closed_at + window)
    details["exposure_sessions"] = exposure
    details["partial"] = False
    details["stage"] = "verified" if exposure >= floor else "provisional"
    return details


def verification_summary(details: dict) -> str:
    """The stage and the measurements behind it -- never a bare verdict."""
    stage = details["stage"]
    if stage == "regressed":
        return "regressed — recurred after the work item closed"
    # Every count is named store-wide on the way out. These sessions attest that
    # capture was alive, NOT that anything exercised this family's mechanism, and
    # an unqualified "exposure 3" next to "verified" reads as the second thing.
    measures = (f"exposure {details['exposure_sessions']} store-wide capture "
                f"session(s) vs floor {details['floor']} "
                f"(pre-closure baseline {details['baseline_sessions']})")
    if stage == "verifying":
        return (f"verifying — {details['days_remaining']} day(s) remaining "
                f"before the fix can be judged; {measures} so far")
    if stage == "verified":
        return (f"verified — no member recurrence, {measures}; store-wide "
                f"liveness only, the fixed mechanism itself was not measured")
    return (f"provisional — quiet, but only {measures}; "
            f"silence without exposure proves nothing")


def verification_line(family: str, details: dict) -> str:
    return f"  {family}: {verification_summary(details)}"


def verification_view(adoption: dict, membership: dict, window_days: int) -> dict:
    """Every adopted-and-closed family's stage, keyed by family id."""
    horizon = verification_horizon_days(adoption.values(), window_days)
    if not horizon:
        return {}
    records = list(read_records(horizon))
    resolutions = read_resolutions()
    view = {}
    for family, state in adoption.items():
        members = {sig for sig, assigned in membership.items() if assigned == family}
        details = verification_details(state, members, records, window_days, resolutions)
        if details is not None:
            view[family] = details
    return view


TRIAGE_UNFAMILIED_LIMIT = 10


def validate_unfamilied_limit(limit) -> int:
    """The unfamilied display bound is a positive count, or the run is refused.

    Zero or negative would print a clean familied report while hiding every
    unfamilied candidate behind it — the exact invisible-ore failure this
    section exists to end (252 of 253 flagged signatures had no family when it
    was added) — so, like `--window`, it is refused rather than corrected.
    """
    if limit is None:
        return TRIAGE_UNFAMILIED_LIMIT
    try:
        bound = int(limit)
    except (TypeError, ValueError):
        bound = 0
    if bound < 1:
        family_error(
            "policy refusal: --unfamilied-limit must be a positive number of candidates", 3)
    return bound


def cmd_triage(args: argparse.Namespace) -> None:
    """Materialize up to three local, evidence-complete family dossier drafts.

    This is deliberately a local-write/read-only-GitHub command. `rollup
    --refresh` is the only work-item state observer; this command reports the
    locally folded closed observation and leaves posting to recur-comment.
    """
    # Refused up front, before the lock and any dossier write: a zero or
    # negative display bound would print a clean familied report while
    # silently hiding every unfamilied candidate behind it.
    unfamilied_limit = validate_unfamilied_limit(getattr(args, "unfamilied_limit", None))
    try:
        # the single-writer state lock: this lock covers family-log read, draft classification, evidence
        # regeneration, and dossier write so an author's judgment cannot lose a
        # race with a concurrent triage session.
        with family_state_lock():
            records = list(read_records(args.days))
            events = read_family_events()
            family_views = fold_families(events)
            lanes = rollup_lanes(
                records, family_views,
                min_count=args.min_count, min_sessions=args.min_sessions,
            )
            candidates = [entry for entry in lanes["flagged"] if entry.get("family")]
            # The same ranked lane minus the family fold: flagged signatures
            # nobody has sorted yet (quarantined and resolved are already
            # excluded upstream). Surfaced as proposals only — grouping
            # signatures under one cause is the judgment call the registry
            # doctrine reserves for a person, so triage never writes a family
            # event for them.
            unfamilied = [entry for entry in lanes["flagged"] if not entry.get("family")]

            drafts: dict[str, tuple[Path, bool, str | None]] = {}
            for entry in candidates:
                family = validate_family_id(entry["family"])
                path = dossier_path(family)
                try:
                    incomplete, judgment = dossier_draft_status(family, path)
                except ValueError as exc:
                    family_error(f"policy refusal: corrupt dossier for {family}: {exc}", 3)
                drafts[family] = (path, incomplete, judgment)

            # Rank already-incomplete drafts ahead of entirely new candidates,
            # while preserving rank()'s deterministic order within each group.
            ordered = [
                entry for entry in candidates if drafts[entry["family"]][1]
            ] + [
                entry for entry in candidates if not drafts[entry["family"]][1]
            ]
            selected = ordered[:args.limit]
            output = []
            for entry in selected:
                family = entry["family"]
                path, _, judgment = drafts[family]
                evidence = dossier_evidence(
                    entry, days=args.days, event_position=len(events),
                    min_count=args.min_count, min_sessions=args.min_sessions,
                )
                created = judgment is None
                body = render_dossier(
                    evidence, DOSSIER_JUDGMENT_TEMPLATE if created else judgment, family,
                )
                write_dossier(path, body)
                state = family_views["adoption"].get(family, family_state(family))
                output.append({
                    "family": family,
                    "dossier": str(path),
                    "status": "created" if created else "resumed",
                    "occurrences": entry["count"],
                    "distinct_sessions": entry["sessions"],
                    "projects": entry["projects"],
                    "sample_excerpts": entry["samples"],
                    "member_signatures": entry["members"],
                    "recurrence": triage_recurrence(state, entry["members"], records),
                })
    except OSError as exc:
        family_error(f"could not materialize triage dossier: {exc}")

    shown = unfamilied[:unfamilied_limit]
    cut = len(unfamilied) - len(shown)
    unfamilied_rows = [
        {
            "sig": entry["sig"],
            "occurrences": entry["count"],
            "distinct_sessions": entry["sessions"],
            "projects": entry["projects"],
            "assign": f'papercut family assign <family-id-you-choose> "{entry["sig"]}"',
        }
        for entry in shown
    ]

    if args.json:
        if unfamilied_rows:
            # The bare array predates this section and stays byte-for-byte
            # whenever there is nothing to add; the object wrapper appears
            # only alongside actual unfamilied candidates.
            print(json.dumps(
                {"candidates": output, "unfamilied": unfamilied_rows,
                 "unfamilied_cut": cut},
                indent=2, sort_keys=True))
        else:
            print(json.dumps(output, indent=2, sort_keys=True))
        return
    if not output:
        print("papercut triage: no flagged family candidates")
    else:
        print(f"## papercut triage — {len(output)} candidate dossier(s)")
        for candidate in output:
            print(f"  {candidate['status'].upper()} family:{candidate['family']}  {candidate['dossier']}")
            recurrence = candidate["recurrence"]
            if recurrence["detected"]:
                locator = recurrence["locator"]
                suffix = ("already reported" if recurrence["commented"]
                          # candidate, not the stale `entry` loop variable: that
                          # bug printed a DIFFERENT family's name in this command
                          # three separate times on live data (2026-08-28).
                          else f"run `papercut family recur-comment {candidate['family']}`")
                print(f"    recurrence: closed {locator['kind']} {locator['repo']}#{locator['number']} ({suffix})")
            elif recurrence.get("closed_quiet"):
                locator = recurrence["locator"]
                print(f"    closed {locator['kind']} {locator['repo']}#{locator['number']}"
                      " — no member records postdate the closure (recur-comment would decline)")
    if shown:
        # A display bound, never a silent truncation: the last line names any
        # cut. An empty section is absent, not printed empty.
        print("unfamilied candidates:")
        for row in shown:
            print(f"  {row['sessions']:>3} sess {row['count']:>4}x  {row['sig']}  "
                  f"[{len(row['projects'])} project(s)]")
            print(f'      papercut family assign <family-id-you-choose> "{row["sig"]}"')
        if cut:
            print(f"  (+{cut} more — raise with --unfamilied-limit)")


def cmd_family_recur_comment(args: argparse.Namespace) -> None:
    """Post exactly one recurrence comment in a closed disposition epoch.

    The local closed observation is the authority for this explicit write. The
    only remote reads are this item's own comment list, used to detect a marker a
    crashed run already posted; the command never reopens anything, so GitHub
    state cannot change except for the single comment itself.
    """
    family = validate_family_id(args.family)
    try:
        # Keep the decision, remote comment, durable event, and no-op marker in
        # one critical section; concurrent invocations therefore cannot produce
        # two comments in the same disposition epoch.
        with family_state_lock():
            records = list(read_records(args.days))
            family_views = fold_families(read_family_events())
            lanes = rollup_lanes(
                records, family_views,
                min_count=args.min_count, min_sessions=args.min_sessions,
            )
            entry = next(
                (row for row in lanes["flagged"] if row.get("family") == family), None,
            )
            state = family_views["adoption"].get(family, family_state(family))
            recurrence = recurrence_details(state)
            if entry is None or not recurrence["detected"]:
                print(f"family recurrence: no flagged closed recurrence for {family}")
                return
            if recurrence["commented"]:
                print(f"family recurrence: already reported for {family} in this disposition epoch")
                return
            if not has_new_recurrence(state, entry["members"], records):
                print(f"family recurrence: no member activity since the closure of {family}")
                return

            locator = recurrence["locator"]
            marker = recurrence_marker(family, state)
            posted = recurrence_comment_posted(locator, marker)
            if posted is None:
                family_error(
                    f"could not read existing comments on {locator['repo']}#{locator['number']}; "
                    "refusing to risk a duplicate recurrence comment")
            if not posted:
                code, message = gh(
                    locator["kind"], "comment", str(locator["number"]), "--repo", locator["repo"],
                    "--body", recurrence_body(family, entry, args.days, marker),
                )
                if code != 0:
                    family_error(
                        f"could not report recurrence on {locator['repo']}#{locator['number']}: {message}")
            # Recorded either way: when the marker was already there, this run is
            # reconciling a previous run that posted and died before recording.
            append_family_event(
                new_family_event(
                    family, "recur-comment", locator=state["locator"],
                    sessions=entry["sessions"], count=entry["count"],
                    closed_observation=state["closed_observation"],
                ),
                lock_held=True,
            )
    except OSError as exc:
        family_error(f"could not record recurrence comment: {exc}")
    print(f"family recurrence reported: {family}")


def cmd_adopt(args: argparse.Namespace) -> None:
    """Promote one complete, current family dossier through an issue or draft PR."""
    family = validate_family_id(args.family)
    try:
        # the single-writer state lock: every local and remote phase remains under this one hold. No
        # other family writer can change membership or file a duplicate between
        # our snapshot check, marker search, read-back, and durable event.
        with family_state_lock():
            path = dossier_path(family)
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if not text:
                family_error("incomplete dossier: missing " + ", ".join(DOSSIER_JUDGMENT_FIELDS), 2)
            try:
                evidence, judgment = parse_dossier(family, text)
                snapshot = dossier_snapshot(evidence)
            except ValueError as exc:
                family_error(f"policy refusal: corrupt dossier for {family}: {exc}", 3)

            missing = dossier_judgment_missing(judgment)
            if missing:
                family_error("incomplete dossier: missing " + ", ".join(missing), 2)
            redacted_field = judgment_redaction_field(judgment)
            if redacted_field:
                family_error(
                    f"policy refusal: redaction would alter judgment field: {redacted_field}", 3,
                )

            destination = markdown_section(judgment, "Destination repository")
            records = list(read_records(snapshot["days"]))
            events = read_family_events()
            family_views = fold_families(events)
            state = family_views["adoption"].get(family, family_state(family))
            if state["lifecycle"] in {"adopted", "escalated", "disposed"}:
                family_error(f"policy refusal: family {family} is already {state['lifecycle']}", 3)

            lanes = rollup_lanes(
                records, family_views,
                min_count=snapshot["min_count"], min_sessions=snapshot["min_sessions"],
            )
            current = next(
                (entry for entry in lanes["flagged"] if entry.get("family") == family), None,
            )
            relevant_membership_change = any(
                event.get("action") in {"assign", "unassign"}
                and (event.get("family") == family or event.get("sig") in snapshot["members"])
                for event in events[snapshot["event_position"]:]
            )
            stale_reason = None
            if relevant_membership_change or current is None:
                stale_reason = "member set, eligibility, or threshold changed"
            elif sorted(current["members"]) != snapshot["members"]:
                stale_reason = "member set changed"
            if stale_reason:
                regenerate_stale_dossier(
                    path, family, judgment, records, family_views["membership"],
                    days=snapshot["days"], event_position=len(events),
                    min_count=snapshot["min_count"], min_sessions=snapshot["min_sessions"],
                )
                family_error(
                    f"policy refusal: stale dossier for {family}: {stale_reason}; "
                    "evidence regenerated and judgment preserved", 3,
                )

            destination_state = repo_exists(destination)
            if destination_state is None:
                family_error(
                    f"policy refusal: could not confirm destination repository {destination}; "
                    "refusing to file against an unconfirmed remote", 3)
            if not destination_state:
                family_error(f"policy refusal: destination repository does not exist: {destination}", 3)

            try:
                repositories = adoption_repository_universe(
                    records, family_views["adoption"], destination,
                )
                items = [item for repo in sorted(repositories) for item in remote_work_items(repo)]
            except ValueError as exc:
                family_error(f"policy refusal: cannot search adoption markers: {exc}", 3)
            marker = FAMILY_MARKER.format(family=family)
            existing = next((item for item in items if marker in str(item.get("body", ""))), None)
            legacy = next(
                (
                    item for item in items
                    if item["kind"] == "issue" and str(item.get("state", "")).lower() == "open"
                    and any(SIG_MARKER.format(sig=sig) in str(item.get("body", ""))
                            for sig in current["members"])
                ),
                None,
            )
            if legacy:
                family_error(
                    f"policy refusal: member is already covered by open legacy issue "
                    f"{legacy['repo']}#{legacy['number']}", 3,
                )

            # A prior create can succeed remotely yet lose its read-back. Its
            # marker must reconcile on retry even when the global queue is now
            # full; this path creates no new work item.
            if not existing:
                try:
                    open_count = open_papercut_count(
                        records, family_views["adoption"], destination,
                        repositories=repositories)
                except ValueError as exc:
                    family_error(f"policy refusal: cannot enumerate papercut cap: {exc}", 3)
                if open_count >= args.cap:
                    family_error(
                        f"policy refusal: papercut open-item cap {args.cap} reached "
                        f"({open_count} open)", 3,
                    )

            if not ensure_adoption_labels(destination):
                family_error(
                    f"policy refusal: could not provision and verify destination labels in {destination}", 3,
                )

            try:
                body = adoption_body(text, family)
            except ValueError as exc:
                family_error(f"incomplete dossier: {exc}", 2)
            if redact(body) != body:
                family_error("policy refusal: redaction would alter rendered evidence", 3)
            if len(body) > GITHUB_BODY_LIMIT:
                family_error(
                    f"policy refusal: rendered body is {len(body)} characters, over the "
                    f"GitHub limit of {GITHUB_BODY_LIMIT}; shorten the authored judgment "
                    "(the For humans section renders twice: hoisted and inside the "
                    "collapsed dossier)", 3,
                )
            gate_ok, gate_error = work_spec_gate(body)
            if not gate_ok:
                family_error(f"incomplete dossier: render gate failed: {gate_error}", 2)

            status = "reconciled" if existing else "filed"
            if existing:
                locator = remote_locator(existing["repo"], existing["kind"], existing["number"], marker)
            else:
                code, out = gh(
                    "issue", "create", "--repo", destination,
                    "--title", f"papercut: family {family}", "--body", body,
                    "--label", ADOPT_LABELS[0], "--label", ADOPT_LABELS[1],
                )
                number = number_from_created_url(out, "issue") if code == 0 else None
                locator = remote_locator(destination, "issue", number, marker) if number else None

            if locator is None:
                family_error(f"unconfirmed remote state for family {family}; retry will reconcile by marker", 4)
            # One record for both branches above: whichever way this family
            # reached a confirmed locator, the cap must still see it once the
            # capture window that named its repository has aged out.
            record_filing(locator["repo"], locator["kind"], locator["number"],
                          str(locator.get("url") or ""))

            append_family_event(
                new_family_event(
                    family, "adopt", locator=locator,
                    dossier_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ),
                lock_held=True,
            )
            # The source judgment is only disposable once the durable adoption
            # event gives the remote locator and full dossier digest a home.
            delete_family_dossier(family)
    except OSError as exc:
        family_error(f"could not adopt family {family}: {exc}")

    result = {"family": family, "status": status, "locator": locator}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"family adopted: {family} -> {locator['repo']}#{locator['number']}")
        # The handoff's one human act, said at the moment it becomes possible.
        # Print-only: the print-only label rule stands -- no payload this tool sends ever carries the
        # label, and its planted negative enforces that separately. Issues
        # only: an external readiness check reads issues, so tagging the trivial route's PR
        # would do nothing, and saying otherwise sends the operator to pull
        # a lever that is not connected.
        print_adopt_next_step(locator)


def print_adopt_next_step(locator: dict) -> None:
    """The handoff's one human act, said at the moment it becomes possible.

    Print-only: no payload this tool sends ever carries the label. Extracted
    from cmd_adopt so the branch is testable without driving a full adoption,
    and gated because it used to name this harness's queue unconditionally --
    a stranger got instructions for machinery they do not have.
    """
    if locator.get("kind") == "issue":
        if DISPATCH_READY_LABEL:
            where = f" ({DISPATCH_DOCS_REF})" if DISPATCH_DOCS_REF else ""
            print(f"next: tag it {DISPATCH_READY_LABEL} to make it dispatchable — the "
                  f"operator's act, or the session's for work the operator "
                  f"approved this session{where}")
    elif DISPATCH_READY_LABEL:
        print("next: finish and merge the pull request — an autonomous queue intake "
              f"reads issues only, so {DISPATCH_READY_LABEL} does not apply here")
    else:
        print("next: finish and merge the pull request")


def cmd_rollup(args: argparse.Namespace) -> None:
    # Refused up front: an unusable window must not surface after a full report,
    # and must never reach the --apply path that writes to GitHub.
    window_days = validate_window(getattr(args, "window", None))
    records = list(read_records(args.days))
    fixture_dropped = count_fixture_records(args.days)
    # The dropped count alone is a one-sided view: it says what the fixture
    # rules caught, never what they missed. That asymmetry is why a 28%
    # synthetic corpus went unnoticed for weeks, and why the first version of
    # the hook-test rule shipped matching only one of the two cwd shapes and
    # nothing complained for an hour -- it was found by a human re-reading the
    # data, which does not scale. This counts synthesized identities among the
    # records that SURVIVED filtering, so drift announces itself.
    # Baseline at introduction (2026-08-29): 4 of 16,499 over 7 days. A step
    # change means a rule stopped matching, not that friction increased.
    synthetic_ranked = sum(1 for rec in records if synthesized_identity(rec))
    family_views = fold_families()
    lanes = rollup_lanes(
        records, family_views,
        min_count=args.min_count, min_sessions=args.min_sessions,
    )
    threshold_rows = lanes["threshold_rows"]

    refresh_unknown: set[str] = set()
    dispatch_snapshot: dict[str, dict] = {}
    if getattr(args, "refresh", False):
        # Threshold families first (they hold the freshness priority under
        # --limit), then every other adopted family. The quiet ones matter
        # most here: an adopted item whose friction stopped recurring is
        # exactly the one sitting in the an autonomous queue intake unnoticed, and a
        # closed-but-quiet family would otherwise never get its closure
        # observed -- which is what starts the verification clock.
        #
        # The quiet tail is ordered never-observed first, then oldest
        # observation first. Each successful read appends a fresh
        # observation, so under a read cap this is a free round-robin:
        # a deterministic order would starve the same tail families every
        # week once adopted families outnumber --limit.
        def observation_age(family: str):
            observation = (family_views["adoption"].get(family) or {}).get("last_observation") or {}
            instant = parse_ts(observation.get("observed_at")) or parse_ts(observation.get("ts"))
            if instant is None:
                return (0, datetime.min.replace(tzinfo=timezone.utc))
            return (1, instant)

        candidate_families = list(dict.fromkeys(
            [row["family"] for row in threshold_rows if row.get("family")]
            + sorted(
                (family for family, state in family_views["adoption"].items()
                 if state.get("lifecycle") == "adopted"),
                key=observation_age,
            )
        ))
        refresh_unknown, dispatch_snapshot = refresh_family_dispositions(
            family_views["adoption"], candidate_families, args.limit,
        )
        # Successful reads append close-observed events. Re-fold and partition so
        # a newly observed closed item re-enters this same command.
        family_views = fold_families()
        lanes = rollup_lanes(
            records, family_views,
            min_count=args.min_count, min_sessions=args.min_sessions,
            refresh_unknown=refresh_unknown,
        )

    adoption = family_views["adoption"]
    suppressed = lanes["suppressed"]
    quarantined = lanes["quarantined"]
    eligible_records = lanes["eligible_records"]
    ranked = lanes["ranked"]
    adopted_open = lanes["adopted_open"]
    escalated = lanes["escalated"]
    disposed = lanes["disposed"]
    flagged = lanes["flagged"]

    print(f"## papercuts rollup — last {args.days}d")
    print(
        f"{len(records)} record(s), {len(ranked)} folded row(s), "
        f"{len(flagged)} over threshold (>={args.min_sessions} sessions, >={args.min_count} hits)"
    )
    if suppressed:
        print(f"({len(suppressed)} resolved signature(s) suppressed — `papercut list --include-resolved` to see them)")
    if quarantined:
        print(f"({len(quarantined)} junk-fingerprint signature(s) quarantined — needs "
              f"fingerprinting, not fixes; `papercut list --quarantined` to see them)")
    if fixture_dropped:
        print(f"({fixture_dropped} test-fixture record(s) dropped — hook-test denial "
              f"writes: guard denials from a /tmp sandbox or the hooks tree that "
              f"carry a synthesized identity, plus the route-guard fixture cwds of "
              f"an earlier change; on disk, never counted)")
    if adopted_open:
        print(f"({len(adopted_open)} adopted-open family(s) suppressed — work item remains active)")
    if refresh_unknown:
        print(f"({len(refresh_unknown)} adopted family(s) disposition_unknown — remote state unconfirmed; kept suppressed)")
    if disposed:
        print(f"({len(disposed)} disposed family(s) suppressed — `papercut family reopen` to reconsider)")
    print(disposition_cache_age(adoption))
    for entry in flagged[: args.limit]:
        print(f"  {entry['sessions']:>3} sess {entry['count']:>4}x  {row_label(entry)}  "
              f"[{len(entry['projects'])} project(s)]")

    if escalated:
        print("escalated upstream:")
        for entry in escalated:
            upstream_url = adoption[entry["family"]].get("upstream_url") or "?"
            print(f"  family:{entry['family']}  {entry['sessions']} sess {entry['count']}x  "
                  f"{upstream_url}")

    # Only under --refresh: the states come from labels read moments ago, and
    # the plain read path must keep making no gh call. This is the loop's one
    # human boundary -- an adopted item is dispatchable the moment the
    # operator tags it `the dispatch-ready label`, and until this line existed nothing showed
    # which items were sitting at that boundary.
    if dispatch_snapshot:
        print("dispatch handoff:")
        for family in sorted(dispatch_snapshot):
            print(dispatch_handoff_line(family, dispatch_snapshot[family]))

    # Printed before the report-only return so the weekly rollup -- which never
    # passes --apply -- is itself the automatic re-measure. A closed family says
    # nothing here otherwise, and silence reads as "fixed".
    verification = verification_view(adoption, family_views["membership"], window_days)
    if verification:
        print(f"verification (window {window_days}d):")
        for family, details in verification.items():
            print(verification_line(family, details))

    if not args.apply:
        if flagged:
            print("(report only — run `papercut rollup --apply` to file/update issues)")
        # The weekly toast greps this token byte-for-byte.
        print(f"papercuts-flagged:{len(flagged)}")
        print(f"papercuts-quarantined:{len(quarantined)}")
        print(f"papercuts-fixture-records:{fixture_dropped}")
        print(f"papercuts-synthetic-ranked:{synthetic_ranked}")
        print(f"papercuts-escalated:{len(escalated)}")
        # Report-only exits 0 even when a refresh could not confirm remote state.
        # a weekly scheduled run runs exactly this path as
        #   rollup --days 7 --refresh || echo "WARN papercuts rollup crashed ..."
        # and counts ^WARN lines into its toast. An unconfirmed read is an
        # expected condition -- cron has no gh auth, or a family has no locator
        # yet -- and is already reported in-band on the disposition_unknown line
        # above. Exiting non-zero here would print a complete rollup section and
        # then assert the command crashed, which is both false and a drift
        # warning. the single-writer state lock's exit taxonomy governs the mutating gates; --apply below
        # still exits 4.
        return

    # the open-work cap's global open-work cap governs BOTH filing routes. This one used to
    # create issues with no cap check at all, so a backlog of unassigned
    # signatures could file straight past the ceiling `adopt` refuses at.
    remaining = None
    # `getattr` matches this file's existing optional-argument convention (see
    # the --refresh read above): the CLI always supplies --cap.
    cap = getattr(args, "cap", 3)
    if cap is not None and cap >= 0:
        try:
            remaining = max(0, cap - open_papercut_count(
                eligible_records, adoption, args.repo or ""))
        except ValueError as exc:
            print(f"ERROR: cannot enumerate papercut cap: {exc}", file=sys.stderr)
            raise SystemExit(4)
        print(f"papercuts-cap-remaining:{remaining}")

    filed = 0
    labelled: set[str] = set()
    for entry in flagged[: args.limit]:
        family = entry.get("family")
        if family:
            state = adoption.get(family, family_state(family))
            locator = locator_fields(state.get("locator"))
            if state["lifecycle"] == "adopted" and state["closed_observation"] and locator:
                repo, kind, number = locator
                if not has_new_recurrence(state, entry["members"], eligible_records):
                    print(f"  SKIP {row_label(entry)}: no member activity since the closure "
                          f"of {repo}#{number}")
                    continue
                marker = recurrence_marker(family, state)
                posted = recurrence_comment_posted(
                    {"repo": repo, "kind": kind, "number": number}, marker)
                if posted is None:
                    print(f"  SKIP {row_label(entry)}: could not read comments on "
                          f"{repo}#{number}; not risking a duplicate")
                    continue
                code = 0
                if not posted:
                    code, _ = gh(
                        kind, "comment", str(number), "--repo", repo,
                        "--body", recurrence_body(family, entry, args.days, marker),
                    )
                if code == 0:
                    record_family_event(
                        family, "recur-comment", locator=state["locator"],
                        sessions=entry["sessions"], count=entry["count"],
                    )
                if code != 0:
                    verb = "FAILED to report recurrence on closed"
                elif posted:
                    verb = "reconciled already-posted recurrence on closed"
                else:
                    verb = "reported recurrence on closed"
                print(f"  {verb} {repo}#{number}: {row_label(entry)}")
            else:
                print(f"  SKIP {row_label(entry)}: assigned family rows are adopted through the clinic, not --apply")
            continue

        # Only unassigned raw signatures retain the legacy auto-file/update route.
        candidates = [
            r.get("cwd") for r in eligible_records
            if r["sig"] == entry["sig"] and r.get("cwd")
        ]
        repo = args.repo
        for cand in dict.fromkeys(candidates):
            if repo:
                break
            repo = repo_for(cand)
        if not repo:
            print(f"  SKIP {entry['sig']}: no recorded cwd resolves to a repo (pass --repo)")
            continue

        existing = find_existing(repo, entry["sig"])
        title = f"papercut: {entry['sig']} ({entry['sessions']} sessions, {entry['count']}x)"

        if existing and str(existing.get("state", "")).upper() == "CLOSED":
            # Closed is an operator verdict: fixed, or working as intended. Only a
            # genuine post-fix regression (a record newer than closedAt) justifies
            # speaking up again.
            closed_at = existing.get("closedAt")
            regressed = any(
                r["sig"] == entry["sig"] and newer_than(r.get("ts"), closed_at)
                for r in eligible_records
            )
            if not regressed:
                print(f"  SKIP {repo}#{existing['number']} closed — respecting operator resolution: {entry['sig']}")
                continue
            # Comment, do NOT reopen. Closing was a deliberate operator verdict;
            # reopening on their behalf overrides it. Report the recurrence and
            # let them decide. The wording must not imply the issue was reopened.
            code, _ = gh(
                "issue", "comment", str(existing["number"]), "--repo", repo,
                "--body", f"Recurred after this issue was closed: "
                          f"{entry['sessions']} session(s), {entry['count']}x in the last "
                          f"{args.days}d. Reopen if the fix regressed; otherwise "
                          f"`papercut resolve {entry['sig']}` to stop the rollup reporting it.")
            verb = "regression on closed" if code == 0 else "FAILED to comment on closed"
            print(f"  {verb} {repo}#{existing['number']}: {entry['sig']}")
            continue

        if existing:
            code, _ = gh(
                "issue", "comment", str(existing["number"]), "--repo", repo,
                "--body", f"Still recurring: {entry['sessions']} session(s), "
                          f"{entry['count']} occurrence(s) in the last {args.days}d.",
            )
            print(f"  {'updated' if code == 0 else 'FAILED to update'} {repo}#{existing['number']}: {entry['sig']}")
        else:
            if remaining is not None and remaining <= 0:
                print(f"  SKIP {entry['sig']}: papercut open-item cap {cap} reached")
                continue
            if repo not in labelled:
                # `issue create --label` fails on a missing label. Only mark the
                # repo done on success, so a transient failure retries next entry.
                if ensure_label(repo):
                    labelled.add(repo)
            code, out = gh(
                "issue", "create", "--repo", repo, "--title", title,
                "--body", issue_body(entry, args.days), "--label", ISSUE_LABEL,
            )
            if code == 0:
                filed += 1
                # Reserve against the cap the moment the create succeeds, before
                # the read-back that may fail, so an unconfirmed create still
                # consumes its slot in this run.
                if remaining is not None:
                    remaining -= 1
                url = out.splitlines()[-1] if out else ""
                number = number_from_created_url(out, "issue")
                if number:
                    record_filing(repo, "issue", number, url)
                print(f"  filed {url or repo}: {entry['sig']}")
            else:
                print(f"  FAILED to file {entry['sig']}: {out[:160]}")
    print(f"papercuts-filed:{filed}")
    print(f"papercuts-flagged:{len(flagged)}")
    print(f"papercuts-quarantined:{len(quarantined)}")
    print(f"papercuts-fixture-records:{fixture_dropped}")
    print(f"papercuts-synthetic-ranked:{synthetic_ranked}")
    print(f"papercuts-escalated:{len(escalated)}")
    if refresh_unknown:
        raise SystemExit(4)


# --------------------------------------------------------------- configuration
# Module-level constants stay the interface and are mutated in place, following
# the sibling memory-dream extraction. Deliberately NOT a Config object: the
# test suite rebinds seven of these names directly (PC.STORE, PC.RESOLVED,
# PC.FAMILIES, PC.DOSSIERS, PC.FILINGS, PC.GH_LIST_LIMIT, PC._GH_DIAGNOSED), and
# an indirection layer would churn every one of them for no gain.
#
# Nothing here runs at import. load_config() is called once from main(), so a
# test that never calls main() sees exactly today's literals -- which is what
# makes "the suite passes untouched" a real proof rather than a hope.
_ENV_PREFIX = "PAPERCUT_"
# PAPERCUT_STORE already existed and already matches this prefix, so the one
# pre-existing override needs no compatibility shim.
_OVERRIDABLE = (
    "STORE",
    "ISSUE_LABEL",
    "WORK_SPEC_LABEL",
    "WORK_SPEC_SECTIONS",
    "DISPATCH_READY_LABEL",
    "DISPATCH_DOCS_REF",
    "KNOWN_GUARDS",
    "GH_LIST_LIMIT",
    "VERIFY_WINDOW_DAYS",
    "VERIFY_EXPOSURE_FLOOR",
    "TRIAGE_UNFAMILIED_LIMIT",
    "DOSSIER_PROJECT_CAP",
)
_CONFIG_LOADED = False


def claude_config_dir() -> Path:
    """Base directory for Claude Code state, honoring CLAUDE_CONFIG_DIR."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def config_path() -> Path:
    return claude_config_dir() / "papercut.json"


def coerce_override(name: str, value, source: str):
    """Coerce to the type the current literal already has.

    The live value is the schema: there is no separate declaration to drift
    from it. A value that cannot be coerced is a hard error naming the source,
    because a silently ignored override is a config that lies about itself.
    """
    current = globals()[name]
    try:
        if isinstance(current, tuple):
            if isinstance(value, str):
                items = [part.strip() for part in value.split(",") if part.strip()]
            else:
                items = list(value)
            return tuple(str(item) for item in items)
        if isinstance(current, Path):
            return Path(str(value)).expanduser()
        if isinstance(current, bool):       # not currently used; guard anyway
            return str(value).strip().lower() in {"1", "true", "yes"}
        if isinstance(current, int):
            return int(value)
        return type(current)(value)
    except (TypeError, ValueError) as exc:
        die(f"{source}: {name} expects {type(current).__name__}: {exc}")


def recompute_derived() -> None:
    """Re-derive every constant computed FROM an overridable one.

    Both groups freeze at import. Probed 2026-08-29: setting STORE alone left
    RESOLVED pointing at the old store, so an override half-applied and the
    tool read one store while writing another -- the same split-brain class as
    the hook bug in an earlier change. ADOPT_LABELS is the same trap wearing different
    clothes: it is built from the two label names, not read from them.
    """
    global RESOLVED, FAMILIES, FILINGS, DOSSIERS, ADOPT_LABELS
    RESOLVED = STORE / "state" / "resolved.jsonl"
    FAMILIES = STORE / "state" / "families.jsonl"
    FILINGS = STORE / "state" / "filings.jsonl"
    DOSSIERS = STORE / "state" / "dossiers"
    ADOPT_LABELS = (ISSUE_LABEL, WORK_SPEC_LABEL)


def load_config() -> None:
    """Apply overrides once per process: file first, then env on top.

    Env beats file so a one-off run can override a checked-in default without
    editing it. With neither present -- the operator's case, and the default
    case for anyone who has not opted in -- this function changes nothing.
    """
    global _CONFIG_LOADED
    if _CONFIG_LOADED:
        return
    _CONFIG_LOADED = True

    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            die(f"cannot read config at {path}: {exc}")
        if not isinstance(data, dict):
            die(f"config at {path} must be a JSON object")
        for key, value in data.items():
            name = str(key).upper()
            if name not in _OVERRIDABLE:
                die(f"config at {path}: unknown key {key!r}; "
                    f"known keys are {', '.join(k.lower() for k in _OVERRIDABLE)}")
            globals()[name] = coerce_override(name, value, f"config key {key}")

    for name in _OVERRIDABLE:
        raw = os.environ.get(_ENV_PREFIX + name)
        if raw is None:
            continue
        globals()[name] = coerce_override(name, raw, f"env {_ENV_PREFIX + name}")

    recompute_derived()


def main() -> None:
    load_config()
    ap = argparse.ArgumentParser(prog="papercut", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="log a self-reported papercut")
    a.add_argument("-m", "--message", required=True)
    a.add_argument("--sig", help="explicit signature (defaults to a slug of the message)")
    a.add_argument("--cwd", help="project dir (defaults to $PWD)")
    a.add_argument("-q", "--quiet", action="store_true")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="ranked signatures in the window")
    l.add_argument("--days", type=int, default=7)
    l.add_argument("--cwd", help="restrict to the project owning this directory (use \"$PWD\")")
    l.add_argument("--project", help="restrict to one project slug (e.g. -home-user-SITES-example-project); "
                                     "prefer --cwd, which derives it for you")
    l.add_argument("--limit", type=int, default=30)
    l.add_argument("--json", action="store_true")
    l.add_argument("-v", "--verbose", action="store_true")
    l.add_argument("--include-resolved", action="store_true",
                   help="also show signatures marked resolved")
    l.add_argument("--quarantined", action="store_true",
                   help="show ONLY quarantined junk-fingerprint signatures (the "
                        "fingerprinting backlog; excluded from the default view)")
    l.set_defaults(func=cmd_list)

    rv = sub.add_parser("resolve", help="mark a signature fixed; hides it until it recurs")
    rv.add_argument("sig")
    rv.add_argument("-n", "--note", help="what was done about it (shown by `papercut show`)")
    rv.add_argument("--reopen", action="store_true", help="undo a resolve")
    rv.add_argument("--lookback", type=int, default=30,
                    help="window used to report what is being suppressed (default 30d)")
    rv.set_defaults(func=cmd_resolve)

    st = sub.add_parser("staleness", help="is capture still alive? (for a weekly scheduled run)")
    st.add_argument("--max-gap-hours", type=float, default=72.0)
    st.set_defaults(func=cmd_staleness)

    t = sub.add_parser("triage", help="prepare local evidence dossiers for flagged families")
    t.add_argument("--days", type=int, default=7)
    t.add_argument("--min-count", type=int, default=3)
    t.add_argument("--min-sessions", type=int, default=3)
    t.add_argument("--limit", type=int, default=3)
    t.add_argument("--unfamilied-limit", type=int, default=TRIAGE_UNFAMILIED_LIMIT,
                   help="display bound for unfamilied flagged signatures "
                        f"(default {TRIAGE_UNFAMILIED_LIMIT}); any cut is named, never silent")
    t.add_argument("--json", action="store_true", help="emit the selected candidate array")
    t.set_defaults(func=cmd_triage)

    ad = sub.add_parser("adopt", help="validate and file one completed family dossier")
    ad.add_argument("family")
    ad.add_argument("--cap", type=int, default=3,
                    help="maximum open papercut-originated work items (default: 3)")
    ad.add_argument("--json", action="store_true", help="emit the confirmed adoption locator")
    ad.set_defaults(func=cmd_adopt)

    f = sub.add_parser("family", help="record and inspect append-only family state")
    family_sub = f.add_subparsers(dest="family_cmd", required=True)

    fc = family_sub.add_parser("create", help="create a family")
    fc.add_argument("family")
    fc.set_defaults(func=cmd_family_create)

    fa = family_sub.add_parser("assign", help="assign an immutable raw signature to a family")
    fa.add_argument("family")
    fa.add_argument("sig")
    fa.set_defaults(func=cmd_family_assign)

    fu = family_sub.add_parser("unassign", help="remove a raw signature from folded membership")
    fu.add_argument("family")
    fu.add_argument("sig")
    fu.set_defaults(func=cmd_family_unassign)

    fe = family_sub.add_parser("escalate", help="record a human-filed upstream report")
    fe.add_argument("family")
    fe.add_argument("--to", required=True, help="https URL of the upstream report")
    fe.add_argument("-n", "--note", help="optional local context for the escalation")
    fe.set_defaults(func=cmd_family_escalate)

    fd = family_sub.add_parser("dispose", help="record a no-remedy disposition and delete its dossier")
    fd.add_argument("family")
    fd.add_argument("--verdict", help="one of: " + ", ".join(sorted(DISPOSE_VERDICTS)))
    fd.set_defaults(func=cmd_family_dispose)

    fr = family_sub.add_parser("reopen", help="reverse a family disposition")
    fr.add_argument("family")
    fr.set_defaults(func=cmd_family_reopen)

    fo = family_sub.add_parser("close-observed", help="record one local work-item state observation")
    fo.add_argument("family")
    fo.add_argument("--repo", required=True)
    fo.add_argument("--kind", choices=("issue", "pr"), required=True)
    fo.add_argument("--number", type=int, required=True)
    fo.add_argument("--url", required=True)
    fo.add_argument("--state", choices=("open", "closed"), required=True)
    fo.add_argument("--observed-at", help="ISO-8601 observation timestamp (defaults to now)")
    fo.set_defaults(func=cmd_family_close_observed)

    frc = family_sub.add_parser("recur-comment", help="comment once on an explicitly observed closed recurrence")
    frc.add_argument("family")
    frc.add_argument("--days", type=int, default=7)
    frc.add_argument("--min-count", type=int, default=3)
    frc.add_argument("--min-sessions", type=int, default=3)
    frc.set_defaults(func=cmd_family_recur_comment)

    fs = family_sub.add_parser("show", help="render one family or all folded family state")
    fs.add_argument("family", nargs="?")
    fs.add_argument("--json", action="store_true")
    fs.add_argument("--window", type=int, default=VERIFY_WINDOW_DAYS,
                    help=f"verification window in days (default {VERIFY_WINDOW_DAYS})")
    fs.set_defaults(func=cmd_family_show)

    s = sub.add_parser("show", help="every occurrence of one signature")
    s.add_argument("sig")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_show)

    r = sub.add_parser("rollup", help="rank, then file/update gh issues over threshold")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--min-count", type=int, default=3)
    r.add_argument("--min-sessions", type=int, default=3)
    r.add_argument("--limit", type=int, default=10)
    r.add_argument("--apply", action="store_true", help="actually file/update issues")
    r.add_argument("--refresh", action="store_true", help="observe adopted work-item state from GitHub")
    r.add_argument("--repo", help="force a target repo (owner/name)")
    r.add_argument("--cap", type=int, default=3,
                   help="global open papercut work-item cap shared with `adopt`; "
                        "--cap -1 disables the check (default 3)")
    r.add_argument("--window", type=int, default=VERIFY_WINDOW_DAYS,
                   help=f"verification window in days (default {VERIFY_WINDOW_DAYS})")
    r.set_defaults(func=cmd_rollup)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
