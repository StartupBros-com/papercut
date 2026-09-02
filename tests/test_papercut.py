#!/usr/bin/env python3
"""Tests for claude/bin/papercut.py — the friction log's write path and rollup.

The rollup is the half that keeps the store from becoming write-only, so its
ranking and thresholding are what these tests actually pin down.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE = Path(__file__).resolve().parents[1]
PAPERCUT = CLAUDE / "papercut" / "cli.py"

_spec = importlib.util.spec_from_file_location("papercut_module", PAPERCUT)
PC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PC)


def iso(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class PapercutBase(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp(prefix="papercut-store-"))
        PC.STORE = self.store
        # RESOLVED, FAMILIES, DOSSIERS and FILINGS are import-time constants
        # derived from STORE, so they do not follow the patch above. Bind every
        # one of them here: a subclass that rebinds only the constants it happens
        # to touch leaves the rest pointed at the operator's real store, and the
        # test still passes while writing outside its temp directory.
        PC.RESOLVED = self.store / "state" / "resolved.jsonl"
        PC.FAMILIES = self.store / "state" / "families.jsonl"
        PC.DOSSIERS = self.store / "state" / "dossiers"
        PC.FILINGS = self.store / "state" / "filings.jsonl"

    def tearDown(self):
        shutil.rmtree(self.store, ignore_errors=True)

    def write(self, slug, records):
        fp = self.store / f"{slug}.jsonl"
        with open(fp, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return fp

    def rec(self, sig="command_not_found:pytest", days_ago=0, session="s1", source="auto", **kw):
        base = {"ts": iso(days_ago), "sig": sig, "session": session, "source": source,
                "err": "pytest: command not found", "tool": "Bash"}
        base.update(kw)
        return base


class TestReadRecords(PapercutBase):
    def test_window_excludes_older_records(self):
        self.write("-p", [self.rec(days_ago=1), self.rec(days_ago=30)])
        self.assertEqual(len(list(PC.read_records(7))), 1)

    def test_malformed_lines_are_skipped_not_fatal(self):
        fp = self.store / "-p.jsonl"
        fp.write_text(
            json.dumps(self.rec()) + "\n"
            + "{ this is a torn line\n"
            + "\n"
            + json.dumps({"no_sig_field": True}) + "\n"
            + json.dumps(self.rec(sig="timed_out")) + "\n",
            encoding="utf-8",
        )
        sigs = sorted(r["sig"] for r in PC.read_records(7))
        self.assertEqual(sigs, ["command_not_found:pytest", "timed_out"])

    def test_project_filter(self):
        self.write("-a", [self.rec()])
        self.write("-b", [self.rec(sig="timed_out")])
        self.assertEqual([r["sig"] for r in PC.read_records(7, project="-b")], ["timed_out"])

    def test_naive_timestamps_are_treated_as_utc_not_dropped(self):
        naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        self.write("-p", [{"ts": naive, "sig": "timed_out", "session": "s1"}])
        self.assertEqual(len(list(PC.read_records(7))), 1)


class TestRank(PapercutBase):
    def test_ranks_by_distinct_sessions_not_raw_count(self):
        # One stuck session retrying 10x is ONE problem; three separate sessions
        # hitting the same wall is three independent rediscoveries and ranks higher.
        self.write("-p", [self.rec(sig="noisy_retry", session="s1") for _ in range(10)])
        self.write("-p", [self.rec(sig="spread", session=f"s{i}") for i in range(3)])
        ranked = PC.rank(PC.read_records(7))
        self.assertEqual(ranked[0]["sig"], "spread")
        self.assertEqual(ranked[0]["sessions"], 3)
        self.assertEqual(ranked[1]["sig"], "noisy_retry")
        self.assertEqual(ranked[1]["sessions"], 1)
        self.assertEqual(ranked[1]["count"], 10)

    def test_counts_projects_and_self_reports(self):
        self.write("-a", [self.rec(sig="x", session="s1")])
        self.write("-b", [self.rec(sig="x", session="s2", source="self", msg="docs were wrong")])
        entry = PC.rank(PC.read_records(7))[0]
        self.assertEqual(entry["projects"], ["-a", "-b"])
        self.assertEqual(entry["self_reported"], 1)
        self.assertEqual(entry["sessions"], 2)

    def test_samples_are_capped_and_single_line(self):
        self.write("-p", [self.rec(sig="x", session=f"s{i}", err="line one\nline two") for i in range(9)])
        entry = PC.rank(PC.read_records(7))[0]
        self.assertLessEqual(len(entry["samples"]), 3)
        self.assertTrue(all("\n" not in s for s in entry["samples"]))


class TestStoreResolution(PapercutBase):
    """One resolution order, both sides: PAPERCUT_STORE, then
    CLAUDE_CONFIG_DIR/papercuts, then home. The hook's parity test pins the
    JS side; this pins the CLI via subprocess (STORE binds at import, so
    in-process rebinding cannot exercise it)."""

    def cli(self, env_extra, *args):
        env = {k: v for k, v in os.environ.items()
               if k not in ("PAPERCUT_STORE", "CLAUDE_CONFIG_DIR")}
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(PAPERCUT), *args],
            capture_output=True, text=True, timeout=30, check=False, env=env)

    def test_claude_config_dir_isolates_the_store(self):
        cfg = self.mkdtemp()
        p = self.cli({"CLAUDE_CONFIG_DIR": cfg}, "add", "-m", "isolated", "--cwd", "/tmp/x")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((Path(cfg) / "papercuts" / "-tmp-x.jsonl").exists(),
                        "record must land under the overridden config dir")

    def test_papercut_store_wins_over_config_dir(self):
        cfg, store = self.mkdtemp(), self.mkdtemp()
        p = self.cli({"CLAUDE_CONFIG_DIR": cfg, "PAPERCUT_STORE": store},
                     "add", "-m", "override wins", "--cwd", "/tmp/x")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((Path(store) / "-tmp-x.jsonl").exists(),
                        "PAPERCUT_STORE must take precedence")
        self.assertFalse((Path(cfg) / "papercuts").exists(),
                         "nothing may land under the config dir when the store is overridden")

    def mkdtemp(self):
        d = tempfile.mkdtemp(prefix="pc-storeres-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d


class TestAddCommand(PapercutBase):
    def run_cli(self, *argv, cwd=None):
        # CLAUDE_CODE_SESSION_ID is the real variable and leaks in from the
        # ambient environment, so pin it explicitly rather than inheriting it.
        env = dict(os.environ, PAPERCUT_STORE=str(self.store), CLAUDE_CODE_SESSION_ID="zzzz9999")
        return subprocess.run(
            [sys.executable, str(PAPERCUT), *argv],
            capture_output=True, text=True, timeout=30, check=False,
            cwd=cwd or str(self.store), env=env,
        )

    def test_add_writes_a_self_sourced_record(self):
        p = self.run_cli("add", "-m", "a-cli row limit truncates silently", "--cwd", "/home/user/SITES/demo")
        self.assertEqual(p.returncode, 0, p.stderr)
        fp = self.store / "-home-user-SITES-demo.jsonl"
        self.assertTrue(fp.exists(), f"expected {fp}; got {list(self.store.iterdir())}")
        rec = json.loads(fp.read_text().strip())
        self.assertEqual(rec["source"], "self")
        self.assertEqual(rec["msg"], "a-cli row limit truncates silently")
        self.assertEqual(rec["session"], "zzzz9999")
        self.assertTrue(rec["sig"].startswith("self:"))

    def test_explicit_signature_is_honoured_so_self_reports_can_join_auto_ones(self):
        self.run_cli("add", "-m", "same thing again", "--sig", "timed_out", "--cwd", "/x")
        rec = json.loads((self.store / "-x.jsonl").read_text().strip())
        self.assertEqual(rec["sig"], "timed_out")

    def test_empty_message_is_rejected(self):
        self.assertEqual(self.run_cli("add", "-m", "   ", "--cwd", "/x").returncode, 1)

    def test_add_appends_rather_than_overwrites(self):
        self.run_cli("add", "-m", "first", "--cwd", "/x")
        self.run_cli("add", "-m", "second", "--cwd", "/x")
        self.assertEqual(len((self.store / "-x.jsonl").read_text().strip().split("\n")), 2)


class TestRollup(PapercutBase):
    def run_rollup(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run(
            [sys.executable, str(PAPERCUT), "rollup", *argv],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )

    def test_emits_the_toast_counter_even_when_empty(self):
        out = self.run_rollup("--days", "7").stdout
        self.assertIn("papercuts-flagged:0", out,
                      "a weekly scheduled run greps this line; it must always be present")

    def test_threshold_flags_only_recurring_signatures(self):
        self.write("-p", [self.rec(sig="rare", session="s1")])
        self.write("-p", [self.rec(sig="recurring", session=f"s{i}") for i in range(4)])
        out = self.run_rollup("--days", "7", "--min-sessions", "3", "--min-count", "3").stdout
        self.assertIn("papercuts-flagged:1", out)
        self.assertIn("recurring", out)
        self.assertNotIn("  rare", out)

    def test_does_not_file_issues_without_apply(self):
        self.write("-p", [self.rec(sig="recurring", session=f"s{i}") for i in range(5)])
        out = self.run_rollup("--days", "7").stdout
        self.assertIn("report only", out)
        self.assertNotIn("papercuts-filed:", out)


class TestSessionAttribution(PapercutBase):
    """Regression tests for the bug that made every self-report invisible to the
    rollup: papercut.py read CLAUDE_SESSION_ID, which Claude Code does not set."""

    def add(self, env_extra, cwd="/x"):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CLAUDE_SESSION_ID", None)
        env.update(env_extra)
        subprocess.run(
            [sys.executable, str(PAPERCUT), "add", "-q", "-m", "friction", "--cwd", cwd],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        return json.loads((self.store / f"{PC.project_slug(cwd)}.jsonl").read_text().strip().split("\n")[-1])

    def test_reads_the_variable_claude_code_actually_exports(self):
        rec = self.add({"CLAUDE_CODE_SESSION_ID": "aaaaaaaabbbbcccc"})
        self.assertEqual(rec["session"], "bbbbcccc")

    def test_falls_back_to_claude_session_id_when_set_deliberately(self):
        rec = self.add({"CLAUDE_SESSION_ID": "1111111122223333"})
        self.assertEqual(rec["session"], "22223333")

    def test_self_reports_can_cross_the_rollup_threshold(self):
        # The actual regression: three self-reports of one signature from three
        # sessions must reach --min-sessions 3, not score zero.
        for sid in ("sess0001", "sess0002", "sess0003"):
            self.write("-p", [self.rec(sig="guard_blocked:a-command-guard", session=sid, source="self")])
        entry = PC.rank(PC.read_records(7))[0]
        self.assertEqual(entry["sessions"], 3)
        self.assertEqual(entry["self_reported"], 3)

    def test_unattributed_records_are_not_scored_zero(self):
        self.write("-p", [self.rec(sig="x", session="", source="self") for _ in range(3)])
        entry = PC.rank(PC.read_records(7))[0]
        self.assertEqual(entry["sessions"], 3,
                         "a missing session id must not make a signature unreachable")


class TestTimestampComparison(PapercutBase):
    """Three producers write three ISO shapes. Raw string `>` inverts between
    them, because ASCII 'Z' outranks every digit."""

    JS = "2026-08-06T10:00:00.789Z"            # hook: toISOString(), ms + Z
    PY = "2026-08-06T10:00:00.789012+00:00"    # CLI: isoformat(), us + offset
    GH = "2026-08-06T10:00:00Z"                # GitHub closedAt: second precision

    def test_raw_string_compare_is_the_bug(self):
        self.assertGreater(self.JS, self.PY, "precondition: the naive compare inverts")

    def test_parsed_compare_is_correct(self):
        self.assertTrue(PC.newer_than(self.PY, self.JS), "PY instant is later than JS")
        self.assertFalse(PC.newer_than(self.JS, self.PY))

    def test_subsecond_record_after_github_close_counts_as_regression(self):
        # ".789Z" string-compares BELOW "Z" at second precision; parsed, it is after.
        self.assertTrue(PC.newer_than(self.JS, self.GH))

    def test_unparseable_never_invents_a_regression(self):
        self.assertFalse(PC.newer_than("not-a-date", self.GH))
        self.assertFalse(PC.newer_than(self.JS, None))

    def test_resolve_is_not_defeated_by_format_mismatch(self):
        # A JS-format record written BEFORE a Python-format resolve must stay resolved.
        self.write("-p", [self.rec(sig="x", **{"ts": self.JS})])
        PC.RESOLVED = self.store / "state" / "resolved.jsonl"
        PC.RESOLVED.parent.mkdir(parents=True, exist_ok=True)
        PC.RESOLVED.write_text(json.dumps({"sig": "x", "ts": self.PY, "action": "resolve"}) + "\n")
        recs = [r for r in PC.read_records(3650)]
        self.assertTrue(PC.is_resolved("x", recs, PC.read_resolutions()))


class TestEnsureLabelRetry(PapercutBase):
    def test_failure_is_not_marked_done(self):
        orig, PC.gh = PC.gh, lambda *a, **k: (1, "boom")
        try:
            self.assertFalse(PC.ensure_label("o/r"),
                             "a failed label create must be retryable, not silently sticky")
        finally:
            PC.gh = orig

    def test_success_reports_true(self):
        orig, PC.gh = PC.gh, lambda *a, **k: (0, "")
        try:
            self.assertTrue(PC.ensure_label("o/r"))
        finally:
            PC.gh = orig


class TestSynthesizedIdentityFixtureLane(PapercutBase):
    """Hook-test denial writes are dropped from ranking; genuine ones are not.

    Measured 2026-08-29 on the live store: of the last 7 days' guard records,
    a-vcs-guard was 2,280 test writes against 100 genuine, a-worker-guard 1,938
    against 3, and a-messaging-guard 991 against 5 -- so three of the weekly's
    top signatures were ranked almost entirely by test artifacts. The rule is
    CONJUNCTIVE (hook-test cwd AND synthesized identity) because either signal
    alone also matches records that must keep counting.
    """

    HOOK_CWD = "/home/user/harness/.claude/worktrees/demo/claude/hooks"
    REAL_CWD = "/home/user/SITES/arena-strategy"

    def rec_for(self, **kw):
        base = {"sig": "guard_blocked:a-vcs-guard", "cwd": self.HOOK_CWD,
                "session": "d-318283"}
        base.update(kw)
        return base

    def test_a_hook_test_write_is_dropped_from_ranking(self):
        self.assertEqual(PC.fixture_rule(self.rec_for()),
                         "hook-test-synthesized-identity")

    def test_the_full_ppid_marker_matches_too(self):
        # Records written after the marker fix carry the untruncated prefix.
        self.assertEqual(PC.fixture_rule(self.rec_for(session="ppid-318283")),
                         "hook-test-synthesized-identity")

    def test_a_literal_test_session_is_synthesized_too(self):
        # Measured 2026-09-02: a harness-vet suite handed the hook the literal
        # session id `test` from a /tmp scratch repo -- 796 a-vcs-guard records in
        # one day, 43% of everything the hyphen-only rule let through. A real
        # session is the last 8 hex characters of a UUID; anything else was
        # invented by whoever called the hook.
        self.assertEqual(
            PC.fixture_rule(self.rec_for(session="test", cwd="/tmp/hv-848-vet-2/scratch-repo")),
            "hook-test-synthesized-identity")
        # Planted negative: a real session id at the same /tmp cwd keeps counting
        # (the rule stays conjunctive).
        self.assertIsNone(PC.fixture_rule(
            self.rec_for(session="cda2bb6d", cwd="/tmp/hv-848-vet-2/scratch-repo")))

    def test_the_rollup_reports_synthetic_records_that_still_rank(self):
        """The tripwire for a fixture rule that stops matching.

        Counting only what was dropped is one-sided: it cannot show what the
        rules missed. The first hook-test rule shipped covering one of two cwd
        shapes and nothing complained for an hour, because no counter watched
        the survivors.
        """
        self.write("-p", [
            # ranks, and carries a synthesized identity -> counted
            self.rec(sig="guard_blocked:a-messaging-guard", session="ppid-4242",
                     cwd="/home/user/SITES/arena-strategy", days_ago=1),
            # ranks with a real identity -> not counted
            self.rec(sig="guard_blocked:a-messaging-guard", session="cda2bb6d",
                     cwd="/home/user/SITES/arena-strategy", days_ago=1),
            # dropped by the fixture rule -> must not be counted as ranking
            self.rec(sig="guard_blocked:a-vcs-guard", session="ppid-99",
                     cwd="/tmp/gg-branch-zzz", days_ago=1),
        ])
        buf = io.StringIO()
        args = argparse.Namespace(days=7, min_count=3, min_sessions=3, limit=10,
                                  apply=False, repo="o/r", refresh=False,
                                  cap=-1, window=None)
        with contextlib.redirect_stdout(buf):
            PC.cmd_rollup(args)
        out = buf.getvalue()
        self.assertIn("papercuts-synthetic-ranked:1", out,
                      "exactly the ranking synthesized record is counted")
        self.assertIn("papercuts-fixture-records:1", out,
                      "the dropped record stays in its own counter")

    def test_a_mkdtemp_sandbox_write_is_dropped_too(self):
        # Guard suites write from mkdtemp sandboxes as well as the hooks tree:
        # route-guard uses /tmp/route-guard-*, a-vcs-guard /tmp/gg-branch-* and
        # /tmp/gg-varpath-*. Matching the SHAPE avoids enumerating a prefix per
        # suite, which silently under-filters until someone notices -- it did,
        # for 2,406 records, until 2026-08-29.
        for cwd in ("/tmp/gg-branch-RaB2wn",
                    "/tmp/gg-varpath-xdf8cz/feature-wt",
                    "/tmp/route-guard-abc123"):
            self.assertEqual(PC.fixture_rule(self.rec_for(cwd=cwd)),
                             "hook-test-synthesized-identity", cwd)

    def test_a_real_session_in_a_tmp_sandbox_keeps_counting(self):
        # Planted negative: the conjunction still governs. Measured 2026-08-29,
        # 5 live records look exactly like this and must keep ranking.
        self.assertIsNone(PC.fixture_rule(
            self.rec_for(cwd="/tmp/ggrepro-9zzlFy/mainrepo", session="cda2bb6d")))

    def test_a_real_session_in_a_hook_dir_keeps_counting(self):
        # Planted negative: an agent legitimately working in claude/hooks trips
        # a guard. One signal is not enough to drop it.
        self.assertIsNone(PC.fixture_rule(self.rec_for(session="cda2bb6d")))

    def test_a_synthesized_identity_in_a_real_project_keeps_counting(self):
        # Planted negative: the other single signal, on its own.
        self.assertIsNone(PC.fixture_rule(self.rec_for(cwd=self.REAL_CWD)))

    def test_a_production_guard_with_real_denials_is_untouched(self):
        # A genuine denial from a real project cwd keeps ranking. (The original
        # form cited a-cost-guard's "2,365 live records, 0% synthesized" as
        # proof it must not move -- 2,313 of those were SITES/demo fixture
        # writes carrying real session ids, the blind spot the demo-cwd tests
        # below now pin.)
        self.assertIsNone(PC.fixture_rule(
            {"sig": "guard_blocked:a-cost-guard", "cwd": self.REAL_CWD,
             "session": "cda2bb6d"}))

    def test_a_demo_cwd_fixture_write_is_dropped(self):
        # /home/user/SITES/demo does not exist on disk; it is the literal cwd
        # a-cost-guard.test.js and an-admission-guard.test.js pin in their
        # payloads, so records land there only when a suite runs unsandboxed
        # (the frozen pre-an earlier change worktree) -- with a REAL session id, which is
        # exactly why the conjunctive rule above cannot catch them.
        for sig in ("guard_blocked:a-cost-guard",
                    "guard_blocked:an-admission-guard"):
            self.assertEqual(
                PC.fixture_rule({"sig": sig, "cwd": "/home/user/SITES/demo",
                                 "session": "cda2bb6d"}),
                "guard-suite-demo-cwd", sig)

    def test_a_demo_subdirectory_is_covered_but_a_prefix_sibling_is_not(self):
        self.assertEqual(
            PC.fixture_rule({"sig": "guard_blocked:a-vcs-guard",
                             "cwd": "/home/user/SITES/demo/sub",
                             "session": "cda2bb6d"}),
            "guard-suite-demo-cwd")
        # Planted negative: a real project whose name merely starts with
        # "demo" must keep counting.
        self.assertIsNone(PC.fixture_rule(
            {"sig": "guard_blocked:a-vcs-guard",
             "cwd": "/home/user/SITES/demo-app", "session": "cda2bb6d"}))

    def test_a_guard_denial_whose_command_works_in_a_tmp_scratch_repo_is_dropped(self):
        # Measured 2026-09-02: one harness-vet fixture ran `cd /tmp/hv-848-vet-2/
        # scratch-repo && git add -A && git commit ...` 1,000 times in a day --
        # real session, real worktree cwd, so neither conjunctive rule fires --
        # and it was 53% of every a-vcs-guard denial in the window.
        self.assertEqual(
            PC.fixture_rule({"sig": "guard_blocked:a-vcs-guard", "cwd": self.REAL_CWD,
                             "session": "cda2bb6d",
                             "cmd": "cd /tmp/hv-848-vet-2/scratch-repo && git add -A && git commit -q -F -"}),
            "guard-scratch-sandbox-cmd")
        # Planted negatives: the same command under a non-guard signature is
        # ordinary telemetry, and a cd into a real project keeps counting.
        self.assertIsNone(PC.fixture_rule(
            {"sig": "bash:exit code <n>", "cwd": self.REAL_CWD, "session": "cda2bb6d",
             "cmd": "cd /tmp/hv-848-vet-2/scratch-repo && git add -A"}))
        self.assertIsNone(PC.fixture_rule(
            {"sig": "guard_blocked:a-vcs-guard", "cwd": self.REAL_CWD, "session": "cda2bb6d",
             "cmd": "cd /home/user/SITES/example-project && git add -A"}))

    def test_non_guard_telemetry_at_the_demo_cwd_keeps_counting(self):
        # Planted negative: scope is guard_blocked:* -- ordinary friction
        # recorded with that cwd stays out of the fixture lane (the voluntary
        # add test above relies on exactly this).
        self.assertIsNone(PC.fixture_rule(
            {"sig": "no_such_file", "cwd": "/home/user/SITES/demo",
             "session": "cda2bb6d"}))

    def test_non_guard_telemetry_is_never_touched(self):
        # The rule is scoped to guard_blocked:*; ordinary friction is out of
        # scope even from a hook directory with no session id.
        self.assertIsNone(PC.fixture_rule(
            {"sig": "no_such_file", "cwd": self.HOOK_CWD, "session": "d-318283"}))

    def test_a_record_with_no_cwd_keeps_counting(self):
        # The python suite's own fixtures carry no cwd key at all.
        self.assertIsNone(PC.fixture_rule(
            {"sig": "guard_blocked:a-vcs-guard", "session": "d-318283"}))

    def test_the_route_guard_rules_still_fire(self):
        # Regression: an earlier change's original lane must not be narrowed by this change.
        self.assertEqual(
            PC.fixture_rule({"sig": "guard_blocked:a-route-guard",
                             "cwd": "/parent/workspace", "session": "cda2bb6d"}),
            "route-guard-fixture-cwd")

    def test_dropped_records_are_counted_not_silent(self):
        self.write("-p", [
            self.rec(sig="guard_blocked:a-vcs-guard", session="d-318283",
                     cwd=self.HOOK_CWD, days_ago=1),
            self.rec(sig="guard_blocked:a-vcs-guard", session="cda2bb6d",
                     cwd=self.REAL_CWD, days_ago=1),
        ])
        kept = [r["session"] for r in PC.read_records(7)]
        self.assertEqual(kept, ["cda2bb6d"], "only the genuine record ranks")
        self.assertEqual(PC.count_fixture_records(7), 1,
                         "the dropped record must still be reported as a count")


class TestDispatchGuidance(unittest.TestCase):
    """adopt's closing line, which used to name this harness's queue always."""

    def setUp(self):
        self.saved = (PC.DISPATCH_READY_LABEL, PC.DISPATCH_DOCS_REF)

    def tearDown(self):
        PC.DISPATCH_READY_LABEL, PC.DISPATCH_DOCS_REF = self.saved

    def guidance(self, kind="issue"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            PC.print_adopt_next_step({"kind": kind})
        return buf.getvalue()

    def test_no_queue_configured_says_nothing(self):
        # A stranger has no queue: silence beats instructions for machinery
        # they do not have, and a doc reference that resolves to nothing.
        PC.DISPATCH_READY_LABEL = ""
        self.assertEqual(self.guidance(), "")

    def test_a_configured_label_is_used_verbatim(self):
        PC.DISPATCH_READY_LABEL = "ready-for-bot"
        PC.DISPATCH_DOCS_REF = ""
        out = self.guidance()
        self.assertIn("tag it ready-for-bot", out)
        self.assertNotIn("the dispatch-ready label", out)
        self.assertNotIn("(", out, "an unset doc reference must drop its parenthetical")

    def test_a_pull_request_locator_still_gets_a_next_step(self):
        # Reachable via reconciliation of a legacy PR adoption, which is why
        # the pr branch survived the trivial-route deletion.
        self.assertIn("merge the pull request", self.guidance(kind="pr"))
        PC.DISPATCH_READY_LABEL = ""
        out = self.guidance(kind="pr")
        self.assertIn("merge the pull request", out)
        self.assertNotIn("an autonomous queue", out)


class TestLegacyLabelCreate(PapercutBase):
    """The legacy rollup --apply label path must not restyle what it finds.

    ensure_adoption_labels fixed this on the adopt path 2026-08-26, after
    measuring that --force would have rewritten this org's curated `work-spec`
    label while filing an unrelated item. The same defect survived here.
    """

    def calls_for(self, present):
        calls = []

        def fake_gh(*argv, **kwargs):
            calls.append(list(map(str, argv)))
            if argv[:2] == ("label", "list"):
                body = '[{"name": "papercut"}]' if present else "[]"
                return 0, body
            return 0, ""

        original, PC.gh = PC.gh, fake_gh
        try:
            ok = PC.ensure_label("o/r")
        finally:
            PC.gh = original
        return ok, calls

    def test_an_existing_label_is_left_alone(self):
        ok, calls = self.calls_for(present=True)
        self.assertTrue(ok)
        created = [c for c in calls if c[:2] == ["label", "create"]]
        self.assertEqual(created, [], "an existing label must not be rewritten")

    def test_a_missing_label_is_created_without_force(self):
        ok, calls = self.calls_for(present=False)
        self.assertTrue(ok)
        created = [c for c in calls if c[:2] == ["label", "create"]]
        self.assertEqual(len(created), 1)
        self.assertNotIn("--force", created[0],
                         "--force rewrites colour and description on an existing label")

    def test_an_unreadable_label_list_fails_closed(self):
        # An unreadable remote is not permission to restyle whatever is there.
        original, PC.gh = PC.gh, lambda *a, **k: (1, "dial tcp: connection refused")
        try:
            self.assertFalse(PC.ensure_label("o/r"))
        finally:
            PC.gh = original


class TestWorkSpecGate(unittest.TestCase):
    """The adopt render gate, after it stopped shelling out to a sibling path."""

    BODY = ("## Acceptance Criteria\nit works\n\n"
            "## Planted negative\nit fails loudly\n\n"
            "## No-Claim Boundary\nsays nothing else\n")

    def setUp(self):
        # Pinned explicitly: this class tests the gate MECHANISM, and must hold
        # whether the installed default is the harness's three sections or the
        # packaged copy's empty tuple.
        self.saved = PC.WORK_SPEC_SECTIONS
        PC.WORK_SPEC_SECTIONS = (
            "Acceptance Criteria", "Planted negative", "No-Claim Boundary")

    def tearDown(self):
        PC.WORK_SPEC_SECTIONS = self.saved

    def test_a_complete_body_passes(self):
        ok, err = PC.work_spec_gate(self.BODY)
        self.assertTrue(ok, err)

    def test_a_missing_section_is_named(self):
        body = self.BODY.replace("## Planted negative\nit fails loudly\n", "")
        ok, err = PC.work_spec_gate(body)
        self.assertFalse(ok)
        self.assertIn("Planted negative", err)

    def test_an_empty_section_counts_as_missing(self):
        body = self.BODY.replace("it fails loudly", "")
        ok, err = PC.work_spec_gate(body)
        self.assertFalse(ok)
        self.assertIn("Planted negative", err)

    def test_it_spawns_no_subprocess(self):
        # The portability defect: the old shellout resolved a path that does
        # not exist in a packaged layout, so every adopt failed the gate.
        original = PC.subprocess.run
        PC.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("work_spec_gate must not shell out"))
        try:
            self.assertTrue(PC.work_spec_gate(self.BODY)[0])
        finally:
            PC.subprocess.run = original

    def test_an_empty_section_list_passes_everything(self):
        # The packaged default: a stranger's adopt is not gated on headings
        # their process never defined.
        PC.WORK_SPEC_SECTIONS = ()
        ok, err = PC.work_spec_gate("nothing in here at all")
        self.assertTrue(ok, err)

    def test_the_gate_now_agrees_with_the_renderer(self):
        # Deliberate behavior change. The old shellout accepted a suffixed
        # heading that markdown_section -- the extractor adopt renders with --
        # reads as missing, so the gate could pass text the renderer dropped.
        body = self.BODY.replace("## Acceptance Criteria",
                                 "## Acceptance Criteria (required)")
        self.assertEqual(PC.markdown_section(body, "Acceptance Criteria"), "",
                         "precondition: the renderer reads this as missing")
        ok, err = PC.work_spec_gate(body)
        self.assertFalse(ok, "the gate must not pass what the renderer drops")
        self.assertIn("Acceptance Criteria", err)


class TestTargetRendering(PapercutBase):
    """A non-Bash failure must show what it failed on."""

    def test_show_renders_the_target_when_there_is_no_command(self):
        self.write("-p", [self.rec(sig="read:too big", days_ago=1,
                                   target="/home/user/SITES/demo/huge.md")])
        buf = io.StringIO()
        args = argparse.Namespace(sig="read:too big", days=7, limit=10)
        with contextlib.redirect_stdout(buf):
            PC.cmd_show(args)
        self.assertIn("-> /home/user/SITES/demo/huge.md", buf.getvalue())

    def test_a_bash_command_still_renders_with_its_dollar_prefix(self):
        self.write("-p", [self.rec(sig="bash:boom", days_ago=1, cmd="pytest -q")])
        buf = io.StringIO()
        args = argparse.Namespace(sig="bash:boom", days=7, limit=10)
        with contextlib.redirect_stdout(buf):
            PC.cmd_show(args)
        self.assertIn("$ pytest -q", buf.getvalue())


class TestConfigOverrideLayer(unittest.TestCase):
    """The override layer, and the guarantee that it is inert by default.

    Stage 1 of the extraction plan. Its whole promise is that the operator's
    machine keeps today's behavior because it keeps today's defaults, so the
    load-bearing test here is the first one: with no config file and no env
    vars, nothing moves.
    """

    NAMES = ("STORE", "ISSUE_LABEL", "WORK_SPEC_LABEL", "KNOWN_GUARDS",
             "GH_LIST_LIMIT", "VERIFY_WINDOW_DAYS", "VERIFY_EXPOSURE_FLOOR",
             "TRIAGE_UNFAMILIED_LIMIT", "DOSSIER_PROJECT_CAP",
             "RESOLVED", "FAMILIES", "FILINGS", "DOSSIERS", "ADOPT_LABELS")

    def setUp(self):
        self.saved = {name: getattr(PC, name) for name in self.NAMES}
        self.saved_env = {k: v for k, v in os.environ.items()
                          if k.startswith("PAPERCUT_") or k == "CLAUDE_CONFIG_DIR"}
        self.home = Path(tempfile.mkdtemp(prefix="pc-config-"))
        for key in list(os.environ):
            if key.startswith("PAPERCUT_"):
                del os.environ[key]
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
        PC._CONFIG_LOADED = False

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(PC, name, value)
        for key in list(os.environ):
            if key.startswith("PAPERCUT_") or key == "CLAUDE_CONFIG_DIR":
                del os.environ[key]
        os.environ.update(self.saved_env)
        PC._CONFIG_LOADED = False
        shutil.rmtree(self.home, ignore_errors=True)

    def write_config(self, payload):
        (self.home / "papercut.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_with_no_config_and_no_env_nothing_moves(self):
        """The zero-behavior-change guarantee, asserted rather than assumed."""
        before = {name: getattr(PC, name) for name in self.NAMES}
        PC.load_config()
        after = {name: getattr(PC, name) for name in self.NAMES}
        self.assertEqual(before, after,
                         "an unconfigured run must leave every constant untouched")

    def test_an_env_override_applies_and_derived_paths_follow(self):
        # The trap this exists for: the four state paths are computed from
        # STORE at import and freeze. Probed 2026-08-29 -- setting STORE alone
        # left RESOLVED on the old store, so the tool read one store and wrote
        # another.
        os.environ["PAPERCUT_STORE"] = str(self.home / "elsewhere")
        PC.load_config()
        self.assertEqual(PC.STORE, self.home / "elsewhere")
        self.assertEqual(PC.RESOLVED, PC.STORE / "state" / "resolved.jsonl")
        self.assertEqual(PC.FAMILIES, PC.STORE / "state" / "families.jsonl")
        self.assertEqual(PC.FILINGS, PC.STORE / "state" / "filings.jsonl")
        self.assertEqual(PC.DOSSIERS, PC.STORE / "state" / "dossiers")

    def test_adopt_labels_are_derived_too_not_just_paths(self):
        # ADOPT_LABELS is built from the two label names rather than read from
        # them, so it is the same freeze-at-import trap in different clothes.
        self.write_config({"work_spec_label": "planned-work"})
        PC.load_config()
        self.assertEqual(PC.WORK_SPEC_LABEL, "planned-work")
        self.assertEqual(PC.ADOPT_LABELS, (PC.ISSUE_LABEL, "planned-work"))

    def test_a_config_file_override_applies_with_its_json_type(self):
        self.write_config({"known_guards": ["alpha-guard", "beta-guard"],
                           "verify_window_days": 45})
        PC.load_config()
        self.assertEqual(PC.KNOWN_GUARDS, ("alpha-guard", "beta-guard"))
        self.assertEqual(PC.VERIFY_WINDOW_DAYS, 45)

    def test_env_beats_the_config_file(self):
        self.write_config({"verify_window_days": 45})
        os.environ["PAPERCUT_VERIFY_WINDOW_DAYS"] = "60"
        PC.load_config()
        self.assertEqual(PC.VERIFY_WINDOW_DAYS, 60,
                         "a one-off run must override a checked-in default")

    def test_a_comma_list_is_accepted_from_the_environment(self):
        os.environ["PAPERCUT_KNOWN_GUARDS"] = "one-guard, two-guard"
        PC.load_config()
        self.assertEqual(PC.KNOWN_GUARDS, ("one-guard", "two-guard"))

    def test_an_unknown_config_key_is_refused_not_ignored(self):
        # A silently ignored override is a config that lies about itself.
        self.write_config({"verify_windwo_days": 45})
        with self.assertRaises(SystemExit):
            PC.load_config()

    def test_a_value_of_the_wrong_type_is_refused(self):
        self.write_config({"verify_window_days": "not-a-number"})
        with self.assertRaises(SystemExit):
            PC.load_config()

    def test_loading_twice_is_a_no_op(self):
        os.environ["PAPERCUT_VERIFY_WINDOW_DAYS"] = "60"
        PC.load_config()
        os.environ["PAPERCUT_VERIFY_WINDOW_DAYS"] = "90"
        PC.load_config()
        self.assertEqual(PC.VERIFY_WINDOW_DAYS, 60,
                         "config resolves once per process, not per call")


class TestGhDiagnostics(PapercutBase):
    """A missing or unauthenticated gh must name itself.

    Before this, str(FileNotFoundError) for gh was "[Errno 2] No such file or
    directory: 'gh'", which REMOTE_MISSING_RE matches on "no such". Every
    caller asking "is the remote missing?" got yes -- about the repository.
    """

    def no_gh_path(self):
        empty = self.store / "nogh-bin"
        empty.mkdir(exist_ok=True)
        return str(empty)

    def test_a_missing_gh_is_not_mistaken_for_a_missing_repository(self):
        original, PC._GH_DIAGNOSED = os.environ["PATH"], set()
        os.environ["PATH"] = self.no_gh_path()
        try:
            code, out = PC.gh("repo", "view", "o/r", "--json", "nameWithOwner")
            self.assertEqual(code, 1)
            self.assertIn("unavailable", out)
            self.assertIsNone(
                PC.REMOTE_MISSING_RE.search(out),
                "the unavailable-tool message must not match the remote-missing regex")
            self.assertIsNone(
                PC.repo_exists("o/r"),
                "a missing gh means 'could not answer', never 'the repo does not exist'")
        finally:
            os.environ["PATH"] = original

    def test_the_missing_tool_is_named_once_not_once_per_call(self):
        original, PC._GH_DIAGNOSED = os.environ["PATH"], set()
        os.environ["PATH"] = self.no_gh_path()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                for _ in range(4):
                    PC.gh("repo", "view", "o/r")
        finally:
            os.environ["PATH"] = original
        said = err.getvalue()
        self.assertEqual(said.count("is unavailable"), 1,
                         "one rollup calls gh per family; the cause is stated once")
        self.assertIn("cli.github.com", said, "the diagnostic must name the remedy")

    def test_a_missing_gh_is_not_read_as_an_empty_work_queue(self):
        """The safety-critical half of the same bug.

        REMOTE_MISSING_RE matched "[Errno 2] No such file or directory: 'gh'"
        on "no such", so list_items_or_refuse took the "repository does not
        exist, therefore its queue holds nothing" branch and returned [] --
        without having read anything at all. That empty list is what the
        global open-work cap counts and what the duplicate-marker search
        trusts, so a machine with no gh could file past the cap and refile an
        item that already existed.
        """
        original, PC._GH_DIAGNOSED = os.environ["PATH"], set()
        os.environ["PATH"] = self.no_gh_path()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                with self.assertRaises(ValueError):
                    PC.list_items_or_refuse(
                        "issue", "open papercut issues in o/r", "--repo", "o/r")
        finally:
            os.environ["PATH"] = original

    def test_an_auth_failure_names_the_login_command(self):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "gh: To get started with GitHub CLI, please run: gh auth login"

        original, PC._GH_DIAGNOSED = PC.subprocess.run, set()
        PC.subprocess.run = lambda *a, **k: Result()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                code, _ = PC.gh("issue", "list")
        finally:
            PC.subprocess.run = original
        self.assertEqual(code, 1, "the non-zero return must survive: callers fail closed on it")
        self.assertIn("gh auth login", err.getvalue())


class TestResolveVisibility(PapercutBase):
    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run([sys.executable, str(PAPERCUT), *argv],
                              capture_output=True, text=True, timeout=30,
                              check=False, env=env)

    def test_resolve_echoes_what_it_suppresses(self):
        self.write("-p", [self.rec(sig="guard_blocked:a-command-guard", session=f"s{i}") for i in range(4)])
        out = self.run_cli("resolve", "guard_blocked:a-command-guard").stdout
        self.assertIn("4 occurrence(s)", out)
        self.assertIn("session(s)", out)

    def test_resolve_warns_on_a_signature_with_no_occurrences(self):
        out = self.run_cli("resolve", "typo:doesnotexist").stdout
        self.assertIn("WARNING", out, "a typo'd resolve must not look like a real suppression")

    def test_show_surfaces_the_resolution_note(self):
        self.write("-p", [self.rec(sig="x")])
        self.run_cli("resolve", "x", "-n", "fixed in PR 999")
        out = self.run_cli("show", "x").stdout
        self.assertIn("fixed in PR 999", out, "--note would otherwise be write-only")


class TestRedaction(PapercutBase):
    # Mirrors the JS cases in papercut-log.test.js. The two REDACTIONS lists must
    # agree, so both copies get the same corpus.
    SECRETS = [
        ('curl -H "Authorization: Bearer sk-abc123def456ghi789"', "sk-abc123def456ghi789"),  # gitleaks:allow
        ("psql postgres://admin:hunter2swordfish@db:5432/app", "hunter2swordfish"),  # gitleaks:allow
        ("token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"),  # gitleaks:allow
        ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),  # gitleaks:allow
        ("PGPASSWORD=s3cr3tvalue psql -h db", "s3cr3tvalue"),  # gitleaks:allow
        ('{"api_key": "abcd1234efgh5678"}', "abcd1234efgh5678"),  # gitleaks:allow
        ("stripe error: using key sk_live_51H8xAbCdEfGhIjKlMnOp", "sk_live_51H8xAbCdEfGhIjKlMnOp"),  # gitleaks:allow
        ("stripe test mode pk_test_51H8xAbCdEfGhIjKlMnOp failed", "pk_test_51H8xAbCdEfGhIjKlMnOp"),  # gitleaks:allow
        ("GET https://maps.googleapis.com/api?key=AIzaSyD1234567890abcdefghijklmnopqrst",  # gitleaks:allow
         "AIzaSyD1234567890abcdefghijklmnopqrst"),  # gitleaks:allow
        ("jwt rejected: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",  # gitleaks:allow
         "dBjftJeZ4CVPmB92K27uhbUJU1p1r"),  # gitleaks:allow
    ]

    def test_scrubs_every_credential_shape(self):
        for text, secret in self.SECRETS:
            out = PC.redact(text)
            self.assertNotIn(secret, out, f"secret survived: {text} -> {out}")
            self.assertIn("<redacted>", out)

    def test_leaves_ordinary_friction_text_intact(self):
        plain = "/bin/bash: line 1: pytest: command not found"
        self.assertEqual(PC.redact(plain), plain)

    def test_add_redacts_before_writing(self):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        subprocess.run(
            [sys.executable, str(PAPERCUT), "add", "-q", "--cwd", "/x",
             "-m", "curl failed with Authorization: Bearer sk-livetoken1234567890"],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
        raw = (self.store / "-x.jsonl").read_text()
        self.assertNotIn("sk-livetoken1234567890", raw)
        self.assertIn("<redacted>", raw)

    def test_rollup_samples_redact_records_written_before_this_change(self):
        # Simulates a store populated by the pre-redaction hook: the rollup copies
        # samples into a GitHub issue body, so it must scrub on the way out too.
        self.write("-p", [
            self.rec(sig="x", session=f"s{i}", err="fatal: auth failed for ghp_LeakedTokenAAAAAAAAAAAA1234")
            for i in range(3)
        ])
        entry = PC.rank(PC.read_records(7))[0]
        self.assertTrue(entry["samples"])
        self.assertNotIn("ghp_LeakedTokenAAAAAAAAAAAA1234", " ".join(entry["samples"]))
        self.assertNotIn("ghp_LeakedTokenAAAAAAAAAAAA1234", PC.issue_body(entry, 7))


class TestResolution(PapercutBase):
    """Resolution tracking: every mature public implementation has it, and without
    it a signature nags forever after the underlying issue is fixed."""

    def setUp(self):
        super().setUp()
        PC.RESOLVED = self.store / "state" / "resolved.jsonl"

    def resolve(self, sig, action="resolve", ts=None):
        PC.RESOLVED.parent.mkdir(parents=True, exist_ok=True)
        with open(PC.RESOLVED, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"sig": sig, "ts": ts or iso(0), "action": action}) + "\n")

    def test_resolutions_live_outside_the_record_glob(self):
        # A resolve event must never be readable as a friction record.
        self.resolve("x")
        self.assertEqual(list(PC.read_records(7)), [])
        self.assertIn("x", PC.read_resolutions())

    def test_resolved_signature_is_hidden(self):
        self.write("-p", [self.rec(sig="x", days_ago=1)])
        self.resolve("x")
        recs = list(PC.read_records(7))
        self.assertTrue(PC.is_resolved("x", recs, PC.read_resolutions()))

    def test_recurrence_after_a_resolve_unhides_it(self):
        # The most valuable thing the log can report: it came back after the fix.
        self.resolve("x", ts=iso(2))
        self.write("-p", [self.rec(sig="x", days_ago=0)])
        recs = list(PC.read_records(7))
        self.assertFalse(PC.is_resolved("x", recs, PC.read_resolutions()),
                         "a regression after a resolve must resurface")

    def test_reopen_undoes_a_resolve(self):
        self.write("-p", [self.rec(sig="x", days_ago=1)])
        self.resolve("x", ts=iso(3))
        self.resolve("x", action="reopen", ts=iso(0))
        recs = list(PC.read_records(7))
        self.assertFalse(PC.is_resolved("x", recs, PC.read_resolutions()))

    def test_latest_event_wins(self):
        self.write("-p", [self.rec(sig="x", days_ago=5)])
        self.resolve("x", action="reopen", ts=iso(4))
        self.resolve("x", action="resolve", ts=iso(1))
        recs = list(PC.read_records(7))
        self.assertTrue(PC.is_resolved("x", recs, PC.read_resolutions()))

    def test_rollup_suppresses_resolved_signatures(self):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        self.write("-p", [self.rec(sig="noisy", session=f"s{i}") for i in range(4)])
        self.resolve("noisy")
        out = subprocess.run(
            [sys.executable, str(PAPERCUT), "rollup", "--days", "7"],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        ).stdout
        self.assertIn("papercuts-flagged:0", out, "a resolved signature must not keep toasting")
        self.assertIn("resolved signature(s) suppressed", out)


class TestClosedIssueHandling(PapercutBase):
    """rollup --apply must respect an operator closing an issue. Without this it
    comments 'Still recurring' on a closed issue forever, contradicting the text
    printed into every issue body."""

    def test_find_existing_requests_state(self):
        seen = {}

        def fake_gh(*argv, check=False):
            seen["argv"] = argv
            return 0, json.dumps([{"number": 7, "body": PC.SIG_MARKER.format(sig="x"),
                                   "state": "CLOSED", "closedAt": "2026-08-01T00:00:00Z"}])

        orig, PC.gh = PC.gh, fake_gh
        try:
            issue = PC.find_existing("o/r", "x")
        finally:
            PC.gh = orig
        self.assertIn("state", " ".join(seen["argv"]),
                      "state must be requested or closed issues cannot be detected")
        self.assertEqual(issue["state"], "CLOSED")
        self.assertEqual(issue["number"], 7)

    def test_ensure_label_is_idempotent_and_targets_the_repo(self):
        # Same intent as before -- idempotent, and aimed at the right repo --
        # but idempotence is now check-then-create rather than --force. Creating
        # an EXISTING label with --force rewrites its colour and description;
        # measured 2026-08-26, that would have restyled this org's curated
        # `work-spec` label while filing an unrelated item. The assertion moved
        # with the mechanism; the guarantee did not weaken.
        calls = []

        def fake_gh(*argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ("label", "list"):
                return 0, "[]"           # absent, so the create path runs
            return 0, ""

        orig, PC.gh = PC.gh, fake_gh
        try:
            self.assertTrue(PC.ensure_label("o/r"))
        finally:
            PC.gh = orig
        joined = [" ".join(map(str, c)) for c in calls]
        self.assertTrue(any("label list" in c and "o/r" in c for c in joined),
                        "existence must be read before anything is created")
        created = [c for c in joined if "label create" in c]
        self.assertEqual(len(created), 1)
        self.assertIn("o/r", created[0])
        self.assertNotIn("--force", created[0])


class TestStaleness(PapercutBase):
    """HOME is isolated in every test here: cmd_staleness reads
    $HOME/.claude/projects, so without it these read the real machine and the
    result depends on whatever sessions happen to be running."""

    def staleness(self, *argv, sessions=True):
        home = Path(tempfile.mkdtemp(prefix="papercut-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        projects = home / ".claude" / "projects"
        projects.mkdir(parents=True)
        if sessions:
            (projects / "-some-project").mkdir()
        env = dict(os.environ, PAPERCUT_STORE=str(self.store),
                   HOME=str(home), USERPROFILE=str(home))
        return subprocess.run(
            [sys.executable, str(PAPERCUT), "staleness", *argv],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        ).stdout

    def test_empty_store_with_active_sessions_warns_specifically(self):
        # The dead-index shape: capture is dead but silence looks like "nothing happened".
        out = self.staleness()
        self.assertIn("WARN papercut capture", out)
        self.assertIn("EMPTY", out)

    def test_fresh_records_report_healthy(self):
        self.write("-p", [self.rec()])
        out = self.staleness()
        self.assertIn("healthy", out)
        self.assertNotIn("WARN", out)

    def test_no_session_activity_is_not_a_warning(self):
        out = self.staleness(sessions=False)
        self.assertIn("no session activity", out)
        self.assertNotIn("WARN", out)

    def test_stale_records_warn(self):
        self.write("-p", [self.rec(days_ago=10)])
        # Backdate the store so it is provably older than session activity.
        old = time.time() - 10 * 86400
        os.utime(self.store / "-p.jsonl", (old, old))
        out = self.staleness("--max-gap-hours", "1")
        self.assertIn("WARN papercut capture stale", out)


class TestHelpers(unittest.TestCase):
    def test_project_slug_matches_the_hook(self):
        self.assertEqual(PC.project_slug("/home/user/SITES/example-project"), "-home-user-SITES-example-project")

    def test_sig_marker_roundtrips_for_issue_dedupe(self):
        marker = PC.SIG_MARKER.format(sig="command_not_found:pytest")
        body = PC.issue_body(
            {"sig": "command_not_found:pytest", "count": 9, "sessions": 4,
             "projects": ["-a"], "samples": ["pytest: command not found"]}, 7)
        self.assertIn(marker, body, "the rollup finds existing issues by this exact marker")


class TestSelfReportGuardAttribution(PapercutBase):
    """The self-report path reproduced the same fragmentation the automatic path
    already fixed: 17 of 46 self-reports were about a-command-guard, but only 3 used
    guard_blocked:a-command-guard, so 8 sessions of a-command-guard friction surfaced in the rollup as 3."""

    def add(self, msg, *extra):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        subprocess.run([sys.executable, str(PAPERCUT), "add", "-q", "-m", msg, "--cwd", "/x", *extra],
                       capture_output=True, text=True, timeout=30, check=False, env=env)
        return json.loads((self.store / "-x.jsonl").read_text().strip().split("\n")[-1])

    def test_differently_worded_guard_reports_share_one_signature(self):
        # Explicit vocabulary, injected through the REAL config seam: add() runs
        # the CLI as a subprocess, so rebinding PC.KNOWN_GUARDS here never
        # reaches it -- that rebind passed in the source harness only because
        # the default list happened to contain the name. The env override is
        # what a stranger would actually use, and it must hold when the
        # installed default is empty, as it is in the packaged copy.
        saved = os.environ.get("PAPERCUT_KNOWN_GUARDS")
        os.environ["PAPERCUT_KNOWN_GUARDS"] = "a-command-guard"
        try:
            a = self.add("a-command-guard blocked a truncating redirect into $A_JOB_DIR/tmp")
            b = self.add("hit a a-command-guard block on piping into node; had to use a temp file")
        finally:
            if saved is None:
                del os.environ["PAPERCUT_KNOWN_GUARDS"]
            else:
                os.environ["PAPERCUT_KNOWN_GUARDS"] = saved
        self.assertEqual(a["sig"], "guard_blocked:a-command-guard")
        self.assertEqual(a["sig"], b["sig"], "same guard, different prose -> one bucket")

    def test_each_known_guard_is_recognised(self):
        for guard in PC.KNOWN_GUARDS:
            self.assertEqual(PC.guard_in_message(f"{guard} got in the way again"), guard)

    def test_explicit_sig_still_wins(self):
        rec = self.add("a-command-guard blocked something", "--sig", "custom:thing")
        self.assertEqual(rec["sig"], "custom:thing")

    def test_non_guard_message_keeps_a_prose_slug(self):
        rec = self.add("the a-cli CLI truncates rows without warning")
        self.assertTrue(rec["sig"].startswith("self:"), rec["sig"])

    def test_guard_name_is_not_matched_inside_another_word(self):
        self.assertIsNone(PC.guard_in_message("the abcdcgxyz token is unrelated"))


class TestSignalLineParity(PapercutBase):
    """Operator-facing output must show the line signature() keyed on. It showed
    'Exit code 1' for every Bash record while the real error sat on line 2+."""

    def test_a_banner_first_output_keys_on_its_trailing_summary(self):
        text = ("Exit code 1\nChecking formatting...\n[warn] a.ts\n"
                "[warn] Code style issues found in 2 files. "
                "Run Prettier with --write to fix.")
        self.assertEqual(PC.signal_line(text),
                         "[warn] Code style issues found in 2 files. "
                         "Run Prettier with --write to fix.")

    def test_a_lone_progress_banner_still_keys_on_itself(self):
        self.assertEqual(PC.signal_line("Exit code 1\nChecking formatting..."),
                         "Checking formatting...")

    def test_an_errorish_first_line_ending_in_ellipsis_is_kept(self):
        # The skip is for bare banners only; an error that happens to end in
        # an ellipsis keeps its established key (no signature drift).
        self.assertEqual(
            PC.signal_line("Exit code 1\nerror: connection failed...\ndetail"),
            "error: connection failed...")

    def test_a_python_traceback_keys_on_its_exception_line(self):
        # Same fixture the hook keys on: the header names the shape, the
        # exception on the LAST line names the cause.
        text = ('Exit code 1\nTraceback (most recent call last):\n'
                '  File "<string>", line 1\n'
                'json.decoder.JSONDecodeError: Expecting value')
        self.assertEqual(PC.signal_line(text),
                         "json.decoder.JSONDecodeError: Expecting value")

    def test_a_node_internal_frame_keys_on_its_error_line(self):
        text = ("Exit code 1\nnode:internal/modules/run_main:107\n"
                "    triggerUncaughtException(\n    ^\n\n"
                "Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/repo/bin/x.ts'"
                " imported from /repo/")
        self.assertEqual(
            PC.signal_line(text),
            "Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/repo/bin/x.ts'"
            " imported from /repo/")

    def test_a_node_frame_with_no_error_line_keeps_the_frame(self):
        self.assertEqual(
            PC.signal_line("Exit code 1\nnode:internal/process/promises:394\n"
                           "    triggerUncaughtException("),
            "node:internal/process/promises:394")

    def test_skips_an_injected_claude_code_hint_line(self):
        # Harness metadata, same class as the exit-code wrapper: show must
        # surface the real cause under it, not the hint.
        text = ('Exit code 1\n'
                '<claude-code-hint v="1" type="plugin"'
                ' value="vercel@claude-plugins-official" />\n'
                "Error: Your codebase isn't linked to a project on Vercel.")
        self.assertEqual(
            PC.signal_line(text),
            "Error: Your codebase isn't linked to a project on Vercel.")

    def test_skips_the_exit_code_wrapper(self):
        self.assertEqual(PC.signal_line("Exit code 1\ntsc: type error in foo.ts"),
                         "tsc: type error in foo.ts")

    def test_wrapper_alone_is_better_than_nothing(self):
        self.assertEqual(PC.signal_line("Exit code 127"), "Exit code 127")

    def test_show_displays_the_real_error_not_the_wrapper(self):
        self.write("-p", [self.rec(sig="x", err="Exit code 2\nls: cannot access '/nope': No such file or directory")])
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        out = subprocess.run([sys.executable, str(PAPERCUT), "show", "x", "--days", "7"],
                             capture_output=True, text=True, timeout=30, check=False, env=env).stdout
        self.assertIn("cannot access", out)
        self.assertNotIn("Exit code 2", out)


class TestQuarantineClassifier(unittest.TestCase):
    """quarantine_rule() against the junk classes the 2026-08-26 live-corpus
    census measured (33,379 events / 3,873 signatures; 7 of the top-50 rows were
    capture defects). Exact strings — the hook has already normalized them."""

    ESC = "\x1b"
    JUNK = {
        "bash:{": "no-signal-residue",
        "bash:[]": "no-signal-residue",
        "bash:<path>": "no-signal-residue",
        "bash:<path>:<n>": "no-signal-residue",
        "bash:exit code <n>": "generic-exit-code",
        "timed_out": "mixed-timeout",
        (f"bash:{ESC}[<n>m{ESC}[<n>mbun test {ESC}[<n>m{ESC}[<n>m"
         f"v<n>.<n>.<n> (<hex>){ESC}[<n>m"): "ansi-escape",
    }
    COHERENT_CONTROLS = [
        "read:file content (<n> tokens) exceeds maximum allowed tokens (<n>). "
        "use offset and limit parameters to read specific portion",
        "bash:this session is isolated in the worktree <path> but this command "
        "is too complex to verify that it stays inside the workt",
        "websearch:api error: <n> no configured provider route implements the "
        "requested server-tool semantics. this is a server-side issue,",
    ]

    def test_progress_and_listing_lines_are_quarantined(self):
        self.assertEqual(PC.quarantine_rule("bash:checking formatting..."),
                         "progress-line")
        self.assertEqual(
            PC.quarantine_rule(
                "bash:-rw-r--r-- <n> will will <n> aug <n> <n>:<n> <path>"),
            "file-listing-line")
        self.assertEqual(PC.quarantine_rule("bash:total <n>"),
                         "file-listing-line")
        self.assertEqual(
            PC.quarantine_rule("bash:drwxr-xr-x <n> will will <n> aug <n> <path>"),
            "file-listing-line")

    def test_the_trailing_summary_key_the_hook_now_prefers_is_coherent(self):
        # The very key the banner-skip produces must never land in quarantine,
        # or the fix would move records from one dead lane to another.
        self.assertIsNone(PC.quarantine_rule(
            "bash:[warn] code style issues found in <n> files. "
            "run prettier with --write to fix."))

    def test_census_junk_classes_are_quarantined(self):
        for sig, rule in self.JUNK.items():
            self.assertEqual(PC.quarantine_rule(sig), rule, f"junk not caught: {sig!r}")

    def test_coherent_controls_are_untouched(self):
        for sig in self.COHERENT_CONTROLS:
            self.assertIsNone(PC.quarantine_rule(sig), f"coherent signature quarantined: {sig!r}")

    # Planted negatives: each pair differs ONLY in the one dimension that makes
    # the key coherent, so an over-broad rule fails exactly here.
    def test_placeholder_plus_cause_text_is_not_junk(self):
        self.assertIsNone(PC.quarantine_rule("bash:<path> permission denied"))
        self.assertEqual(PC.quarantine_rule("bash:<path>"), "no-signal-residue")

    def test_exit_code_with_trailing_cause_is_not_junk(self):
        self.assertIsNone(PC.quarantine_rule("bash:exit code <n>: permission denied"))
        self.assertEqual(PC.quarantine_rule("bash:exit code <n>"), "generic-exit-code")

    def test_escape_stripped_banner_is_not_junk(self):
        # The raw-ESC banner is junk because of its ESC bytes alone; the same
        # key with styling stripped names a real subject and must pass.
        self.assertIsNone(PC.quarantine_rule("bash:bun test v<n>.<n>.<n> (<hex>)"))

    def test_any_raw_esc_signature_is_junk_not_just_the_census_banner(self):
        # Mutation check: an implementation that string-matches the one census
        # banner passes the banner test but misses every other ESC-bearing key.
        # These shapes are live-observed (list --quarantined, 2026-08-26).
        for sig in (f"bash:{self.ESC}[<n>mundefined{self.ESC}[<n>m",
                    f"bash:{self.ESC}[<n>m{self.ESC}[<n>m",
                    f"read:{self.ESC}[2k some wrapped progress line"):
            self.assertEqual(PC.quarantine_rule(sig), "ansi-escape", f"ESC escaped the rule: {sig!r}")

    def test_category_key_with_collapsed_discriminator_is_junk(self):
        # Intended, not accidental: a category prefix names the failure CLASS,
        # but with a collapsed discriminator the bucket cannot rank a fix (a
        # live record keyed command_not_found:1 was actually a missing
        # systemctl). Same reasoning that quarantines `timed_out`.
        self.assertEqual(PC.quarantine_rule("no_such_file:."), "no-signal-residue")
        self.assertEqual(PC.quarantine_rule("command_not_found:1"), "no-signal-residue")
        # ...while an intact discriminator keeps the key in the fix queue.
        self.assertIsNone(PC.quarantine_rule("command_not_found:pytest"))
        self.assertIsNone(PC.quarantine_rule("no_such_file:node modules zod"))

    def test_existing_fixture_signatures_stay_coherent(self):
        for sig in ("command_not_found:pytest", "guard_blocked:a-command-guard",
                    "self:gsc-row-limit-truncates", "x", "spread", "noisy_retry"):
            self.assertIsNone(PC.quarantine_rule(sig), f"fixture signature quarantined: {sig!r}")


class TestQuarantineRank(PapercutBase):
    def test_every_ranked_row_carries_its_classification(self):
        # Classified once, in rank(), so list/rollup/apply can never disagree.
        self.write("-p", [self.rec(sig="timed_out"), self.rec(sig="x")])
        by_sig = {r["sig"]: r["quarantine"] for r in PC.rank(PC.read_records(7))}
        self.assertEqual(by_sig["timed_out"], "mixed-timeout")
        self.assertIsNone(by_sig["x"])


class TestQuarantineRollup(PapercutBase):
    def run_rollup(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run(
            [sys.executable, str(PAPERCUT), "rollup", *argv],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )

    def test_quarantined_is_excluded_from_flagged_rows_and_counter(self):
        # Planted negative: the two signatures differ ONLY in quarantine
        # classification — identical sessions, counts, and window.
        self.write("-p", [self.rec(sig="bash:<path>", session=f"s{i}") for i in range(5)])
        self.write("-p", [self.rec(sig="bash:permission denied", session=f"s{i}") for i in range(5)])
        out = self.run_rollup("--days", "7", "--min-sessions", "3", "--min-count", "3").stdout
        self.assertIn("papercuts-flagged:1", out)
        self.assertIn("bash:permission denied", out)
        self.assertNotIn("bash:<path>", out, "a junk key must not reach the fix queue")
        self.assertIn("papercuts-quarantined:1", out)
        self.assertIn("list --quarantined", out, "the summary line must name the lane")

    def test_quarantined_counter_is_always_present(self):
        out = self.run_rollup("--days", "7").stdout
        self.assertIn("papercuts-flagged:0", out)
        self.assertIn("papercuts-quarantined:0", out,
                      "the counter must print unconditionally, like papercuts-flagged")
        self.assertNotIn("junk-fingerprint", out, "no summary line when nothing is quarantined")

    def test_apply_never_files_a_quarantined_signature(self):
        PC.RESOLVED = self.store / "state" / "resolved.jsonl"
        self.write("-p", [self.rec(sig="bash:<path>", session=f"s{i}") for i in range(5)])
        self.write("-p", [self.rec(sig="bash:permission denied", session=f"s{i}") for i in range(5)])
        calls = []
        orig, PC.gh = PC.gh, lambda *a, **k: (calls.append(" ".join(map(str, a))), (0, "[]"))[1]
        buf = io.StringIO()
        try:
            args = argparse.Namespace(days=7, min_count=3, min_sessions=3,
                                      limit=10, apply=True, repo="o/r", cap=-1)
            with contextlib.redirect_stdout(buf):
                PC.cmd_rollup(args)
        finally:
            PC.gh = orig
        joined = "\n".join(calls)
        self.assertIn("bash:permission denied", joined, "the coherent control must be filed")
        self.assertNotIn("bash:<path>", joined, "--apply must never file a quarantined signature")
        self.assertIn("papercuts-quarantined:1", buf.getvalue())


class TestQuarantineList(PapercutBase):
    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run([sys.executable, str(PAPERCUT), *argv],
                              capture_output=True, text=True, timeout=30, check=False, env=env)

    def seed(self):
        self.write("-p", [self.rec(sig="bash:<path>", session=f"s{i}") for i in range(3)])
        self.write("-p", [self.rec(sig="bash:permission denied", session=f"s{i}") for i in range(3)])

    def test_default_list_excludes_quarantined(self):
        self.seed()
        out = self.run_cli("list", "--days", "7").stdout
        self.assertIn("bash:permission denied", out)
        self.assertNotIn("bash:<path>", out)

    def test_quarantined_lane_shows_only_junk_with_rule_id(self):
        self.seed()
        out = self.run_cli("list", "--quarantined", "--days", "7").stdout
        self.assertIn("bash:<path>", out)
        self.assertIn("no-signal-residue", out, "each row must name the rule that caught it")
        self.assertNotIn("permission denied", out)

    def test_json_excludes_quarantined(self):
        self.seed()
        rows = json.loads(self.run_cli("list", "--json", "--days", "7").stdout)
        sigs = [r["sig"] for r in rows]
        self.assertIn("bash:permission denied", sigs)
        self.assertNotIn("bash:<path>", sigs)


class TestQuarantineShow(PapercutBase):
    def run_show(self, sig):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run([sys.executable, str(PAPERCUT), "show", sig, "--days", "7"],
                              capture_output=True, text=True, timeout=30, check=False, env=env).stdout

    def test_show_appends_the_fingerprinting_note(self):
        self.write("-p", [self.rec(sig="bash:<path>")])
        out = self.run_show("bash:<path>")
        self.assertIn("occurrence(s)", out, "occurrences must still print")
        self.assertIn("no-signal-residue", out)
        self.assertIn("needs fingerprinting, not a fix", out)

    def test_show_of_a_coherent_signature_has_no_note(self):
        self.write("-p", [self.rec(sig="x")])
        self.assertNotIn("quarantined", self.run_show("x"))


class TestQuarantineResolveIndependence(PapercutBase):
    """Quarantine and resolved-suppression are independent lanes: a resolve must
    not surface a junk key, and a reopen must not promote one into the fix queue."""

    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run([sys.executable, str(PAPERCUT), *argv],
                              capture_output=True, text=True, timeout=30, check=False, env=env)

    def test_resolving_a_quarantined_signature_does_not_surface_it(self):
        self.write("-p", [self.rec(sig="timed_out", session=f"s{i}") for i in range(5)])
        self.run_cli("resolve", "timed_out")
        out = self.run_cli("rollup", "--days", "7").stdout
        self.assertIn("papercuts-flagged:0", out)
        self.assertIn("papercuts-quarantined:0", out, "resolved wins: hidden everywhere")
        self.assertNotIn("timed_out", self.run_cli("list", "--quarantined").stdout)

    def test_reopen_does_not_unquarantine(self):
        self.write("-p", [self.rec(sig="timed_out", session=f"s{i}") for i in range(5)])
        self.run_cli("resolve", "timed_out")
        self.run_cli("resolve", "timed_out", "--reopen")
        out = self.run_cli("rollup", "--days", "7").stdout
        self.assertIn("papercuts-flagged:0", out,
                      "a reopened junk key must return to quarantine, not the fix queue")
        self.assertIn("papercuts-quarantined:1", out)


class TestFamilyTimestampOrder(PapercutBase):
    """Family events must order by parsed instants, never ISO text."""

    def test_membership_events_fold_by_parsed_instants_not_lexical_timestamps(self):
        # These timestamps invert when ordered as text: 10:00-01:00 (11:00 UTC)
        # sorts before 10:30+00:00 (10:30 UTC). The later instant must win.
        families = self.store / "state" / "families.jsonl"
        PC.FAMILIES = families
        families.parent.mkdir(parents=True)
        families.write_text("\n".join(json.dumps(event) for event in [
            {"schema_version": 1, "ts": "2026-01-01T10:30:00+00:00", "session": "s1",
             "family": "old-family", "action": "assign", "sig": "shared-signature"},
            {"schema_version": 1, "ts": "2026-01-01T10:00:00-01:00", "session": "s1",
             "family": "new-family", "action": "assign", "sig": "shared-signature"},
        ]) + "\n", encoding="utf-8")

        folded = PC.fold_families(PC.read_family_events())

        self.assertEqual(folded["membership"]["shared-signature"], "new-family")


class TestFamilyCommands(PapercutBase):
    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store), CLAUDE_CODE_SESSION_ID="family01")
        return subprocess.run([sys.executable, str(PAPERCUT), "family", *argv],
                              capture_output=True, text=True, timeout=30,
                              check=False, env=env)

    def test_latest_assignment_wins_across_explicit_reassignment(self):
        for argv in (("create", "first"), ("assign", "first", "shared"),
                     ("create", "second"), ("assign", "second", "shared")):
            result = self.run_cli(*argv)
            self.assertEqual(result.returncode, 0, result.stderr)

        state = json.loads(self.run_cli("show", "--json").stdout)

        self.assertEqual(state["membership"]["shared"], "second")
        events = [json.loads(line) for line in (self.store / "state" / "families.jsonl").read_text().splitlines()]
        self.assertTrue(all(event["schema_version"] == 1 for event in events))
        self.assertTrue(all(event["session"] == "family01" for event in events))

    def test_unassign_removes_membership_and_unknown_signature_is_a_noop(self):
        for argv in (("create", "one"), ("assign", "one", "known"),
                     ("unassign", "one", "known"), ("unassign", "one", "never-assigned")):
            result = self.run_cli(*argv)
            self.assertEqual(result.returncode, 0, result.stderr)

        state = json.loads(self.run_cli("show", "--json").stdout)

        self.assertNotIn("known", state["membership"])
        self.assertNotIn("never-assigned", state["membership"])
        self.assertEqual(json.loads((self.store / "state" / "families.jsonl").read_text().splitlines()[-1])["sig"],
                         "never-assigned")

    def test_assignment_keeps_the_raw_capture_signature_immutable(self):
        capture = self.write("-p", [self.rec(sig="Raw Signature: untouched")])
        before = capture.read_text(encoding="utf-8")

        result = self.run_cli("assign", "one", "Raw Signature: untouched")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(capture.read_text(encoding="utf-8"), before)
        event = json.loads((self.store / "state" / "families.jsonl").read_text())
        self.assertEqual(event["sig"], "Raw Signature: untouched")

    def test_close_observed_records_the_locator_and_observation(self):
        result = self.run_cli(
            "close-observed", "one", "--repo", "o/r", "--kind", "issue", "--number", "7",
            "--url", "https://example.test/o/r/issues/7", "--state", "closed",
            "--observed-at", "2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state = json.loads(self.run_cli("show", "one", "--json").stdout)["state"]

        self.assertEqual(state["locator"]["number"], 7)
        self.assertEqual(state["closed_observation"]["observed_at"], "2026-01-02T03:04:05+00:00")

    def test_concurrent_writers_leave_one_complete_event_per_command(self):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store), CLAUDE_CODE_SESSION_ID="family01")
        processes = [
            subprocess.Popen([sys.executable, str(PAPERCUT), "family", "create", f"family-{index}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for index in range(8)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)

        events = [json.loads(line) for line in (self.store / "state" / "families.jsonl").read_text().splitlines()]
        self.assertEqual({event["family"] for event in events}, {f"family-{index}" for index in range(8)})
        self.assertTrue(all(event["action"] == "create" for event in events))


class TestFamilyEventReader(PapercutBase):
    def setUp(self):
        super().setUp()
        PC.FAMILIES = self.store / "state" / "families.jsonl"
        PC.DOSSIERS = self.store / "state" / "dossiers"

    def test_absent_or_malformed_family_log_is_empty_or_skips_bad_lines(self):
        self.assertEqual(PC.read_family_events(), [])
        PC.FAMILIES.parent.mkdir(parents=True)
        valid = {"schema_version": 1, "ts": "2026-01-01T00:00:00+00:00", "session": "s1",
                 "family": "one", "action": "assign", "sig": "valid"}
        PC.FAMILIES.write_text("{torn json\n\n" + json.dumps({"action": "assign"}) + "\n"
                               + json.dumps(valid) + "\n", encoding="utf-8")

        folded = PC.fold_families(PC.read_family_events())

        self.assertEqual(folded["membership"], {"valid": "one"})

    def test_repeated_closed_observations_keep_the_first_in_one_epoch(self):
        locator = {"repo": "o/r", "kind": "issue", "number": 1, "url": "https://example.test/1"}
        events = [
            {"schema_version": 1, "ts": "2026-01-01T00:00:00+00:00", "session": "s",
             "family": "one", "action": "adopt", "locator": locator, "dossier_digest": "a"},
            {"schema_version": 1, "ts": "2026-01-02T00:00:00+00:00", "session": "s",
             "family": "one", "action": "close-observed", "locator": locator,
             "observed_state": "closed", "observed_at": "2026-01-02T00:00:00+00:00"},
            {"schema_version": 1, "ts": "2026-01-03T00:00:00+00:00", "session": "s",
             "family": "one", "action": "close-observed", "locator": locator,
             "observed_state": "closed", "observed_at": "2026-01-03T00:00:00+00:00"},
            {"schema_version": 1, "ts": "2026-01-04T00:00:00+00:00", "session": "s",
             "family": "one", "action": "reopen"},
            {"schema_version": 1, "ts": "2026-01-05T00:00:00+00:00", "session": "s",
             "family": "one", "action": "close-observed", "locator": locator,
             "observed_state": "closed", "observed_at": "2026-01-05T00:00:00+00:00"},
        ]

        state = PC.fold_families(events)["adoption"]["one"]

        self.assertEqual(state["closed_observation"]["observed_at"], "2026-01-05T00:00:00+00:00",
                         "a lifecycle event closes the old epoch before the next closed observation")
        first_epoch = PC.fold_families(events[:3])["adoption"]["one"]
        self.assertEqual(first_epoch["closed_observation"]["observed_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(first_epoch["last_observation"]["observed_at"], "2026-01-03T00:00:00+00:00")


class TestFamilyDispose(PapercutBase):
    def setUp(self):
        super().setUp()
        PC.FAMILIES = self.store / "state" / "families.jsonl"
        PC.DOSSIERS = self.store / "state" / "dossiers"

    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store), CLAUDE_CODE_SESSION_ID="family01")
        return subprocess.run([sys.executable, str(PAPERCUT), "family", *argv],
                              capture_output=True, text=True, timeout=30,
                              check=False, env=env)

    def write_dossier(self, body):
        path = self.store / "state" / "dossiers" / "one.md"
        path.parent.mkdir(parents=True)
        path.write_text(body, encoding="utf-8")
        return path

    def complete_dispose_dossier(self):
        # Deliberately omits all three remedy fields: a no-remedy disposition
        # needs causal evidence and boundaries, not an invented remedy gate.
        return """# Causal hypothesis
The guard reflects the intended policy.

# Strongest counterexample
A target-repo fix would help only if the policy were not intentional.

# Owner class
intended-policy

# No-Claim Boundary
This verdict does not prove other guards are correctly scoped.
"""

    def test_dispose_requires_verdict_and_no_claim_boundary_but_not_remedy_fields(self):
        draft = self.write_dossier(self.complete_dispose_dossier())
        missing_verdict = self.run_cli("dispose", "one")
        self.assertEqual(missing_verdict.returncode, 2)
        self.assertIn("verdict", missing_verdict.stderr)
        self.assertTrue(draft.exists())

        invalid_verdict = self.run_cli("dispose", "one", "--verdict", "not-a-verdict")
        self.assertEqual(invalid_verdict.returncode, 3)
        self.assertIn("policy refusal", invalid_verdict.stderr)
        self.assertTrue(draft.exists())

        without_boundary = self.complete_dispose_dossier().replace(
            "# No-Claim Boundary\nThis verdict does not prove other guards are correctly scoped.\n", "",
        )
        draft.write_text(without_boundary, encoding="utf-8")
        missing_boundary = self.run_cli("dispose", "one", "--verdict", "intended-policy")
        self.assertEqual(missing_boundary.returncode, 2)
        self.assertIn("No-Claim Boundary", missing_boundary.stderr)
        self.assertTrue(draft.exists())

        draft.write_text(self.complete_dispose_dossier(), encoding="utf-8")
        disposed = self.run_cli("dispose", "one", "--verdict", "intended-policy")
        self.assertEqual(disposed.returncode, 0, disposed.stderr)
        self.assertFalse(draft.exists(), "draft deletion follows the durable disposal event")
        event = json.loads((self.store / "state" / "families.jsonl").read_text())
        self.assertEqual(event["action"], "dispose")
        self.assertEqual(event["verdict"], "intended-policy")
        self.assertEqual(len(event["dossier_digest"]), 64)

    def test_dispose_rejects_an_owner_class_outside_the_vocabulary(self):
        """Both terminals suppress a family, so both owe the same routing enum.

        A disposal records its owner class under a digest that makes the record
        authoritative. Accepting free text there would let a suppressed family
        carry a class no downstream consumer can route on.
        """
        draft = self.write_dossier(
            self.complete_dispose_dossier().replace(
                "# Owner class\nintended-policy\n", "# Owner class\nnot-a-class\n",
            ),
        )

        refused = self.run_cli("dispose", "one", "--verdict", "intended-policy")

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("Owner class", refused.stderr)
        self.assertIn("upstream", refused.stderr, "the refusal must name the vocabulary")
        self.assertTrue(draft.exists(), "a refused disposal keeps the draft for correction")
        self.assertFalse(
            (self.store / "state" / "families.jsonl").exists(),
            "no disposal event may be appended for a rejected owner class",
        )

    def test_dispose_suppresses_until_reopen_reverses_it(self):
        self.write_dossier(self.complete_dispose_dossier())
        self.assertEqual(self.run_cli("dispose", "one", "--verdict", "intended-policy").returncode, 0)
        disposed = json.loads(self.run_cli("show", "one", "--json").stdout)
        self.assertEqual(disposed["state"]["lifecycle"], "disposed")

        reopened = self.run_cli("reopen", "one")
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        state = json.loads(self.run_cli("show", "one", "--json").stdout)
        self.assertEqual(state["state"]["lifecycle"], "active")
        self.assertIsNone(state["state"]["verdict"])

    def test_failed_append_keeps_the_draft_on_disk(self):
        draft = self.write_dossier(self.complete_dispose_dossier())
        original = PC.append_family_event
        PC.append_family_event = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    PC.cmd_family_dispose(argparse.Namespace(family="one", verdict="intended-policy"))
        finally:
            PC.append_family_event = original

        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(draft.exists())
        self.assertFalse(PC.FAMILIES.exists())


class TestFamilyEscalate(PapercutBase):
    URL = "https://upstream.example/issues/436?source=papercut#report"

    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store), CLAUDE_CODE_SESSION_ID="family01")
        return subprocess.run([sys.executable, str(PAPERCUT), "family", *argv],
                              capture_output=True, text=True, timeout=30,
                              check=False, env=env)

    def create_escalated(self, family="upstream-work", member="member"):
        PC.record_family_event(family, "create")
        PC.record_family_event(family, "assign", sig=member)
        with contextlib.redirect_stdout(io.StringIO()):
            PC.cmd_family_escalate(argparse.Namespace(
                family=family, to=self.URL, note="operator filed this upstream",
            ))

    def rollup(self, *, apply=False):
        args = argparse.Namespace(
            days=7, min_count=3, min_sessions=3, limit=10,
            apply=apply, repo="o/r", refresh=False, cap=-1, window=30,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            PC.cmd_rollup(args)
        return output.getvalue()

    def test_cli_records_verbatim_locator_note_and_shows_escalated_state(self):
        self.assertEqual(self.run_cli("create", "upstream-work").returncode, 0)

        escalated = self.run_cli(
            "escalate", "upstream-work", "--to", self.URL,
            "-n", "operator filed this upstream",
        )

        self.assertEqual(escalated.returncode, 0, escalated.stderr)
        events = [json.loads(line) for line in PC.FAMILIES.read_text().splitlines()]
        event = events[-1]
        self.assertEqual(event["action"], "escalate")
        self.assertEqual(event["upstream_url"], self.URL)
        self.assertEqual(event["note"], "operator filed this upstream")
        one = self.run_cli("show", "upstream-work")
        self.assertIn("upstream-work: escalated", one.stdout)
        self.assertIn(f"locator: {self.URL}", one.stdout)
        all_families = self.run_cli("show")
        self.assertIn("upstream-work: escalated", all_families.stdout)
        self.assertIn(f"locator: {self.URL}", all_families.stdout)
        json_state = json.loads(self.run_cli("show", "--json").stdout)
        self.assertEqual(json_state["adoption"]["upstream-work"]["lifecycle"], "escalated")
        self.assertEqual(json_state["adoption"]["upstream-work"]["upstream_url"], self.URL)
        self.assertEqual(json_state["adoption"]["upstream-work"]["escalation_note"],
                         "operator filed this upstream")
        self.assertIsNone(json_state["adoption"]["upstream-work"]["locator"])

    def test_escalate_makes_zero_gh_calls(self):
        PC.record_family_event("upstream-work", "create")
        original = PC.gh

        def refuse_gh(*argv, **kwargs):
            raise AssertionError(f"escalate must make no gh call: {argv}")

        PC.gh = refuse_gh
        try:
            PC.cmd_family_escalate(argparse.Namespace(
                family="upstream-work", to=self.URL, note=None,
            ))
        finally:
            PC.gh = original

        self.assertEqual(
            PC.fold_families()["adoption"]["upstream-work"]["lifecycle"],
            "escalated",
        )

    def test_unknown_family_and_invalid_urls_exit_three_without_an_event(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as unknown:
                PC.cmd_family_escalate(argparse.Namespace(
                    family="missing", to=self.URL, note=None,
                ))
        self.assertEqual(unknown.exception.code, 3)
        self.assertFalse(PC.FAMILIES.exists(), "unknown escalation must write no event")

        PC.record_family_event("placeholder", "unassign", sig="never-assigned")
        placeholder_before = PC.FAMILIES.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as placeholder:
                PC.cmd_family_escalate(argparse.Namespace(
                    family="placeholder", to=self.URL, note=None,
                ))
        self.assertEqual(placeholder.exception.code, 3)
        self.assertEqual(PC.FAMILIES.read_bytes(), placeholder_before,
                         "a non-create placeholder must not satisfy existence")

        PC.record_family_event("upstream-work", "create")
        before = PC.FAMILIES.read_bytes()
        for locator in ("http://upstream.example/436", "https://", "https:///436",
                        "https://upstream.example/bad port", "not-a-url"):
            with self.subTest(locator=locator):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as invalid:
                        PC.cmd_family_escalate(argparse.Namespace(
                            family="upstream-work", to=locator, note=None,
                        ))
                self.assertEqual(invalid.exception.code, 3)
                self.assertEqual(PC.FAMILIES.read_bytes(), before,
                                 "invalid URL must append nothing")

    def test_recurrence_only_updates_live_volume_and_never_comments(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.create_escalated()
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(2)])

        first = self.rollup()

        self.assertIn("papercuts-flagged:0", first)
        self.assertIn("papercuts-escalated:1", first)
        self.assertIn("family:upstream-work  2 sess 2x", first,
                      "live escalation volume remains visible below the action threshold")
        self.assertIn(self.URL, first)
        self.assertEqual(first.count("papercuts-escalated:"), 1)
        self.assertLess(first.index("escalated upstream:"), first.index("papercuts-escalated:"))

        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(2, 5)])
        original = PC.gh

        def refuse_gh(*argv, **kwargs):
            raise AssertionError(f"escalated recurrence must make no gh call: {argv}")

        PC.gh = refuse_gh
        try:
            triage = io.StringIO()
            with contextlib.redirect_stdout(triage):
                PC.cmd_triage(argparse.Namespace(
                    days=7, min_count=3, min_sessions=3, limit=3, json=False,
                ))
            second = self.rollup(apply=True)
            recurrence = io.StringIO()
            with contextlib.redirect_stdout(recurrence):
                PC.cmd_family_recur_comment(argparse.Namespace(
                    family="upstream-work", days=7, min_count=3, min_sessions=3,
                ))
        finally:
            PC.gh = original

        self.assertIn("papercuts-flagged:0", second)
        self.assertIn("papercuts-escalated:1", second)
        self.assertIn("family:upstream-work  5 sess 5x", second)
        self.assertIn("no flagged family candidates", triage.getvalue())
        self.assertFalse(PC.dossier_path("upstream-work").exists())
        self.assertIn("no flagged closed recurrence", recurrence.getvalue())
        self.assertFalse(any(event["action"] == "recur-comment"
                             for event in PC.read_family_events()))

    def test_reopen_and_dispose_are_valid_escalated_transitions(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.create_escalated()
            PC.cmd_family_reopen(argparse.Namespace(family="upstream-work"))
        reopened = PC.fold_families()["adoption"]["upstream-work"]
        self.assertEqual(reopened["lifecycle"], "active")
        self.assertIsNone(reopened["upstream_url"])
        self.assertIsNone(reopened["escalation_note"])

        with contextlib.redirect_stdout(io.StringIO()):
            PC.cmd_family_escalate(argparse.Namespace(
                family="upstream-work", to=self.URL, note=None,
            ))
        before_observation = PC.FAMILIES.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as observed:
                PC.cmd_family_close_observed(argparse.Namespace(
                    family="upstream-work", repo="o/r", kind="issue", number=1,
                    url="https://example.test/o/r/issues/1", state="closed",
                    observed_at=iso(),
                ))
        self.assertEqual(observed.exception.code, 3)
        self.assertEqual(PC.FAMILIES.read_bytes(), before_observation)

        dossier = PC.dossier_path("upstream-work")
        dossier.parent.mkdir(parents=True, exist_ok=True)
        dossier.write_text("""# Causal hypothesis
The behavior is owned upstream.

# Strongest counterexample
A local wrapper could reduce impact but cannot fix the owner.

# Owner class
upstream

# No-Claim Boundary
The report does not assert that upstream accepted or fixed it.
""", encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()):
            PC.cmd_family_dispose(argparse.Namespace(
                family="upstream-work", verdict="upstream-reported",
            ))

        self.assertEqual(
            PC.fold_families()["adoption"]["upstream-work"]["lifecycle"], "disposed",
        )
        self.assertFalse(dossier.exists())


class TestFamilyRollup(PapercutBase):
    """The weekly lane folds eligible raw captures into family rows."""

    def setUp(self):
        super().setUp()
        PC.RESOLVED = self.store / "state" / "resolved.jsonl"

    def assign(self, family, *sigs):
        for sig in sigs:
            PC.record_family_event(family, "assign", sig=sig)

    def adopt(self, family, *, locator=None):
        PC.record_family_event(
            family, "adopt", dossier_digest="dossier",
            locator=locator if locator is not None else {
                "repo": "o/r", "kind": "issue", "number": 17,
                "url": "https://example.test/o/r/issues/17",
            },
        )

    def rollup(self, *, apply=False, refresh=False, limit=10, cap=-1):
        # cap=-1 disables the global open-work check by default so these cases
        # keep testing what they were written for; the cap has its own tests.
        args = argparse.Namespace(
            days=7, min_count=3, min_sessions=3, limit=limit,
            apply=apply, repo="o/r", refresh=refresh, cap=cap,
        )
        output = io.StringIO()
        status = 0
        with contextlib.redirect_stdout(output):
            try:
                PC.cmd_rollup(args)
            except SystemExit as exc:
                status = exc.code
        return output.getvalue(), status

    def test_family_combines_raw_members_before_thresholding(self):
        # Neither signature reaches three sessions alone. Their family must cross
        # the threshold from the raw-record union, not from ranked child rows.
        self.assign("tooling", "missing-tool", "bad-default")
        self.write("-p", [
            self.rec(sig="missing-tool", session="a1"),
            self.rec(sig="missing-tool", session="a2"),
            self.rec(sig="bad-default", session="b1"),
            self.rec(sig="bad-default", session="b2"),
        ])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("papercuts-flagged:1", out)
        self.assertIn("family:tooling", out)
        self.assertNotIn("  missing-tool", out)
        self.assertNotIn("  bad-default", out)

    def test_family_union_does_not_double_count_shared_sessions(self):
        # A session can emit several member signatures. Folding raw records must
        # union its session ids; summing per-signature ranks would report ten.
        self.assign("shared-session", "one", "two")
        self.write("-p", [
            rec
            for session in range(5)
            for rec in (self.rec(sig="one", session=f"s{session}"),
                        self.rec(sig="two", session=f"s{session}"))
        ])

        rows = PC.rank(PC.read_records(7))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["family"], "shared-session")
        self.assertEqual(rows[0]["sessions"], 5)
        self.assertEqual(rows[0]["count"], 10)

    def test_ae2_open_adopted_family_suppresses_five_folded_members(self):
        members = [f"member-{index}" for index in range(5)]
        self.assign("open-work", *members)
        self.adopt("open-work")
        self.write("-p", [
            self.rec(sig=sig, session=f"{sig}-{occurrence}")
            for sig in members
            for occurrence in range(3)
        ])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("adopted-open family(s) suppressed", out)
        self.assertIn("papercuts-flagged:0", out)
        for sig in members:
            self.assertNotIn(sig, out)

    def test_apply_skips_family_rows_and_files_only_unassigned_signatures(self):
        self.assign("clinic-candidate", "member-one", "member-two")
        self.write("-p", [
            self.rec(sig=sig, session=f"{sig}-{occurrence}")
            for sig in ("member-one", "member-two", "unassigned")
            for occurrence in range(3)
        ])
        calls = []
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (
            calls.append(" ".join(map(str, argv))), (0, "[]")
        )[1]
        try:
            out, status = self.rollup(apply=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        joined = "\n".join(calls)
        self.assertIn("unassigned", joined)
        self.assertNotIn("member-one", joined)
        self.assertNotIn("member-two", joined)
        self.assertIn("papercuts-flagged:2", out)

    def test_refresh_closed_observation_reenters_and_persists_without_refresh(self):
        self.assign("closed-work", "member")
        self.adopt("closed-work")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (
            0, json.dumps({"state": "CLOSED", "url": "https://example.test/o/r/issues/17"})
        )
        try:
            refreshed, status = self.rollup(refresh=True)
        finally:
            PC.gh = original
        cached, cached_status = self.rollup()
        state = PC.fold_families()["adoption"]["closed-work"]

        self.assertEqual(status, 0)
        self.assertEqual(cached_status, 0)
        self.assertIsNotNone(state["closed_observation"])
        self.assertEqual(state["closed_observation"]["state"], "closed")
        self.assertIn("family:closed-work", refreshed)
        self.assertIn("family:closed-work", cached)
        self.assertIn("papercut disposition-cache-age:", cached)

    def test_refresh_drops_result_when_family_is_escalated_during_remote_read(self):
        PC.record_family_event("raced", "create")
        self.assign("raced", "member")
        self.adopt("raced")
        snapshot = PC.fold_families()["adoption"]
        original = PC.gh

        def racing_gh(*argv, **kwargs):
            with contextlib.redirect_stdout(io.StringIO()):
                PC.cmd_family_reopen(argparse.Namespace(family="raced"))
                PC.cmd_family_escalate(argparse.Namespace(
                    family="raced", to="https://upstream.example/issues/436", note=None,
                ))
            return 0, json.dumps({
                "state": "OPEN", "url": "https://example.test/o/r/issues/17",
                "labels": [{"name": "the dispatch-ready label"}],
            })

        PC.gh = racing_gh
        try:
            unknown, dispatch = PC.refresh_family_dispositions(snapshot, ["raced"], 1)
        finally:
            PC.gh = original

        state = PC.fold_families()["adoption"]["raced"]
        actions = [event["action"] for event in PC.read_family_events()]
        self.assertEqual(unknown, set())
        self.assertEqual(dispatch, {}, "a stale open read must not enter dispatch output")
        self.assertEqual(state["lifecycle"], "escalated")
        self.assertEqual(state["upstream_url"], "https://upstream.example/issues/436")
        self.assertIsNone(state["locator"])
        self.assertIsNone(state["last_observation"])
        self.assertEqual(actions[-2:], ["reopen", "escalate"],
                         "the stale read must append no close-observed event")

    def test_apply_reports_closed_family_recurrence_by_commenting_its_locator(self):
        self.assign("closed-recurrence", "member")
        self.adopt("closed-recurrence")
        PC.record_family_event(
            "closed-recurrence", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            # Dated a day back so "records arrived after the closure" is a
            # fact about the fixture rather than a race on clock resolution.
            observed_state="closed", observed_at=iso(days_ago=1),
        )
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        calls = []
        posted = []

        def fake_gh(*argv, **kwargs):
            argv = tuple(map(str, argv))
            calls.append(" ".join(argv))
            if argv[:2] == ("issue", "view"):
                # The comment read is bounded and real: serve back what this
                # fixture actually posted so the dedupe marker behaves as GitHub
                # would, rather than handing back a body that cannot parse.
                return 0, json.dumps({"comments": [{"body": b} for b in posted]})
            if argv[:2] == ("issue", "comment"):
                posted.append(argv[argv.index("--body") + 1])
            return 0, "ok"

        original, PC.gh = PC.gh, fake_gh
        try:
            out, status = self.rollup(apply=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertIn("issue comment 17 --repo o/r", "\n".join(calls))
        self.assertNotIn("issue create", "\n".join(calls))
        self.assertIn("reported recurrence on closed o/r#17", out)

    def test_refresh_failure_keeps_family_suppressed_and_reports_unknown(self):
        self.assign("unconfirmed", "member")
        self.adopt("unconfirmed")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (1, "network unavailable")
        try:
            out, status = self.rollup(refresh=True)
        finally:
            PC.gh = original

        self.assertIn("disposition_unknown", out)
        self.assertIn("papercuts-flagged:0", out)
        # Report-only stays exit 0: a weekly scheduled run turns any non-zero
        # into a "rollup crashed" WARN that feeds its toast.
        self.assertEqual(status, 0)

    def test_refresh_without_locator_reports_unknown_without_a_remote_read(self):
        self.assign("missing-locator", "member")
        self.adopt("missing-locator", locator={})
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        calls = []
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (calls.append(argv), (0, "{}"))[1]
        try:
            out, status = self.rollup(refresh=True)
        finally:
            PC.gh = original

        self.assertIn("disposition_unknown", out)
        self.assertIn("papercuts-flagged:0", out)
        self.assertEqual(calls, [])
        # Report-only stays exit 0: a weekly scheduled run turns any non-zero
        # into a "rollup crashed" WARN that feeds its toast.
        self.assertEqual(status, 0)

    def test_report_only_refresh_never_exits_nonzero_for_the_weekly_consumer(self):
        """a weekly scheduled run invokes the report-only refresh as

            rollup --days 7 --refresh || echo "WARN papercuts rollup crashed ..."

        and counts ^WARN lines into its toast. Unconfirmed remote state is an
        expected cron condition, so a non-zero exit here would emit a full
        rollup section and then falsely claim the command crashed.
        """
        self.assign("unconfirmed", "member")
        self.adopt("unconfirmed")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (1, "gh: not authenticated")
        try:
            out, status = self.rollup(refresh=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertIn("disposition_unknown", out)
        self.assertIn("papercuts-flagged:", out)

    def test_apply_still_exits_four_when_remote_state_is_unconfirmed(self):
        """the single-writer state lock's exit taxonomy still governs the mutating lane."""
        self.assign("unconfirmed", "member")
        self.adopt("unconfirmed")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (1, "network unavailable")
        try:
            _, status = self.rollup(apply=True, refresh=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 4)

    def test_cache_age_uses_latest_recorded_observation_without_refresh(self):
        self.assign("cached", "member")
        self.adopt("cached")
        PC.record_family_event(
            "cached", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            observed_state="closed", observed_at=iso(),
        )
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("family:cached", out)
        self.assertRegex(out, r"papercut disposition-cache-age: \d+s")

    def test_all_quarantined_or_resolved_members_never_surface_through_family(self):
        self.assign("all-filtered", "bash:<path>", "resolved-member")
        self.write("-p", [
            *[self.rec(sig="bash:<path>", session=f"q{index}") for index in range(3)],
            *[self.rec(sig="resolved-member", session=f"r{index}") for index in range(3)],
        ])
        PC.RESOLVED.parent.mkdir(parents=True, exist_ok=True)
        PC.RESOLVED.write_text(json.dumps({
            "sig": "resolved-member", "ts": iso(), "action": "resolve", "note": "fixed",
        }) + "\n", encoding="utf-8")

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("papercuts-flagged:0", out)
        self.assertNotIn("family:all-filtered", out)

    def test_disposed_family_is_suppressed_before_the_flagged_lane(self):
        self.assign("disposed-work", "member")
        PC.record_family_event(
            "disposed-work", "dispose", dossier_digest="dossier",
            verdict="intended-policy",
        )
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("disposed family(s) suppressed", out)
        self.assertIn("papercuts-flagged:0", out)

    def test_refresh_reads_no_more_than_the_rollup_limit(self):
        for family in ("first", "second"):
            member = f"{family}-member"
            self.assign(family, member)
            self.adopt(family)
            self.write("-p", [self.rec(sig=member, session=f"{family}-{index}") for index in range(3)])
        calls = []
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (
            calls.append(argv), (0, json.dumps({"state": "OPEN", "url": "https://example.test/item"}))
        )[1]
        try:
            _, status = self.rollup(refresh=True, limit=1)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        events = [event for event in PC.read_family_events() if event["action"] == "close-observed"]
        self.assertEqual(len(events), 1)


class TestVerificationLifecycle(PapercutBase):
    """an earlier change: a closed work item is re-measured, never assumed fixed.

    The stage is derived on every read from the closure observation, member
    records, and store-wide capture liveness — silence only counts as verified
    when capture was demonstrably alive, and it is never stored.
    """

    def assign(self, family, *sigs):
        for sig in sigs:
            PC.record_family_event(family, "assign", sig=sig)

    def adopt(self, family):
        PC.record_family_event(
            family, "adopt", dossier_digest="dossier",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
        )

    def close(self, family, *, days_ago):
        PC.record_family_event(
            family, "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            observed_state="closed", observed_at=iso(days_ago=days_ago),
        )

    def rollup(self, *, window=None):
        args = argparse.Namespace(
            days=7, min_count=3, min_sessions=3, limit=10,
            apply=False, repo="o/r", refresh=False, cap=-1, window=window,
        )
        output = io.StringIO()
        status = 0
        with contextlib.redirect_stdout(output):
            try:
                PC.cmd_rollup(args)
            except SystemExit as exc:
                status = exc.code
        return output.getvalue(), status

    def show(self, family, *, as_json=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            PC.cmd_family_show(argparse.Namespace(family=family, json=as_json))
        return output.getvalue()

    def test_planted_negative_a_quiet_family_with_no_exposure_is_provisional(self):
        """Closed 31+ days, zero member records since, zero store-wide capture
        in the post-closure window: silence with no exposure must classify
        provisional and must never be promoted to verified. Today the report
        says nothing at all about such a family, which reads as fixed.
        """
        self.assign("quiet-dead", "member")
        self.adopt("quiet-dead")
        self.close("quiet-dead", days_ago=31)
        # Baseline liveness exists; the post-closure window is bone dry.
        self.write("-p", [self.rec(sig="other", days_ago=35, session=f"b{i}")
                          for i in range(4)])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("quiet-dead", out)
        self.assertIn("provisional", out)
        self.assertNotIn("verified", out)

    def test_a_closure_younger_than_the_window_reports_verifying(self):
        self.assign("fresh-close", "member")
        self.adopt("fresh-close")
        self.close("fresh-close", days_ago=5)

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("fresh-close", out)
        self.assertIn("verifying", out)
        self.assertIn("25 day(s) remaining", out)
        self.assertIn("30", out, "the window must be stated, never implicit")

    def test_a_quiet_family_with_live_exposure_reports_verified_with_numbers(self):
        self.assign("held", "member")
        self.adopt("held")
        self.close("held", days_ago=31)
        # Baseline: 4 distinct capture sessions in the 30d before closure.
        self.write("-p", [self.rec(sig="other", days_ago=40, session=f"b{i}")
                          for i in range(4)])
        # Post-closure exposure: 3 distinct sessions, no member activity.
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"p{d}")
                          for d in (20, 15, 10)])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("held", out)
        self.assertIn("verified", out)
        self.assertIn("exposure 3", out)
        self.assertIn("floor 3", out)
        self.assertIn("baseline 4", out)

    def test_a_member_record_after_closure_reports_regressed_in_agreement(self):
        """One boundary, two consumers: the stage and the recurrence commenter
        must classify the same records as post-fix."""
        self.assign("broke-again", "member")
        self.adopt("broke-again")
        self.close("broke-again", days_ago=2)
        self.write("-p", [self.rec(sig="member", days_ago=1, session="s1")])

        out, status = self.rollup()
        state = PC.fold_families()["adoption"]["broke-again"]
        records = list(PC.read_records(90))
        details = PC.verification_details(state, {"member"}, records,
                                          window_days=30)

        self.assertEqual(status, 0)
        self.assertIn("regressed", out)
        self.assertEqual(details["stage"], "regressed")
        self.assertTrue(PC.has_new_recurrence(state, {"member"}, records))

    def test_a_regressed_read_names_its_first_post_closure_record(self):
        """A bare 'regressed' cost a multi-command dig to learn WHICH record
        tripped it (2026-09-01: one pre-fix plugin-version invocation). The
        stage carries the earliest post-boundary member record so the read is
        checkable in place."""
        self.assign("broke-again", "member")
        self.adopt("broke-again")
        self.close("broke-again", days_ago=3)
        later = self.rec(sig="member", days_ago=1, session="s2", cwd="/srv/app-b")
        earlier = self.rec(sig="member", days_ago=2, session="s1", cwd="/srv/app-a")
        self.write("-p", [later, earlier])

        state = PC.fold_families()["adoption"]["broke-again"]
        details = PC.verification_details(state, {"member"}, list(PC.read_records(90)),
                                          window_days=30)
        self.assertEqual(details["stage"], "regressed")
        self.assertEqual(details["first_recurrence"]["ts"], earlier["ts"])
        self.assertEqual(details["first_recurrence"]["project"],
                         PC.project_slug("/srv/app-a"))
        summary = PC.verification_summary(details)
        self.assertIn(earlier["ts"], summary)
        self.assertIn(PC.project_slug("/srv/app-a"), summary)
        self.assertIn("(member)", summary)
        self.assertNotIn(later["ts"], summary)
        self.assertIn(earlier["ts"], self.show("broke-again"))

    def test_a_regressed_read_from_the_comment_alone_says_its_records_aged_out(self):
        self.assign("comment-only", "member")
        self.adopt("comment-only")
        self.close("comment-only", days_ago=40)
        # A PRE-closure member record makes the scan actually run, so the
        # None below proves exclusion rather than an empty store.
        self.write("-p", [self.rec(sig="member", days_ago=45, session="old",
                                   cwd="/srv/app-a")])
        PC.record_family_event(
            "comment-only", "recur-comment",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            sessions=3, count=5,
        )
        state = PC.fold_families()["adoption"]["comment-only"]
        details = PC.verification_details(state, {"member"}, list(PC.read_records(90)),
                                          window_days=30)
        self.assertEqual(details["stage"], "regressed")
        self.assertIsNone(details["first_recurrence"])
        self.assertIsNotNone(details["recur_comment_at"])
        self.assertIn("aged out", PC.verification_summary(details))

    def test_family_show_explains_an_absent_verification_stage(self):
        """Silence read as 'not yet' when it meant 'never': only an adopted
        family carries a claim, and only a closed observation starts its clock."""
        self.assign("unadopted", "member")
        out = self.show("unadopted")
        self.assertIn("verification: none", out)
        self.assertIn("only an adopted family", out)
        self.adopt("unadopted")
        out = self.show("unadopted")
        self.assertIn("no closed observation yet", out)
        self.close("unadopted", days_ago=1)
        self.assertIn("verification (window", self.show("unadopted"))

    def test_a_recur_comment_outlasts_the_records_that_caused_it(self):
        """Records age out of the read horizon; the recur-comment event is the
        durable evidence that the fix did not hold. A family that regressed
        must never drift to verified once its records expire."""
        self.assign("expired-evidence", "member")
        self.adopt("expired-evidence")
        self.close("expired-evidence", days_ago=40)
        PC.record_family_event(
            "expired-evidence", "recur-comment",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            sessions=3, count=5,
        )
        # Live exposure that would otherwise satisfy the verified floor.
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"p{d}")
                          for d in (39, 35, 25, 20, 15)])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("regressed", out)
        self.assertNotIn("verified", out)

    def test_the_measurements_are_named_store_wide_not_family_specific(self):
        """`exposure 3` beside `verified` reads as three sessions that exercised
        this family. They are store-wide capture sessions and may have touched
        nothing related, so the line has to say so itself — the summary is the
        only thing an operator reads."""
        self.assign("held", "member")
        self.adopt("held")
        self.close("held", days_ago=40)
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"p{d}")
                          for d in (35, 25, 20, 15)])

        out, status = self.rollup()

        self.assertEqual(status, 0)
        self.assertIn("verified", out)
        self.assertIn("store-wide capture session(s)", out)
        self.assertIn("pre-closure baseline", out)
        self.assertIn("the fixed mechanism itself was not measured", out)

    def test_a_verifying_family_shows_how_far_it_is_from_the_floor(self):
        """Without the running count, a family headed for `provisional` with zero
        exposure prints exactly what one about to clear the floor prints, and the
        difference only surfaces once the window is spent."""
        self.assign("in-flight", "member")
        self.adopt("in-flight")
        self.close("in-flight", days_ago=5)
        # Baseline of 4 -> floor 3; nothing at all since the closure.
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"b{d}")
                          for d in (30, 25, 20, 10)])

        out, status = self.rollup()
        data = PC.family_show_data("in-flight", 30)

        self.assertEqual(status, 0)
        self.assertIn("verifying", out)
        self.assertIn("exposure 0 store-wide capture session(s)", out)
        self.assertIn("floor 3", out)
        self.assertEqual(data["verification"]["exposure_sessions"], 0)
        self.assertEqual(data["verification"]["floor"], 3)
        self.assertTrue(data["verification"]["partial"])

    def test_the_all_family_show_carries_stages_and_honors_the_window(self):
        """`family show` with no family accepted --window and then ignored it,
        printing lifecycle counts only — so the all-family view, which is what an
        operator scans when they do not yet know which family broke, was the one
        view that could not show a regression."""
        self.assign("everything", "member")
        self.adopt("everything")
        self.close("everything", days_ago=2)
        self.write("-p", [self.rec(sig="member", days_ago=1, session="s1")])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            PC.cmd_family_show(argparse.Namespace(family=None, json=False, window=60))
        out = output.getvalue()
        data = PC.family_show_data(None, 60)

        self.assertIn("everything", out)
        self.assertIn("regressed", out)
        self.assertIn("window 60d", out)
        self.assertEqual(data["verification"]["everything"]["stage"], "regressed")

    def test_a_quarantined_member_record_is_not_a_regression(self):
        """Parity with the recurrence commenter. `rollup_lanes` strips junk
        fingerprints into their own lane *before* the family fold, so
        `has_new_recurrence` never sees them. Reading raw records here would
        report a regression rollup deliberately refuses to act on, and because
        the read horizon grows with the closure age it would never age out.
        """
        self.assign("junk-member", "timed_out")
        self.adopt("junk-member")
        self.close("junk-member", days_ago=40)
        # A post-closure hit on the quarantined signature...
        self.write("-p", [self.rec(sig="timed_out", days_ago=5, session="q1")])
        # ...and enough live exposure that the stage is decidable.
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"p{d}")
                          for d in (35, 25, 20, 15)])

        out, status = self.rollup()
        views = PC.fold_families()
        state = views["adoption"]["junk-member"]
        records = list(PC.read_records(90))
        details = PC.verification_details(state, {"timed_out"}, records, window_days=30)
        lanes = PC.rollup_lanes(records, views, min_count=3, min_sessions=3)

        self.assertEqual(status, 0)
        self.assertIsNotNone(PC.quarantine_rule("timed_out"))
        self.assertEqual(details["stage"], "verified")
        self.assertNotIn("regressed", out)
        self.assertFalse(
            PC.has_new_recurrence(state, {"timed_out"}, lanes["eligible_records"]))

    def test_a_resolved_member_signature_quiet_since_is_not_a_regression(self):
        """The same parity rule for the resolved lane: a signature marked fixed
        and quiet since is suppressed from `eligible_records`, so the stage must
        not call it a regression while rollup stays silent about it."""
        self.assign("settled", "member")
        self.adopt("settled")
        self.close("settled", days_ago=40)
        self.write("-p", [self.rec(sig="member", days_ago=35, session="r1")])
        PC.RESOLVED.parent.mkdir(parents=True, exist_ok=True)
        PC.RESOLVED.write_text(json.dumps(
            {"sig": "member", "ts": iso(days_ago=30), "action": "resolve"}) + "\n")
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"p{d}")
                          for d in (35, 25, 20, 15)])

        out, status = self.rollup()
        views = PC.fold_families()
        state = views["adoption"]["settled"]
        records = list(PC.read_records(90))
        details = PC.verification_details(state, {"member"}, records, window_days=30)
        lanes = PC.rollup_lanes(records, views, min_count=3, min_sessions=3)

        self.assertEqual(status, 0)
        self.assertEqual(details["stage"], "verified")
        self.assertNotIn("regressed", out)
        self.assertFalse(
            PC.has_new_recurrence(state, {"member"}, lanes["eligible_records"]))

    def test_a_non_positive_window_is_refused_on_both_surfaces(self):
        """`--window 0` is falsy, so the default swallowed it and measured 30
        days while the report printed the number that was asked for; a negative
        window inverted the baseline and exposure intervals. Both are the
        silently-wrong output this feature exists to catch, so both are refused.
        """
        self.assign("shown", "member")
        self.adopt("shown")
        self.close("shown", days_ago=5)

        for bad in (0, -5):
            with contextlib.redirect_stderr(io.StringIO()):
                _, status = self.rollup(window=bad)
                self.assertEqual(status, 3, f"rollup --window {bad}")
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stdout(io.StringIO()):
                        PC.cmd_family_show(argparse.Namespace(
                            family="shown", json=False, window=bad))
            self.assertEqual(caught.exception.code, 3, f"family show --window {bad}")

    def test_a_future_close_observation_is_refused(self):
        """A closure stamp ahead of now starts the clock in the future and holds
        the family in `verifying` until that date — invisible to the re-measure
        for as long as the typo says."""
        self.assign("typo", "member")
        self.adopt("typo")
        args = argparse.Namespace(
            family="typo", repo="o/r", kind="issue", number=17,
            url="https://example.test/o/r/issues/17",
            state="closed", observed_at=iso(days_ago=-365),
        )
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                PC.cmd_family_close_observed(args)

        self.assertEqual(caught.exception.code, 3)
        self.assertIsNone(
            PC.fold_families()["adoption"]["typo"]["closed_observation"])

    def test_family_show_carries_the_stage_and_unclosed_families_do_not(self):
        self.assign("shown", "member")
        self.adopt("shown")
        self.close("shown", days_ago=5)
        self.assign("still-open", "other-member")
        self.adopt("still-open")

        shown = self.show("shown")
        shown_json = json.loads(self.show("shown", as_json=True))
        still_open = self.show("still-open")
        still_open_json = json.loads(self.show("still-open", as_json=True))

        self.assertIn("verifying", shown)
        self.assertEqual(shown_json["verification"]["stage"], "verifying")
        self.assertNotIn("verifying", still_open)
        self.assertIsNone(still_open_json.get("verification"))

    def test_the_report_path_appends_no_events_and_stays_quiet_without_closures(self):
        """Derived, never stored: reading the stage must not write anything,
        and a store with no closed families gets no verification section."""
        self.assign("no-closure", "member")
        self.adopt("no-closure")
        before_out, _ = self.rollup()
        self.assertNotIn("verification", before_out)

        self.close("no-closure", days_ago=5)
        events_before = PC.FAMILIES.read_bytes()
        out, status = self.rollup()
        self.show("no-closure")
        self.show("no-closure", as_json=True)

        self.assertEqual(status, 0)
        self.assertIn("verification", out)
        self.assertEqual(PC.FAMILIES.read_bytes(), events_before)

    def test_the_window_override_is_honored(self):
        self.assign("short-window", "member")
        self.adopt("short-window")
        self.close("short-window", days_ago=5)
        # Baseline inside (closure-4d, closure]; exposure inside (closure, closure+4d].
        self.write("-p", [self.rec(sig="other", days_ago=6, session="b1")])
        self.write("-p", [self.rec(sig="other", days_ago=d, session=f"p{d}")
                          for d in (4, 3, 2)])

        out, status = self.rollup(window=4)

        self.assertEqual(status, 0)
        self.assertIn("short-window", out)
        self.assertIn("verified", out)
        self.assertIn("window 4", out)


class TestClaimScopedVerification(PapercutBase):
    """`family scope` narrows recurrence/verification to the dossier's claim.

    Born of the third live occurrence of one wall (a-vcs-guard, route-guard,
    whole-transcript-reads): a member signature counting a superset of the
    remedy reads `regressed` forever over records the No-Claim Boundary never
    covered — measured 2026-09-01, 131 post-closure records, zero in scope.
    """

    SIG = "read:limit"

    def close(self, family):
        PC.record_family_event(family, "assign", sig=self.SIG)
        PC.record_family_event(
            family, "adopt", dossier_digest="d",
            locator={"repo": "o/r", "kind": "issue", "number": 9,
                     "url": "https://example.test/o/r/issues/9"})
        PC.record_family_event(
            family, "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 9,
                     "url": "https://example.test/o/r/issues/9"},
            observed_state="closed", observed_at=iso(days_ago=2))

    def stage(self, family):
        return PC.family_show_data(family)["verification"]["stage"]

    def test_out_of_scope_recurrence_stops_reading_regressed(self):
        self.close("scoped")
        PC.record_family_event("scoped", "scope", sig=self.SIG,
                               target_suffix=".jsonl")
        self.write("-p", [self.rec(sig=self.SIG, session="s1", days_ago=1,
                                   target="/big/source-file.py")])
        self.assertEqual(self.stage("scoped"), "verifying")

    def test_in_scope_recurrence_still_regresses(self):
        # Planted negative: the scope must not become a blanket pardon.
        self.close("bitten")
        PC.record_family_event("bitten", "scope", sig=self.SIG,
                               target_suffix=".jsonl")
        self.write("-p", [self.rec(sig=self.SIG, session="s1", days_ago=1,
                                   target="/x/transcript.jsonl")])
        self.assertEqual(self.stage("bitten"), "regressed")

    def test_unscoped_behavior_is_unchanged(self):
        self.close("plain")
        self.write("-p", [self.rec(sig=self.SIG, session="s1", days_ago=1,
                                   target="/big/source-file.py")])
        self.assertEqual(self.stage("plain"), "regressed")

    def test_a_targetless_record_cannot_prove_scope(self):
        # Fail-closed: a legacy record with no captured target does not count
        # toward a scoped claim (mirrors session-less exposure records).
        self.close("legacy")
        PC.record_family_event("legacy", "scope", sig=self.SIG,
                               target_suffix=".jsonl")
        self.write("-p", [self.rec(sig=self.SIG, session="s1", days_ago=1)])
        self.assertEqual(self.stage("legacy"), "verifying")

    def test_scope_clears_on_reassignment_and_on_empty_suffix(self):
        PC.record_family_event("first", "assign", sig=self.SIG)
        PC.record_family_event("first", "scope", sig=self.SIG,
                               target_suffix=".jsonl")
        self.assertEqual(PC.fold_families()["scopes"], {self.SIG: ".jsonl"})
        PC.record_family_event("first", "scope", sig=self.SIG, target_suffix="")
        self.assertEqual(PC.fold_families()["scopes"], {})
        PC.record_family_event("first", "scope", sig=self.SIG,
                               target_suffix=".jsonl")
        PC.record_family_event("second", "assign", sig=self.SIG)
        self.assertEqual(PC.fold_families()["scopes"], {},
                         "a claim boundary must not travel to a new family")

    def test_cross_family_scope_event_is_a_noop_audit_record(self):
        PC.record_family_event("owner", "assign", sig=self.SIG)
        PC.record_family_event("stranger", "scope", sig=self.SIG,
                               target_suffix=".jsonl")
        self.assertEqual(PC.fold_families()["scopes"], {})

    def test_cli_scope_refuses_an_unassigned_signature(self):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        p = subprocess.run(
            [sys.executable, str(PAPERCUT), "family", "scope", "nobody",
             "ghost:sig", "--target-suffix", ".jsonl"],
            capture_output=True, text=True, timeout=30, check=False, env=env)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("not assigned", p.stderr + p.stdout)


class TestFamilyTriage(PapercutBase):
    """Triage prepares local candidate dossiers from rollup's flagged family lane."""

    def assign(self, family, *sigs):
        for sig in sigs:
            PC.record_family_event(family, "assign", sig=sig)

    def adopt(self, family):
        PC.record_family_event(
            family, "adopt", dossier_digest="dossier",
            locator={
                "repo": "o/r", "kind": "issue", "number": 17,
                "url": "https://example.test/o/r/issues/17",
            },
        )

    @staticmethod
    def mutating_gh_call(argv):
        command = tuple(map(str, argv))
        if command[:2] in {
            ("issue", "create"), ("issue", "comment"), ("issue", "reopen"),
            ("pr", "create"), ("label", "create"),
        }:
            return True
        if command[:1] != ("api",):
            return False
        return any(
            command[index] == "-X" and index + 1 < len(command)
            and command[index + 1].upper() in {"POST", "PATCH", "PUT", "DELETE"}
            for index in range(len(command))
        )

    def triage_args(self, **overrides):
        values = {
            "days": 7, "min_count": 3, "min_sessions": 3, "limit": 3,
            "unfamilied_limit": 10, "json": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def triage(self, **overrides):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            PC.cmd_triage(self.triage_args(**overrides))
        return output.getvalue()

    def test_a_closed_quiet_family_gets_no_recurrence_banner(self):
        """Observed live 2026-08-29: python-command-missing closed that evening,
        zero member records postdated the observation stamp, and the banner
        still said "recurrence: ... run recur-comment" -- an action that would
        correctly decline. The banner now shares recur-comment's predicate."""
        self.assign("closed-quiet", "member")
        self.adopt("closed-quiet")
        self.write("-p", [self.rec(sig="member", session=f"s{index}", days_ago=3)
                          for index in range(3)])
        PC.record_family_event(
            "closed-quiet", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            observed_state="closed", observed_at=iso(days_ago=1),
        )
        out = self.triage()
        self.assertNotIn("recurrence: closed", out)
        self.assertNotIn("recur-comment closed-quiet", out)
        self.assertIn("closed issue o/r#17", out)
        self.assertIn("no member records postdate the closure", out)

    def test_a_post_closure_member_record_restores_the_banner(self):
        self.assign("really-recurred", "member")
        self.adopt("really-recurred")
        PC.record_family_event(
            "really-recurred", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            observed_state="closed", observed_at=iso(days_ago=2),
        )
        self.write("-p", [self.rec(sig="member", session=f"s{index}", days_ago=1)
                          for index in range(3)])
        out = self.triage()
        self.assertIn("recurrence: closed issue o/r#17", out)
        self.assertIn("recur-comment really-recurred", out)

    def test_triage_never_mutates_github_with_closed_recurrence(self):
        self.assign("closed-recurrence", "member")
        self.adopt("closed-recurrence")
        PC.record_family_event(
            "closed-recurrence", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            observed_state="closed", observed_at=iso(),
        )
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        calls = []
        original, PC.gh = PC.gh, lambda *argv, **kwargs: (calls.append(argv), (0, "ok"))[1]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                PC.cmd_triage(self.triage_args())
        finally:
            PC.gh = original

        mutating = [tuple(map(str, call)) for call in calls if self.mutating_gh_call(call)]
        self.assertEqual(mutating, [])

    def test_recurrence_suffix_names_its_own_family_not_the_last_iterated(self):
        """The stale-variable bug (seen live 3x, 2026-08-28): the recur-comment
        suffix read `entry` — a leftover from the earlier candidate loop — so
        it printed whichever family happened to iterate LAST, telling the
        operator to run recur-comment against the wrong family."""
        self.assign("aa-recurred", "aa-member")
        self.adopt("aa-recurred")
        PC.record_family_event(
            "aa-recurred", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            observed_state="closed", observed_at=iso(days_ago=2),
        )
        self.write("-p", [self.rec(sig="aa-member", session=f"a{index}")
                          for index in range(4)])
        # A second, later-iterating candidate with NO recurrence: under the
        # bug, the suffix printed THIS family's name on aa-recurred's line.
        self.assign("zz-quiet", "zz-member")
        self.write("-p", [self.rec(sig="zz-member", session=f"z{index}")
                          for index in range(3)])

        out = self.triage()

        self.assertIn("recur-comment aa-recurred", out)
        self.assertNotIn("recur-comment zz-quiet", out)

    def test_triage_empty_json_is_a_valid_empty_array(self):
        output = self.triage(json=True)

        self.assertEqual(json.loads(output), [])

    def test_triage_generates_the_fixed_evidence_and_judgment_template(self):
        self.assign("candidate", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])

        self.triage()
        body = (self.store / "state" / "dossiers" / "candidate.md").read_text(encoding="utf-8")
        evidence, judgment = PC.parse_dossier("candidate", body)

        self.assertEqual(evidence, """

# Papercut candidate dossier

## Snapshot metadata
- Triage window: 7 day(s)
- Event-log position: 1
- Thresholds: >= 3 distinct session(s); >= 3 occurrence(s)
- Family: "candidate"

## Occurrence count
3

## Distinct sessions
3

## Projects
1 distinct project(s)
- "-p"

## Sample excerpts
- "pytest: command not found"
- "pytest: command not found"
- "pytest: command not found"

## Member signatures
- "member"

""")
        self.assertEqual(judgment, PC.DOSSIER_JUDGMENT_TEMPLATE)

    def test_a_two_hundred_project_family_renders_a_count_and_a_capped_list(self):
        """an earlier change: a wide family must not dump every project slug.

        The full total is stated, the list is capped, and the cut is named —
        never silent. Member signatures stay complete: the adopt staleness
        comparison strict-parses that section, so the cap must never spread
        to it.
        """
        projects = [f"-home-user-project-{index:03d}" for index in range(200)]
        members = [f"member-{index:02d}" for index in range(20)]
        entry = {"family": "wide", "count": 400, "sessions": 200,
                 "projects": projects, "samples": [], "members": members}

        body = PC.dossier_evidence(entry, days=7, event_position=1,
                                   min_count=3, min_sessions=3)

        section = PC.markdown_section(body, "Projects")
        bullets = [line for line in section.splitlines() if line.startswith('- "')]
        self.assertLessEqual(len(bullets), 15,
                             "the project list must be capped, not dumped wholesale")
        self.assertIn("200 distinct project(s)", section,
                      "the full total must be stated")
        self.assertIn("185 more", section, "the cut must be named, never silent")
        member_section = PC.markdown_section(body, "Member signatures")
        for member in members:
            self.assertIn(f'- "{member}"', member_section,
                          "member signatures must stay complete for the staleness gate")

    def test_for_humans_is_required_by_the_judgment_contract_itself(self):
        """an earlier change: the completeness registry — not just the renderer —
        owns the requirement. adoption_body independently refuses an empty
        For humans, so an adopt-level negative alone stays green if the field
        falls out of DOSSIER_JUDGMENT_FIELDS; this pins the registry that
        triage draft-prioritization also reads.
        """
        judgment = PC.DOSSIER_JUDGMENT_TEMPLATE
        values = {
            "Causal hypothesis": "value", "Strongest counterexample": "value",
            "Owner class": "target-repo", "Destination repository": "owner/repository",
            "Destination justification": "value", "Cheapest remedy": "value",
            "Acceptance Criteria": "value", "Pre-registered success measure": "value",
            "Planted negative": "value", "No-Claim Boundary": "value",
        }
        for heading, value in values.items():
            judgment = judgment.replace(f"## {heading}\n", f"## {heading}\n{value}\n", 1)

        self.assertEqual(PC.dossier_judgment_missing(judgment), ["For humans"])

    def test_triage_resume_refreshes_evidence_and_preserves_judgment_bytes(self):
        self.assign("resume", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        self.triage()
        path = self.store / "state" / "dossiers" / "resume.md"
        authored = path.read_text(encoding="utf-8").replace(
            "## Causal hypothesis\n\n",
            "## Causal hypothesis\n\nThis byte sequence is session-authored.  \n\n",
            1,
        )
        path.write_text(authored, encoding="utf-8")
        _, judgment_before = PC.parse_dossier("resume", authored)

        self.write("-p", [self.rec(sig="member", session="s3")])
        self.triage()
        evidence_after, judgment_after = PC.parse_dossier("resume", path.read_text(encoding="utf-8"))

        self.assertIn("4", evidence_after)
        self.assertEqual(judgment_after, judgment_before)

    def test_triage_refuses_a_corrupt_hidden_marker_without_overwriting_draft(self):
        self.assign("corrupt", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        self.triage()
        path = self.store / "state" / "dossiers" / "corrupt.md"
        corrupt = path.read_text(encoding="utf-8").replace("schema=1", "schema=9", 1)
        path.write_text(corrupt, encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.triage()

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(path.read_text(encoding="utf-8"), corrupt)

    def test_triage_prioritizes_existing_incomplete_draft_before_new_candidates(self):
        self.assign("zeta", "zeta-member")
        self.write("-zeta", [self.rec(sig="zeta-member", session=f"z{index}") for index in range(3)])
        self.triage()
        for family in ("alpha", "beta", "gamma"):
            member = f"{family}-member"
            self.assign(family, member)
            self.write(f"-{family}", [self.rec(sig=member, session=f"{family}{index}") for index in range(3)])

        candidates = json.loads(self.triage(json=True))

        self.assertEqual([candidate["family"] for candidate in candidates], ["zeta", "alpha", "beta"])

    # --- an earlier change: triage surfaces unfamilied flagged signatures ------------

    def test_triage_surfaces_unfamilied_flagged_signatures_after_familied_output(self):
        self.assign("candidate", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        self.write("-p", [self.rec(sig="guard_blocked:a-command-guard", session=f"g{index}")
                          for index in range(4)])

        out = self.triage()

        self.assertIn("CREATED family:candidate", out)
        self.assertIn("unfamilied candidates:", out)
        self.assertLess(out.index("family:candidate"), out.index("unfamilied candidates:"))
        self.assertIn("    4 sess    4x  guard_blocked:a-command-guard  [1 project(s)]", out)
        self.assertIn('papercut family assign <family-id-you-choose> "guard_blocked:a-command-guard"', out)
        # The familied member is folded into its family, never re-proposed raw.
        self.assertNotIn('assign <family-id-you-choose> "member"', out)

    def test_unfamilied_section_ranks_by_sessions_like_the_rollup_lane(self):
        self.write("-p", [self.rec(sig="wider", session=f"w{index}") for index in range(4)])
        self.write("-p", [self.rec(sig="narrower", session=f"n{index}") for index in range(3)])

        out = self.triage()

        self.assertIn("no flagged family candidates", out)
        self.assertIn("unfamilied candidates:", out)
        self.assertLess(out.index("wider"), out.index("narrower"))

    def test_unfamilied_cap_names_the_cut_and_the_flag_raises_it(self):
        for index in range(12):
            sig = f"raw-{index:02d}"
            self.write("-p", [self.rec(sig=sig, session=f"{sig}-s{j}") for j in range(3)])

        out = self.triage()
        self.assertEqual(out.count("papercut family assign"), 10)
        self.assertIn("(+2 more — raise with --unfamilied-limit)", out)

        widened = self.triage(unfamilied_limit=12)
        self.assertEqual(widened.count("papercut family assign"), 12)
        self.assertNotIn("more — raise with --unfamilied-limit", widened)

    def test_unfamilied_limit_zero_or_negative_is_refused_before_any_dossier_write(self):
        self.assign("candidate", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        for bad in (0, -5):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as raised:
                    self.triage(unfamilied_limit=bad)
            self.assertEqual(raised.exception.code, 3)
            self.assertIn(
                "policy refusal: --unfamilied-limit must be a positive number",
                err.getvalue())
        self.assertFalse((self.store / "state" / "dossiers" / "candidate.md").exists(),
                         "the refusal must precede every dossier write")

    def test_quarantined_and_resolved_signatures_never_appear_unfamilied(self):
        self.write("-p", [self.rec(sig="timed_out", session=f"q{index}") for index in range(5)])
        self.write("-p", [self.rec(sig="fixed-one", session=f"r{index}", days_ago=2)
                          for index in range(4)])
        PC.RESOLVED.parent.mkdir(parents=True, exist_ok=True)
        with open(PC.RESOLVED, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"sig": "fixed-one", "ts": iso(), "action": "resolve"}) + "\n")
        self.write("-p", [self.rec(sig="live-one", session=f"l{index}") for index in range(3)])

        out = self.triage()

        self.assertIn('"live-one"', out)
        self.assertNotIn("timed_out", out)
        self.assertNotIn("fixed-one", out)

    def test_triage_proposes_and_never_writes_a_family_event(self):
        # Planted negative: a store with no families must stay that way.
        self.write("-p", [self.rec(sig="raw-ore", session=f"r{index}") for index in range(4)])
        self.triage()
        self.assertFalse(PC.FAMILIES.exists(),
                         "triage over unfamilied candidates must create no family log")

        self.assign("candidate", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        before = PC.FAMILIES.read_bytes()

        out = self.triage()

        self.assertIn('assign <family-id-you-choose> "raw-ore"', out)
        self.assertEqual(PC.FAMILIES.read_bytes(), before,
                         "triage proposes; the family event log must be byte-identical")

    def test_all_familied_leaves_the_section_absent_not_empty(self):
        # Planted negative: an empty section is absent, and the familied output
        # is byte-for-byte what it was before the section existed.
        self.assign("candidate", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])

        out = self.triage()

        self.assertNotIn("unfamilied", out)
        self.assertEqual(
            out,
            "## papercut triage — 1 candidate dossier(s)\n"
            f"  CREATED family:candidate  {PC.dossier_path('candidate')}\n",
        )
        payload = json.loads(self.triage(json=True))
        self.assertIsInstance(payload, list,
                              "with no unfamilied candidates --json stays the bare array")

    def test_empty_store_triage_output_is_unchanged(self):
        self.assertEqual(self.triage(), "papercut triage: no flagged family candidates\n")

    def test_json_gains_the_unfamilied_list_under_its_own_key_with_the_named_cut(self):
        self.assign("candidate", "member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        for index in range(11):
            sig = f"raw-{index:02d}"
            self.write("-q", [self.rec(sig=sig, session=f"{sig}-s{j}") for j in range(3)])

        payload = json.loads(self.triage(json=True))

        self.assertEqual([c["family"] for c in payload["candidates"]], ["candidate"])
        self.assertEqual(len(payload["unfamilied"]), 10)
        self.assertEqual(payload["unfamilied_cut"], 1)
        first = payload["unfamilied"][0]
        self.assertEqual(first["sig"], "raw-00")
        self.assertEqual(first["distinct_sessions"], 3)
        self.assertEqual(first["occurrences"], 3)
        self.assertEqual(first["projects"], ["-q"])
        self.assertEqual(first["assign"],
                         'papercut family assign <family-id-you-choose> "raw-00"')

    def test_family_id_that_would_escape_dossiers_directory_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                PC.dossier_path("../../escape")

        self.assertEqual(raised.exception.code, 3)
        self.assertFalse((self.store / "state" / "escape.md").exists())

    def test_ae3_closed_family_recurrence_reenters_and_comments_once_without_reopen(self):
        self.assign("closed-recurrence", "member")
        self.adopt("closed-recurrence")
        PC.record_family_event(
            "closed-recurrence", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 17,
                     "url": "https://example.test/o/r/issues/17"},
            # Dated a day back so "records arrived after the closure" is a
            # fact about the fixture rather than a race on clock resolution.
            observed_state="closed", observed_at=iso(days_ago=1),
        )
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        views = PC.fold_families()
        lanes = PC.rollup_lanes(
            list(PC.read_records(7)), views, min_count=3, min_sessions=3,
        )
        self.assertEqual([row["family"] for row in lanes["flagged"]], ["closed-recurrence"])

        calls = []
        posted = []

        def fake_gh(*argv, **kwargs):
            argv = tuple(map(str, argv))
            calls.append(argv)
            if argv[:2] == ("issue", "view"):
                # Serve back what was actually posted, so the second invocation
                # sees the first one's epoch marker exactly as GitHub would.
                return 0, json.dumps({"comments": [{"body": body} for body in posted]})
            if argv[:2] == ("issue", "comment"):
                posted.append(argv[argv.index("--body") + 1])
                return 0, "ok"
            return 0, "ok"

        original, PC.gh = PC.gh, fake_gh
        args = argparse.Namespace(family="closed-recurrence", days=7, min_count=3, min_sessions=3)
        try:
            self.triage()
            with contextlib.redirect_stdout(io.StringIO()):
                PC.cmd_family_recur_comment(args)
                PC.cmd_family_recur_comment(args)
        finally:
            PC.gh = original

        state = PC.fold_families()["adoption"]["closed-recurrence"]
        self.assertEqual(state["lifecycle"], "adopted")
        self.assertEqual(state["closed_observation"]["state"], "closed")
        self.assertEqual([call[:2] for call in calls if call[:2] == ("issue", "comment")],
                         [("issue", "comment")],
                         "exactly one recurrence comment across both invocations")
        self.assertEqual(len([event for event in PC.read_family_events()
                              if event["action"] == "recur-comment"]), 1)


class TestFamilyAdoptPlantedNegatives(PapercutBase):
    """Planted negatives for the U4 promotion gate.

    These begin through the public CLI so the initial red is an assertion on the
    current command result—not an AttributeError from a not-yet-defined handler.
    Once adopt exists, the the dispatch-ready label test uses its in-process gh seam to inspect
    the actual payload sent for filing.
    """

    def seed_dossier(self, *, missing=()):
        PC.record_family_event("candidate", "assign", sig="member")
        self.write("-p", [self.rec(sig="member", session=f"s{index}") for index in range(3)])
        with contextlib.redirect_stdout(io.StringIO()):
            PC.cmd_triage(argparse.Namespace(
                days=7, min_count=3, min_sessions=3, limit=3, json=False,
            ))
        path = PC.dossier_path("candidate")
        body = path.read_text(encoding="utf-8")
        values = {
            "Causal hypothesis": "The target repository omits a required validation.",
            "Strongest counterexample": "A passing path already validates related input.",
            "Owner class": "target-repo",
            "Destination repository": "owner/repository",
            "Destination justification": "The failure is owned by this repository.",
            "Cheapest remedy": "Add the narrow validation before the shared write.",
            "Acceptance Criteria": "- Reject the malformed input before any write.",
            "Pre-registered success measure": "The planted negative fails before the remedy and passes after.",
            "Planted negative": "Malformed input must not create a record.",
            "No-Claim Boundary": "This does not prove unrelated validation paths are correct.",
            "For humans": "Bad input slips through and leaves broken records "
                          "that someone has to clean up by hand.",
        }
        for heading, value in values.items():
            if heading not in missing:
                body = body.replace(f"## {heading}\n", f"## {heading}\n{value}\n", 1)
        path.write_text(body, encoding="utf-8")
        return path

    def run_cli(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store), CLAUDE_CODE_SESSION_ID="adopt0001")
        return subprocess.run(
            [sys.executable, str(PAPERCUT), *argv],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )

    def test_ae1_planted_negative_missing_field_refuses_before_filing(self):
        self.seed_dossier(missing=("Planted negative",))

        # This public-CLI branch produced the pre-production red below. The
        # finished branch calls the handler through a constrained gh seam, so a
        # mutation that misses this field reaches the render gate and this exact
        # assertion—not an accidental live GitHub failure—must fire.
        if not hasattr(PC, "cmd_adopt"):
            result = self.run_cli("adopt", "candidate", "--json")
            status, error = result.returncode, result.stderr
        else:
            def fake_gh(*argv, check=False):
                if argv[:3] == ("repo", "view", "owner/repository"):
                    return 0, json.dumps({"nameWithOwner": "owner/repository"})
                if argv[:2] in {("issue", "list"), ("pr", "list")}:
                    return 0, "[]"
                if argv[:2] == ("label", "create"):
                    return 0, ""
                if argv[:2] == ("label", "list"):
                    return 0, json.dumps([{"name": argv[argv.index("--search") + 1]}])
                raise AssertionError(f"unexpected gh call: {argv}")

            original, PC.gh = PC.gh, fake_gh
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    try:
                        PC.cmd_adopt(argparse.Namespace(family="candidate", cap=3, json=True))
                    except SystemExit as exc:
                        status = exc.code
            finally:
                PC.gh = original
            error = stderr.getvalue()

        self.assertEqual(status, 2, error)
        self.assertIn("incomplete dossier: missing Planted negative", error)
        events = PC.read_family_events()
        self.assertFalse(any(event["action"] == "adopt" for event in events),
                         "an incomplete dossier must not file an adoption event")

    def test_planted_negative_a_dossier_without_for_humans_is_refused(self):
        """an earlier change: judgment must carry a plain-language `For humans` section.

        The fixture fills every field the pre-an earlier change contract required, then
        strips any `For humans` section, so this fails today on observed wrong
        behavior — the checker passes such a dossier and adopt files it — not
        on a missing symbol.
        """
        path = self.seed_dossier()
        body = path.read_text(encoding="utf-8")
        stripped = re.sub(r"(?ms)^## For humans[ \t]*$\n.*?(?=^## |\Z)", "", body)
        path.write_text(stripped, encoding="utf-8")

        calls = []
        filed = {"value": ""}

        def fake_gh(*argv, check=False):
            argv = tuple(map(str, argv))
            calls.append(argv)
            if argv[:3] == ("repo", "view", "owner/repository"):
                return 0, json.dumps({"nameWithOwner": "owner/repository"})
            if argv[:2] in {("issue", "list"), ("pr", "list")}:
                return 0, "[]"
            if argv[:2] == ("label", "create"):
                return 0, ""
            if argv[:2] == ("label", "list"):
                return 0, json.dumps([{"name": argv[argv.index("--search") + 1]}])
            if argv[:2] == ("issue", "create"):
                filed["value"] = argv[argv.index("--body") + 1]
                return 0, "https://github.com/owner/repository/issues/42"
            if argv[:2] == ("issue", "view"):
                return 0, json.dumps({
                    "number": 42,
                    "url": "https://github.com/owner/repository/issues/42",
                    "state": "OPEN",
                    "body": filed["value"],
                })
            raise AssertionError(f"unexpected gh call: {argv}")

        original, PC.gh = PC.gh, fake_gh
        stderr, status = io.StringIO(), None
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(stderr):
                try:
                    PC.cmd_adopt(argparse.Namespace(family="candidate", cap=3, json=True))
                except SystemExit as exc:
                    status = exc.code
        finally:
            PC.gh = original

        self.assertEqual(status, 2, stderr.getvalue())
        self.assertIn("For humans", stderr.getvalue())
        self.assertNotIn(("issue", "create"), [call[:2] for call in calls],
                         "an incomplete dossier must never reach filing")
        self.assertFalse(
            any(event["action"] == "adopt" for event in PC.read_family_events()),
            "an incomplete dossier must not file an adoption event",
        )

    def test_adopt_rejects_an_owner_class_outside_the_vocabulary(self):
        """The owner class routes the remedy, so free text cannot reach filing.

        The refusal is a local completeness check, so it must land before any
        GitHub contact: this seam fails the test if adopt reaches the network
        with an uninterpretable owner class.
        """
        path = self.seed_dossier()
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "## Owner class\ntarget-repo\n", "## Owner class\nnot-a-class\n", 1,
            ),
            encoding="utf-8",
        )

        def refuse_gh(*argv, check=False):
            raise AssertionError(f"refusal must precede any gh call, got: {argv}")

        original, PC.gh = PC.gh, refuse_gh
        stderr = io.StringIO()
        status = None
        try:
            with contextlib.redirect_stderr(stderr):
                try:
                    PC.cmd_adopt(argparse.Namespace(family="candidate", cap=3, json=True))
                except SystemExit as exc:
                    status = exc.code
        finally:
            PC.gh = original
        error = stderr.getvalue()

        self.assertEqual(status, 2, error)
        self.assertIn("Owner class", error)
        self.assertIn("upstream", error, "the refusal must name the vocabulary")
        self.assertFalse(
            any(event["action"] == "adopt" for event in PC.read_family_events()),
            "an uninterpretable owner class must not file an adoption event",
        )

    def test_planted_negative_no_filed_payload_carries_loop_ok(self):
        self.seed_dossier()

        # Before U4 defines cmd_adopt, use the public CLI so the planted test is
        # red through an assertion on an observable command result rather than a
        # missing-Python-symbol error. The finished branch exercises every gh
        # payload directly, which is what the negative actually guards.
        if not hasattr(PC, "cmd_adopt"):
            result = self.run_cli("adopt", "candidate", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            return

        calls = []
        body = {"value": ""}

        def fake_gh(*argv, check=False):
            calls.append(tuple(map(str, argv)))
            if argv[:3] == ("repo", "view", "owner/repository"):
                return 0, json.dumps({"nameWithOwner": "owner/repository"})
            if argv[:2] == ("issue", "list"):
                return 0, "[]"
            if argv[:2] == ("pr", "list"):
                return 0, "[]"
            if argv[:2] == ("label", "create"):
                return 0, ""
            if argv[:2] == ("label", "list"):
                return 0, json.dumps([{"name": argv[argv.index("--search") + 1]}])
            if argv[:2] == ("issue", "create"):
                body["value"] = argv[argv.index("--body") + 1]
                return 0, "https://github.com/owner/repository/issues/42"
            if argv[:2] == ("issue", "view"):
                return 0, json.dumps({
                    "number": 42,
                    "url": "https://github.com/owner/repository/issues/42",
                    "state": "OPEN",
                    "body": body["value"],
                })
            raise AssertionError(f"unexpected gh call: {argv}")

        original, PC.gh = PC.gh, fake_gh
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                PC.cmd_adopt(argparse.Namespace(family="candidate", cap=3, json=True))
        finally:
            PC.gh = original

        # Assert the filing actually happened FIRST. Without this, any early
        # return leaves `calls` empty and the negative passes while proving
        # nothing: the payload it claims to inspect was never sent.
        creates = [call for call in calls if call[:2] == ("issue", "create")]
        self.assertEqual(len(creates), 1, f"adopt must file exactly one issue: {calls}")
        create = creates[0]
        self.assertIn("--body", create)
        self.assertTrue(body["value"], "the filed body must be non-empty")
        labels = [create[index + 1] for index, part in enumerate(create) if part == "--label"]
        self.assertEqual(sorted(labels), sorted(PC.ADOPT_LABELS),
                         "adopt files under exactly its two own labels")
        self.assertNotIn("the dispatch-ready label", labels)
        payloads = [part for call in calls for part in call if "the dispatch-ready label" in part]
        self.assertEqual(payloads, [], "adopt must never apply operator-only the dispatch-ready label")


class TestFamilyAdopt(PapercutBase):
    """The promotion gate is exercised in-process so every gh payload is visible."""

    def setUp(self):
        super().setUp()
        PC.RESOLVED = self.store / "state" / "resolved.jsonl"
        self.calls = []
        self.items = []
        self.bodies = {}
        self.legacy_open = []
        self.label_failure = None
        self.present_labels = {PC.ISSUE_LABEL, PC.WORK_SPEC_LABEL}
        self.query_failures = {}          # (repo, kind) -> stderr text
        self.confirm_failure = False

    def seed(self, *, cwd=None):
        PC.record_family_event("candidate", "assign", sig="member")
        extra = {"cwd": cwd} if cwd else {}
        self.write("-p", [self.rec(sig="member", session=f"s{index}", **extra)
                          for index in range(3)])
        with contextlib.redirect_stdout(io.StringIO()):
            PC.cmd_triage(argparse.Namespace(
                days=7, min_count=3, min_sessions=3, limit=3, json=False,
            ))
        path = PC.dossier_path("candidate")
        body = path.read_text(encoding="utf-8")
        values = {
            "Causal hypothesis": "The target repository omits a required validation.",
            "Strongest counterexample": "A passing path already validates related input.",
            "Owner class": "target-repo",
            "Destination repository": "owner/repository",
            "Destination justification": "The failure is owned by this repository.",
            "Cheapest remedy": "Add the narrow validation before the shared write.",
            "Acceptance Criteria": "- Reject the malformed input before any write.",
            "Pre-registered success measure": "The planted negative fails before the remedy and passes after.",
            "Planted negative": "Malformed input must not create a record.",
            "No-Claim Boundary": "This does not prove unrelated validation paths are correct.",
            "For humans": "Bad input slips through and leaves broken records "
                          "that someone has to clean up by hand.",
        }
        for heading, value in values.items():
            body = body.replace(f"## {heading}\n", f"## {heading}\n{value}\n", 1)
        path.write_text(body, encoding="utf-8")
        return path

    def patch_repo_for(self, mapping):
        """Place extra repositories in the adoption universe via capture cwds."""
        original = PC.repo_for
        self.addCleanup(setattr, PC, "repo_for", original)
        PC.repo_for = lambda cwd: mapping.get(str(cwd)) or original(cwd)

    def fake_gh(self, *argv, check=False):
        argv = tuple(map(str, argv))
        self.calls.append(argv)
        repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
        if argv[:3] == ("repo", "view", "owner/repository"):
            return 0, json.dumps({"nameWithOwner": "owner/repository"})
        if argv[:1] == ("api",):
            return 0, json.dumps({"object": {"sha": "abc"}})
        if argv[:2] == ("label", "create"):
            if argv[2] == self.label_failure:
                return 1, "label denied"
            self.present_labels.add(argv[2])
            return 0, ""
        if argv[:2] == ("label", "list"):
            search = argv[argv.index("--search") + 1] if "--search" in argv else None
            names = sorted(self.present_labels)
            if search is not None:
                names = [name for name in names if search in name]
            return 0, json.dumps([{"name": name} for name in names])
        if argv[:2] in {("issue", "list"), ("pr", "list")}:
            kind = argv[0]
            state = argv[argv.index("--state") + 1]
            failure = self.query_failures.get((repo, kind))
            if failure is not None:
                return 1, failure
            if kind == "issue" and state == "open" and "--label" in argv:
                return 0, json.dumps(self.legacy_open if repo == "owner/repository" else [])
            # Honor --label and --head the way gh does. A stub more permissive
            # than the server cannot catch a query scoped to the wrong set.
            label = argv[argv.index("--label") + 1] if "--label" in argv else None
            head = argv[argv.index("--head") + 1] if "--head" in argv else None
            values = [
                {key: value for key, value in item.items() if key != "kind"}
                for item in self.items
                if item["kind"] == kind and item["repo"] == repo
                and (state == "all" or str(item.get("state", "")).lower() == state)
                and (label is None or any(entry.get("name") == label
                                          for entry in item.get("labels", [])))
                and (head is None or item.get("headRefName") == head)
            ]
            return 0, json.dumps(values)
        if argv[:2] in {("issue", "create"), ("pr", "create")}:
            kind = argv[0]
            number = 42 if kind == "issue" else 43
            self.bodies[(kind, number)] = argv[argv.index("--body") + 1]
            path = "issues" if kind == "issue" else "pull"
            return 0, f"https://github.com/{repo}/{path}/{number}"
        if argv[:2] == ("pr", "edit"):
            self.bodies[("pr", int(argv[2]))] = argv[argv.index("--body") + 1]
            return 0, ""
        if argv[:2] in {("issue", "view"), ("pr", "view")}:
            kind, number = argv[0], int(argv[2])
            if self.confirm_failure and (kind, number) in self.bodies:
                return 1, "confirmation unavailable"
            body = self.bodies.get((kind, number))
            if body is None:
                item = next((item for item in self.items if item["kind"] == kind
                             and item["repo"] == repo and item["number"] == number), None)
                body = item.get("body", "") if item else ""
            path = "issues" if kind == "issue" else "pull"
            return 0, json.dumps({
                "number": number, "url": f"https://github.com/{repo}/{path}/{number}",
                "state": "OPEN", "body": body,
            })
        raise AssertionError(f"unexpected gh call: {argv}")

    def adopt(self, **overrides):
        values = {"family": "candidate", "cap": 3, "json": True}
        values.update(overrides)
        stdout, stderr, status = io.StringIO(), io.StringIO(), 0
        original, PC.gh = PC.gh, self.fake_gh
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    PC.cmd_adopt(argparse.Namespace(**values))
                except SystemExit as exc:
                    status = exc.code
        finally:
            PC.gh = original
        return status, stdout.getvalue(), stderr.getvalue()

    def event(self, family="candidate"):
        return next(event for event in PC.read_family_events()
                    if event["family"] == family and event["action"] == "adopt")

    def test_adoption_body_leads_with_for_humans_and_collapses_the_dossier(self):
        """an earlier change: the filed body reads human-first.

        Line 1 keeps the dossier marker (digest untouched), line 2 keeps the
        remote-dedupe family marker, the authored `For humans` judgment renders
        before any machine evidence, and the full dossier is carried verbatim
        inside a collapsed block — where the structural work-spec gate must
        still find its required sections.
        """
        path = self.seed()
        text = path.read_text(encoding="utf-8")

        body = PC.adoption_body(text, "candidate")

        lines = body.splitlines()
        self.assertEqual(lines[0], text.splitlines()[0],
                         "line 1 must keep the dossier marker byte-identical")
        self.assertEqual(lines[1], PC.FAMILY_MARKER.format(family="candidate"),
                         "line 2 must keep the remote-dedupe family marker")
        self.assertIn("## For humans", body)
        self.assertIn("<details>", body)
        self.assertLess(body.index("## For humans"), body.index("<details>"),
                        "the authored section must render before the collapsed dossier")
        self.assertLess(body.index("<details>"),
                        body.index("# Papercut candidate dossier"),
                        "machine evidence must sit inside the collapsed block")
        self.assertIn("</details>", body)
        evidence, judgment = PC.parse_dossier("candidate", text)
        self.assertIn(evidence, body, "evidence must be carried verbatim")
        self.assertIn(judgment, body, "judgment must be carried verbatim")
        gate_ok, gate_error = PC.work_spec_gate(body)
        self.assertTrue(gate_ok,
                        f"the render gate must pass the reordered body: {gate_error}")

    def test_adopt_success_output_names_the_operator_loop_ok_act(self):
        """an earlier change: the handoff must not be silent at the human boundary.

        After filing, the next act belongs to the operator -- tagging the item
        `the dispatch-ready label` is what makes it dispatchable -- and adopt's own output is
        the only place that fact is in front of whoever just ran it. The line
        is print-only: the print-only label rule's planted negative separately proves no gh payload
        ever carries the label.
        """
        self.seed()

        status, out, _ = self.adopt(json=False)

        self.assertEqual(status, 0)
        self.assertIn("family adopted: candidate", out)
        # Asserted against the CONSTANT so the test holds under any configured
        # default: a set label must be named (wording tracks the amended
        # admission rule, an earlier change -- human-admitted, which since 2026-08-28
        # includes a session tagging work the operator explicitly approved that
        # same session); an unset one must silence the guidance, not reword it.
        if PC.DISPATCH_READY_LABEL:
            self.assertIn(
                f"next: tag it {PC.DISPATCH_READY_LABEL} to make it dispatchable — the "
                f"operator's act, or the session's for work the operator "
                f"approved this session", out)
            if PC.DISPATCH_DOCS_REF:
                self.assertIn(f"({PC.DISPATCH_DOCS_REF})", out)
        else:
            self.assertNotIn("next: tag it", out,
                             "no queue configured -> no tagging guidance")

    def test_an_over_limit_rendered_body_is_refused_before_any_create(self):
        """an earlier change review finding: For humans renders twice (hoisted and
        inside the collapsed dossier), so an authored section over half the
        GitHub body limit pushed a previously-fileable dossier past 65,536
        characters — every local gate passed, gh create failed remotely, and
        adopt fell through to the misleading exit-4 unconfirmed-remote path.
        The refusal must be local and named.
        """
        path = self.seed()
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("## For humans\n", "## For humans\n" + "x" * 40_000 + "\n", 1),
            encoding="utf-8",
        )

        status, _, error = self.adopt()

        self.assertEqual(status, 3, error)
        self.assertIn("65536", error)
        self.assertNotIn(("issue", "create"), [call[:2] for call in self.calls],
                         "an over-limit body must never reach filing")

    def test_capture_cwd_that_resolves_to_no_repo_does_not_block_adoption(self):
        """The cap universe must mirror `rollup --apply`, which SKIPs such a record.

        Measured 2026-08-26 against the live store: 1306 of 1431 distinct capture
        cwds no longer resolve to a repo, mostly worktrees that have since been
        swept. Raising on them made adopt refuse unconditionally on real data and
        would have blocked the the live-data verification pass live pass -- while every fixture-based test
        stayed green, because no fixture record carries a `cwd` key at all.
        """
        self.seed(cwd="/nonexistent/definitely-not-a-repo")

        status, output, error = self.adopt()

        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["locator"]["repo"], "owner/repository")

    def test_existing_label_is_left_alone_rather_than_restyled(self):
        """`gh label create --force` rewrites colour and description.

        Both adoption labels are shared harness labels. Measured 2026-08-26,
        the source harness carries `work-spec` as "Implementation-ready
        unit of work (bead-quality spec)" in 1D76DB, which a forced create
        would have silently replaced while filing an unrelated work item.
        """
        self.seed()

        status, _, error = self.adopt()

        self.assertEqual(status, 0, error)
        self.assertEqual(
            [call for call in self.calls if call[:2] == ("label", "create")], [],
            "an already-present label must not be re-created",
        )

    def test_absent_label_is_created_then_read_back(self):
        """Existence is still mandatory: the duplicate-filing guard forbids filing unlabeled."""
        self.seed()
        self.present_labels = {PC.WORK_SPEC_LABEL}

        status, _, error = self.adopt()

        self.assertEqual(status, 0, error)
        created = [call for call in self.calls if call[:2] == ("label", "create")]
        self.assertEqual([call[2] for call in created], [PC.ISSUE_LABEL], created)
        for call in created:
            self.assertNotIn("--force", call, "creation must not restyle on collision")

    def test_universe_member_with_issues_disabled_is_skipped_not_refused(self):
        """A disabled queue provably holds nothing, so it hides no marker.

        Measured 2026-08-26 on the second live adoption attempt:
        an-org/example-project is in the real universe with issues disabled, and
        reading its non-zero exit as an unqueryable source refused every
        adoption in every repository.
        """
        self.seed(cwd="/home/user/other-checkout")
        self.patch_repo_for({"/home/user/other-checkout": "owner/queue-disabled"})
        self.query_failures[("owner/queue-disabled", "issue")] = (
            "the 'owner/queue-disabled' repository has disabled issues"
        )

        status, output, error = self.adopt()

        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["locator"]["repo"], "owner/repository")
        self.assertTrue(
            [call for call in self.calls if call[:2] == ("pr", "list")
             and "owner/queue-disabled" in call],
            "the pull-request queue of that repository must still be searched",
        )
        # Both listing call sites meet this repository: the marker search
        # (--state all) and the global cap count (--state open). Guarding only
        # the first left the second refusing on live data.
        states = {call[call.index("--state") + 1]
                  for call in self.calls if call[:2] == ("issue", "list")
                  and "owner/queue-disabled" in call}
        self.assertEqual(states, {"all", "open"}, f"only one call site exercised: {states}")

    def test_universe_member_that_does_not_exist_is_skipped_not_refused(self):
        """A repository that does not exist holds no queue, so it hides nothing.

        REMOTE_MISSING_RE already drew this distinction for repo_exists;
        only the listing path was left out, and that asymmetry
        made one dead repository anywhere in the universe refuse every adoption
        in every repository. Measured 2026-08-27 on the live store: twelve
        fixture filings for `owner/repository` in state/filings.jsonl put it in
        the universe, and both adopt and `rollup --apply` refused for every
        family -- the marker search first, then the global cap count.
        """
        self.seed(cwd="/home/user/other-checkout")
        self.patch_repo_for({"/home/user/other-checkout": "owner/deleted-repo"})
        self.query_failures[("owner/deleted-repo", "issue")] = (
            "GraphQL: Could not resolve to a Repository with the name "
            "'owner/deleted-repo'. (repository)"
        )

        status, output, error = self.adopt()

        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["locator"]["repo"], "owner/repository")
        # Both listing call sites meet this repository, exactly as the disabled
        # queue does: guarding only the marker search left the cap count
        # refusing on live data.
        states = {call[call.index("--state") + 1]
                  for call in self.calls if call[:2] == ("issue", "list")
                  and "owner/deleted-repo" in call}
        self.assertEqual(states, {"all", "open"}, f"only one call site exercised: {states}")

    def test_universe_member_that_fails_for_any_other_reason_still_refuses(self):
        """Neither skip may widen into a general error swallow.

        the open-work cap makes a source that cannot be read a refusal. Without this control
        either skip -- the disabled queue or the missing repository -- could be
        broadened to any non-zero exit, and adopt would file over a marker it
        simply failed to fetch.
        """
        self.seed(cwd="/home/user/other-checkout")
        self.patch_repo_for({"/home/user/other-checkout": "owner/unreachable"})
        self.query_failures[("owner/unreachable", "issue")] = "HTTP 502 Bad Gateway"

        status, _, error = self.adopt()

        self.assertEqual(status, 3, error)
        self.assertIn("could not query issue markers in owner/unreachable", error)
        self.assertEqual(
            [call for call in self.calls
             if call[:2] in {("issue", "create"), ("pr", "create")}], [],
            "an unreadable marker source must never reach a create",
        )

    def test_marker_search_is_filtered_to_the_label_both_routes_file_with(self):
        """An unlabeled item is one neither filing route wrote, so it is out of scope.

        Measured 2026-08-26 on the first live adoption: enumerating unfiltered
        made adopt refuse outright, because an-org/example-project holds
        >= 1000 PRs and filled the list limit. Filtering server-side is also
        the correctness argument -- both routes label what they create, so an
        unlabeled item cannot hold a marker.
        """
        self.seed()

        status, _, error = self.adopt()

        self.assertEqual(status, 0, error)
        listings = [call for call in self.calls
                    if call[:2] in {("issue", "list"), ("pr", "list")}
                    and call[call.index("--state") + 1] == "all"]
        self.assertEqual(len(listings), 2, f"expected both queues searched: {listings}")
        for call in listings:
            self.assertIn("--label", call, f"unfiltered marker search: {call}")
            self.assertEqual(call[call.index("--label") + 1], PC.ISSUE_LABEL, call)

    def test_marker_search_that_fills_the_list_limit_refuses_instead_of_filing(self):
        """A full page is indistinguishable from a truncated one, so it refuses.

        `gh <kind> list --limit N` truncates rather than paging. Measured
        2026-08-26, the source harness held 362 PRs against the original
        200-item limit, so the marker search was already blind to older items
        and would have filed a duplicate over an existing tracked one. the open-work cap
        makes an unenumerable universe a refusal, never a silent miss.
        """
        self.seed()
        original_limit = PC.GH_LIST_LIMIT
        self.addCleanup(setattr, PC, "GH_LIST_LIMIT", original_limit)
        PC.GH_LIST_LIMIT = 2
        self.items = [
            {"kind": "pr", "repo": "owner/repository", "number": number,
             "state": "MERGED", "body": "unrelated",
             "labels": [{"name": "papercut"}],
             "headRefName": f"branch-{number}"}
            for number in (100, 101)
        ]

        status, _, error = self.adopt()

        self.assertEqual(status, 3, error)
        self.assertIn("could not enumerate", error)
        self.assertEqual(
            [call for call in self.calls
             if call[:2] in {("issue", "create"), ("pr", "create")}],
            [], "a truncated marker search must never reach a create",
        )
        self.assertEqual(
            [event for event in PC.read_family_events() if event["action"] == "adopt"], [],
        )

    def test_complete_non_trivial_dossier_files_verified_work_spec_issue(self):
        draft = self.seed()

        status, output, error = self.adopt()

        self.assertEqual(status, 0, error)
        result = json.loads(output)
        self.assertEqual(result["locator"], {
            "repo": "owner/repository", "kind": "issue", "number": 42,
            "url": "https://github.com/owner/repository/issues/42",
        })
        create = next(call for call in self.calls if call[:2] == ("issue", "create"))
        body = create[create.index("--body") + 1]
        self.assertIn(PC.FAMILY_MARKER.format(family="candidate"), body)
        self.assertIn("## Acceptance Criteria", body)
        self.assertIn("papercut", create)
        self.assertIn("work-spec", create)
        self.assertNotIn("the dispatch-ready label", create)
        self.assertEqual(self.event()["locator"], result["locator"])
        self.assertFalse(draft.exists(), "deletion follows the durable adoption event")

    def test_ae4_cap_reached_refuses_before_any_new_item(self):
        self.seed()
        for number in (1, 2, 3):
            PC.record_family_event(
                f"already-{number}", "adopt", dossier_digest="d",
                locator={"repo": "other/repository", "kind": "issue", "number": number,
                         "url": f"https://github.com/other/repository/issues/{number}"},
            )

        status, _, error = self.adopt()

        self.assertEqual(status, 3)
        self.assertIn("cap 3 reached (3 open)", error)
        self.assertFalse(any(call[:2] in {("issue", "create"), ("pr", "create")}
                             for call in self.calls))

    def test_cap_unions_open_legacy_apply_issues(self):
        self.seed()
        for number in (1, 2):
            PC.record_family_event(
                f"already-{number}", "adopt", dossier_digest="d",
                locator={"repo": "other/repository", "kind": "issue", "number": number,
                         "url": f"https://github.com/other/repository/issues/{number}"},
            )
        self.legacy_open = [{"number": 99, "url": "https://github.com/owner/repository/issues/99"}]

        status, _, error = self.adopt()

        self.assertEqual(status, 3)
        self.assertIn("cap 3 reached (3 open)", error)

    def test_open_legacy_member_issue_refuses_and_names_locator(self):
        self.seed()
        self.items = [{
            "repo": "owner/repository", "kind": "issue", "number": 71,
            "url": "https://github.com/owner/repository/issues/71", "state": "OPEN",
            "labels": [{"name": "papercut"}],
            "body": PC.SIG_MARKER.format(sig="member"),
        }]

        status, _, error = self.adopt()

        self.assertEqual(status, 3)
        self.assertIn("owner/repository#71", error)
        self.assertFalse(any(call[:2] == ("issue", "create") for call in self.calls))

    def test_member_set_change_refuses_and_regenerates_only_evidence(self):
        draft = self.seed()
        _, judgment = PC.parse_dossier("candidate", draft.read_text(encoding="utf-8"))
        PC.record_family_event("candidate", "assign", sig="other-member")
        self.write("-p", [self.rec(sig="other-member", session=f"o{index}") for index in range(3)])

        status, _, error = self.adopt()

        self.assertEqual(status, 3)
        self.assertIn("stale dossier", error)
        evidence_after, judgment_after = PC.parse_dossier("candidate", draft.read_text(encoding="utf-8"))
        self.assertEqual(judgment_after, judgment)
        self.assertIn('"other-member"', evidence_after)

    def test_quarantined_member_refuses_and_preserves_judgment(self):
        draft = self.seed()
        _, judgment = PC.parse_dossier("candidate", draft.read_text(encoding="utf-8"))
        original = PC.quarantine_rule
        PC.quarantine_rule = lambda sig: "newly-quarantined" if sig == "member" else original(sig)
        try:
            status, _, error = self.adopt()
        finally:
            PC.quarantine_rule = original

        self.assertEqual(status, 3)
        self.assertIn("stale dossier", error)
        _, judgment_after = PC.parse_dossier("candidate", draft.read_text(encoding="utf-8"))
        self.assertEqual(judgment_after, judgment)

    def test_existing_remote_marker_reconciles_without_duplicate_create(self):
        self.seed()
        marker = PC.FAMILY_MARKER.format(family="candidate")
        self.items = [{
            "repo": "owner/repository", "kind": "issue", "number": 61,
            "url": "https://github.com/owner/repository/issues/61", "state": "OPEN",
            "labels": [{"name": "papercut"}],
            "body": f"existing\n{marker}",
        }]

        status, output, error = self.adopt()

        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["status"], "reconciled")
        self.assertEqual(self.event()["locator"]["number"], 61)
        self.assertFalse(any(call[:2] in {("issue", "create"), ("pr", "create")}
                             for call in self.calls))

    def test_label_provision_failure_refuses_before_creation(self):
        self.seed()
        # Provisioning only runs for a label that is genuinely absent, so the
        # denial is unreachable unless the label is missing to begin with.
        self.present_labels = {PC.ISSUE_LABEL}
        self.label_failure = PC.WORK_SPEC_LABEL

        status, _, error = self.adopt()

        self.assertEqual(status, 3)
        self.assertIn("could not provision and verify destination labels", error)
        self.assertFalse(any(call[:2] in {("issue", "create"), ("pr", "create")}
                             for call in self.calls))

    def test_unconfirmed_create_exits_four_without_adoption_event(self):
        draft = self.seed()
        self.confirm_failure = True

        status, _, error = self.adopt()

        self.assertEqual(status, 4)
        self.assertIn("unconfirmed remote state", error)
        self.assertTrue(draft.exists())
        self.assertFalse(any(event["action"] == "adopt" for event in PC.read_family_events()))

    def test_secret_in_judgment_refuses_and_names_field(self):
        self.seed()
        draft = PC.dossier_path("candidate")
        body = draft.read_text(encoding="utf-8").replace(
            "The target repository omits a required validation.",
            "Authorization: Bearer sk-abcdefghijklmno123456", 1,
        )
        draft.write_text(body, encoding="utf-8")

        status, _, error = self.adopt()

        self.assertEqual(status, 3)
        self.assertIn("redaction would alter judgment field: Causal hypothesis", error)
        self.assertEqual(self.calls, [])

    def test_failed_adoption_event_keeps_draft_after_confirmed_remote_create(self):
        draft = self.seed()
        original = PC.append_family_event
        PC.append_family_event = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        try:
            status, _, error = self.adopt()
        finally:
            PC.append_family_event = original

        self.assertEqual(status, 1)
        self.assertIn("could not adopt family", error)
        self.assertTrue(draft.exists())

    def test_remote_marker_reconciles_even_when_the_cap_is_now_full(self):
        self.seed()
        marker = PC.FAMILY_MARKER.format(family="candidate")
        self.items = [{
            "repo": "owner/repository", "kind": "issue", "number": 61,
            "url": "https://github.com/owner/repository/issues/61", "state": "OPEN",
            "labels": [{"name": "papercut"}],
            "body": f"existing\n{marker}",
        }]
        self.legacy_open = [{"number": number, "url": f"https://example.test/{number}"}
                            for number in (1, 2, 3)]

        status, output, error = self.adopt()

        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)["status"], "reconciled")
        self.assertFalse(any(call[:2] in {("issue", "create"), ("pr", "create")}
                             for call in self.calls))

    def test_escalated_family_cannot_escape_into_direct_adoption(self):
        self.seed()
        PC.record_family_event(
            "candidate", "escalate", upstream_url="https://upstream.example/issues/436",
            note="reported upstream",
        )
        original = PC.gh

        def refuse_gh(*argv, **kwargs):
            raise AssertionError(f"adopting an escalated family must make no gh call: {argv}")

        PC.gh = refuse_gh
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refused:
                    PC.cmd_adopt(argparse.Namespace(family="candidate", cap=3, json=False))
        finally:
            PC.gh = original

        self.assertEqual(refused.exception.code, 3)
        self.assertEqual(
            PC.fold_families()["adoption"]["candidate"]["lifecycle"], "escalated",
        )


class TestReviewRegressions(PapercutBase):
    """One case per defect the post-implementation review confirmed.

    Each test names the wrong behaviour it pins down. A regression test whose
    only comment restates its own assertion tells a later reader nothing about
    why removing the guard is unsafe.
    """

    def event(self, family, action, **payload):
        return PC.record_family_event(family, action, **payload)

    def state(self, family):
        return PC.fold_families()["adoption"].get(family, PC.family_state(family))

    def call(self, fn, **kwargs):
        """Run a command function, returning (stdout, stderr, exit status)."""
        out, err = io.StringIO(), io.StringIO()
        status = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                fn(argparse.Namespace(**kwargs))
            except SystemExit as exc:
                status = exc.code if isinstance(exc.code, int) else 1
        return out.getvalue(), err.getvalue(), status

    def dossier(self):
        return """# Causal hypothesis
The guard reflects the intended policy.

# Strongest counterexample
A target-repo fix would help only if the policy were not intentional.

# Owner class
intended-policy

# No-Claim Boundary
This verdict does not prove other guards are correctly scoped.
"""

    # --- R#5: an upstream reopen ends the disposition epoch -------------------

    def test_upstream_reopen_clears_the_closure_so_the_next_close_starts_fresh(self):
        locator = {"repo": "o/r", "kind": "issue", "number": 5,
                   "url": "https://example.test/o/r/issues/5"}
        self.event("epoch", "assign", sig="member")
        self.event("epoch", "adopt", dossier_digest="d", locator=locator)
        self.event("epoch", "close-observed", locator=locator,
                   observed_state="closed", observed_at=iso(days_ago=3))
        first = self.state("epoch")["closed_observation"]
        self.assertIsNotNone(first)

        self.event("epoch", "close-observed", locator=locator,
                   observed_state="open", observed_at=iso(days_ago=2))
        # Without this the family stayed flagged forever against a closure that
        # no longer held, and the recurrence comment kept firing on live work.
        self.assertIsNone(self.state("epoch")["closed_observation"])
        self.assertIsNone(self.state("epoch")["recur_comment"])

        self.event("epoch", "close-observed", locator=locator,
                   observed_state="closed", observed_at=iso(days_ago=1))
        second = self.state("epoch")["closed_observation"]
        self.assertIsNotNone(second)
        self.assertNotEqual(second["observed_at"], first["observed_at"],
                            "the second closure opens its own epoch")

    # --- R#16: unassign is scoped to the family that recorded it --------------

    def test_unassign_against_one_family_cannot_evict_a_member_of_another(self):
        self.event("alpha", "assign", sig="shared")
        self.event("beta", "assign", sig="shared")
        # Recorded against alpha, which no longer owns the signature. Before the
        # fix this popped the membership entry outright, so the signature fell
        # back to its raw row and silently split beta's family.
        self.event("alpha", "unassign", sig="shared")
        self.assertEqual(PC.fold_families()["membership"].get("shared"), "beta")

        self.event("beta", "unassign", sig="shared")
        self.assertIsNone(PC.fold_families()["membership"].get("shared"))

    # --- R#9: a torn final line must not swallow the next event ---------------

    def test_append_terminates_a_torn_tail_instead_of_concatenating_onto_it(self):
        self.event("torn", "create")
        with open(PC.FAMILIES, "a", encoding="utf-8") as fh:
            fh.write('{"family": "torn", "action": "assi')  # crash mid-write

        self.event("torn", "assign", sig="after-the-tear")
        lines = PC.FAMILIES.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-2], '{"family": "torn", "action": "assi',
                         "the torn bytes stay verbatim, visible to an auditor")
        self.assertEqual(json.loads(lines[-1])["sig"], "after-the-tear",
                         "the next event is its own parseable line")
        self.assertEqual(PC.fold_families()["membership"].get("after-the-tear"), "torn")

    # --- R#10: an unreadable log fails closed ---------------------------------

    def test_unreadable_family_log_refuses_instead_of_reading_as_no_families(self):
        self.event("perm", "assign", sig="member")
        PC.FAMILIES.chmod(0o000)
        err = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(err):
                    PC.read_family_events()
        finally:
            PC.FAMILIES.chmod(0o600)
        # Degrading to [] silently unassigned every member and re-opened the
        # legacy auto-file route against work that had already been adopted.
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("cannot read family state", err.getvalue())

    def test_missing_family_log_is_an_empty_history_not_a_refusal(self):
        self.assertEqual(PC.read_family_events(), [])

    # --- R#22: the authored dossier survives a failed write -------------------

    def test_a_failed_dossier_write_leaves_the_previous_draft_intact(self):
        path = PC.dossier_path("atomic")
        PC.write_dossier(path, "# Causal hypothesis\nthe original judgment\n")
        original_fdopen = PC.os.fdopen

        def exploding_fdopen(fd, *a, **kw):
            os.close(fd)
            raise OSError("no space left on device")

        PC.os.fdopen = exploding_fdopen
        try:
            with self.assertRaises(OSError):
                PC.write_dossier(path, "a replacement that never lands")
        finally:
            PC.os.fdopen = original_fdopen

        # Truncating the live path would have destroyed hand-authored judgment
        # that nothing else holds a copy of.
        self.assertIn("the original judgment", path.read_text(encoding="utf-8"))
        siblings = [q.name for q in path.parent.iterdir() if q.name != path.name]
        self.assertEqual(siblings, [], "the temp file is removed on failure")

    # --- R#2: MERGED is terminal, exactly like CLOSED -------------------------

    def test_remote_item_state_classifies_merged_as_closed(self):
        self.assertEqual(PC.remote_item_state("MERGED"), "closed")
        self.assertEqual(PC.remote_item_state("CLOSED"), "closed")
        self.assertEqual(PC.remote_item_state("open"), "open")
        # Anything neither route can classify stays None so the caller refuses
        # rather than guesses. Leaving MERGED there made every adopted merged PR
        # permanently unconfirmable: refused by the cap, never disposed.
        self.assertIsNone(PC.remote_item_state("DRAFT"))
        self.assertIsNone(PC.remote_item_state(None))

    def test_a_merged_pull_request_resolves_to_a_closed_disposition(self):
        self.event("merged", "assign", sig="member")
        self.event("merged", "adopt", dossier_digest="d", locator={
            "repo": "o/r", "kind": "pr", "number": 9,
            "url": "https://example.test/o/r/pull/9"})
        adoption = PC.fold_families()["adoption"]

        original, PC.gh = PC.gh, lambda *a, **k: (0, json.dumps({"state": "MERGED"}))
        try:
            unknown, _ = PC.refresh_family_dispositions(adoption, ["merged"], 10)
        finally:
            PC.gh = original
        self.assertEqual(unknown, set())
        self.assertEqual(
            PC.fold_families()["adoption"]["merged"]["closed_observation"]["state"],
            "closed")

    # --- R#1: an open labeled PR counts against the cap -----------------------

    def test_the_open_work_cap_counts_labeled_prs_not_only_issues(self):
        queried = []

        def fake_gh(*argv, **kw):
            argv = tuple(map(str, argv))
            queried.append(argv[0])
            if argv[:2] == ("pr", "list"):
                return 0, json.dumps([{"number": 4, "url": "u"}])
            return 0, "[]"

        original, PC.gh = PC.gh, fake_gh
        try:
            count = PC.open_papercut_count([], {}, "o/r", repositories={"o/r"})
        finally:
            PC.gh = original
        # The trivial adopt route files a PR, so counting issues alone let every
        # one of them escape the cap entirely.
        self.assertEqual(count, 1)
        self.assertEqual(sorted(set(queried)), ["issue", "pr"])

    # --- R#7: recorded filings outlive the capture window ---------------------

    def test_a_recorded_filing_keeps_its_repository_in_the_cap_universe(self):
        PC.record_filing("other/repo", "issue", 12, "https://example.test/12")
        # The capture window is not the universe: an item filed into a forced
        # destination, or one whose capture cwd has since been swept, is still
        # ours and must still count.
        self.assertIn("other/repo", PC.adoption_repository_universe([], {}, ""))
        self.assertEqual([f["number"] for f in PC.read_filings()], [12])

    def test_an_unreadable_filing_registry_refuses_rather_than_widening_the_cap(self):
        PC.record_filing("other/repo", "issue", 12, "u")
        PC.FILINGS.chmod(0o000)
        try:
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    PC.read_filings()
        finally:
            PC.FILINGS.chmod(0o600)

    # --- R#12: unconfirmed is a different claim from absent -------------------

    def test_remote_probes_report_unconfirmed_separately_from_absent(self):
        original = PC.gh
        try:
            PC.gh = lambda *a, **k: (1, "could not resolve to a Repository")
            self.assertIs(PC.repo_exists("o/r"), False)
            # Collapsing the next case into False reported a network blip to the
            # operator as "destination repository does not exist" -- a different
            # and far more alarming claim than the truth.
            PC.gh = lambda *a, **k: (1, "dial tcp: connection refused")
            self.assertIsNone(PC.repo_exists("o/r"))
        finally:
            PC.gh = original

    # --- W#7 / R#17: a credential shape never enters the registry -------------

    def test_assign_refuses_a_signature_carrying_a_credential_shape(self):
        secret = "auth_failed:ghp_" + "A" * 36
        _, err, status = self.call(PC.cmd_family_assign, family="leak", sig=secret)
        self.assertEqual(status, 3)
        self.assertIn("credential shape", err)
        # Refused at the door rather than scrubbed: the signature is an immutable
        # capture address, so rewriting it would forge a different one.
        self.assertEqual(PC.read_family_events(), [])

    def test_a_legacy_member_signature_is_redacted_at_the_dossier_boundary(self):
        secret = "auth_failed:ghp_" + "B" * 36
        # Recorded straight into the log, as a store written before the
        # assign-time guard existed would already hold.
        entry = {"family": "legacy", "count": 3, "sessions": 3, "projects": ["p"],
                 "samples": [], "members": [secret]}
        body = PC.dossier_evidence(entry, days=7, event_position=1,
                                   min_count=3, min_sessions=3)
        self.assertNotIn(secret, body)
        self.assertIn("auth_failed:ghp_<redacted>", body)

    # --- W#3 / W#4: a terminal family absorbs no silent mutation --------------

    def test_assigning_into_a_disposed_family_is_refused(self):
        self.event("done", "assign", sig="first")
        self.event("done", "dispose", verdict="intended-policy", dossier_digest="d")
        _, err, status = self.call(PC.cmd_family_assign, family="done", sig="second")
        self.assertEqual(status, 3)
        self.assertIn("already disposed", err)
        self.assertIn("papercut family reopen done", err)
        # A disposed family's members are suppressed from the flagged lane, so an
        # absorbed assignment vanished from triage without a trace.
        self.assertIsNone(PC.fold_families()["membership"].get("second"))

    def test_disposing_an_adopted_family_is_refused_before_any_deletion(self):
        path = PC.dossier_path("live")
        PC.write_dossier(path, self.dossier())
        self.event("live", "adopt", dossier_digest="d", locator={
            "repo": "o/r", "kind": "issue", "number": 3, "url": "u"})
        _, err, status = self.call(PC.cmd_family_dispose, family="live",
                                   verdict="intended-policy")
        self.assertEqual(status, 3)
        self.assertIn("already adopted", err)
        # Disposing an adopted family would strand the open work item it filed.
        self.assertTrue(path.exists(), "the refusal precedes the unlink")

    # --- R#18: reopen restores what dispose retained --------------------------

    def test_reopen_restores_the_dossier_that_dispose_retained(self):
        path = PC.dossier_path("cycle")
        PC.write_dossier(path, self.dossier())
        _, err, status = self.call(PC.cmd_family_dispose, family="cycle",
                                   verdict="intended-policy")
        self.assertEqual(status, 0, err)
        self.assertFalse(path.exists())

        out, err, status = self.call(PC.cmd_family_reopen, family="cycle")
        self.assertEqual(status, 0, err)
        self.assertIn("dossier restored", out)
        # Reopening without this left the operator to re-author judgment the
        # tool was still holding a copy of.
        self.assertIn("intended policy", path.read_text(encoding="utf-8"))

    def test_reopen_never_clobbers_a_dossier_the_operator_reauthored(self):
        path = PC.dossier_path("cycle")
        PC.write_dossier(path, self.dossier())
        self.call(PC.cmd_family_dispose, family="cycle", verdict="intended-policy")
        PC.write_dossier(path, "# Causal hypothesis\na replacement draft\n")

        out, _, status = self.call(PC.cmd_family_reopen, family="cycle")
        self.assertEqual(status, 0)
        self.assertNotIn("dossier restored", out)
        # An operator who authored a replacement outranks the retained copy.
        self.assertIn("a replacement draft", path.read_text(encoding="utf-8"))

    # --- R#11: the observed locator is validated, and cannot be redirected ----

    def test_close_observed_validates_its_locator_before_appending(self):
        for bad, reason in (
            ({"repo": "not-a-repo"}, "owner/repository"),
            ({"number": 0}, "positive work-item number"),
            ({"url": "   "}, "--url must not be empty"),
        ):
            fields = {"family": "obs", "repo": "o/r", "kind": "issue", "number": 3,
                      "url": "u", "state": "closed", "observed_at": None}
            fields.update(bad)
            _, err, status = self.call(PC.cmd_family_close_observed, **fields)
            self.assertEqual(status, 3, f"{bad} must be refused")
            self.assertIn(reason, err)
        self.assertEqual(PC.read_family_events(), [],
                         "a refused observation appends nothing")

    def test_an_observation_cannot_redirect_an_adopted_family_at_other_work(self):
        self.event("adopted", "adopt", dossier_digest="d", locator={
            "repo": "o/r", "kind": "issue", "number": 3, "url": "u"})
        _, err, status = self.call(
            PC.cmd_family_close_observed, family="adopted", repo="o/r", kind="issue",
            number=99, url="u", state="closed", observed_at=None)
        self.assertEqual(status, 3)
        self.assertIn("refusing to observe a different work item", err)
        # Disposition, recurrence and the cap all read this locator back, so a
        # hijacked one silently retargets every downstream decision.
        self.assertEqual(
            PC.fold_families()["adoption"]["adopted"]["locator"]["number"], 3)

    # --- recurrence: a closure is not by itself a recurrence ------------------

    def test_a_closure_with_no_later_member_activity_is_not_a_recurrence(self):
        state = dict(PC.family_state("quiet"))
        state["closed_observation"] = {"state": "closed", "observed_at": iso(days_ago=1),
                                       "ts": iso(days_ago=1)}
        # Commenting on every closed family every week is spam, not signal: only
        # activity that postdates the closure is evidence the fix did not hold.
        self.assertFalse(
            PC.has_new_recurrence(state, {"member"}, [{"sig": "member", "ts": iso(days_ago=3)}]))
        self.assertTrue(
            PC.has_new_recurrence(state, {"member"}, [{"sig": "member", "ts": iso(days_ago=0)}]))

    def test_the_recurrence_marker_is_keyed_on_the_closure_not_the_last_comment(self):
        state = dict(PC.family_state("marked"))
        state["closed_observation"] = {"state": "closed", "observed_at": iso(days_ago=2),
                                       "ts": iso(days_ago=2)}
        first = PC.recurrence_marker("marked", state)
        state["recur_comment"] = {"ts": iso(days_ago=1)}
        # A marker that moved with each comment would never match the one already
        # on the issue, so every run would post another duplicate.
        self.assertEqual(PC.recurrence_marker("marked", state), first)

    # --- W#12 push-back: the ranked row schema is locked, not narrowed --------

    def test_ranked_rows_carry_one_stable_key_set_for_every_caller(self):
        self.event("keys", "assign", sig="member")
        rows = PC.rank([self.rec(sig="member")],
                       membership=PC.fold_families()["membership"])
        # `family` and `members` are additive, and one rank() shape for every
        # caller beats a caller-dependent schema. Locked here so a later
        # narrowing of `list --json` fails a test instead of silently breaking
        # whatever else already reads these rows.
        self.assertEqual(sorted(rows[0]), [
            "count", "family", "members", "projects", "quarantine",
            "samples", "self_reported", "sessions", "sig",
        ])
        self.assertEqual(rows[0]["family"], "keys")
        self.assertEqual(rows[0]["members"], ["member"])

    # --- an empty heading is one problem, not two ----------------------------

    def test_an_empty_owner_class_heading_is_reported_once_as_missing(self):
        # owner_class_problem() deliberately ignores an empty heading because
        # every caller already reports it as missing. Without that `owner and`
        # guard the operator is told the same heading is both absent and an
        # invalid value, and has to work out that those are one problem.
        text = ("## Causal hypothesis\nh\n\n## Strongest counterexample\nc\n\n"
                "## Owner class\n\n## No-Claim Boundary\nb\n")
        missing = PC.dispose_dossier_missing(text, "intended-policy")
        self.assertEqual(missing, ["Owner class"])
        self.assertIsNone(PC.owner_class_problem(text))
        # ...and a wrong value is still caught, on its own.
        wrong = text.replace("## Owner class\n", "## Owner class\nnot-a-class\n")
        self.assertEqual(PC.dispose_dossier_missing(wrong, "intended-policy"), [
            "Owner class (must be one of: "
            + ", ".join(sorted(PC.DOSSIER_OWNER_CLASSES)) + ")",
        ])


class TestDispatchHandoff(PapercutBase):
    """The adopt-to-an autonomous queue handoff surface (an earlier change).

    Dispatch state is derived from the labels the refresh pass already reads
    and printed, never stored. The plain read path keeps making no gh call.
    """

    def assign(self, family, *sigs):
        for sig in sigs:
            PC.record_family_event(family, "assign", sig=sig)

    def adopt(self, family, number):
        PC.record_family_event(
            family, "adopt", dossier_digest="dossier",
            locator={
                "repo": "o/r", "kind": "issue", "number": number,
                "url": f"https://example.test/o/r/issues/{number}",
            },
        )

    def rollup(self, *, refresh=False, limit=10):
        args = argparse.Namespace(
            days=7, min_count=3, min_sessions=3, limit=limit,
            apply=False, repo="o/r", refresh=refresh, cap=-1,
        )
        output = io.StringIO()
        status = 0
        with contextlib.redirect_stdout(output):
            try:
                PC.cmd_rollup(args)
            except SystemExit as exc:
                status = exc.code
        return output.getvalue(), status

    def seed_four_states(self):
        """One adopted family per dispatch state, none with recent records.

        Deliberately record-free: the families sit below every threshold, so
        this fixture also proves the refresh candidate set includes quiet
        adopted families -- the ones most likely to be awaiting the operator.
        """
        labels_by_number = {
            17: [],
            18: [{"name": "the dispatch-ready label"}],
            19: [{"name": "the dispatch-ready label"}, {"name": "claimed"}],
            20: [{"name": "the dispatch-ready label"}, {"name": "blocked"}],
        }
        for family, number in self.families().items():
            self.assign(family, f"{family}-member")
            self.adopt(family, number)

        def fake_gh(*argv, check=False):
            argv = tuple(map(str, argv))
            self.assertEqual(argv[:2], ("issue", "view"),
                             f"refresh must only view adopted items: {argv}")
            number = int(argv[2])
            return 0, json.dumps({
                "state": "OPEN",
                "url": f"https://example.test/o/r/issues/{number}",
                "labels": labels_by_number[number],
            })

        return fake_gh

    @staticmethod
    def families():
        return {"untagged": 17, "queued": 18, "in-flight": 19, "held": 20}

    def test_refresh_prints_each_adopted_open_familys_dispatch_state(self):
        fake_gh = self.seed_four_states()
        original, PC.gh = PC.gh, fake_gh
        try:
            out, status = self.rollup(refresh=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertIn("dispatch handoff:", out)
        # Every status is a label fact, never an inferred outcome: readiness
        # stays an external readiness check's call and a claim names a session, not a an autonomous queue
        # run (the claimed label is the shared cross-session signal).
        self.assertIn("untagged: awaiting operator the dispatch-ready label tag "
                      "— https://example.test/o/r/issues/17", out)
        self.assertIn("queued: tagged the dispatch-ready label — intake-eligible once an external readiness check "
                      "clears it — https://example.test/o/r/issues/18", out)
        self.assertIn("in-flight: claimed — a session holds it, excluded from "
                      "ready work — https://example.test/o/r/issues/19", out)
        self.assertIn("held: blocked — excluded from ready work "
                      "— https://example.test/o/r/issues/20", out)

    def test_plain_rollup_prints_no_dispatch_state_and_makes_no_gh_call(self):
        self.seed_four_states()

        def refusing_gh(*argv, **kwargs):
            raise AssertionError(f"plain rollup must make no gh call: {argv}")

        original, PC.gh = PC.gh, refusing_gh
        try:
            out, status = self.rollup()
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertNotIn("dispatch handoff", out)
        self.assertNotIn("the dispatch-ready label", out)

    def test_an_adopted_pull_request_is_named_outside_the_intake(self):
        # The trivial route adopts a PR. an external readiness check reads issues only, so a
        # the dispatch-ready label tag on a PR pulls a lever that is not connected -- the line
        # must say so instead of calling it intake-eligible.
        self.assign("trivial", "trivial-member")
        PC.record_family_event(
            "trivial", "adopt", dossier_digest="dossier",
            locator={"repo": "o/r", "kind": "pr", "number": 7,
                     "url": "https://example.test/o/r/pull/7"})
        original, PC.gh = PC.gh, lambda *a, **k: (
            0, json.dumps({"state": "OPEN",
                           "url": "https://example.test/o/r/pull/7",
                           "labels": [{"name": "the dispatch-ready label"}]}))
        try:
            out, status = self.rollup(refresh=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertIn("trivial: pull request — outside an autonomous queue intake "
                      "(an external readiness check reads issues); finish or merge it directly "
                      "— https://example.test/o/r/pull/7", out)
        self.assertNotIn("intake-eligible", out)

    def test_the_read_cap_rotates_toward_the_least_recently_observed_family(self):
        # Under a read cap, a deterministic order would starve the same tail
        # families every week once adopted families outnumber --limit. The
        # quiet tail is ordered never-observed first, then oldest observation
        # first -- and each read appends a fresh observation, so the cap
        # round-robins across runs with no new state.
        #
        # The observed family sorts alphabetically FIRST ("aa-" < "zz-") so
        # fold_families' sorted adoption order alone cannot pass this test:
        # only the observation-age ordering puts the never-observed family
        # ahead of it. (The first fixture named them the other way around and
        # a kill-the-sort mutant survived.)
        self.assign("aa-seen-recently", "seen-member")
        self.adopt("aa-seen-recently", 30)
        PC.record_family_event(
            "aa-seen-recently", "close-observed",
            locator={"repo": "o/r", "kind": "issue", "number": 30,
                     "url": "https://example.test/o/r/issues/30"},
            observed_state="open", observed_at=iso(days_ago=1))
        self.assign("zz-never-seen", "never-member")
        self.adopt("zz-never-seen", 31)

        viewed = []

        def fake_gh(*argv, check=False):
            argv = tuple(map(str, argv))
            viewed.append(int(argv[2]))
            return 0, json.dumps({
                "state": "OPEN",
                "url": f"https://example.test/o/r/issues/{argv[2]}",
                "labels": [],
            })

        original, PC.gh = PC.gh, fake_gh
        try:
            out, status = self.rollup(refresh=True, limit=1)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertEqual(viewed, [31],
                         "the single read must go to the never-observed family")
        self.assertIn("zz-never-seen:", out)
        self.assertNotIn("aa-seen-recently:", out)

    def test_a_closed_item_gets_no_dispatch_line_but_still_records_closure(self):
        # A closed work item leaves the handoff queue entirely: disposition
        # (and from there the verification stage) is its surface, not this one.
        self.assign("finished", "finished-member")
        self.adopt("finished", 21)
        original, PC.gh = PC.gh, lambda *a, **k: (
            0, json.dumps({"state": "CLOSED",
                           "url": "https://example.test/o/r/issues/21",
                           "labels": [{"name": "the dispatch-ready label"}]}))
        try:
            out, status = self.rollup(refresh=True)
        finally:
            PC.gh = original

        self.assertEqual(status, 0)
        self.assertNotIn("dispatch handoff", out)
        state = PC.fold_families()["adoption"]["finished"]
        self.assertIsNotNone(state["closed_observation"])




class TestFixtureRule(unittest.TestCase):
    """fixture_rule() — record-level, scoped to the route-guard test matrix
    (an earlier change). Planted negatives pin the dossier's no-claim boundary:
    genuine denials under the same signature keep counting."""

    SIG = "guard_blocked:a-route-guard"

    def fx(self, cwd="/tmp/route-guard-B9VGxg", sig=None, err=None):
        return {
            "sig": sig or self.SIG,
            "cwd": cwd,
            "err": err or ("[a-route-guard] r is an independent "
                           "noncanonical clone. Enter or create worktrees from "
                           "/canonical; use an external worktree tool --issue for planned work."),
        }

    def test_fixture_cwds_are_classified(self):
        # The live shapes: the test's mkdtemp dir, a historical subdir-guard
        # fixture (with subpath), and the literal laundering-workspace payload.
        for cwd in ("/tmp/route-guard-B9VGxg",
                    "/tmp/route-guard-B9VGxg/nested",
                    "/tmp/canonical-route-subdir-guard-hci95pf_/canonical/apps/nested",
                    "/parent/workspace"):
            self.assertEqual(PC.fixture_rule(self.fx(cwd=cwd)),
                             "route-guard-fixture-cwd", cwd)

    def test_synthetic_input_reasons_trip_from_any_cwd(self):
        # A malformed-input deny fires before setDenyContext(), so a production
        # single-shot hook can never log one — only the in-process test matrix
        # can, and it may carry a stale (even realistic) cwd.
        for reason in ("Hook input is malformed; refusing EnterWorktree because "
                       "its target cannot be verified.",
                       "Hook input is not an object; refusing EnterWorktree "
                       "because its target cannot be verified."):
            rec = self.fx(cwd="/home/user/SITES/real-repo",
                          err=f"[a-route-guard] {reason}")
            self.assertEqual(PC.fixture_rule(rec), "route-guard-synthetic-input")

    def test_genuine_denial_from_a_real_cwd_is_kept(self):
        # No-claim boundary: the guard blocking a real noncanonical clone is
        # intended policy — the genuine remainder is the family's signal.
        self.assertIsNone(PC.fixture_rule(self.fx(cwd="/home/user/SITES/example-project")))

    def test_cannot_verify_errors_from_real_cwds_are_kept(self):
        # "Cannot verify (bad state)" is a potential guard DEFECT and must stay
        # measurable — quarantining it would hide the bug class the dossier
        # explicitly keeps in scope.
        rec = self.fx(cwd="/home/user/SITES/example-project",
                      err="[a-route-guard] Cannot verify "
                          "repository route (bad state); refusing EnterWorktree "
                          "before it creates or enters worktree state.")
        self.assertIsNone(PC.fixture_rule(rec))

    def test_other_signatures_never_trip_even_from_fixture_cwds(self):
        # Scope-limited on purpose: another guard's record from a /tmp fixture
        # dir is a DIFFERENT leak that must surface as its own papercut, not be
        # absorbed by this family's cleanup.
        self.assertIsNone(PC.fixture_rule(self.fx(sig="guard_blocked:a-vcs-guard")))

    def test_record_with_no_cwd_is_kept(self):
        self.assertIsNone(PC.fixture_rule({"sig": self.SIG, "err": "x"}))


class TestFixtureReadRecords(PapercutBase):
    def fixture_rec(self, **kw):
        kw.setdefault("sig", TestFixtureRule.SIG)
        kw.setdefault("cwd", "/tmp/route-guard-abc123")
        kw.setdefault("err", "[a-route-guard] r is an "
                             "independent noncanonical clone.")
        return self.rec(**kw)

    def test_fixture_records_are_dropped_at_the_shared_chokepoint(self):
        # One guard in read_records covers every consumer (list, show, rollup,
        # triage, verification) — no per-call-site filtering to drift.
        self.write("-tmp-route-guard-abc123", [self.fixture_rec()])
        self.write("-home-user-SITES-p",
                   [self.fixture_rec(cwd="/home/user/SITES/p")])
        self.assertEqual([r["cwd"] for r in PC.read_records(7)],
                         ["/home/user/SITES/p"])

    def test_include_fixtures_escape_returns_everything(self):
        self.write("-tmp-route-guard-abc123", [self.fixture_rec()])
        self.assertEqual(len(list(PC.read_records(7, include_fixtures=True))), 1)
        self.assertEqual(len(list(PC.read_records(7))), 0)
        self.assertEqual(PC.count_fixture_records(7), 1)


class TestFixtureRollup(PapercutBase):
    def run_rollup(self, *argv):
        env = dict(os.environ, PAPERCUT_STORE=str(self.store))
        return subprocess.run(
            [sys.executable, str(PAPERCUT), "rollup", *argv],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )

    def test_fixture_volume_is_counted_never_ranked(self):
        # Five sessions of fixture records would clear the rollup threshold if
        # they counted; they must instead surface ONLY as the dropped counter.
        self.write("-tmp-route-guard-abc123", [
            self.rec(sig=TestFixtureRule.SIG, cwd="/tmp/route-guard-abc123",
                     err="[a-route-guard] r is an independent "
                         "noncanonical clone.", session=f"s{i}")
            for i in range(5)
        ])
        out = self.run_rollup("--days", "7", "--min-sessions", "3",
                              "--min-count", "3").stdout
        self.assertIn("papercuts-fixture-records:5", out)
        self.assertIn("papercuts-flagged:0", out)
        self.assertIn("0 record(s), 0 folded row(s)", out)
        self.assertIn("test-fixture record(s) dropped", out)

    def test_zero_fixture_days_still_print_the_token(self):
        # The token is grep-stable for a weekly scheduled run either way; the prose
        # line appears only when something was dropped.
        self.write("-p", [self.rec()])
        out = self.run_rollup("--days", "7").stdout
        self.assertIn("papercuts-fixture-records:0", out)
        self.assertNotIn("test-fixture record(s) dropped", out)


if __name__ == "__main__":
    unittest.main()
