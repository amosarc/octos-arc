"""goal-first 转向单测(09-27 宿主优先→目标优先)。

覆盖:家族调度(group_families)、需求地图(render_requirements_map)、
家族契约逐字(describe_family_context)、goal-first 契约文案路径点名
(RULES_MUST_NAME 双断言)、envelope rules_text 覆写、红项 probe 过滤。

Run: python3 arc/goal_first_test.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402
import plan_envelope  # noqa: E402
from scaffold import manifest as sc  # noqa: E402


def node(nid, ntype="ATOMIC", children=None, description="", name=None):
    return {"id": nid, "name": name or nid, "type": ntype,
            "children": children or [],
            **({"description": description} if description else {})}


TREE = node("ROOT", "FOLDER", [
    node("REQ-1", "FOLDER", [
        node("REQ-1-1", "FOLDER", [
            node("REQ-1-1-1", description="d111"),
            node("REQ-1-1-2", description="d112"),
        ], description="sub-family contract one", name="Sub Family One"),
        node("REQ-1-2", "FOLDER", [
            node("REQ-1-2-1", description="d121"),
        ], description="sub-family contract two", name="Sub Family Two"),
    ], description="family one module contract", name="Family One"),
    node("REQ-2", "FOLDER", [
        node("REQ-2-1", description="d21"),
        node("REQ-2-2", description="d22"),
    ], description="family two module contract", name="Family Two"),
    node("REQ-3", "FOLDER", [node("REQ-3-1", description="d31")]),
], description="root global contract")


class GroupFamiliesTests(unittest.TestCase):
    def test_batches_concatenate_to_flatten_atomic_order(self):
        batches = main.group_families(TREE)
        flat = [str(n["id"]) for b in batches for n in b]
        self.assertEqual(
            flat, [str(n["id"]) for n in main.flatten_atomic(TREE)])

    def test_small_families_stay_whole(self):
        batches = main.group_families(TREE)
        # REQ-2(2 节点)与 REQ-3(1 节点)各成一单
        ids = [[str(n["id"]) for n in b] for b in batches]
        self.assertIn(["REQ-2-1", "REQ-2-2"], ids)
        self.assertIn(["REQ-3-1"], ids)

    def test_big_family_splits_along_subfamily_boundaries(self):
        batches = main.group_families(TREE, max_batch=2)
        # REQ-1(3 原子)>2 → 按 group_siblings(folder,4) 切,序不变
        flat = [str(n["id"]) for b in batches for n in b]
        self.assertEqual(flat[:3], ["REQ-1-1-1", "REQ-1-1-2", "REQ-1-2-1"])
        self.assertLessEqual(max(len(b) for b in batches), 2 + 2)  # clamp 4

    def test_malformed_tree_never_crashes(self):
        weird = node("ROOT", "FOLDER", [node("REQ-1")])
        weird["children"] = "not-a-list"
        batches = main.group_families(weird)
        flat = [str(n["id"]) for b in batches for n in b]
        self.assertEqual(
            flat, [str(n["id"]) for n in main.flatten_atomic(weird)])

    def test_family_index_maps_atomics_to_top_family(self):
        fam = main.family_index(TREE)
        self.assertEqual(fam["REQ-1-1-2"]["id"], "REQ-1")
        self.assertEqual(fam["REQ-2-1"]["id"], "REQ-2")


class RequirementsMapTests(unittest.TestCase):
    def test_map_carries_root_description_verbatim(self):
        rmap = main.render_requirements_map(TREE)
        self.assertIn("root global contract", rmap)
        self.assertIn("ROOT CONTRACT", rmap)

    def test_map_lists_every_level1_and_sub_heading(self):
        rmap = main.render_requirements_map(TREE)
        for nid in ("REQ-1", "REQ-2", "REQ-3",
                    "REQ-1-1", "REQ-1-2"):
            self.assertIn(nid, rmap, f"map missing {nid}")
        # 标题名字在场;ATOMIC 叶子是工单项,不进地图
        self.assertIn("Sub Family One", rmap)
        self.assertIn("Family One", rmap)
        self.assertNotIn("REQ-1-1-1", rmap)
        self.assertNotIn("REQ-2-1:", rmap)

    def test_real_task_maps_are_compact(self):
        for task, budget in (("hackathon--github", 6000),
                             ("hackathon--sheet", 6000)):
            req = Path(__file__).resolve().parent / "tasks" / task
            if not (req / "requirements.yaml").exists():
                continue
            tree = main.load_requirement_tree(req)
            rmap = main.render_requirements_map(tree)
            self.assertLess(len(rmap), budget, f"{task} map too big")
            self.assertIn(str(tree.get("description"))[:60].strip(), rmap)


class FamilyContextTests(unittest.TestCase):
    def test_family_contract_verbatim_and_pruned(self):
        fam = main.family_index(TREE)["REQ-1-1-1"]
        batch = [n for n in main.flatten_atomic(fam)
                 if n["id"] == "REQ-1-1-1"]
        ctx = main.describe_family_context(fam, batch)
        self.assertIn("family one module contract", ctx)
        self.assertIn("sub-family contract one", ctx)
        # 未命中子家族剪枝
        self.assertNotIn("sub-family contract two", ctx)

    def test_full_family_batch_carries_all_subfamilies(self):
        fam = main.family_index(TREE)["REQ-1-1-1"]
        ctx = main.describe_family_context(fam, main.flatten_atomic(fam))
        self.assertIn("sub-family contract one", ctx)
        self.assertIn("sub-family contract two", ctx)


class GoalFirstRulesTests(unittest.TestCase):
    def test_goal_first_rules_name_every_contract_path(self):
        for name in sc.RULES_MUST_NAME:
            self.assertIn(
                name, plan_envelope.HOST_STACK_RULES_GOAL_FIRST,
                f"goal-first rules missing {name!r}")

    def test_goal_first_rules_own_the_frontend_and_demote_the_kit(self):
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("goal-first", rules)
        self.assertIn("the model owns it", rules)
        self.assertIn("TOOLBOX (optional, unwired)", rules)
        # 后端数据面各段与 host-first 逐字一致(已决保留)
        for section in ("Host-owned (never rewrite)", "SCHEMA-DRIVEN BACKEND",
                        "FIXTURE CONTRACT", "SERVER-SIDE VALIDATION",
                        "UI CONTRACT HYGIENE"):
            self.assertIn(section, rules)

    def test_envelope_rules_text_override(self):
        env = plan_envelope.build_envelope(
            seq=1, phase="node", task="t", atomic_nodes=[],
            statuses={}, focus=None, ports={"grade": 1, "smoke": 2,
                                            "extra": []},
            spec_map={}, rules_text="CUSTOM RULES")
        self.assertEqual(env["rules_text"], "CUSTOM RULES")
        env2 = plan_envelope.build_envelope(
            seq=2, phase="node", task="t", atomic_nodes=[],
            statuses={}, focus=None, ports={}, spec_map={})
        self.assertEqual(env2["rules_text"], plan_envelope.HOST_STACK_RULES)


class RedLineFilterTests(unittest.TestCase):
    def test_probe_filter_keeps_flow_only(self):
        run = {"reds": [
            {"probe": "flow", "string": "REQ-1: walk", "nodes": ["REQ-1-1-1"],
             "detail": "scenario walk failed"},
            {"probe": "text", "string": "Delete", "nodes": ["REQ-2-1"],
             "detail": "missing"},
        ]}
        lines = main._ui_smoke_red_lines(run, probes=("flow",))
        self.assertIn("REQ-1: walk", lines)
        self.assertNotIn("Delete", lines)
        # 不过滤时全渲染
        self.assertIn("Delete", main._ui_smoke_red_lines(run))


if __name__ == "__main__":
    unittest.main(verbosity=1)
