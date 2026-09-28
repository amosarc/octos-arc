"""夹具种子域门控回归(09-28 硬编码规则核验的修复钉)。

防止回退:裸角色词误触发 github 域骨架、坐标重复计数误判 sheet、
无仓库时附属种子挂幻影仓。全部零 IO、确定性。

09-28 追加定则(宿主合成整体退役):角色词只用于 kind 分类,任何
权限语境短语都不得产出合成账号——账号由模型按需求自行推理播种
(github 真跑 24 角色账号全 400 的教训:宿主猜的名字/密码必错)。

Run: python3 arc/tests/fixture_gate_test.py (或 unittest discovery)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture_manifest as fm  # noqa: E402


def _tree(data_items=None, given=""):
    data = [{"category": "accounts", "items": data_items}] if data_items else []
    return {"id": "ROOT", "type": "FOLDER", "data": data, "children": [
        {"id": "REQ-1", "type": "FOLDER", "name": "X", "children": [
            {"id": "REQ-1-1", "type": "ATOMIC", "name": "Y",
             "description": "A page.", "children": [],
             "scenarios": ([{"name": "s", "steps": [
                 {"keyword": "GIVEN", "content": given}]}] if given else [])
             }]}]}


class SeedDomainGateTests(unittest.TestCase):
    def test_bare_role_words_do_not_create_scaffold(self):
        # "write comments"/"admin panel" 裸词不得触发合成底座(B5)。
        c = fm.compile_seed(fm.extract(_tree(
            data_items=["Members may write comments; an admin panel exists."])))
        self.assertEqual(c["kind"], "none")
        self.assertEqual(c["db"], {})

    def test_permission_phrase_classifies_but_seeds_nothing(self):
        # 权限语境短语命中角色 → kind=graph 分类;但宿主不再合成任何
        # 账号/实体(合成底座退役,模型自决)。db 必须为空。
        c = fm.compile_seed(fm.extract(_tree(
            data_items=["Seed data supplies accounts with admin permission."])))
        self.assertEqual(c["kind"], "graph")
        self.assertEqual(c["db"], {})
        self.assertEqual(c["logins"], [])

    def test_repeated_single_coordinate_is_not_sheet(self):
        # 同一坐标反复提及去重后 <4,不构成 sheet 证据(B6)。
        c = fm.compile_seed(fm.extract(_tree(
            given="Cell X1 is ready. X1 again. X1 and X1. Model X1 spec.")))
        self.assertNotIn("workbooks", c["db"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
