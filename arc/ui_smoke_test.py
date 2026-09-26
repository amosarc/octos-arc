#!/usr/bin/env python3
"""ui_smoke 单测 — 纯函数层(串收集/静态扫描/spec 生成/报告解析/命令形状)。

跑法:python3 arc/ui_smoke_test.py(与仓库其它 *_test.py 同风格,无 pytest 依赖)。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ui_smoke  # noqa: E402


def _node(nid: str, desc: str, children=None, ntype="ATOMIC"):
    return {"id": nid, "type": ntype, "description": desc,
            "children": children or []}


SHEET_TREE = _node(
    "ROOT", "root", ntype="FOLDER", children=[
        _node("REQ-1-2-1", 'button with the accessible name "New blank '
                           'workbook"; submit button is named "Create".'),
        _node("REQ-1-2-2", 'dialog "Rename workbook", field "Workbook name", '
                           'button "Save", error "Workbook name cannot be empty."'),
        _node("REQ-5", "folder", ntype="FOLDER", children=[
            _node("REQ-5-2-1", 'dialog "Data validation"; option "Dropdown"; '
                               'button "Open dropdown for <cell coordinate>"; '
                               'error "Please select one of the following '
                               'values: <comma-separated allowed values>."'),
            _node("REQ-5-3-1", 'editor "Pivot table editor"; field '
                               '"<summarization method> of <value field>".'),
        ]),
    ])


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.entries = ui_smoke.collect_contract_strings(SHEET_TREE)

    def _get(self, value, kind="exact"):
        return next(e for e in self.entries
                    if e["value"] == value and e["kind"] == kind)

    def test_short_control_strings_survive(self):
        # 3-6 字符的短控件名("Save"/"Create")在 plan_envelope 的 6 字符
        # 下限会丢——冒烟必须保住它们(sheet 3 分里 Delete/Rename 全靠它们)。
        for v in ("Create", "Save", "Rename workbook", "New blank workbook"):
            self.assertIn(v, [e["value"] for e in self.entries], v)

    def test_placeholder_becomes_prefix_anchor(self):
        # "Open dropdown for <cell coordinate>" → prefix "Open dropdown for"
        anchor = self._get("Open dropdown for", kind="prefix")
        self.assertEqual(anchor["nodes"], ["REQ-5-2-1"])

    def test_message_with_placeholder_is_message_prefix(self):
        e = self._get("Please select one of the following values:",
                      kind="prefix")
        self.assertTrue(e["message"])

    def test_pure_template_skipped(self):
        # "<summarization method> of <value field>" 无静态锚点 → 放弃。
        vals = [e["value"] for e in self.entries]
        self.assertNotIn("of", vals)

    def test_message_classification(self):
        e = self._get("Workbook name cannot be empty.")
        self.assertTrue(e["message"])
        self.assertEqual(e["nodes"], ["REQ-1-2-2"])

    def test_dedupe_merges_nodes(self):
        tree = _node("R", "r", children=[
            _node("A", 'button "Sort"'),
            _node("B", 'menu "Sort"'),
        ])
        entries = ui_smoke.collect_contract_strings(tree)
        e = next(x for x in entries if x["value"] == "Sort")
        self.assertEqual(e["nodes"], ["A", "B"])
        self.assertEqual(sum(1 for x in entries if x["value"] == "Sort"), 1)

    def test_cap_controls_first(self):
        descs = " ".join(f'"{chr(65 + i % 26)}{i:03d} button"' for i in range(80))
        tree = _node("R", descs, children=[_node("A", 'keep "Me last"')])
        entries = ui_smoke.collect_contract_strings(tree, max_strings=10)
        self.assertEqual(len(entries), 10)
        self.assertIn("A000 button", [e["value"] for e in entries])


class StaticScanTests(unittest.TestCase):
    def _dist(self, files: dict):
        tmp = tempfile.TemporaryDirectory()
        dist = Path(tmp.name) / "dist"
        (dist / "assets").mkdir(parents=True)
        for name, body in files.items():
            p = dist / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return dist

    def test_missing_chain_is_red(self):
        dist = self._dist({"index.html": '<script src="assets/a.js">',
                           "assets/a.js": '"New blank workbook" "Create"'})
        entries = [
            {"value": "New blank workbook", "kind": "exact", "message": False, "nodes": []},
            {"value": "Rename workbook", "kind": "exact", "message": False, "nodes": ["REQ-1-2-2"]},
        ]
        out = ui_smoke.scan_built_assets(dist, entries)
        self.assertTrue(out["attempted"])
        self.assertEqual(len(out["reds"]), 1)
        self.assertEqual(out["reds"][0]["nodes"], ["REQ-1-2-2"])

    def test_prefix_anchor_hits_template_literal(self):
        dist = self._dist({"assets/a.js": 'aria-label={`Filter ${h}`}'})
        entries = [{"value": "Filter", "kind": "prefix", "message": False, "nodes": []}]
        out = ui_smoke.scan_built_assets(dist, entries)
        self.assertEqual(out["reds"], [])

    def test_no_dist_not_attempted(self):
        out = ui_smoke.scan_built_assets(Path("/nonexistent"), [])
        self.assertFalse(out["attempted"])


class SpecGenTests(unittest.TestCase):
    def setUp(self):
        self.entries = ui_smoke.collect_contract_strings(SHEET_TREE)
        self.spec = ui_smoke.generate_spec(self.entries)

    def test_contract_json_embedded(self):
        contract = ui_smoke._contract_for_spec(self.entries)
        self.assertIn("New blank workbook", contract["exact"])
        # 消息类串不进爬虫 exact(只在静态层查),但必须留在 entries 里。
        self.assertNotIn("Workbook name cannot be empty.", contract["exact"])
        self.assertIn("Workbook name cannot be empty.",
                      [e["value"] for e in self.entries])
        anchors = {a["value"] for a in contract["anchors"]}
        self.assertIn("Open dropdown for", anchors)
        self.assertIn(json.dumps(contract, ensure_ascii=False), self.spec)

    def test_six_probe_markers_present(self):
        for marker in ("boot - no pageerror", "contract crawler",
                       "escape-close", "page-alive", "shift-select",
                       "downloads - non-empty, no BOM", "0xef"):
            self.assertIn(marker, self.spec)

    def test_messages_not_in_crawler_exact(self):
        # 消息类串只在静态层查(不要求可见可点),爬虫 exact 清单只收控件。
        contract = ui_smoke._contract_for_spec([
            {"value": "Workbook name cannot be empty.", "kind": "exact",
             "message": True, "nodes": []},
            {"value": "Create", "kind": "exact", "message": False, "nodes": []},
        ])
        self.assertEqual(contract["exact"], ["Create"])

    def test_config_and_stage(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stage = ui_smoke.stage_ui_smoke(out, self.entries, 3100, "/x/cli.js")
            cfg = (stage / "playwright.config.ts").read_text()
            self.assertIn("http://127.0.0.1:3100", cfg)
            self.assertIn("E2E_BASE_URL", cfg)
            readme = (stage / "README.md").read_text()
            self.assertIn("HOST-OWNED", readme)
            self.assertIn("/x/cli.js", readme)
            gi = (stage / ".gitignore").read_text()
            self.assertIn("node_modules", gi)
            self.assertIn("last-run.json", gi)


class ReportTests(unittest.TestCase):
    def _payload(self, outcomes):
        suites = {"suites": [
            {"specs": [
                {"title": f"spec{i}", "tests": [
                    {"title": t, "outcome": o, "results": [
                        {"status": "passed" if o == "expected" else "failed"}]}
                ]} for i, (t, o) in enumerate(outcomes)]}]}
        return json.dumps(suites)

    def test_parse_counts_and_reds(self):
        with tempfile.TemporaryDirectory() as td:
            lr = Path(td) / "last-run.jsonl"
            lr.write_text(
                '{"probe": "affordance", "string": "Rename workbook", "detail": "missing"}\n'
                '{"probe": "affordance", "string": "Rename workbook", "detail": "dup"}\n',
                encoding="utf-8")
            out = ui_smoke.parse_report(
                self._payload([("boot", "expected"),
                               ("contract crawler", "unexpected")]), lr)
        self.assertEqual(out["passed"], 1)
        self.assertEqual(out["failed"], 1)
        self.assertIn("contract crawler", out["titles"])
        self.assertEqual(len(out["reds"]), 1)  # jsonl 逐条去重
        self.assertEqual(out["reds"][0]["string"], "Rename workbook")

    def test_parse_garbage_stdout(self):
        out = ui_smoke.parse_report("not json", None)
        self.assertEqual((out["passed"], out["failed"]), (0, 0))
        self.assertEqual(out["reds"], [])


class CmdTests(unittest.TestCase):
    def test_command_shape_passes_kernel_guards(self):
        # 与内核 shell.rs 三道 guard 的形状契约:非内联脚本、不指向
        # .arc/acceptance、不在 trace 拦截清单。这里锁 Python 侧下发的
        # 命令文本;内核侧重叠回归见 crates 测试。
        cmd = ui_smoke.build_ui_smoke_cmd("/w/local-grader/node_modules/"
                                          "@playwright/test/cli.js")
        self.assertIn("--config .arc/ui-smoke/playwright.config.ts", cmd)
        self.assertNotIn("node -e", cmd)
        self.assertNotIn(".arc/acceptance", cmd)
        fallback = ui_smoke.build_ui_smoke_cmd(None)
        self.assertIn("npx --yes @playwright/test", fallback)


if __name__ == "__main__":
    unittest.main(verbosity=2)
