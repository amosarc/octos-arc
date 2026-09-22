"""Tests for the .arc/ trace contract (host-computes-everything artifacts).

Covers: trace_artifact_paths() derivation (default .arc/ + platform-injected
ARCBENCH_RUNNER_EVENTS_PATH), log()'s third write to .arc/debug.log, and the
OctosDriver protocol-frame stream format (one {"method","params"} JSON per
line, byte-identical to the reference agent-arc-based interaction).

Run: python3 arc/trace_contract_test.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402
from arcbench_agent_runtime import AgentRuntime  # noqa: E402


class TraceArtifactPathsTests(unittest.TestCase):
    def test_default_paths_sit_beside_runner_events(self):
        with TemporaryDirectory() as td:
            runtime = AgentRuntime.from_env(project_dir=td)
            events_log, debug_log = main.trace_artifact_paths(runtime.paths)
            expected_root = Path(td).resolve()
        self.assertEqual(events_log, expected_root / ".arc" / "octos-events.jsonl")
        self.assertEqual(debug_log, expected_root / ".arc" / "debug.log")

    def test_injected_runner_events_path_relocates_trace(self):
        with TemporaryDirectory() as td:
            custom = str(Path(td) / "custom" / "events.jsonl")
            os.environ["ARCBENCH_RUNNER_EVENTS_PATH"] = custom
            try:
                runtime = AgentRuntime.from_env(project_dir=td)
                events_log, debug_log = main.trace_artifact_paths(runtime.paths)
            finally:
                del os.environ["ARCBENCH_RUNNER_EVENTS_PATH"]
            expected_parent = Path(custom).parent
        self.assertEqual(events_log, expected_parent / "octos-events.jsonl")
        self.assertEqual(debug_log, expected_parent / "debug.log")


class DebugLogTests(unittest.TestCase):
    def setUp(self):
        self._previous = main._DEBUG_LOG_PATH

    def tearDown(self):
        main.set_debug_log_path(self._previous)

    def test_log_appends_timestamped_lines_after_path_is_set(self):
        with TemporaryDirectory() as td:
            path = Path(td) / ".arc" / "debug.log"
            main.set_debug_log_path(path)
            main.log("[flow] single line")
            main.log("multi\nline")
            text = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(text), 3)
        for line in text:
            self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
        self.assertTrue(text[0].endswith("[flow] single line"))
        self.assertTrue(text[1].endswith("multi"))
        self.assertTrue(text[2].endswith("line"))

    def test_no_debug_log_written_before_runtime_init(self):
        with TemporaryDirectory() as td:
            main.set_debug_log_path(None)
            main.log("probe phase line")
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_debug_log_failure_never_raises(self):
        # Path under a file: open("a") raises NotADirectoryError (an OSError)
        # — the run must continue with stdout/stderr mirroring only.
        with TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("not a dir", encoding="utf-8")
            main.set_debug_log_path(blocker / "sub" / "debug.log")
            main.log("still fine")


class OctosEventsStreamTests(unittest.TestCase):
    def test_log_event_writes_reference_frame_format(self):
        with TemporaryDirectory() as td:
            driver = main.OctosDriver(
                octos_bin="/nonexistent/octos", cwd=Path(td), env={},
                data_dir=Path(td) / "state", max_iterations=1,
                events_log=Path(td) / ".arc" / "octos-events.jsonl",
            )
            driver._log_event("tool/started", {"tool_name": "bash", "turn_id": "t1"})
            driver._log_event("turn/completed", {"turn_id": "t1"})
            frames = [json.loads(line) for line in
                      (Path(td) / ".arc" / "octos-events.jsonl")
                      .read_text(encoding="utf-8").splitlines()]
        self.assertEqual(frames, [
            {"method": "tool/started", "params": {"tool_name": "bash", "turn_id": "t1"}},
            {"method": "turn/completed", "params": {"turn_id": "t1"}},
        ])

    def test_log_event_swallows_os_errors(self):
        with TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("not a dir", encoding="utf-8")
            driver = main.OctosDriver(
                octos_bin="/nonexistent/octos", cwd=Path(td), env={},
                data_dir=Path(td) / "state", max_iterations=1,
                events_log=blocker / "sub" / "octos-events.jsonl",
            )
            driver._log_event("turn/started", {})  # must not raise

    def test_log_event_skips_reasoning_deltas(self):
        # run11: reasoning-tier providers stream ~90% of all frames as
        # message/reasoning_delta (80MB trace) — they stay out of the
        # host contract stream.
        with TemporaryDirectory() as td:
            events_log = Path(td) / ".arc" / "octos-events.jsonl"
            driver = main.OctosDriver(
                octos_bin="/nonexistent/octos", cwd=Path(td), env={},
                data_dir=Path(td) / "state", max_iterations=1,
                events_log=events_log,
            )
            driver._log_event("message/reasoning_delta", {"text": "think" * 100})
            driver._log_event("message/delta", {"text": "hi"})
            lines = events_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["method"], "message/delta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
