#!/usr/bin/env python3
"""Sanitization regression guard: no private-harness references ship.

papercut was extracted from a personal Claude Code harness. Every literal
below means nothing outside that harness, and several would leak private
infrastructure or point a stranger at scripts they do not have.

Deliberately NOT banned: the publishing org and its repository URL. This
plugin really is published by StartupBros / House of Vibe, so those strings
are identity, not leakage -- the precedent guard in the sibling memory-dream
extraction makes the same call.

Exempt: this file (it necessarily quotes what it looks for) and
docs/EXTRACTION-DESIGN.md (the design record, which documents the source it
was extracted from).

Usage:
  check_no_private_refs.py            list every hit
  check_no_private_refs.py --summary  count by label and by file
Exit 1 when anything is found.
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
EXEMPT = {SELF, (ROOT / "docs/EXTRACTION-DESIGN.md").resolve()}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".ruff_cache", "dist", "build"}

# (label, literal, case-insensitive)
LITERALS: list[tuple[str, str, bool]] = [
    ("private-home-path", "/home/will", False),
    # The same token slug-encoded: project_slug() turns /home/will/x into
    # -home-will-x, and matching only the path form let the slug form pass as
    # clean while the vendor was asymmetrically rewriting test expectations.
    ("private-home-path-slug", "-home-will", False),
    ("private-dotfiles-path", "~/dotfiles", True),
    ("private-repo-name", "dotfiles", True),
    ("private-project", "smaug", True),
    # Personal transcript-search CLI; a hint naming it must never ship
    # (refute-vet finding 2026-08-30: the vendor rule scrubs it, but the
    # gate must catch what the transform one day misses).
    ("private-tool", "cass", True),
    ("private-project", "pi-evals", True),
    ("private-project", "prbot", True),
    ("private-project", "blog-writer", True),
    ("private-harness-script", "sync.sh", True),
    ("private-harness-script", "wt-new.sh", True),
    ("private-harness-script", "gh-ready", True),
    ("private-harness-script", "harness-weekly", True),
    ("private-harness-script", "verify-seed.sh", True),
    ("private-harness-doc", "claude/harness/", True),
    ("private-harness-doc", "HARNESS.md", False),
    ("private-queue-label", "loop-ok", True),
    ("private-queue-term", "dispatcher", True),
    ("private-guard", "git-guard", True),
    ("private-guard", "runner-guard", True),
    ("private-guard", "loop-guard", True),
    ("private-guard", "codex-quota-guard", True),
    ("private-tool", "gsc ", False),
    ("private-env", "CLAUDE_JOB_DIR", False),
]

# Internal doctrine shorthand: gate codes like KTD6, R17, AE8. Public docs must
# name the invariant instead of citing a code no outside reader can resolve.
DOCTRINE_RE = re.compile(r"\b(?:R1?[0-9]|KTD[0-9]+|AE[0-9]+)\b")

# Private issue/PR references: "PR #123", "issue #61", or a bare "#123" that is
# not a hex color and not a markdown heading.
ISSUE_REF_RE = re.compile(
    r"(?:PR|issue|pull)\s*#[0-9]+|(?<![\w&])#[0-9]{2,}(?![0-9A-Fa-f]{1,4}\b)",
    re.IGNORECASE,
)


def scan(path: Path) -> list[tuple[str, int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    rel = path.relative_to(ROOT).as_posix()
    out: list[tuple[str, int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, needle, fold in LITERALS:
            hay = line.lower() if fold else line
            probe = needle.lower() if fold else needle
            if probe in hay:
                out.append((rel, lineno, label, needle))
        m = DOCTRINE_RE.search(line)
        if m:
            out.append((rel, lineno, "doctrine-shorthand", m.group()))
        m = ISSUE_REF_RE.search(line)
        if m:
            out.append((rel, lineno, "private-issue-ref", m.group()))
    return out


def main() -> int:
    summary = "--summary" in sys.argv
    hits: list[tuple[str, int, str, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.resolve() in EXEMPT:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        hits.extend(scan(path))

    if not hits:
        print("sanitization: clean")
        return 0

    print(f"private references still present: {len(hits)} hit(s)")
    if summary:
        by_label = collections.Counter(h[2] for h in hits)
        by_file = collections.Counter(h[0] for h in hits)
        print("\nby label:")
        for label, n in by_label.most_common():
            print(f"  {n:>5}  {label}")
        print("\nby file:")
        for rel, n in by_file.most_common():
            print(f"  {n:>5}  {rel}")
    else:
        for rel, lineno, label, needle in hits[:200]:
            print(f"  {rel}:{lineno}: {label}: {needle}")
        if len(hits) > 200:
            print(f"  ... and {len(hits) - 200} more")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
