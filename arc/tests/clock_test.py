"""Tests for the shared clock source (Beijing UTC+8 for all host stamps).

The runner container's TZ is uncontrolled (usually UTC); log panels, event
streams and telemetry must all read Beijing wall-clock from clock.py —
never gmtime/utcnow scattered around. These tests pin that contract.

Run: python3 arc/tests/clock_test.py
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clock  # noqa: E402


class ClockTests(unittest.TestCase):
    def test_stamp_is_exactly_utc_plus_eight(self):
        parsed = datetime.strptime(
            clock.stamp(), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=clock.BEIJING)
        drift = abs((parsed - datetime.now(timezone.utc)).total_seconds())
        self.assertLess(drift, 5, "stamp 必须是北京时间(UTC+8),不得回到 gmtime")

    def test_stamp_matches_log_panel_format(self):
        self.assertRegex(clock.stamp(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_compact_matches_run_dir_format(self):
        self.assertRegex(clock.compact(), r"^\d{8}-\d{6}$")

    def test_iso_carries_explicit_plus_eight_offset(self):
        got = clock.iso()
        self.assertTrue(got.endswith("+08:00"), got)
        datetime.fromisoformat(got)  # 可逆解析,偏移不丢

    def test_no_stray_utc_stamps_remain(self):
        # 单一时钟源定则:包内不得再出现散落的 gmtime/utcnow 时间戳
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for py in root.rglob("*.py"):
            if "tests" in py.parts:
                continue
            text = py.read_text(encoding="utf-8")
            if re.search(r"gmtime\(|utcnow\(|timezone\.utc\)", text):
                offenders.append(py.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
