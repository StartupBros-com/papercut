#!/usr/bin/env python3
"""Test-gate integrity guard: a red suite must produce a red check.

THE DEFECT THIS EXISTS FOR
    GitHub's implicit Linux shell is `bash -e`, which does not set pipefail.
    A step spelled

        run: python3 -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tail -5

    therefore reports `tail`'s exit status, not the suite's. Measured
    2026-09-14 against a deliberately failing one-test suite: the step printed
    "FAILED (failures=1)" and exited 0. Every test in this repository was
    advisory for as long as that line stood, and nothing in the repository
    would have told anyone.

WHY A GUARD AND NOT JUST THE FIX
    The fix is one line and the regression is one line. A reviewer reading
    `| tail -5` sees log tidiness, not a disabled gate, which is precisely why
    it survived. This check makes the reintroduction fail loudly instead.

DELETION CONDITION
    Delete this when the workflows no longer run a test suite, or if a future
    Actions runner sets pipefail by default AND the pinned runner image is
    verified to do so -- not before.

Usage:
  check_ci_test_gate.py            report every violation
Exit 1 when anything is found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# This file necessarily quotes the shape it forbids, the same exemption
# check_no_private_refs.py takes for itself. Without it the guard matches its
# own source and fails on a clean tree -- observed on the first run.
SELF = Path(__file__).resolve()

# A test runner whose exit status is piped ANYWHERE.
#
# The first version matched only a single hop into tail/head on one physical
# line. Three separate reviewers broke it the same day: `runner | tee x | tail`
# evades it, `runner | grep -v warn` evades it, and a backslash-continued
# `run: |` block that puts the pipe on the next line evades it. Under a shell
# without pipefail EVERY one of those reports the last stage's status, which is
# the whole defect -- so the filter's identity was never the thing that
# mattered. Any pipe after a test runner is the signal.
PIPED_TEST = re.compile(
    r"(?:unittest|node\s+--test|pytest|\bbash\s+tests/)"   # a test runner
    r"[^\n]*\|"                                             # ...whose status is piped anywhere
)

# A run: block can continue across lines with a trailing backslash, which puts
# the pipe on a different physical line from the runner. Join those before
# scanning so the continuation cannot hide the pipe.
CONTINUED = re.compile(r"\\\n\s*")


def main() -> int:
    if not WORKFLOWS.is_dir():
        print(f"no workflows directory at {WORKFLOWS}", file=sys.stderr)
        return 1

    failures: list[str] = []

    for wf in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        if wf.resolve() == SELF:
            continue
        raw = wf.read_text(encoding="utf-8")
        # Join backslash continuations first, then scan. Line numbers are taken
        # from the joined text's own numbering, which stays correct for every
        # line before the first continuation and is close enough after it to
        # point a reader at the right step.
        joined = CONTINUED.sub(" ", raw)
        for lineno, line in enumerate(joined.splitlines(), 1):
            # A comment is not a command. The fix's own rationale comment quotes
            # the forbidden shape, and matching it failed the clean tree on the
            # first run -- the guard has to read what RUNS, not what explains.
            if line.lstrip().startswith("#"):
                continue
            if PIPED_TEST.search(line):
                failures.append(
                    f"{wf.relative_to(ROOT)}:{lineno}: test status piped into a "
                    f"truncating filter -- the step reports the filter's exit code\n"
                    f"    {line.strip()}"
                )

    # pipefail is what makes any surviving pipe safe. Selecting bash explicitly
    # is the documented way to get it.
    ci = WORKFLOWS / "ci.yml"
    if ci.is_file():
        # Structural, not a bare text search: `shell: bash` buried in one step
        # leaves every other step unprotected, and a commented example would
        # satisfy a naive search. Require the workflow-level defaults.run block.
        ci_text = ci.read_text(encoding="utf-8")
        has_default_shell = re.search(
            r"^defaults:\s*$\n(?:[ \t]+.*\n)*?[ \t]+run:\s*$\n(?:[ \t]+.*\n)*?[ \t]+shell:\s*bash\s*$",
            ci_text, re.M)
        if not has_default_shell:
            failures.append(
                "ci.yml: no explicit `shell: bash`, so steps run under `bash -e` "
                "without pipefail -- add a workflow-level defaults.run.shell"
            )

    if failures:
        print("test-gate integrity: FAILED")
        for f in failures:
            print(f"  {f}")
        return 1

    print("test-gate integrity: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
