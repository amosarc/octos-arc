#!/usr/bin/env python3
"""flow_suite 单测 — 提取器/生成器/合并层的纯函数测试。

跑法:python3 arc/flow_suite_test.py(与仓库其它 *_test.py 同风格)。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flow_suite  # noqa: E402


def _node(nid, name, desc="", children=None, ntype="ATOMIC", scenarios=None):
    return {"id": nid, "name": name, "type": ntype,
            "description": desc, "children": children or [],
            "scenarios": scenarios or []}


SHEET_FOLDER_DESC = (
    'Worksheet tabs on the same editor page use the ARIA tab role. The '
    'active worksheet grid uses the ARIA grid role, has the accessible '
    'name "Worksheet grid", and exposes aria-multiselectable="true". '
    'Each worksheet tab provides a button with the accessible name '
    '"Worksheet options for <worksheet name>"; clicking it opens a menu '
    'whose commands use the ARIA menuitem role.')

SHEET_GIVEN = ("The evaluation seed contains the seeded workbook `Q3 "
               "Sales`, worksheet `Sheet1`, and cell A1 value `Region`.")

SHEET_TREE = _node(
    "ROOT", "sheet", ntype="FOLDER", children=[
        _node("REQ-1", "Workbook", ntype="FOLDER", children=[
            _node("REQ-1-2-1", "Create a Blank Workbook",
                  'The home page provides a button with the accessible '
                  'name "New blank workbook"; clicking it opens the '
                  'creation page, whose submit button is named "Create". '
                  'After creation succeeds, the editor shows only a '
                  'blank worksheet named Sheet1. The state remains after '
                  'the user refreshes the page.',
                  scenarios=[{"name": "s1", "steps": [
                      {"keyword": "GIVEN", "content": SHEET_GIVEN}]}]),
            _node("REQ-1-2-2", "Rename a Workbook",
                  'Next to the editor title is a button with the '
                  'accessible name "Rename workbook"; clicking it '
                  'displays a text box labeled "Workbook name", '
                  'prefilled with the last saved name, and a "Save" '
                  'button. An empty name must be rejected with '
                  '"Workbook name cannot be empty". Reopening the '
                  'workbook shows the most recently saved name.'),
        ]),
        _node("REQ-2", "Worksheets", SHEET_FOLDER_DESC, ntype="FOLDER", children=[
            _node("REQ-2-1-3", "Rename a Worksheet",
                  'The "Rename" menu item opens a dialog named "Rename '
                  'worksheet", containing a text box labeled "Worksheet '
                  'name" prefilled with the current name and a "Save" '
                  'button. An empty name displays "Worksheet name '
                  'cannot be empty", and a duplicate name displays '
                  '"Worksheet name already exists". After a successful '
                  'save, the tab displays the new name. From the '
                  'worksheet tab menu.', scenarios=[
                      {"name": "s1", "steps": [
                          {"keyword": "GIVEN", "content": SHEET_GIVEN}]}]),
            _node("REQ-3-1-1", "Edit a Cell",
                  'After selecting a cell, users can modify its content '
                  'by typing directly in the grid.', scenarios=[
                      {"name": "s1", "steps": [
                          {"keyword": "GIVEN", "content": SHEET_GIVEN}]}]),
        ]),
    ])

GITHUB_TREE = _node(
    "ROOT", "github", ntype="FOLDER", children=[
        _node("REQ-1-1-1", "Register a New GitHub Account",
              'The registration page is the form opened by the unique '
              'link named \u201cCreate an account\u201d from the '
              'sign-in page. The form contains exactly one textbox '
              'labeled \u201cUsername\u201d, one textbox labeled '
              '\u201cEmail\u201d, one password input labeled '
              '\u201cPassword\u201d, one password input labeled '
              '\u201cConfirm password\u201d, one initially unchecked '
              'checkbox named \u201cAgree to the terms\u201d, and one '
              'enabled button named \u201cCreate account\u201d. The '
              'resulting signed-in username remains visible after '
              'reload.'),
        _node("REQ-1-1-2", "Sign In with an Existing Account",
              'The sign-in page is the form on the account-access page '
              'containing \u201cUsername or email\u201d, '
              '\u201cPassword\u201d, and \u201cSign in\u201d.'),
        _node("REQ-1-2", "Sign Out",
              'This feature builds on the authenticated-session context '
              'provided by REQ-1-1-2. The page contains exactly one '
              'button named \u201cAccount menu\u201d; its menu contains '
              'exactly one link named \u201cSign out\u201d. Activating '
              'it displays a dialog named \u201cSign out\u201d with '
              'buttons named \u201cConfirm sign out\u201d and '
              '\u201cCancel\u201d.'),
    ])


class ExtractionTests(unittest.TestCase):

    def test_seed_info_backticks(self):
        seed = flow_suite._seed_info(SHEET_TREE)
        self.assertEqual(seed["workbook"], "Q3 Sales")
        self.assertEqual(seed["worksheet"], "Sheet1")
        self.assertEqual(seed["grid"], "Worksheet grid")
        self.assertEqual(seed["menu_opener"], "Worksheet options for")

    def test_straight_quote_roles(self):
        ctrls = flow_suite._finditer_role(
            str(SHEET_TREE["children"][0]["children"][0]["description"]))
        roles = {(r, n) for _, r, n in ctrls}
        self.assertIn(("button", "New blank workbook"), roles)
        self.assertIn(("button", "Create"), roles)
        self.assertNotIn(("button", "Workbook name"), roles)  # 直引号闭合歧义已滤

    def test_curly_quote_roles(self):
        desc = str(GITHUB_TREE["children"][2]["description"])
        ctrls = flow_suite._finditer_role(desc)
        roles = {(r, n) for _, r, n in ctrls}
        self.assertIn(("button", "Account menu"), roles)
        self.assertIn(("link", "Sign out"), roles)
        self.assertIn(("dialog", "Sign out"), roles)
        self.assertIn(("button", "Confirm sign out"), roles)
        self.assertIn(("button", "Cancel"), roles)  # 列表尾巴补收

    def test_placeholder_names_dropped(self):
        ctrls = flow_suite._finditer_role(
            'a button with the accessible name "Open dropdown for '
            '<cell coordinate>" and a "Save" button')
        names = {n for _, _, n in ctrls}
        self.assertNotIn("Open dropdown for <cell coordinate>", names)
        self.assertIn("Save", names)

    def test_bare_list_sentence_offsets_are_global(self):
        desc = ('Opening text. \u201cEmail\u201d, \u201cVerification '
                'code\u201d, and \u201cConfirm password\u201d are '
                'associated field labels; \u201cSend reset link\u201d '
                'and \u201cReset password\u201d are buttons.')
        ctrls = flow_suite._finditer_role(desc)
        by_name = {n: o for o, _, n in ctrls}
        self.assertIn("Email", by_name)
        self.assertIn("Send reset link", by_name)
        # 全局坐标:字段句在后段,offset 必须大于句首位置而非句内小坐标
        self.assertGreater(by_name["Email"], desc.index("\u201cEmail\u201d") - 1)

    def test_messages(self):
        msgs = [m for _, m in flow_suite._messages(
            'an empty name must be rejected with "Workbook name cannot '
            'be empty" and displays "Last updated: <value>"')]
        self.assertIn("Workbook name cannot be empty", msgs)
        self.assertNotIn("Last updated: <value>", msgs)  # 占位符消息不收

    def test_signin_controls(self):
        got = flow_suite._signin_controls(GITHUB_TREE)
        self.assertIsNotNone(got)
        labels, button, entry = got
        self.assertIn("Username or email", labels)
        self.assertIn("Password", labels)
        self.assertEqual(button, "Sign in")


class GenerationTests(unittest.TestCase):

    def test_sheet_walks(self):
        text, nmap = flow_suite.generate_spec(SHEET_TREE, None)
        self.assertIn("REQ-1-2-1", nmap)
        self.assertIn("REQ-1-2-2", nmap)
        self.assertIn("REQ-2-1-3", nmap)
        self.assertIn("REQ-3-1-1", nmap)
        # 创建流:Create 不填任何名即点击(创建页强制名缺陷的杀手)
        self.assertIn("""{ name: "New blank workbook", exact: true }).click();""", text)
        self.assertIn("""{ name: "Create", exact: true }).click();""", text)
        # rename 流:验证腿 + 还原段(用新名重开 tab 菜单)
        self.assertIn('getByText("Worksheet name cannot be empty")', text)
        self.assertIn('"Worksheet options for Flow Renamed"', text)
        # 网格流:A1 选中 + 空 A5 键入 + reload 持久
        self.assertIn("cell('A5').click();", text)
        self.assertIn("toHaveText('Flow123')", text)

    def test_github_register_and_signin(self):
        auth = {"username": "alice-dev", "passwords": ["Valid-password-123!"]}
        text, nmap = flow_suite.generate_spec(GITHUB_TREE, auth)
        self.assertIn("REQ-1-1-1", nmap)
        self.assertIn("REQ-1-1-2", nmap)
        self.assertIn("REQ-1-2", nmap)
        # 注册流:勾选 checkbox + 用户名精确文本可见 + reload
        self.assertIn("'checkbox', { name: \"Agree to the terms\"", text)
        self.assertIn("getByText('flow-probe-user', { exact: true })", text)
        # 登录前奏带密码候选循环
        self.assertIn("for (const pw of AUTH.passwords)", text)
        # 登出流:确认按钮只断言不点击
        i = text.index("REQ-1-2 flow")
        block = text[i:i + 700]
        self.assertIn("Confirm sign out", block)
        self.assertNotIn('"Confirm sign out", exact: true }).click();', block)

    def test_no_auth_no_signin_flows(self):
        # 无 auth:登录前奏不可用 → 需登录的流整条跳过,不产必红
        _, nmap = flow_suite.generate_spec(GITHUB_TREE, None)
        self.assertNotIn("REQ-1-2", nmap)

    def test_deterministic(self):
        auth = {"username": "u", "passwords": ["p12345678"]}
        a = flow_suite.generate_spec(GITHUB_TREE, auth)
        b = flow_suite.generate_spec(GITHUB_TREE, auth)
        self.assertEqual(a, b)

    def test_spec_is_playwright_shaped(self):
        text, _ = flow_suite.generate_spec(SHEET_TREE, None)
        self.assertIn("import { expect, test } from '@playwright/test';", text)
        self.assertNotIn("__", text.replace("__proto__", ""))  # 模板占位符全替换


class MergeTests(unittest.TestCase):

    def test_merge_flow_fails_req1_first(self):
        report = {"passed": 2, "failed": 3,
                  "fails": ["REQ-5-1-1 flow: sort",
                            "REQ-1-1-2 flow: sign in (round-trip)"],
                  "reds": [{"probe": "affordance", "string": "X"}]}
        nmap = {"REQ-1-1-2": "REQ-1-1-2 flow: sign in (round-trip)",
                "REQ-5-1-1": "REQ-5-1-1 flow: sort"}
        n = flow_suite.merge_flow_fails(report, nmap)
        self.assertEqual(n, 2)
        self.assertEqual(report["flow_reds"], 2)
        self.assertEqual(report["reds"][0]["probe"], "flow")
        self.assertTrue(report["reds"][0]["nodes"][0].startswith("REQ-1"))
        self.assertEqual(report["reds"][0]["string"],
                         "REQ-1-1-2 flow: sign in (round-trip)")

    def test_grep_for(self):
        got = flow_suite.grep_for(["REQ-1-2-1", "REQ-2.3"])
        self.assertEqual(got, r"REQ\-1\-2\-1 flow|REQ\-2\.3 flow")

    def test_grep_for_empty(self):
        self.assertIsNone(flow_suite.grep_for([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
