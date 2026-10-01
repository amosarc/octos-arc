"""rejection_audit 单测(09-30 sheet 95×400 法医的静态保险网)。

09-30 深夜去注释改版:接地判定=文案归一化后逐字存在于需求原文
(membership IS the grounding);查无此文=编造嫌疑。覆盖:回放钉
(yaml 有的文案有据/查无的红)、info 分级、不可读文件跳过、提示词面
钉子(无注释措辞+neutral default 教义)。

Run: python3 -m unittest tests.rejection_audit_test
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rejection_audit as ra  # noqa: E402


def write_workspace(td: Path, routes_js: str) -> Path:
    (td / "backend" / "src" / "routes").mkdir(parents=True)
    (td / "backend" / "src" / "routes" / "workbooks.js").write_text(routes_js)
    return td


# 回放底座:同一文案被 create 与 rename 两条路由使用(09-30 sheet 真跑
# 形态)。membership 语义下该句在 yaml 里(rename 节点)→ 两条路由都
# 有据——跨操作泄漏的可见性让位给无注释 code style(模块 docstring 如
# 实记档);审计抓的是「需求原文查无此文」的纯编造。
CREATE_400_ROUTES = """\
const U = require('../lib/util');
module.exports = (app) => {
  app.post('/api/workbooks', (req, res) => {
    const name = String((req.body || {}).name || '').trim();
    if (!name) return U.err(res, 'Workbook name cannot be empty');
    return U.json(res, 201, { name });
  });
  app.patch('/api/workbooks/:id/name', (req, res) => {
    const name = String((req.body || {}).name || '').trim();
    if (!name) return U.err(res, 'Workbook name cannot be empty');
    return U.json(res, 200, { name });
  });
  app.get('/api/workbooks/:id', (req, res) => {
    if (!req.params.id) {
      return U.json(res, 404, { errors: [{ msg: 'Not found' }] });
    }
    return U.json(res, 200, {});
  });
};
"""

INVENTED_ROUTES = """\
const U = require('../lib/util');
module.exports = (app) => {
  app.post('/api/workbooks', (req, res) => {
    const name = String((req.body || {}).name || '').trim();
    if (!name) return U.err(res, 'Blank workbook names are forbidden');
    return U.json(res, 201, { name });
  });
  app.get('/api/workbooks/:id', (req, res) => {
    if (!req.params.id) {
      return U.json(res, 404, { errors: [{ msg: 'Not found' }] });
    }
    return U.json(res, 200, {});
  });
};
"""

MINI_YAML = """\
- id: ROOT
  name: sheet
  type: FOLDER
  children:
  - id: REQ-1-2-1
    name: Create a Blank Workbook
    type: ATOMIC
    description: |
      Users create a blank workbook from the home page. If creation
      fails, an error is displayed and no incomplete record appears.
  - id: REQ-1-2-2
    name: Rename a Workbook
    type: ATOMIC
    description: |
      After leading and trailing spaces are trimmed, the name must not
      be empty; an empty name must be rejected with "Workbook name
      cannot be empty".
"""


class MembershipGroundingTests(unittest.TestCase):
    def test_yaml_member_literal_is_grounded(self):
        # 09-30 回放:文案在 yaml(rename 节点)→ create/rename 两条
        # 路由都有据,零红账;'Not found'(2 词,查无)落 info。
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), CREATE_400_ROUTES)
            r = ra.audit(ws, MINI_YAML)
        self.assertEqual(r["red_count"], 0)
        self.assertEqual([f["kind"] for f in r["findings"]], ["info"])

    def test_invented_message_is_red(self):
        # 纯编造(需求原文查无,≥4 词)→ 红:静态保险网的靶心。
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), INVENTED_ROUTES)
            r = ra.audit(ws, MINI_YAML)
        by_line = {f["line"]: f for f in r["findings"]}
        self.assertEqual(by_line[5]["kind"], "red")
        self.assertEqual(by_line[5]["literal"],
                         "Blank workbook names are forbidden")
        self.assertEqual(by_line[10]["kind"], "info")  # 'Not found'
        self.assertEqual(r["red_count"], 1)

    def test_quoted_message_used_verbatim_is_grounded(self):
        # 总纲新措辞:引用需求的原文文案 → membership 豁免。
        grounded = INVENTED_ROUTES.replace(
            "Blank workbook names are forbidden",
            'Workbook name cannot be empty')
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), grounded)
            r = ra.audit(ws, MINI_YAML)
        self.assertEqual(r["red_count"], 0)

    def test_unreadable_files_are_skipped_never_raise(self):
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), INVENTED_ROUTES)
            (ws / "backend" / "src" / "routes" / "broken.js").write_bytes(
                b"\xff\xfe\x00bad")
            r = ra.audit(ws, MINI_YAML)  # must not raise
        # 不可解码文件跳过不计数;好文件照常审计
        self.assertEqual(r["scanned_files"], 1)
        self.assertEqual(r["red_count"], 1)


class PromptBlockTests(unittest.TestCase):
    def test_empty_when_no_reds(self):
        self.assertEqual(ra.audit_prompt_block(
            {"findings": [{"kind": "info", "file": "a.js", "line": 1,
                           "literal": "Not found"}]}), "")

    def test_red_block_is_comment_free(self):
        block = ra.audit_prompt_block({"findings": [
            {"kind": "red", "file": "backend/src/routes/w.js", "line": 63,
             "literal": "Some invented validation message", "words": 4}]})
        self.assertIn("REJECTION AUDIT", block)
        self.assertIn("w.js:63", block)
        # 无注释载体:不再指导模型写引用注释
        self.assertNotIn("// REQ-", block)
        self.assertNotIn("comment", block)
        self.assertIn("appear nowhere in the requirement text", block)
        self.assertIn("must SUCCEED with a neutral default", block)


class DisciplinePinsTest(unittest.TestCase):
    """两形式钉:总纲法则去注释化(用户拍板 code style 无注释)。"""

    def test_charter_carries_text_grounded_rejection(self):
        import plan_envelope
        row = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("only where the text states it", row)
        self.assertIn("must SUCCEED with a neutral default", row)
        # 注释机制整体退役:总纲不再提注释/引用注释/标记形态
        self.assertNotIn("// REQ-", row)
        self.assertNotIn("comment", row)
        self.assertNotIn("message unspecified", row)
        # 权威句:需求原文赢过 app/spec/seed
        self.assertIn("The requirement text wins over the app, over your "
                      "specs", row)
        # 失败类硬编码禁令不回归:新条款不得含历史 bug 名词
        self.assertNotIn("CLIENT ARITY", row)
        self.assertNotIn("[object", row)

    def test_gateway_code_style_has_no_citation_carveout(self):
        import main
        gw = main._GATEWAY_PROMPT_OVERRIDE
        self.assertIn("no comments or docstrings", gw)
        self.assertNotIn("REQ-citation", gw)
        self.assertNotIn("the only exception", gw)

    def test_turn_completion_carries_selftest_requirement(self):
        import main
        c = main.GOAL_FIRST_TURN_COMPLETION.format(smoke=3100)
        self.assertIn("generate Playwright automation tests", c)
        self.assertIn("fix the app", c)
        self.assertIn("port 3100", c)
        # 10-01 用户拍板:自测必须真跑——禁 mock(0 分运行实证:curl 绿
        # ≠ walking flow);完成句空行分块,不与上文粘连。
        self.assertIn("never mock the app, its API, or the test run", c)
        self.assertTrue(c.startswith("\n\n"), "完成句须空行分隔")
        # 09-30 真跑三次 premature wrap-up(366s/1006s/1360s 零进展收工)
        # 后的完成定义锚(用户拍板"加"):宣布未来工作≠完成回合。
        self.assertIn("never end mid-investigation", c)
        self.assertIn("announces future work is an incomplete turn", c)

    def test_system_row_demotes_staged_specs(self):
        import plan_envelope
        row = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("The requirement text wins over the app, over your "
                      "specs", row)
        self.assertNotIn("remain ground truth", row)

    def test_acceptance_staged_note_retired(self):
        import main
        self.assertFalse(hasattr(main, "ACCEPTANCE_STAGED_NOTE"))


if __name__ == "__main__":
    unittest.main()
