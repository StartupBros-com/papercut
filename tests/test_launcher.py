"""Real-subprocess tests for scripts/papercut, the plugin-relative launcher.

The launcher is a POSIX /bin/sh script, not Python, so these tests never
import it -- they exec it as a subprocess exactly as an agent or the shipped
skill would, and assert on real process behavior: exit status, stdout, and
what actually landed in the store file.

Every invocation routes PAPERCUT_STORE at a per-test tempdir so no test ever
touches the real ~/.claude/papercuts store.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

LAUNCHER = (Path(__file__).resolve().parent.parent / "scripts" / "papercut")


def run_launcher(args, cwd, store, extra_env=None, launcher=None):
    """Execute the launcher as a real subprocess and return the CompletedProcess."""
    env = dict(os.environ)
    env["PAPERCUT_STORE"] = str(store)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(launcher or LAUNCHER), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class LauncherExistsTest(unittest.TestCase):
    def test_launcher_is_executable_on_disk(self):
        self.assertTrue(LAUNCHER.is_file(), f"missing launcher at {LAUNCHER}")
        mode = LAUNCHER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "scripts/papercut must be user-executable")

    def test_launcher_is_posix_sh_not_bash(self):
        with open(LAUNCHER, "r", encoding="utf-8") as fh:
            first_line = fh.readline().strip()
        self.assertEqual(first_line, "#!/bin/sh", "launcher must use a POSIX /bin/sh shebang")


class LauncherRunsFromAnyCwdTest(unittest.TestCase):
    def test_runs_successfully_from_an_unrelated_cwd(self):
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            # tmp is deliberately unrelated to the plugin checkout and to the
            # store: nothing here should require being run from the repo.
            result = run_launcher(["--help"], cwd=tmp, store=store)
            self.assertEqual(
                result.returncode, 0,
                f"expected exit 0 from an unrelated cwd, got {result.returncode}\n"
                f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )


class LauncherHelpTest(unittest.TestCase):
    def test_help_exits_zero_and_lists_real_subcommands(self):
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-help-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            result = run_launcher(["--help"], cwd=tmp, store=store)
            self.assertEqual(result.returncode, 0, result.stderr)
            # These are the real subcommands argparse registers in papercut/cli.py
            # main(). This case runs in an EMPTY cwd, so on its own it only
            # catches "launcher can't find the package at all" -- the
            # shadowing case needs a competing package actually present, which
            # test_a_papercut_package_in_the_cwd_cannot_shadow_the_plugin builds.
            for subcommand in ("add", "list", "triage", "rollup", "family", "show"):
                self.assertIn(
                    subcommand, result.stdout,
                    f"--help output missing subcommand {subcommand!r}:\n{result.stdout}",
                )


class LauncherArgumentPassthroughTest(unittest.TestCase):
    def test_arguments_containing_spaces_survive_intact(self):
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            message = "hello   world  with   irregular   spacing"
            result = run_launcher(
                ["add", "-m", message, "--sig", "launcher-space-test", "-q", "--cwd", tmp],
                cwd=tmp,
                store=store,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            jsonl_files = list(Path(store).glob("*.jsonl"))
            self.assertEqual(
                len(jsonl_files), 1,
                f"expected exactly one store file, found {jsonl_files}",
            )
            contents = jsonl_files[0].read_text(encoding="utf-8")
            self.assertIn(
                message, contents,
                "message with irregular internal spacing was mangled in transit "
                f"through the launcher; store contents:\n{contents}",
            )


class LauncherExitStatusTest(unittest.TestCase):
    def test_nonzero_cli_exit_status_propagates(self):
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            # `add` requires -m/--message; omitting it is a real argparse usage
            # error in papercut/cli.py main(), which exits 2. This checks the
            # launcher's own exit status (via `exec`), not the shell's.
            result = run_launcher(["add"], cwd=tmp, store=store)
            self.assertEqual(
                result.returncode, 2,
                f"expected the CLI's own exit 2 to propagate through the "
                f"launcher, got {result.returncode}\nstderr={result.stderr!r}",
            )
            self.assertNotEqual(result.returncode, 0)

    def test_zero_exit_status_also_propagates(self):
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            result = run_launcher(["list", "--json"], cwd=tmp, store=store)
            self.assertEqual(result.returncode, 0, result.stderr)


class LauncherShadowingTest(unittest.TestCase):
    """A package named `papercut` in the caller's cwd must not win.

    THE DEFECT THIS PINS: the launcher used to run `python3 -m papercut` with
    the plugin root merely prepended to PYTHONPATH. CPython inserts the
    caller's working directory at sys.path[0] for -m, AHEAD of PYTHONPATH, so
    any customer repo that happened to contain a `papercut/` directory ran ITS
    code instead of the plugin's -- exit 0, wrong program, no error. That is
    both the opposite of the launcher's stated guarantee and an
    arbitrary-code-execution path on the documented happy path.

    Demonstrated 2026-09-14 against the shipped launcher: a decoy
    papercut/__main__.py in an unrelated cwd printed its own output and exited
    0. Every launcher test passed at the time, because none of them put a
    competing package in the cwd.
    """

    def decoy(self, root: Path) -> None:
        pkg = root / "papercut"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__main__.py").write_text(
            'import sys\nprint("DECOY-MAIN")\nsys.exit(0)\n', encoding="utf-8")
        (pkg / "cli.py").write_text(
            'def main():\n    print("DECOY-CLI")\n    return 0\n', encoding="utf-8")

    def test_a_papercut_package_in_the_cwd_cannot_shadow_the_plugin(self):
        with tempfile.TemporaryDirectory(prefix="papercut-shadow-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-shadow-store-") as store:
            self.decoy(Path(tmp))
            result = run_launcher(["--help"], cwd=tmp, store=store)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("DECOY", result.stdout,
                             "a papercut package in the cwd shadowed the plugin")
            # And it really is the plugin's own CLI that answered.
            for subcommand in ("add", "list", "triage", "rollup"):
                self.assertIn(subcommand, result.stdout, result.stdout)

    def test_shadowing_cwd_still_writes_to_the_real_plugin_store(self):
        """The hijack is not just cosmetic: prove the SHIPPED code ran by
        checking it produced a real record the shipped CLI can read back."""
        with tempfile.TemporaryDirectory(prefix="papercut-shadow2-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-shadow2-store-") as store:
            self.decoy(Path(tmp))
            added = run_launcher(
                ["add", "-m", "shadowed cwd must not win", "--sig", "shadow-test", "-q"],
                cwd=tmp, store=store)
            self.assertEqual(added.returncode, 0, added.stderr)
            listed = run_launcher(["list", "--days", "1"], cwd=tmp, store=store)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn("shadow-test", listed.stdout, listed.stdout)
            self.assertNotIn("DECOY", listed.stdout, listed.stdout)


class LauncherSymlinkTest(unittest.TestCase):
    def test_works_when_invoked_through_a_symlink_from_a_temp_dir(self):
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-symlink-") as symdir, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            link = Path(symdir) / "papercut"
            link.symlink_to(LAUNCHER)
            self.assertTrue(link.is_symlink())

            result = run_launcher(["--help"], cwd=tmp, store=store, launcher=link)
            self.assertEqual(
                result.returncode, 0,
                f"launcher failed when invoked via symlink {link} -> {LAUNCHER}\n"
                f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertIn("add", result.stdout)

    def test_symlinked_invocation_also_writes_to_the_store(self):
        # A symlinked launcher must resolve the SAME plugin checkout (and thus
        # the same PYTHONPATH-relative package) as a direct invocation, not a
        # different or missing one -- exercised here with a real write.
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-symlink-") as symdir, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store:
            link = Path(symdir) / "papercut"
            link.symlink_to(LAUNCHER)

            result = run_launcher(
                ["add", "-m", "symlinked launcher write", "--sig", "launcher-symlink-test", "-q", "--cwd", tmp],
                cwd=tmp,
                store=store,
                launcher=link,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            jsonl_files = list(Path(store).glob("*.jsonl"))
            self.assertEqual(len(jsonl_files), 1, jsonl_files)
            self.assertIn("symlinked launcher write", jsonl_files[0].read_text(encoding="utf-8"))


class LauncherMissingPython3Test(unittest.TestCase):
    def test_actionable_error_when_python3_is_not_on_path(self):
        # Build a PATH that carries the coreutils the launcher itself needs
        # (dirname/basename/readlink, for symlink and self-location
        # resolution) but deliberately no python3, so the launcher's own
        # `command -v python3` guard is what fires -- not some other command
        # missing entirely, which would fail for an unrelated reason.
        with tempfile.TemporaryDirectory(prefix="papercut-launcher-cwd-") as tmp, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-store-") as store, \
             tempfile.TemporaryDirectory(prefix="papercut-launcher-fakebin-") as fakebin:
            fakebin_path = Path(fakebin)
            for tool in ("dirname", "basename", "readlink"):
                src = shutil.which(tool)
                self.assertIsNotNone(src, f"test host is missing {tool!r}; cannot build fixture")
                (fakebin_path / tool).symlink_to(src)

            env = dict(os.environ)
            env["PAPERCUT_STORE"] = str(store)
            env["PATH"] = fakebin  # coreutils present, python3 deliberately absent
            result = subprocess.run(
                [str(LAUNCHER), "--help"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 127,
                f"expected the launcher's own 127 for a missing python3, got "
                f"{result.returncode}\nstderr={result.stderr!r}",
            )
            self.assertIn("python3", result.stderr.lower())
            # No Python traceback -- this must be the shell script's own
            # actionable message, not a confusing crash.
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
