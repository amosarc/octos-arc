"""gherkin 渲染/清洗单测(09-30 两形式之 TURN 核心件)。

Run: python3 -m unittest tests.gherkin_test
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gherkin  # noqa: E402


def _node(scenarios, name="n", description=""):
    return {"id": "REQ-9-9-9", "name": name, "type": "ATOMIC",
            "description": description, "children": [],
            "scenarios": scenarios}


class CleaningRuleTests(unittest.TestCase):
    def test_sentence_split_and_and_chain(self):
        sents, ok = gherkin.clean_sentences(
            "The user clicks the save button. The row appears. "
            "Values persist.")
        self.assertTrue(ok)
        self.assertEqual(len(sents), 3)

    def test_seed_values_sentence_dropped(self):
        sents, ok = gherkin.clean_sentences(
            "A seeded org exists. Seed values: account `alice-dev`, "
            "password `Valid-password-123!`.")
        self.assertTrue(ok)
        self.assertEqual([s for s in sents if "alice" in s.lower()], [])
        self.assertNotIn("Seed values", " ".join(sents))

    def test_placeholder_phrase_kept_verbatim(self):
        # 10-02 拍板:"the requested workflow" 是"所请求的工作流"——
        # LLM 须按顺序走完的测试本体,语义成分,逐字保留不剥。
        sents, ok = gherkin.clean_sentences(
            "The user opens the home page, and the requested workflow,"
            "the requested workflow page entry the requested workflow "
            "with concrete values `East`, `1200`.")
        self.assertTrue(ok)
        joined = " ".join(sents)
        self.assertIn("the requested workflow", joined.lower())
        self.assertIn("concrete values", joined)

    def test_pure_phrase_step_round_trips(self):
        # 纯占位短语、无句界 → 折叠后原样保留(不再判"不可清洗")。
        sents, ok = gherkin.clean_sentences(
            "the requested workflow,the requested workflow")
        self.assertTrue(ok)
        self.assertEqual(sents, [
            "the requested workflow,the requested workflow"])

    def test_name_kept_verbatim_folded(self):
        self.assertEqual(
            gherkin.clean_name(
                "REQ-1-1-1 -the requested workflow,the requested workflow"),
            "REQ-1-1-1 -the requested workflow,the requested workflow")


class RenderTests(unittest.TestCase):
    def _node(self):
        steps = [
            {"keyword": "GIVEN", "content":
                "A fresh unauthenticated browser session. "
                "The evaluation seed contains the seeded workbook "
                "`Q3 Sales`."},
            {"keyword": "WHEN", "content":
                "The user opens the home page, and the requested workflow,"
                "the requested workflow with concrete values `East`, "
                "`1200`."},
            {"keyword": "THEN", "content":
                "The list shows only matching entries."}]
        sc = [{"name": "REQ-1-1-1 -the requested workflow",
               "steps": steps}]
        return _node(sc, description="d")

    def test_full_mode_renders_all_steps_verbatim(self):
        text = gherkin.render_scenarios(self._node(), "full")
        self.assertIn("Scenario: REQ-1-1-1", text)
        self.assertIn("Given A fresh unauthenticated browser session.",
                      text)
        self.assertIn("And The evaluation seed contains", text)
        self.assertIn("When The user opens the home page", text)
        self.assertIn("Then The list shows only matching entries.", text)
        # 10-02:工作流指代短语是语义成分,逐字进场。
        self.assertIn("the requested workflow", text.lower())
        self.assertNotIn("Seed values:", text)

    def test_templated_flags_no_longer_withhold_steps(self):
        # 10-02 全量恢复:占位检测退役,遗留标记不得再改变渲染。
        n = self._node()
        n["_scenarios_templated"] = True
        n["scenarios"][0]["_steps_templated"] = True
        self.assertEqual(gherkin.render_scenarios(n, "full"),
                         gherkin.render_scenarios(self._node(), "full"))

    def test_data_only_mode_renders_given_only(self):
        text = gherkin.render_scenarios(self._node(), "data-only")
        self.assertIn("Given A fresh unauthenticated browser session.",
                      text)
        self.assertNotIn("When The user opens", text)

    def test_deterministic(self):
        n = self._node()
        self.assertEqual(gherkin.render_scenarios(n, "full"),
                         gherkin.render_scenarios(n, "full"))


class FeatureAssemblyTests(unittest.TestCase):
    def test_feature_groups_by_parent_folder(self):
        tree = {"id": "ROOT", "name": "Root", "type": "FOLDER",
                "description": "Global rules.",
                "children": [
                    {"id": "REQ-1-1", "name": "Workbook Navigation",
                     "type": "FOLDER",
                     "description": "View and open workbooks from the "
                                    "home page. After create or rename, "
                                    "home page list updates.",
                     "children": [
                         {"id": "REQ-1-1-1", "name": "Open a Workbook",
                          "type": "ATOMIC",
                          "description": "d",
                          "scenarios": [{"name": "s1", "steps": [
                              {"keyword": "GIVEN",
                               "content": "A seeded workbook."}]}]}]}]}
        feats = gherkin.feature_files(tree)
        self.assertEqual(len(feats), 1)
        self.assertEqual(feats[0]["id"], "REQ-1-1")
        self.assertIn("Feature: Workbook Navigation", feats[0]["text"])
        self.assertIn("View and open workbooks from the home page.",
                      feats[0]["text"])
        self.assertIn("Background:", feats[0]["text"])
        self.assertIn("After create or rename", feats[0]["text"])
        self.assertIn("Scenario: Open a Workbook", feats[0]["text"])
        # 节点 id 绑定:每个 Scenario 上方一行 "# <id>" 注释——id↔场景
        # 块显式对应,场景正文交叉引用("compliant with REQ-1-1-1")可
        # 一步定位;绑定不得只依赖 TASK 行标题对号(占位场景标题同形)。
        text = feats[0]["text"]
        self.assertLess(text.index("\n  # REQ-1-1-1\n"),
                        text.index("  Scenario: Open a Workbook"))


class TurnFullRestoreTests(unittest.TestCase):
    """TURN 渲染(feature_text)10-02 全量恢复:每原子节点 = id 锚 +
    Flow contract(description,定义步骤里 "the requested workflow" 所指
    的工作流)+ 全部场景全部 GIVEN/WHEN/THEN 逐字;占位检测/短语剥离/
    sc0-only 全部退役。"""

    def _folder_and_atom(self):
        steps_a = [
            {"keyword": "GIVEN", "content": "A seeded workbook `Q3`."},
            {"keyword": "WHEN", "content":
                "the requested workflow with concrete values `East`."},
            {"keyword": "THEN", "content": "The workbook opens."}]
        steps_b = [
            {"keyword": "GIVEN", "content": "A seeded workbook `Q3`."},
            {"keyword": "WHEN", "content":
                "the requested workflow SheetN the requested workflow."},
            {"keyword": "THEN", "content": "The first unused tab appears."}]
        atom = {"id": "REQ-1-1-1", "name": "Open a Workbook",
                "type": "ATOMIC",
                "description": "Users open the seeded workbook from the "
                               "home page via its named entry link.",
                "scenarios": [
                    {"name": "s1 -the requested workflow",
                     "steps": steps_a},
                    {"name": "s2 -the requested workflow SheetN",
                     "steps": steps_b}]}
        folder = {"id": "REQ-1-1", "name": "Navigation", "type": "FOLDER",
                  "description": "Open workbooks from the home page.",
                  "children": [atom]}
        return folder, atom

    def test_flow_contract_and_all_steps_of_all_scenarios(self):
        folder, atom = self._folder_and_atom()
        text = gherkin.feature_text(folder, [atom])
        self.assertIn(
            "# Flow contract: Users open the seeded workbook from the "
            "home page via its named entry link.", text)
        # 每节点恰一行 Flow contract(10-02 三次拍板:场景间不继承重复)。
        self.assertEqual(text.count("# Flow contract:"), 1)
        # 首场景标题沿用节点名(与既有 turn 形态字节兼容)。
        self.assertIn("Scenario: Open a Workbook", text)
        # 场景 2..N 逐字进场(不再 sc0-only),标题用场景自身名。
        self.assertIn("Scenario: s2 -the requested workflow SheetN", text)
        # WHEN/THEN 逐字,工作流指代短语原样保留。
        self.assertIn("When the requested workflow with concrete "
                      "values `East`.", text)
        self.assertIn("Then The workbook opens.", text)
        self.assertIn("Then The first unused tab appears.", text)
        # id 锚每节点恰一行。
        self.assertEqual(text.count("# REQ-1-1-1"), 1)

    def test_legacy_flags_inert(self):
        # 退役标记不得改变任何渲染字节。
        folder, atom = self._folder_and_atom()
        atom["_scenarios_templated"] = True
        atom["scenarios"][0]["_steps_templated"] = True
        plain_folder, plain_atom = self._folder_and_atom()
        self.assertEqual(gherkin.feature_text(folder, [atom]),
                         gherkin.feature_text(plain_folder, [plain_atom]))

    def test_absent_fields_render_no_dangling_labels(self):
        # 10-02 拍板"有就展示无就不展示":无 id 不发悬空 "# " 锚,
        # 无 description 不发 Flow contract 行,三者皆无的原子整体省略
        # ——与 SCENARIOS 空省略同律,不留任何空标签。
        folder = {"id": "REQ-1", "name": "F", "type": "FOLDER",
                  "description": "", "children": []}
        no_id_no_desc = {"id": "", "name": "No Id", "type": "ATOMIC",
                         "description": "", "scenarios": [
                             {"name": "s1", "steps": [
                                 {"keyword": "GIVEN",
                                  "content": "seeded."}]}]}
        text = gherkin.feature_text(folder, [no_id_no_desc])
        self.assertNotIn("#", text)
        self.assertIn("Scenario: No Id", text)
        empty = {"id": "", "name": "", "type": "ATOMIC",
                 "description": "", "scenarios": []}
        self.assertEqual(gherkin.feature_text(folder, [empty]),
                         "Feature: F\n")
        # 仅 id 的原子只渲染锚一行(有就展示)。
        id_only = {"id": "REQ-9", "name": "", "type": "ATOMIC",
                   "description": "", "scenarios": []}
        self.assertEqual(gherkin.feature_text(folder, [id_only]),
                         "Feature: F\n\n  # REQ-9\n")
        # 真数据(id/description 恒在场)渲染字节不受影响。
        full_folder, full_atom = self._folder_and_atom()
        full_text = gherkin.feature_text(full_folder, [full_atom])
        self.assertEqual(full_text.count("# REQ-1-1-1"), 1)
        self.assertEqual(full_text.count("# Flow contract:"), 1)

    def test_byte_identical_scenario_clones_collapse(self):
        # 10-02 拍板"同文即残渣":题目 yaml 的 "- Scenario N" 模板
        # 克隆(步骤序列逐字节相同)只渲染首见,零新信息不重复进场。
        # 真实形态=1 真场景 + N 个互为克隆的模板变体。
        real = [{"keyword": "GIVEN", "content": "A seeded workbook `Q3`."},
                {"keyword": "WHEN", "content":
                    "the visitor enters the username and password."},
                {"keyword": "THEN", "content": "The workspace opens."}]
        clone = [{"keyword": "GIVEN", "content": "A seeded workbook `Q3`."},
                 {"keyword": "WHEN", "content":
                     "the requested workflow with visible controls."},
                 {"keyword": "THEN", "content": "The page displays it."}]
        atom = {"id": "REQ-1-1-2", "name": "Sign In",
                "type": "ATOMIC", "description": "Sign in flow.",
                "scenarios": [
                    {"name": "s1 - Scenario 1", "steps": real},
                    {"name": "s1 - Scenario 2", "steps": list(clone)},
                    {"name": "s1 - Scenario 3", "steps": list(clone)},
                    {"name": "s1 - Scenario 4", "steps": list(clone)}]}
        folder = {"id": "REQ-1-1", "name": "Access", "type": "FOLDER",
                  "description": "d.", "children": [atom]}
        text = gherkin.feature_text(folder, [atom])
        self.assertEqual(text.count("  Scenario: "), 2)
        self.assertNotIn("- Scenario 3", text)
        self.assertNotIn("- Scenario 4", text)
        # 变体与真场景步骤不同构时不受去重影响。
        self.assertIn("- Scenario 2", text)

    def test_repeated_sentence_within_scenario_renders_once(self):
        # 题目模板把标准 GIVEN 串进变体步骤时一式两份(REQ-1-1-2 克隆
        # 实测:"The visitor starts …" 句首尾各一次)——同场景内逐字节
        # 相同的句子只渲染首见。
        atom = {"id": "REQ-9", "name": "n", "type": "ATOMIC",
                "description": "",
                "scenarios": [{"name": "s", "steps": [
                    {"keyword": "GIVEN", "content":
                        "The visitor starts at the home page. "
                        "The seeded data is account `alice-dev`. "
                        "The visitor starts at the home page."}]}]}
        text = gherkin.render_scenarios(atom, "full")
        self.assertEqual(
            text.count("The visitor starts at the home page."), 1)
        self.assertIn("The seeded data is account `alice-dev`.", text)

    def test_cross_scenario_sentence_repetition_kept(self):
        # 去重只在同一场景内:不同场景共享前置句是 Gherkin 语义
        # (每场景自含),步骤序列不同构的场景互不吞并。
        atom = {"id": "REQ-9", "name": "n", "type": "ATOMIC",
                "description": "",
                "scenarios": [
                    {"name": "s1", "steps": [
                        {"keyword": "GIVEN",
                         "content": "A seeded workbook."},
                        {"keyword": "WHEN",
                         "content": "the user opens it."}]},
                    {"name": "s2", "steps": [
                        {"keyword": "GIVEN",
                         "content": "A seeded workbook."},
                        {"keyword": "WHEN",
                         "content": "the user closes it."}]}]}
        text = gherkin.render_scenarios(atom, "full")
        self.assertEqual(text.count("Given A seeded workbook."), 2)

    def test_skeleton_fill_variant_dropped(self):
        # 骨架填充变体(10-02 同文即残渣②):github 生成器的通用
        # "走过场"场景——WHEN/THEN 双骨架短语夹真 GIVEN 前置——整体
        # 剔除;真场景与单短语场景不受影响(双短语门槛防误伤)。
        real = [{"keyword": "GIVEN", "content":
                 "The visitor starts at the home page. The seeded data "
                 "is account `alice-dev`."},
                {"keyword": "WHEN", "content":
                 "The visitor enters a compliant username and clicks "
                 "“Create account”."},
                {"keyword": "THEN", "content":
                 "The system displays registration success."}]
        skeleton = [{"keyword": "GIVEN", "content":
                     "The visitor starts at the home page. The seeded "
                     "data is account `alice-dev`."},
                    {"keyword": "WHEN", "content":
                     "The user signs in as `alice-dev` and follows the "
                     "visible controls for the register workflow."},
                    {"keyword": "THEN", "content":
                     "The page displays the required headings, "
                     "controls, values, and status for the register "
                     "workflow."}]
        half = [{"keyword": "WHEN", "content":
                 "the user follows the visible controls for the "
                 "register workflow."}]
        atom = {"id": "REQ-1-1-1", "name": "Register", "type": "ATOMIC",
                "description": "Registration flow.",
                "scenarios": [
                    {"name": "s - Scenario 1", "steps": real},
                    {"name": "s - Scenario 2", "steps": skeleton},
                    {"name": "s - Scenario 3", "steps": half}]}
        folder = {"id": "REQ-1-1", "name": "Access", "type": "FOLDER",
                  "description": "d.", "children": [atom]}
        text = gherkin.feature_text(folder, [atom])
        self.assertEqual(text.count("  Scenario: "), 2)
        self.assertNotIn("- Scenario 2", text)
        self.assertIn("- Scenario 3", text)


class RealTaskSmokeTests(unittest.TestCase):
    def _tree(self, task):
        import main
        req_dir = (Path(__file__).resolve().parent.parent / "tasks" / task)
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest(f"{task} not present")
        return main.load_requirement_tree(req_dir)

    def test_sheet_req_1_1_1_renders_full_steps(self):
        tree = self._tree("hackathon--sheet")
        n = next(x for x in
                 (gherkin.__dict__ and __import__("main").flatten_atomic(tree))
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.render_scenarios(n, "full")
        self.assertIn("Given", text)
        self.assertIn("When ", text)
        self.assertIn("Then ", text)
        # 工作流指代短语逐字保留(10-02 拍板)。
        self.assertIn("the requested workflow", text.lower())
        self.assertIn("`Q3 Sales`", text)

    def test_github_real_steps_kept_verbatim(self):
        import main
        tree = self._tree("hackathon--github")
        n = next(x for x in main.flatten_atomic(tree)
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.render_scenarios(n, "full")
        self.assertIn("Valid-password-123!", text)
        self.assertIn("Create an account", text)

    def _parent_of(self, tree, node):
        stack = [tree]
        while stack:
            cur = stack.pop()
            for c in cur.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if c is node:
                    return cur
                stack.append(c)
        raise AssertionError("parent folder not found")

    def test_sheet_turn_path_full_restore(self):
        # TURN 真路径(feature_text):Flow contract + 全场景全步骤逐字
        # (10-02 全量恢复),工作流指代短语原样在场。
        import main
        tree = self._tree("hackathon--sheet")
        n = next(x for x in main.flatten_atomic(tree)
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.feature_text(self._parent_of(tree, n), [n])
        self.assertIn("# Flow contract:", text)
        # 每节点恰一行:REQ-1-1-1 两个变体场景共用同一条契约
        # (不逐场景继承重复)。
        self.assertEqual(text.count("# Flow contract:"), 1)
        self.assertIn("`Q3 Sales`", text)
        self.assertIn("When ", text)
        self.assertIn("Then ", text)
        self.assertIn("the requested workflow", text.lower())
        # 全场景:REQ-1-1-1 两个变体都在场。
        self.assertEqual(text.count("  Scenario: "), 2)

    def test_github_turn_path_keeps_real_steps(self):
        import main
        tree = self._tree("hackathon--github")
        n = next(x for x in main.flatten_atomic(tree)
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.feature_text(self._parent_of(tree, n), [n])
        self.assertIn("Create an account", text)
        self.assertNotIn("placeholder boilerplate", text)

    def test_real_yaml_skeleton_variants_and_clones_dropped(self):
        # 真题钉子(10-02 同文即残渣):github 生成器骨架变体整体剔除
        # ——REQ-1-1-1 yaml 3 场景(1 真+2 骨架)/REQ-1-1-2 4 场景
        # (1 真+3 骨架,同文克隆并入)→ 各渲染 1,注册节点的 "signs
        # in" 变体不再误导。
        import main
        tree = self._tree("hackathon--github")
        n = next(x for x in main.flatten_atomic(tree)
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.feature_text(self._parent_of(tree, n), [n])
        self.assertEqual(len(n.get("scenarios") or []), 3)
        self.assertEqual(text.count("  Scenario: "), 1)
        self.assertNotIn("- Scenario 2", text)
        n2 = next(x for x in main.flatten_atomic(tree)
                  if x.get("id") == "REQ-1-1-2")
        self.assertEqual(len(n2.get("scenarios") or []), 4)
        self.assertEqual(gherkin.feature_text(
            self._parent_of(tree, n2), [n2]).count("  Scenario: "), 1)
        # 零误伤:REQ-2-2-2 的 "- Scenario 2" 是真变体(循环团队拒绝)
        # → 2 场景全渲染。
        n3 = next(x for x in main.flatten_atomic(tree)
                  if x.get("id") == "REQ-2-2-2")
        self.assertEqual(gherkin.feature_text(
            self._parent_of(tree, n3), [n3]).count("  Scenario: "), 2)
        # sheet 的 "the requested workflow" 场景不含骨架短语,全量恢复
        # 拍板原样有效;其同文克隆仍由克隆律收敛(REQ-1-3-1 4→3)。
        stree = self._tree("hackathon--sheet")
        sn = next(x for x in main.flatten_atomic(stree)
                  if x.get("id") == "REQ-1-1-1")
        self.assertEqual(len(gherkin._unique_scenarios(sn)), 2)
        sn2 = next(x for x in main.flatten_atomic(stree)
                   if x.get("id") == "REQ-1-3-1")
        self.assertEqual(len(sn2.get("scenarios") or []), 4)
        self.assertEqual(len(gherkin._unique_scenarios(sn2)), 3)


class QuotedNamesTests(unittest.TestCase):
    """依赖块 REUSED UI CONTRACT 的提取器(gherkin.quoted_names):
    “…”/"…" 引用名保序去重;反引号(种子值)与单引号(撇号陷阱)不取。"""

    def test_curly_and_straight_quotes_order_preserved(self):
        text = ("Fields “Username or email” and \"Password\"; "
                "clicks “Sign in”; “Sign in” again.")
        self.assertEqual(gherkin.quoted_names(text),
                         ["Username or email", "Password", "Sign in"])

    def test_backticks_and_single_quotes_excluded(self):
        text = ("the seeded account `alice-dev` signs in; "
                "the user's own 'Find branch' control")
        self.assertEqual(gherkin.quoted_names(text), [])

    def test_limit_and_whitespace_folding(self):
        text = ("“A   B” \"C\" “D” “E” “F” “G” “H” “I” “J” “K” “L” “M” "
                "“N”")
        names = gherkin.quoted_names(text)
        self.assertEqual(names[0], "A B")
        self.assertEqual(len(names), 12)

    def test_empty_input(self):
        self.assertEqual(gherkin.quoted_names(""), [])
        self.assertEqual(gherkin.quoted_names(None), [])


class SeedSentencesTests(unittest.TestCase):
    """SEED CONTRACT 块的数据源(gherkin.seed_sentences):含 "seed"
    的整句逐字提取,词汇无关(逐实体正则不具泛化能力——09-30 用户拍板);
    "Seed values:" 字面重复句照既有清洗律丢弃。块本身暂停装配待确认
    是否有用(main._TURN_SEED_CONTRACT),函数与测试保留。"""

    def test_seed_declaration_sentence_extracted_verbatim(self):
        n = {"id": "REQ-1", "name": "n", "description": "", "scenarios": [
            {"name": "s", "steps": [
                {"keyword": "GIVEN", "content":
                 "A fresh session. The seeded data is pull requests "
                 "`Improve onboarding`, check `test`."}]}]}
        sents, dropped = gherkin.seed_sentences([n])
        self.assertEqual(dropped, 0)
        self.assertEqual(sents, [
            "The seeded data is pull requests `Improve onboarding`, "
            "check `test`."])

    def test_sheet_sentence_and_description_scan(self):
        # sheet 句式 + 描述里的种子句都进场(词汇无关的关键证据)。
        n = {"id": "REQ-2", "name": "n", "description":
             "Seed data comprises an admin account.",
             "scenarios": [{"name": "s", "steps": [
                 {"keyword": "GIVEN", "content":
                  "The evaluation seed contains the seeded workbook "
                  "`Q3 Sales`, worksheet `Sheet1`."}]}]}
        sents, _ = gherkin.seed_sentences([n])
        self.assertEqual(sents, [
            "Seed data comprises an admin account.",
            "The evaluation seed contains the seeded workbook "
            "`Q3 Sales`, worksheet `Sheet1`."])

    def test_seed_values_literal_dup_dropped(self):
        n = {"id": "REQ-3", "name": "n", "description": "", "scenarios": [
            {"name": "s", "steps": [
                {"keyword": "GIVEN", "content":
                 "The seeded data is account `alice-dev`. Seed values: "
                 "account `alice-dev`."}]}]}
        sents, _ = gherkin.seed_sentences([n])
        self.assertEqual(sents, ["The seeded data is account `alice-dev`."])

    def test_order_preserved_dedup_and_cap(self):
        nodes = []
        for i in range(20):
            nodes.append({"id": f"REQ-{i}", "name": "n", "description": "",
                          "scenarios": [{"name": "s", "steps": [
                              {"keyword": "GIVEN", "content":
                               f"The seeded data is account `u{i}`."}]}]})
        sents, dropped = gherkin.seed_sentences(nodes)
        self.assertEqual(sents[0], "The seeded data is account `u0`.")
        self.assertEqual(len(sents), 16)
        self.assertEqual(dropped, 4)
        # 同句去重:跨节点重复句只进一次。
        dup = [nodes[0], nodes[1]]
        dup[1]["scenarios"][0]["steps"][0]["content"] = \
            "The seeded data is account `u0`."
        sents2, dropped2 = gherkin.seed_sentences(dup)
        self.assertEqual(len(sents2), 1)
        self.assertEqual(dropped2, 0)

    def test_no_seed_no_output(self):
        n = {"id": "REQ-4", "name": "n", "description": "plain",
             "scenarios": [{"name": "s", "steps": [
                 {"keyword": "GIVEN",
                  "content": "A fresh browser session."}]}]}
        self.assertEqual(gherkin.seed_sentences([n]), ([], 0))
        self.assertEqual(gherkin.seed_sentences([]), ([], 0))
        self.assertEqual(gherkin.seed_sentences(None), ([], 0))


if __name__ == "__main__":
    unittest.main()
