"""Tests for the agent-phase evaluator-browser self-repair.

10-03 arc-006 forensics: the platform image's /ms-playwright carries a
chromium-<rev> dir whose chrome binary is missing; the evaluator dies on
every scenario in 4ms (browserType.launch: Executable doesn't exist) and
scores 0. The host repairs it before the first turn — with two traps
pinned here: (1) plain `install chromium` is a marker-driven no-op
(INSTALLATION_COMPLETE), so the rev dir must be removed first;
(2) the expected revision must come from the GLOBAL package's
browsers.json — the model's self-installed newer PW revisions are
invisible to the evaluator.

Run: cd arc && python3 -m unittest tests.chromium_repair_test
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


def _make_pw_tree(root: Path, rev: str = "1200") -> tuple[Path, Path]:
    """Global @playwright/test layout + browsers.json naming `rev`.

    Returns (cli, browsers_dir) with an EMPTY chromium-<rev> dir (the
    arc-006 defect shape: dir shell present, binary missing)."""
    pkg = root / "usr" / "local" / "node" / "lib" / "node_modules"
    test_pkg = pkg / "@playwright" / "test"
    (test_pkg / "node_modules" / "playwright-core").mkdir(parents=True)
    (test_pkg / "cli.js").write_text("// stub cli\n")
    (test_pkg / "node_modules" / "playwright-core" / "browsers.json").write_text(
        json.dumps({"browsers": [
            {"name": "chromium", "revision": rev},
            {"name": "chromium-headless-shell", "revision": rev},
        ]}))
    browsers = root / "ms-playwright"
    shell = browsers / f"chromium-{rev}" / "chrome-linux64"
    shell.mkdir(parents=True)
    (shell / "INSTALLATION_COMPLETE").write_text("")
    return test_pkg / "cli.js", browsers


class ChromiumRevTests(unittest.TestCase):
    def test_rev_read_from_global_browsers_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli, _ = _make_pw_tree(Path(tmp))
            self.assertEqual(main._chromium_rev_of_global_pw(cli), "1200")

    def test_missing_browsers_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "cli.js"
            cli.write_text("// stub\n")
            self.assertIsNone(main._chromium_rev_of_global_pw(cli))

    def test_binary_check_requires_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, browsers = _make_pw_tree(Path(tmp))
            self.assertFalse(main._chromium_binary_ok(browsers, "1200"))
            binary = (browsers / "chromium-1200" / "chrome-linux64" / "chrome")
            binary.write_text("ELF...")
            self.assertTrue(main._chromium_binary_ok(browsers, "1200"))


class ChromiumRepairTests(unittest.TestCase):
    """_repair_global_chromium: verify → force-reinstall (rev dir removal
    first) → re-verify; loud on failure, silent no-op when fine."""

    def _run(self, root: Path, installed_ok: bool):
        logs = []
        cli, browsers = _make_pw_tree(root)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if installed_ok:
                binary = (Path(kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"])
                          / "chromium-1200" / "chrome-linux64" / "chrome")
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("ELF...")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        real_run = main.subprocess.run
        main.subprocess.run = fake_run
        try:
            main._repair_global_chromium(logs.append, cli=cli,
                                         browsers_dir=browsers)
        finally:
            main.subprocess.run = real_run
        return logs, calls, browsers

    def test_missing_binary_triggers_force_reinstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs, calls, browsers = self._run(Path(tmp), installed_ok=True)
        # marker 陷阱钉:rev 目录必须先被删(install 否则幂等秒退)。
        self.assertFalse(
            (browsers / "chromium-1200" / "INSTALLATION_COMPLETE").exists())
        self.assertEqual(len(calls), 1)
        self.assertIn("install", calls[0])
        self.assertIn("chromium", calls[0])
        self.assertTrue(any("repaired for the evaluator" in x
                            for x in logs), logs)

    def test_failed_reinstall_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs, _, _ = self._run(Path(tmp), installed_ok=False)
        self.assertTrue(any("repair FAILED" in x and "score 0" in x
                            for x in logs), logs)

    def test_healthy_binary_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli, browsers = _make_pw_tree(Path(tmp))
            (browsers / "chromium-1200" / "chrome-linux64" / "chrome") \
                .write_text("ELF...")
            logs = []
            calls = []
            real_run = main.subprocess.run
            main.subprocess.run = (
                lambda cmd, **kw: calls.append(cmd)
                or subprocess.CompletedProcess(cmd, 0, "", ""))
            try:
                main._repair_global_chromium(logs.append, cli=cli,
                                             browsers_dir=browsers)
            finally:
                main.subprocess.run = real_run
            self.assertEqual(calls, [])
            self.assertTrue(any("verified present" in x for x in logs), logs)

    def test_no_cli_or_no_browsers_dir_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            main._repair_global_chromium(logs.append, cli=None, browsers_dir=None)
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
