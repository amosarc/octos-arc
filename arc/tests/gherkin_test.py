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

    def test_placeholder_phrase_stripped(self):
        sents, ok = gherkin.clean_sentences(
            "The user opens the home page, and the requested workflow,"
            "the requested workflow page entry the requested workflow "
            "with concrete values `East`, `1200`.")
        self.assertTrue(ok)
        joined = " ".join(sents)
        self.assertNotIn("the requested workflow", joined.lower())
        self.assertIn("concrete values", joined)

    def test_uncleanable_falls_back_to_original(self):
        # 纯占位短语、无句界、无实义内容 → 清洗后为空 → 回退原文
        sents, ok = gherkin.clean_sentences(
            "the requested workflow,the requested workflow")
        self.assertFalse(ok)
        self.assertIn("the requested workflow", sents[0])

    def test_placeholder_name_collapses_to_id(self):
        self.assertEqual(
            gherkin.clean_name(
                "REQ-1-1-1 -the requested workflow,the requested workflow"),
            "REQ-1-1-1")

    def test_placeholder_strip_keeps_word_boundaries(self):
        # 10-01:占位串以空格替换(原为空串),剜掉后不与真词黏连
        # (真跑实证:"...workbook entry, -8 csv,the requested workflow
        # with concrete..." 曾渲染成 "...-8 csvconcrete...")。
        self.assertEqual(
            gherkin.clean_text(
                "clicks the workbook entry, -8 csv,the requested "
                "workflow with concrete values `East`"),
            "clicks the workbook entry, -8 csv concrete values `East`")


class RenderTests(unittest.TestCase):
    def _node(self, steps_templated=False):
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
        n = _node(sc, description="d")
        if steps_templated:
            n["_scenarios_templated"] = True
        return n

    def test_full_mode_renders_all_steps_cleaned(self):
        text = gherkin.render_scenarios(self._node(), "full")
        self.assertIn("Scenario: REQ-1-1-1", text)
        self.assertIn("Given A fresh unauthenticated browser session.",
                      text)
        self.assertIn("And The evaluation seed contains", text)
        self.assertIn("When The user opens the home page", text)
        self.assertIn("Then The list shows only matching entries.", text)
        self.assertNotIn("the requested workflow", text.lower())
        self.assertNotIn("Seed values:", text)

    def test_templated_scenario_renders_given_only_with_note(self):
        text = gherkin.render_scenarios(self._node(steps_templated=True),
                                        "full")
        self.assertIn("Given A fresh unauthenticated browser session.",
                      text)
        self.assertNotIn("When The user opens", text)
        self.assertIn("platform placeholder boilerplate", text)

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


class TurnTemplatedBranchTests(unittest.TestCase):
    """TURN 渲染(feature_text)接占位标记分支(10-01):打标场景渲染
    Flow contract 注释 + GIVEN 原文 + 注记行,占位 WHEN/THEN 不进场;
    未打标节点逐字保留全步骤(github 真场景零改动)。"""

    def _folder_and_atoms(self, templated):
        steps = [
            {"keyword": "GIVEN", "content": "A seeded workbook `Q3`."},
            {"keyword": "WHEN", "content":
                "the requested workflow with concrete values `East`."},
            {"keyword": "THEN", "content": "The workbook opens."}]
        atom = {"id": "REQ-1-1-1", "name": "Open a Workbook",
                "type": "ATOMIC",
                "description": "Users open the seeded workbook from the "
                               "home page via its named entry link.",
                "scenarios": [{"name": "s1", "steps": steps}]}
        if templated:
            atom["_scenarios_templated"] = True
        folder = {"id": "REQ-1-1", "name": "Navigation", "type": "FOLDER",
                  "description": "Open workbooks from the home page.",
                  "children": [atom]}
        return folder, atom

    def _render(self, templated):
        folder, atom = self._folder_and_atoms(templated)
        return gherkin.feature_text(folder, [atom])

    def test_templated_renders_flow_contract_and_given_only(self):
        text = self._render(True)
        self.assertIn(
            "# Flow contract: Users open the seeded workbook from the "
            "home page via its named entry link.", text)
        self.assertIn("Given A seeded workbook `Q3`.", text)
        self.assertIn("platform placeholder boilerplate", text)
        self.assertNotIn("When ", text)
        self.assertNotIn("Then ", text)
        self.assertNotIn("the requested workflow", text.lower())

    def test_scenario_level_flag_branches_too(self):
        # 混合形态节点(github):节点未打标、首场景被场景级补检打标。
        folder, atom = self._folder_and_atoms(False)
        atom["scenarios"][0]["_steps_templated"] = True
        text = gherkin.feature_text(folder, [atom])
        self.assertIn("# Flow contract:", text)
        self.assertNotIn("When ", text)

    def test_unflagged_keeps_full_steps(self):
        text = self._render(False)
        self.assertIn("When ", text)
        self.assertIn("Then The workbook opens.", text)
        self.assertNotIn("# Flow contract", text)
        self.assertNotIn("placeholder boilerplate", text)


class RealTaskSmokeTests(unittest.TestCase):
    def _tree(self, task):
        import main
        req_dir = (Path(__file__).resolve().parent.parent / "tasks" / task)
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest(f"{task} not present")
        return main.load_requirement_tree(req_dir)

    def test_sheet_req_1_1_1_renders_clean_gherkin(self):
        tree = self._tree("hackathon--sheet")
        n = next(x for x in
                 (gherkin.__dict__ and __import__("main").flatten_atomic(tree))
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.render_scenarios(n, "full")
        self.assertIn("Given", text)
        self.assertNotIn("the requested workflow", text.lower())
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

    def test_sheet_turn_path_templated_branch_clean(self):
        # TURN 真路径(feature_text):sheet 节点级打标 → Flow contract
        # + GIVEN 原文 + 注记行,占位 WHEN/THEN 与黏连残渣不进场。
        import main
        tree = self._tree("hackathon--sheet")
        n = next(x for x in main.flatten_atomic(tree)
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.feature_text(self._parent_of(tree, n), [n])
        self.assertIn("# Flow contract:", text)
        self.assertIn("platform placeholder boilerplate", text)
        self.assertIn("`Q3 Sales`", text)
        self.assertNotIn("When ", text)
        self.assertNotIn("the requested workflow", text.lower())
        self.assertNotIn("csvconcrete", text.replace(" ", ""))

    def test_github_turn_path_keeps_real_steps(self):
        import main
        tree = self._tree("hackathon--github")
        n = next(x for x in main.flatten_atomic(tree)
                 if x.get("id") == "REQ-1-1-1")
        text = gherkin.feature_text(self._parent_of(tree, n), [n])
        self.assertIn("Create an account", text)
        self.assertNotIn("placeholder boilerplate", text)


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
