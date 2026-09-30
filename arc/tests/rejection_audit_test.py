"""rejection_audit 单测(09-30 sheet 95×400 法医的静态保险网)。

覆盖:发明拒绝规则命中(create 路径空名 400 回放)、单行全句引用
豁免、残缺引用不豁免、跨操作泄漏的归属提示、info 分级、yaml 归属
节点反查、提示词面钉子(教义统一 + CITE-BEFORE-REJECT + 走查第一例)。

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


# 最小回放:同一个文件里 create 与 rename 两条路由共用同一句 400 文案;
# yaml 里该句只出现在 REQ-1-2-2(rename)的 description——正是 09-30
# sheet 真跑的形态(create 发明拒绝,句子出处是别的操作)。
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
  - id: REQ-2-3-1
    name: Create a Team
    type: ATOMIC
    description: |
      Duplicate team names display an error and the team is not
      created.
"""

# message-unspecified 形态的回放:需求要求拒绝但没引号给文案——
# 文案是模型自选的合理消息,引用标注尾标后豁免。
TEAM_ROUTE = """\
const U = require('../lib/util');
module.exports = (app) => {
  app.post('/api/teams', (req, res) => {
    const name = String((req.body || {}).name || '').trim();
    if (taken.has(name)) return U.err(res, 'Team name already exists');
    return U.json(res, 201, { name });
  });
};
"""


class Create400ReplayTests(unittest.TestCase):
    def test_uncited_create_rejection_is_red_with_yaml_owner(self):
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), CREATE_400_ROUTES)
            r = ra.audit(ws, MINI_YAML)
        by_line = {f["line"]: f for f in r["findings"]}
        # create 路径(:5)命中,归属节点指向 rename 的 REQ-1-2-2——
        # 正是「文案在 yaml 里,但属于别的操作」的泄漏形态。
        self.assertEqual(by_line[5]["kind"], "red")
        self.assertEqual(by_line[5]["yaml_nodes"], ["REQ-1-2-2"])
        # rename 路径(:10)同文案未引用 → 同样红(补引用即豁免)
        self.assertEqual(by_line[10]["kind"], "red")
        # 'Not found'(:15,2 词)落 info,不进红账
        self.assertEqual(by_line[15]["kind"], "info")
        self.assertEqual(r["red_count"], 2)

    def test_single_line_full_sentence_citation_exempts(self):
        cited = CREATE_400_ROUTES.replace(
            "  app.patch('/api/workbooks/:id/name', (req, res) => {",
            "  // REQ-1-2-2: 'an empty name must be rejected with "
            "\"Workbook name cannot be empty\".'\n"
            "  app.patch('/api/workbooks/:id/name', (req, res) => {")
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), cited)
            r = ra.audit(ws, MINI_YAML)
        # rename(:11,引用在检查点上方 3 行)豁免;create(:5,引用在其
        # 下方)不豁免——引用绑定检查点,不文件级生效
        self.assertEqual([f["line"] for f in r["findings"]
                          if f["kind"] == "red"], [5])

    def test_truncated_citation_does_not_exempt(self):
        cited = CREATE_400_ROUTES.replace(
            "  app.patch('/api/workbooks/:id/name', (req, res) => {",
            "  // REQ-1-2-2: \"an empty name must be rejected with \"\n"
            "  app.patch('/api/workbooks/:id/name', (req, res) => {")
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), cited)
            r = ra.audit(ws, MINI_YAML)
        # 残缺引用句不含文案本身(lit⊂s 不成立)→ rename(:11)不豁免
        self.assertEqual([f["line"] for f in r["findings"]
                          if f["kind"] == "red"], [5, 11])

    def test_unreadable_files_are_skipped_never_raise(self):
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), CREATE_400_ROUTES)
            (ws / "backend" / "src" / "routes" / "broken.js").write_bytes(
                b"\xff\xfe\x00bad")
            r = ra.audit(ws, MINI_YAML)  # must not raise
        # 不可解码文件跳过不计数;好文件照常审计
        self.assertEqual(r["scanned_files"], 1)
        self.assertEqual(r["red_count"], 2)


class MessageUnspecifiedTests(unittest.TestCase):
    """第二引用形态:需求要求拒绝但没引号给文案。"""

    def cite_team_route(self, yaml_text: str) -> dict:
        cited = TEAM_ROUTE.replace(
            "    if (taken.has(name)) return U.err(res, "
            "'Team name already exists');",
            "    // REQ-2-3-1: \"Duplicate team names display an error "
            "and the team is not created.\" (message unspecified in "
            "text)\n"
            "    if (taken.has(name)) return U.err(res, "
            "'Team name already exists');")
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), cited)
            return ra.audit(ws, yaml_text)

    def test_marker_citation_exempts_selfchosen_message(self):
        r = self.cite_team_route(MINI_YAML)
        # 文案不在被引句里,但标注形态 + 被引句真实存在 → 豁免
        self.assertEqual(r["red_count"], 0)

    def test_marker_citation_with_foreign_sentence_still_red(self):
        r = self.cite_team_route(
            MINI_YAML.replace(
                "      Duplicate team names display an error and the team is not\n"
                "      created.\n", ""))
        # 被引句不在 requirements.yaml → 标注救不了
        self.assertEqual([f["line"] for f in r["findings"]
                          if f["kind"] == "red"], [6])

    def test_uncited_team_message_is_red(self):
        with TemporaryDirectory() as td:
            ws = write_workspace(Path(td), TEAM_ROUTE)
            r = ra.audit(ws, MINI_YAML)
        self.assertEqual([f["line"] for f in r["findings"]
                          if f["kind"] == "red"], [5])


class YamlNodeLookupTests(unittest.TestCase):
    def test_owner_is_nearest_preceding_id(self):
        nodes = ra._yaml_nodes(MINI_YAML, "no incomplete record appears")
        self.assertEqual(nodes, ["REQ-1-2-1"])

    def test_quoted_sentence_owner_is_rename_leaf(self):
        nodes = ra._yaml_nodes(MINI_YAML, "Workbook name cannot be empty")
        self.assertEqual(nodes, ["REQ-1-2-2"])


class PromptBlockTests(unittest.TestCase):
    def test_empty_when_no_reds(self):
        self.assertEqual(ra.audit_prompt_block(
            {"findings": [{"kind": "info", "file": "a.js", "line": 1,
                           "literal": "Not found"}]}), "")

    def test_red_lines_carry_node_hints(self):
        block = ra.audit_prompt_block({"findings": [
            {"kind": "red", "file": "backend/src/routes/w.js", "line": 63,
             "literal": "Workbook name cannot be empty",
             "yaml_nodes": ["REQ-1-2-2"]}]})
        self.assertIn("REJECTION AUDIT", block)
        self.assertIn("w.js:63", block)
        self.assertIn("REQ-1-2-2", block)
        self.assertIn("must not reject", block)


class DisciplinePinsTest(unittest.TestCase):
    """提示词面钉子:教义统一 + CITE-BEFORE-REJECT + 走查第一例。"""

    def test_work_order_carries_cite_before_reject(self):
        import main
        tpl = main.GOAL_FIRST_NODE_PROMPT_TEMPLATE
        self.assertIn("CITE BEFORE YOU REJECT", tpl)
        self.assertIn("// REQ-<id>: \"<sentence>\"", tpl)
        self.assertIn("must SUCCEED", tpl)
        # message-unspecified 第二引用形态
        self.assertIn("(message unspecified in text)", tpl)
        # 反发明触发器条款
        self.assertIn("is not a trigger", tpl)
        # 收尾自曝条款
        self.assertIn("with its REQ citation", tpl)
        # description 逐字走查 = spec 第一例 + arrange 忠实(GIVEN 演员
        # 与种子数据不得丢)
        self.assertIn("FIRST spec case", tpl)
        self.assertIn("arrange exactly what its GIVEN states", tpl)
        self.assertIn("sign in as the actor the GIVEN names", tpl)
        self.assertIn("stays untouched", tpl)
        # 失败类硬编码禁令(goal_first_test 既有钉子)不回归:
        # 新条款不得含历史 bug 名词
        self.assertNotIn("CLIENT ARITY", tpl)
        self.assertNotIn("[object", tpl)

    def test_system_row_demotes_staged_specs(self):
        import plan_envelope
        row = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("staged specs are a regression suite, not authority",
                      row)
        self.assertIn("the text wins", row)
        self.assertNotIn("remain ground truth", row)

    def test_acceptance_staged_note_retired(self):
        import main
        self.assertFalse(hasattr(main, "ACCEPTANCE_STAGED_NOTE"))


if __name__ == "__main__":
    unittest.main()
