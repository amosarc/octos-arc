"""Tests for the runtime-length guards (auth fail-fast + final spec filter).

Covers the tb-trace-001 lesson (55% of wall clock陪跑 gateway auth
failures) and the run7 lesson (7 hand-assembled full-suite runs): the
auth-failure ledger that stops LLM retries past OCTOS_AUTH_FAIL_LIMIT, and
final_spec_files() which scopes the final-verification SPEC RUN to the
Tier-2 red tests' own spec files.

Run: python3 arc/perf_guard_test.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class AuthFailureTests(unittest.TestCase):
    def test_auth_failure_matches_credential_errors(self):
        for text in (
            "401: invalid token",
            "HTTP 403",
            "Authentication failed for openai/agnes-2.5-flash",
            "unauthorized",
            "无效的令牌",
        ):
            self.assertTrue(main.OctosDriver._auth_failure(text), text)

    def test_auth_failure_excludes_connection_errors(self):
        for text in (
            "connection_closed: connection closed before turn completed",
            "503 temporarily unavailable",
            "streaming request failed: econnreset",
        ):
            self.assertFalse(main.OctosDriver._auth_failure(text), text)

    def _driver(self, limit: str) -> main.OctosDriver:
        with mock.patch.dict(main.os.environ, {"OCTOS_AUTH_FAIL_LIMIT": limit}):
            return main.OctosDriver(
                octos_bin="/nonexistent/octos", cwd=Path("."), env={},
                data_dir=Path(".") / "state", max_iterations=1,
                events_log=Path(".") / "octos-events.jsonl",
            )

    def test_exhaustion_trips_at_limit_and_respects_zero(self):
        driver = self._driver("3")
        self.assertFalse(driver.auth_exhausted)
        driver.auth_failures = 3
        self.assertTrue(driver.auth_exhausted)
        off = self._driver("0")
        off.auth_failures = 100
        self.assertFalse(off.auth_exhausted, "limit 0 disables the ledger")

    def test_retries_stop_once_exhausted(self):
        driver = self._driver("2")
        calls = []

        def fn(prompt: str):
            calls.append(prompt)
            return False, "401 authentication failed"

        with mock.patch.object(main.time, "sleep"):
            ok, text = driver._run_with_retries(fn, "work", None, attempts=6)
        self.assertFalse(ok)
        # attempt 1 + backoff/retry 1 (failures -> 1) + retry 2 (failures -> 2,
        # exhausted) — the loop breaks before a third retry.
        self.assertLessEqual(len(calls), 3, calls)
        self.assertGreaterEqual(driver.auth_failures, 2)


class FinalSpecFilesTests(unittest.TestCase):
    def _stage(self, td: Path):
        stage = td / ".arc" / "acceptance"
        stage.mkdir(parents=True)
        (stage / "REQ-1.1.spec.ts").write_text(
            "test('book list loads', () => {});\n", encoding="utf-8")
        (stage / "REQ-1.2.spec.ts").write_text(
            "test('note editor opens', () => {});\n", encoding="utf-8")
        return stage

    def test_red_titles_scope_to_owning_specs(self):
        with TemporaryDirectory() as td:
            self._stage(Path(td))
            files = main.final_spec_files((5, 1, ["note editor opens"]), Path(td))
        self.assertEqual(files, ["REQ-1.2.spec.ts"])

    def test_no_red_titles_falls_back_to_all_staged(self):
        with TemporaryDirectory() as td:
            self._stage(Path(td))
            files = main.final_spec_files((5, 0, []), Path(td))
            self.assertEqual(files, ["REQ-1.1.spec.ts", "REQ-1.2.spec.ts"])
            files = main.final_spec_files(None, Path(td))
            self.assertEqual(files, ["REQ-1.1.spec.ts", "REQ-1.2.spec.ts"])

    def test_unmatched_titles_fall_back_to_all_staged(self):
        with TemporaryDirectory() as td:
            self._stage(Path(td))
            files = main.final_spec_files((0, 1, ["ghost title"]), Path(td))
            self.assertEqual(files, ["REQ-1.1.spec.ts", "REQ-1.2.spec.ts"])

    def test_missing_stage_dir_returns_none(self):
        with TemporaryDirectory() as td:
            self.assertIsNone(main.final_spec_files((1, 1, ["x"]), Path(td)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
