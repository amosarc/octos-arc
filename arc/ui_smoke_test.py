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

    def test_new_probe_markers_present(self):
        # 2026-09-27 四类新探针 + 红项截图钩子(sheet 二跑/round31-32 败因)。
        for marker in ("'strict-mode'", "red('grid-structure'",
                       "red('combobox'", "red('disabled-only'",
                       "red-shots", "page0.screenshot", "'combobox'"):
            self.assertIn(marker, self.spec, marker)

    def test_run2_probes_present(self):
        # 2026-09-27 github 6 分/sheet 3 分二跑败因的探针:auth-gate(登录态
        # 用户名精确可见+reload 持久)、grid-extent(固定尺寸网格)、
        # option 载体(原生 select)、静态文本不计分(anti tour-chips)。
        for marker in ("auth-gate - signed-in username",
                       "red('auth-gate'", "gridExtentProbe",
                       "'grid-extent'", "INTERACTIVE_SELECTOR",
                       "STRUCTURAL_SELECTOR", "trySignIn"):
            self.assertIn(marker, self.spec, marker)
        # 载体选择器不得再包含裸 span(div 也无)——静态铺字符串不算绿。
        self.assertNotIn("'button, a, select, input, span,", self.spec)

    def test_config_aligns_eval_timeouts(self):
        # 与隐藏评测配置同构:断言墙 10s(整测时限仍 90s,爬虫自设更长)。
        cfg = ui_smoke.generate_config(3100)
        self.assertIn("expect: { timeout: 10_000 }", cfg)
        self.assertIn("timeout: 90_000", cfg)

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
            self.assertIn("contract-smoke.spec.ts", readme)
            gi = (stage / ".gitignore").read_text()
            self.assertIn("node_modules", gi)
            self.assertIn("last-run.json", gi)
            # 一次性自定义检查正路模板(带 .example 后缀,Playwright 不收集)。
            example = (stage / "MY-CHECK.spec.ts.example").read_text()
            self.assertIn("MY-CHECK.spec.ts", example)
            self.assertIn("explicit file argument", example)


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
        # 显式过滤到权威契约套件:模型自建 MY-CHECK 不混进自检。
        self.assertIn("contract-smoke.spec.ts", cmd)
        self.assertNotIn("node -e", cmd)
        self.assertNotIn(".arc/acceptance", cmd)
        self.assertIn("MY-CHECK.spec.ts.example", cmd)  # 一次性检查正路
        fallback = ui_smoke.build_ui_smoke_cmd(None)
        self.assertIn("npx --yes @playwright/test", fallback)
        self.assertIn("contract-smoke.spec.ts", fallback)


class RedShotVisualTests(unittest.TestCase):
    """红项截图 → 视觉模型描述回填(通道 best-effort,未配置即跳过)。"""

    def test_visuals_backfilled_when_lane_configured(self):
        import visual_ref
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            shots = run_dir / "red-shots"
            shots.mkdir()
            (shots / "01-affordance-Save.png").write_bytes(b"png")
            (shots / "02-exact-name-Delete.png").write_bytes(b"png2")
            report = {"reds": [{"probe": "affordance", "string": "Save"}]}
            saved_lane = visual_ref.visual_lane
            saved_describe = visual_ref._describe_image
            try:
                visual_ref.visual_lane = lambda: ("m", "https://v", "k")
                visual_ref._describe_image = \
                    lambda model, base, key, path, **kw: f"desc of {path.name}"
                ui_smoke._describe_red_shots(run_dir, report, log=lambda *_: None)
            finally:
                visual_ref.visual_lane = saved_lane
                visual_ref._describe_image = saved_describe
        self.assertEqual(len(report["visuals"]), 2)
        self.assertIn("01-affordance-Save", report["visuals"][0]["text"])

    def test_noop_when_lane_unconfigured(self):
        import visual_ref
        with tempfile.TemporaryDirectory() as td:
            shots = Path(td) / "red-shots"
            shots.mkdir()
            (shots / "01-x.png").write_bytes(b"png")
            report = {}
            saved_lane = visual_ref.visual_lane
            try:
                visual_ref.visual_lane = lambda: None
                ui_smoke._describe_red_shots(Path(td), report,
                                             log=lambda *_: None)
            finally:
                visual_ref.visual_lane = saved_lane
        self.assertNotIn("visuals", report)


class MainPromptTests(unittest.TestCase):
    """main.py 侧与 Playwright 评测同构的装配断言。"""

    @classmethod
    def setUpClass(cls):
        import main as main_mod
        cls.main = main_mod

    def test_spec_run_cmd_carries_cold_start_prefix(self):
        cmd = self.main.build_spec_run_cmd("/x/cli.js", ["REQ-1.spec.ts"])
        self.assertIn("rm -f backend/data/db.run.json", cmd)
        self.assertIn("npm run build --prefix frontend", cmd)
        self.assertIn("restart-server.sh", cmd)
        self.assertIn("REQ-1.spec.ts", cmd)
        # build 失败必须挡住 spec run(链式 &&),restart 失败容忍(2>&1; )。
        self.assertIn("frontend && sh", cmd)
        self.assertIn("restart-server.sh >/dev/null 2>&1; ", cmd)

    def test_staged_config_aligns_eval_timeouts(self):
        # 2655 落盘的 config:timeout 10s + expect 10s(隐藏评测同构)。
        source = Path(self.main.__file__).read_text(encoding="utf-8")
        self.assertIn('"  timeout: 10_000, expect: { timeout: 10_000 },\\n"',
                      source)

    def test_ui_contract_prompt_has_new_rules(self):
        for marker in ("- NATIVE-FIRST WIDGETS:", "- DATA GRIDS:",
                       "- SUBMIT ENABLEMENT:", "native <dialog>",
                       "showModal()"):
            self.assertIn(marker, self.main.UI_CONTRACT_PROMPT)
        for marker in ("widgets are native-first", "native <select>",
                       "native <dialog>",
                       "custom widgets only where HTML has none",
                       "coordinate-named gridcells",
                       "submits enable once required fields are"):
            self.assertIn(marker, self.main.UI_CONTRACT_RECAP)

    def test_repair_prompt_covers_new_probes_and_visuals(self):
        p = self.main.UI_SMOKE_REPAIR_PROMPT
        for marker in ("strict-mode =", "grid-structure =", "combobox =",
                       "disabled-only =", "{visual}"):
            self.assertIn(marker, p)
        lines = self.main._ui_smoke_visual_lines(
            {"visuals": [{"label": "01-affordance-Save",
                          "text": "a toolbar with a disabled Save button"}]})
        self.assertIn("visual model descriptions", lines)
        self.assertIn("disabled Save button", lines)
        self.assertEqual(self.main._ui_smoke_visual_lines({}), "")


class GithubStyleTests(unittest.TestCase):
    """hackathon--github 首测暴露的缺口:弯引号/密码候选/错误消息族。"""

    def test_curly_quotes_extracted(self):
        tree = _node("R", 'button named \u201cCreate an account\u201d and '
                           'field \u201cUsername\u201d; value \u201c123456\u201d.')
        vals = [e["value"] for e in ui_smoke.collect_contract_strings(tree)]
        self.assertIn("Create an account", vals)
        self.assertIn("Username", vals)
        self.assertIn("123456", vals)  # 数字起始的逐字值也要保住

    def test_straight_and_curly_not_duplicated(self):
        tree = _node("R", 'a "Save" and a \u201cSave\u201d button')
        entries = [e["value"] for e in ui_smoke.collect_contract_strings(tree)]
        self.assertEqual(entries.count("Save"), 1)

    def test_invalid_family_is_message_class(self):
        tree = _node("R", 'shows "Username format is invalid" and '
                           '"Password requirements are not satisfied" and '
                           '"Account not found"')
        entries = ui_smoke.collect_contract_strings(tree)
        for e in entries:
            if e["value"] in ("Username format is invalid",
                              "Password requirements are not satisfied",
                              "Account not found"):
                self.assertTrue(e["message"], e["value"])

    def test_fixture_auth_prefers_requirement_passwords(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "backend" / "data").mkdir(parents=True)
            (out / "backend" / "data" / "db.json").write_text(json.dumps(
                {"collections": {"users": [
                    {"username": "alice-dev", "email": "a@x.test",
                     "password": "salt:hex"}]}}), encoding="utf-8")
            entries = [{"value": "Valid-password-123!", "kind": "exact",
                        "message": False, "nodes": []},
                       {"value": "not-a-pw", "kind": "exact",
                        "message": False, "nodes": []}]
            auth = ui_smoke.fixture_auth(out, entries)
            self.assertEqual(auth["username"], "alice-dev")
            # 需求密码形串排前,fixture 兜底;非密码形("not-a-pw",无数字)排除
            self.assertEqual(auth["passwords"][0], "Valid-password-123!")
            self.assertIn("Fixture-password-123!", auth["passwords"])
            self.assertNotIn("not-a-pw", auth["passwords"])

    def test_auth_from_compiled_and_login_users(self):
        # 2026-09-27 github 6 分根因:staging 早于 fixture 播种 → db.json 无
        # users → auth=None → 爬虫未登录扫门控应用全量误报。回退源 =
        # materialize 的编译结果(db.users),存在性判定 = db_login_users。
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "backend" / "data").mkdir(parents=True)
            compiled = {"db": {"users": [
                {"username": "seed-user", "email": "s@x.test"}]}}
            auth = ui_smoke.auth_from_compiled(compiled, [])
            self.assertEqual(auth["username"], "seed-user")
            self.assertEqual(auth["passwords"],
                             ["Fixture-password-123!"])
            self.assertIsNone(ui_smoke.auth_from_compiled(None, []))
            # db_login_users: db 缺失/无 users → 空表;有 → 行列表
            self.assertEqual(ui_smoke.db_login_users(out), [])
            (out / "backend" / "data" / "db.json").write_text(json.dumps(
                {"collections": {"users": [{"username": "u1"}]}}),
                encoding="utf-8")
            self.assertEqual(len(ui_smoke.db_login_users(out)), 1)
            # fixture_auth 不再依赖 import fixture_manifest(sys.path 无关)
            self.assertEqual(
                ui_smoke.fixture_auth(out, [], fixture_password="X")["passwords"],
                ["X"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
