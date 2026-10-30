"""Offline contracts for the two positive policy-bound command options."""

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "papercut_policy_bounds", Path(__file__).resolve().parents[1] / "papercut" / "cli.py")
PC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PC)


class PolicyBoundContracts:
    def test_none_uses_the_current_default_without_conversion(self):
        sentinel = object()
        for default in (sentinel, 0, -7, "23"):
            with self.subTest(default=default):
                with mock.patch.object(PC, self.default_name, default):
                    self.assertIs(self.validate(None), default)

    def test_positive_values_retain_int_coercion(self):
        for value, expected in ((1, 1), (19, 19), (" 12 ", 12),
                                ("+3", 3), (True, 1), (1.9, 1), (b"7", 7)):
            with self.subTest(value=value):
                self.assertEqual(self.validate(value), expected)

    def test_invalid_values_preserve_exact_refusal_and_status(self):
        for value in (0, -1, "0", "-4", "", "bad", "1.5", False,
                      0.9, [], {}, float("nan")):
            with self.subTest(value=value):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    with self.assertRaises(SystemExit) as raised:
                        self.validate(value)
                self.assertEqual(raised.exception.code, 3)
                self.assertEqual(output.getvalue(), "ERROR: " + self.refusal + "\n")

    def test_int_conversion_runs_once(self):
        class Counted:
            calls = 0

            def __int__(self):
                self.calls += 1
                return 8

        value = Counted()
        self.assertEqual(self.validate(value), 8)
        self.assertEqual(value.calls, 1)

    def test_type_and_value_errors_use_the_same_policy_refusal(self):
        for exception in (TypeError("type"), ValueError("value")):
            class Invalid:
                def __int__(self):
                    raise exception

            with self.subTest(exception=exception):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    with self.assertRaises(SystemExit) as raised:
                        self.validate(Invalid())
                self.assertEqual(raised.exception.code, 3)
                self.assertEqual(output.getvalue(), "ERROR: " + self.refusal + "\n")

    def test_other_conversion_errors_propagate_unchanged(self):
        for exception in (OverflowError("overflow"), RuntimeError("runtime")):
            class Invalid:
                def __int__(self):
                    raise exception

            with self.subTest(exception=exception):
                with self.assertRaises(type(exception)) as raised:
                    self.validate(Invalid())
                self.assertIs(raised.exception, exception)

    def test_infinity_still_raises_overflow(self):
        for value in (float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(OverflowError):
                    self.validate(value)


class TestWindowBound(PolicyBoundContracts, unittest.TestCase):
    validate = staticmethod(PC.validate_window)
    default_name = "VERIFY_WINDOW_DAYS"
    refusal = "policy refusal: --window must be a positive number of days"


class TestUnfamiliedBound(PolicyBoundContracts, unittest.TestCase):
    validate = staticmethod(PC.validate_unfamilied_limit)
    default_name = "TRIAGE_UNFAMILIED_LIMIT"
    refusal = "policy refusal: --unfamilied-limit must be a positive number of candidates"


if __name__ == "__main__":
    unittest.main()
