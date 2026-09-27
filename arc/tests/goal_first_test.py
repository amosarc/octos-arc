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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import plan_envelope  # noqa: E402
from scaffold import manifest as sc  # noqa: E402


def node(nid, ntype="ATOMIC", children=None, description="", name=None,
         dependencies=None):
    return {"id": nid, "name": name or nid, "type": ntype,
            "children": children or [],
            **({"description": description} if description else {}),
            **({"dependencies": dependencies} if dependencies else {})}


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
        node("REQ-2-1", description="d21", dependencies=["REQ-1"]),
        node("REQ-2-2", description="d22", dependencies=["REQ-2-1"]),
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

    def test_map_lists_headings_and_every_atomic_description(self):
        rmap = main.render_requirements_map(TREE)
        for nid in ("REQ-1", "REQ-2", "REQ-3", "REQ-1-1", "REQ-1-2"):
            self.assertIn(nid, rmap, f"map missing {nid}")
        self.assertIn("Sub Family One", rmap)
        # 用户定则:ATOMIC 描述一个不丢,全量逐字内嵌在所属家族之下
        for nid, desc in (("REQ-1-1-1", "d111"), ("REQ-1-1-2", "d112"),
                          ("REQ-1-2-1", "d121"), ("REQ-2-2", "d22"),
                          ("REQ-3-1", "d31")):
            self.assertIn(f"[{nid}]", rmap)
            self.assertIn(desc, rmap, f"map lost {nid} description")

    def test_real_task_maps_carry_every_atomic(self):
        for task in ("hackathon--github", "hackathon--sheet"):
            req = Path(__file__).resolve().parent.parent / "tasks" / task
            if not (req / "requirements.yaml").exists():
                continue
            tree = main.load_requirement_tree(req)
            rmap = main.render_requirements_map(tree)
            atomics = main.flatten_atomic(tree)
            lost = [str(n.get("id")) for n in atomics
                    if str(n.get("description") or "").strip()[:40]
                    not in rmap]
            self.assertEqual(lost, [], f"{task} map lost descriptions")
            # 地图不含场景正文,应显著小于整份 yaml
            yaml_size = (req / "requirements.yaml").stat().st_size
            self.assertLess(len(rmap), yaml_size)


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


class DependencyClosureTests(unittest.TestCase):
    def test_cross_family_dependency_included_verbatim(self):
        fam_of = main.family_index(TREE)
        batch = [n for n in main.flatten_atomic(TREE)
                 if n["id"] == "REQ-2-1"]
        ctx = main.collect_dependency_context(TREE, batch, fam_of)
        # 依赖 REQ-1(跨家族 FOLDER)→ 契约块逐字
        self.assertIn("[REQ-1 — Family One]", ctx)
        self.assertIn("family one module contract", ctx)

    def test_same_family_dependency_pruned(self):
        fam_of = main.family_index(TREE)
        batch = [n for n in main.flatten_atomic(TREE)
                 if n["id"] == "REQ-2-2"]
        ctx = main.collect_dependency_context(TREE, batch, fam_of)
        # 依赖 REQ-2-1(同家族)→ 剪枝(已在本工单家族段)
        self.assertEqual(ctx, "")

    def test_no_dependencies_returns_empty(self):
        fam_of = main.family_index(TREE)
        batch = [n for n in main.flatten_atomic(TREE)
                 if n["id"] == "REQ-3-1"]
        self.assertEqual(
            main.collect_dependency_context(TREE, batch, fam_of), "")

    def test_unknown_dependency_id_ignored(self):
        fam_of = main.family_index(TREE)
        n = node("REQ-3-1", dependencies=["REQ-99"])
        self.assertEqual(
            main.collect_dependency_context(TREE, [n], fam_of), "")


class RunScopeTests(unittest.TestCase):
    """运行范围机制:OCTOS_ARC_FAMILIES 限定本次 run 实现的家族。"""

    BATCHES = main.group_families(TREE)

    def test_parse_empty_means_all(self):
        self.assertEqual(main.parse_family_scope(None), frozenset())
        self.assertEqual(main.parse_family_scope(""), frozenset())
        self.assertEqual(main.parse_family_scope(" , "),
                         frozenset())

    def test_parse_comma_ids(self):
        self.assertEqual(main.parse_family_scope("REQ-1, REQ-2-3"),
                         frozenset({"REQ-1", "REQ-2-3"}))

    def test_batch_filter_family_level(self):
        scope = main.parse_family_scope("REQ-1")
        kept = [b for b in self.BATCHES
                if main.batch_in_scope(b, scope, main.family_index(TREE))]
        flat = [n["id"] for b in kept for n in b]
        self.assertTrue(all(i.startswith("REQ-1") for i in flat))
        self.assertTrue(flat)

    def test_batch_filter_subfamily_prefix(self):
        # 批粒度语义:子家族前缀命中 → 含它的整个工单保留(家族批整体
        # 进退);未命中的家族批排除。
        scope = main.parse_family_scope("REQ-1-1")
        kept = [b for b in self.BATCHES
                if main.batch_in_scope(b, scope, main.family_index(TREE))]
        flat = [n["id"] for b in kept for n in b]
        self.assertIn("REQ-1-1-1", flat)
        self.assertNotIn("REQ-2-1", flat)
        self.assertNotIn("REQ-3-1", flat)

    def test_batch_filter_no_match_excludes_all(self):
        scope = main.parse_family_scope("REQ-9")
        kept = [b for b in self.BATCHES
                if main.batch_in_scope(b, scope, main.family_index(TREE))]
        self.assertEqual(kept, [])

    def test_scoped_spec_files_follow_scope(self):
        spec_map = {"REQ-1-1-1": ["a.spec.ts"], "REQ-2-1": ["b.spec.ts"]}
        scope = main.parse_family_scope("REQ-1")
        files = main.scoped_spec_files(
            spec_map, self.BATCHES, scope, main.family_index(TREE))
        self.assertEqual(files, ["a.spec.ts"])

    def test_ledger_omits_out_of_scope_families(self):
        fam_of = main.family_index(TREE)
        statuses = {n["id"]: "pending" for n in main.flatten_atomic(TREE)}
        line = main._progress_ledger_line(
            fam_of, statuses, only=frozenset({"REQ-1"}))
        self.assertIn("REQ-1:pending", line)
        self.assertNotIn("REQ-2", line)
        self.assertNotIn("REQ-3", line)

    def test_system_row_scope_section(self):
        with_scope = main.build_system_row_context(
            TREE, "rules", "fx", scope=frozenset({"REQ-1"}))
        self.assertIn("RUN SCOPE", with_scope)
        self.assertIn("REQ-1", with_scope)
        without = main.build_system_row_context(TREE, "rules", "fx")
        self.assertNotIn("RUN SCOPE", without)
        # scope 段不破坏字节稳定性(同 scope 两次生成相同)
        self.assertEqual(with_scope, main.build_system_row_context(
            TREE, "rules", "fx", scope=frozenset({"REQ-1"})))


class SystemRowTests(unittest.TestCase):
    """分层装配:全局层常驻系统行(缓存 0 号位),模块层随工单。"""

    RULES = ("goal-first test rules: " + "x" * 50)
    FIXTURE_RULES = "fixture rules: seeded rows bind verbatim"

    def test_system_row_has_global_layer_not_module_detail(self):
        ctx = main.build_system_row_context(TREE, self.RULES,
                                            self.FIXTURE_RULES)
        # 全局层:root 契约逐字 + 全部标题 + 规则 + 播种契约
        self.assertIn("root global contract", ctx)
        for nid in ("REQ-1", "REQ-2", "REQ-3", "REQ-1-1", "Sub Family One"):
            self.assertIn(nid, ctx)
        self.assertIn(self.RULES, ctx)
        self.assertIn(self.FIXTURE_RULES, ctx)
        # 模块细节(原子描述)不进系统行——随各自家族工单
        for d in ("d111", "d112", "d121", "d21", "d22", "d31"):
            self.assertNotIn(d, ctx)

    def test_system_row_is_byte_stable(self):
        a = main.build_system_row_context(TREE, self.RULES,
                                          self.FIXTURE_RULES)
        b = main.build_system_row_context(TREE, self.RULES,
                                          self.FIXTURE_RULES)
        self.assertEqual(a, b)

    def test_system_row_is_compact(self):
        ctx = main.build_system_row_context(TREE, self.RULES,
                                            self.FIXTURE_RULES)
        self.assertLess(len(ctx), 4000)

    def test_write_system_row_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = main.write_system_row_file(
                Path(td), "stable content")
            self.assertEqual(path.name, "AGENTS.md")
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "stable content")
            # 幂等:同目录重写不炸
            main.write_system_row_file(Path(td), "stable content")


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
        # 数据面基座说明保留(已决保留的后端数据面 API 说明书)
        for section in ("Host-owned (never rewrite)", "SCHEMA-DRIVEN BACKEND",
                        "FIXTURE CONTRACT"):
            self.assertIn(section, rules)
        # 09-27 定则:宿主不制定行为规则——两段宿主发明纪律已裁
        self.assertNotIn("SERVER-SIDE VALIDATION", rules)
        self.assertNotIn("UI CONTRACT HYGIENE", rules)

    def test_stage_writes_config_and_example_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stage = main.stage_selftest_dir(Path(td), 43101)
            names = sorted(p.name for p in stage.iterdir())
            self.assertEqual(
                names, [".gitignore", "MY-CHECK.spec.ts.example",
                        "playwright.config.ts"])
            cfg = (stage / "playwright.config.ts").read_text()
            self.assertIn("http://127.0.0.1:43101", cfg)
            self.assertIn("workers: 1", cfg)
            # 没有宿主编写的 spec(zz-contract-smoke 等)
            self.assertFalse(names == sorted(names)
                             and any(n.endswith(".spec.ts")
                                     for n in names))

    def test_selftest_note_carries_sanctioned_command_shape(self):
        note = main.build_selftest_note("/opt/cli.js")
        self.assertIn("test --config .arc/ui-smoke/playwright.config.ts",
                      note)
        self.assertIn("scenarios ARE the test oracle", note)
        note2 = main.build_selftest_note(None)
        self.assertIn("npx --yes @playwright/test test", note2)

    def test_requirement_messages_extraction(self):
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [node(
                "REQ-1-1",
                description='displays "Username already exists" and '
                'rejected with "Invalid credentials"')],
                description='must display "Welcome back"'),
        ])
        msgs = main._collect_requirement_messages(tree)
        self.assertIn("Username already exists", msgs)
        self.assertIn("Invalid credentials", msgs)
        self.assertIn("Welcome back", msgs)
        self.assertEqual(len(msgs), len(set(msgs)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
