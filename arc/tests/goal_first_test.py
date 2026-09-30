"""goal-first 转向单测(09-27 宿主优先→目标优先)。

覆盖:家族调度(group_families)、需求地图(render_requirements_map)、
家族契约逐字(describe_family_context)、goal-first 契约文案路径点名
(RULES_MUST_NAME 双断言)、envelope rules_text 覆写、红项 probe 过滤。

Run: python3 arc/goal_first_test.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture_manifest  # noqa: E402
import main  # noqa: E402
import plan_envelope  # noqa: E402
from scaffold import manifest as sc  # noqa: E402


def node(nid, ntype="ATOMIC", children=None, description="", name=None,
         dependencies=None, scenarios=None):
    return {"id": nid, "name": name or nid, "type": ntype,
            "children": children or [],
            **({"description": description} if description else {}),
            **({"dependencies": dependencies} if dependencies else {}),
            **({"scenarios": scenarios} if scenarios else {})}


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
        # 依赖 REQ-1(跨家族 FOLDER)→ 双段契约逐字
        self.assertIn("ID: REQ-1", ctx)
        self.assertIn("Name: Family One", ctx)
        self.assertIn("REQ CONTENT (verbatim):", ctx)
        self.assertIn("family one module contract", ctx)

    def test_same_family_cross_batch_dep_included(self):
        fam_of = main.family_index(TREE)
        # 批=[REQ-2-2](单节点批):同族依赖 REQ-2-1 不在本批 → 进闭包
        # (09-29 二轮:旧"同族全剪"假设家族=单工单,大家族拆批后依赖
        # 契约无处在场;github 实测 20 处同族跨批依赖)。
        batch = [n for n in main.flatten_atomic(TREE)
                 if n["id"] == "REQ-2-2"]
        ctx = main.collect_dependency_context(TREE, batch, fam_of)
        self.assertIn("ID: REQ-2-1", ctx)
        # 传递尾巴:REQ-2-1 依赖的跨族 REQ-1 也在场。
        self.assertIn("ID: REQ-1", ctx)

    def test_same_batch_dep_pruned(self):
        fam_of = main.family_index(TREE)
        # 批=[REQ-2-1, REQ-2-2](同批):批内依赖由 THIS BATCH 本身携带,
        # 闭包只输出批外。
        batch = [n for n in main.flatten_atomic(TREE)
                 if n["id"] in ("REQ-2-1", "REQ-2-2")]
        ctx = main.collect_dependency_context(TREE, batch, fam_of)
        self.assertNotIn("ID: REQ-2-1\n", ctx)
        self.assertNotIn("ID: REQ-2-2", ctx)
        self.assertIn("ID: REQ-1", ctx)  # 批外跨族照旧

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

    def test_folder_level_dependency_enters_closure(self):
        # 09-30 封堵:FOLDER 独立声明跨族依赖而无 ATOMIC 重述时,闭包
        # 仍须在场(会话排序图 family_dependency_edges 已含 FOLDER 边,
        # 闭包注入此前只走 ATOMIC deps)。当前真题 FOLDER 边全被 ATOMIC
        # 重述故零行为变化——此测试钉住"换 yaml 不丢契约"的健壮性面。
        tree = node("ROOT", "FOLDER", [
            node("F1", "FOLDER", [
                node("F1-1", description="base feature"),
            ]),
            node("F2", "FOLDER", [
                node("F2-1", description="second feature",
                     scenarios=[{"name": "s", "steps": [
                         {"keyword": "GIVEN", "content": "seeded row"},
                         {"keyword": "WHEN", "content": "act"},
                         {"keyword": "THEN", "content": "assert"}]}]),
            ], dependencies=["F1-1"]),
        ])
        main._annotate_ancestor_folder_deps(tree, ())
        fam_of = main.family_index(tree)
        batch = [n for n in main.flatten_atomic(tree) if n["id"] == "F2-1"]
        ctx = main.collect_dependency_context(tree, batch, fam_of)
        self.assertIn("ID: F1-1", ctx)
        self.assertNotIn("second feature", ctx)  # 批节点自身不在闭包
        # 去重面:ATOMIC 重述同一 FOLDER 依赖时不双发。
        tree2 = node("ROOT", "FOLDER", [
            node("F1", "FOLDER", [node("F1-1", description="base")]),
            node("F2", "FOLDER", [
                node("F2-1", dependencies=["F1-1"], description="restate"),
            ], dependencies=["F1-1"]),
        ])
        main._annotate_ancestor_folder_deps(tree2, ())
        batch2 = [n for n in main.flatten_atomic(tree2) if n["id"] == "F2-1"]
        ctx2 = main.collect_dependency_context(tree2, batch2,
                                               main.family_index(tree2))
        self.assertEqual(ctx2.count("ID: F1-1"), 1)

    def test_dependency_cycle_terminates(self):
        # 环:A→B→A 不死循环,双向都不重复输出。
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [
                node("REQ-1-1", description="a",
                     dependencies=["REQ-2-1"])]),
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="b",
                     dependencies=["REQ-1-1"])]),
        ])
        fam_of = main.family_index(tree)
        batch = [n for n in main.flatten_atomic(tree)
                 if n["id"] == "REQ-1-1"]
        ctx = main.collect_dependency_context(tree, batch, fam_of)
        self.assertEqual(ctx.count("ID: REQ-2-1"), 1)
        self.assertNotIn("ID: REQ-1-1", ctx)

    def test_closure_truncates_with_marker(self):
        # 深距离层超限(>12 节点)整层丢弃留标记;直接依赖永不截。
        # 链方向:T-1 → X-00 → X-01 → …(每个节点依赖下一个)。
        chain = []
        for i in range(20):
            nid = f"REQ-X-{i:02d}"
            nxt = f"REQ-X-{i + 1:02d}" if i < 19 else None
            chain.append(node(nid, description=f"desc {nid}",
                              dependencies=[nxt] if nxt else None))
        tree = node("ROOT", "FOLDER", [
            node("REQ-T", "FOLDER", [
                node("REQ-T-1", description="t",
                     dependencies=["REQ-X-00"])]),
            node("REQ-X", "FOLDER", chain),
        ])
        fam_of = main.family_index(tree)
        batch = [n for n in main.flatten_atomic(tree)
                 if n["id"] == "REQ-T-1"]
        ctx = main.collect_dependency_context(tree, batch, fam_of)
        self.assertIn("ID: REQ-X-00", ctx)      # 直接依赖保留
        self.assertIn("closure truncated", ctx)  # 深层截断标记
        self.assertNotIn("ID: REQ-X-19", ctx)    # 最深的被截

    def test_real_sheet_closure_is_transitive(self):
        # sheet 真树:REQ-5 家族闭包含 REQ-2/3/4 家族文本(传递)。
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon--sheet"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("sheet task not present")
        tree = main.load_requirement_tree(req_dir)
        fam_of = main.family_index(tree)
        batch = [n for n in main.flatten_atomic(tree)
                 if str(n.get("id")).startswith("REQ-5")]
        ctx = main.collect_dependency_context(tree, batch, fam_of)
        for fam in ("REQ-1", "REQ-2", "REQ-3", "REQ-4"):
            self.assertIn(f"ID: {fam}-", ctx,
                          f"{fam} family text missing from closure")

    def test_real_github_cross_batch_same_family_dep_in_closure(self):
        # github 真树:batch3(REQ-2-2-*)依赖 batch2 的 REQ-2-1-2(同族
        # 跨批)——其双段契约必须进闭包(旧"同族全剪"缺口,20 处)。
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon--github"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("github task not present")
        tree = main.load_requirement_tree(req_dir)
        fam_of = main.family_index(tree)
        batches = main.group_families(tree)
        hit = next(b for b in batches
                   if any(str(n.get("id")) == "REQ-2-2-1" for n in b))
        ctx = main.collect_dependency_context(tree, hit, fam_of)
        self.assertIn("ID: REQ-2-1-2", ctx)
        self.assertIn("VERIFICATION REQUIREMENTS", ctx)


class DualSectionContractTests(unittest.TestCase):
    """每轮执行双段契约:REQ CONTENT 与 VERIFICATION REQUIREMENTS 分立。"""

    def test_describe_node_dual_sections(self):
        n = node("REQ-9-1", description="build this",
                 name="Node Nine")
        n["scenarios"] = [{"name": "s1", "steps": [
            {"keyword": "GIVEN", "content": "a seeded workbook"},
            {"keyword": "THEN", "content": "the cell shows 1"}]}]
        text = main.describe_node(n)
        self.assertIn("REQ CONTENT (verbatim):", text)
        self.assertIn("VERIFICATION REQUIREMENTS (verbatim", text)
        self.assertIn("acceptance criteria", text)
        self.assertIn("build this", text)
        self.assertIn("GIVEN a seeded workbook", text)
        self.assertIn("THEN the cell shows 1", text)
        self.assertNotIn("Description:", text)
        self.assertNotIn("Scenarios:", text)

    def test_render_verbatim_nodes_covers_every_atomic(self):
        blob = main.render_verbatim_nodes(TREE)
        for n in main.flatten_atomic(TREE):
            self.assertIn(f"ID: {n['id']}", blob)
        self.assertIn("REQ CONTENT (verbatim):", blob)

    def test_render_verbatim_nodes_carries_verification(self):
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [
                node("REQ-1-1", description="has scenarios")]),
        ])
        tree["children"][0]["children"][0]["scenarios"] = [
            {"name": "s", "steps": [
                {"keyword": "THEN", "content": "it works"}]}]
        blob = main.render_verbatim_nodes(tree)
        self.assertIn("VERIFICATION REQUIREMENTS (verbatim", blob)
        self.assertIn("THEN it works", blob)

    def test_node_prompt_template_binds_verification_text(self):
        self.assertIn("SPEC DISCIPLINE", main.GOAL_FIRST_NODE_PROMPT_TEMPLATE)
        self.assertIn("the TEXT wins", main.GOAL_FIRST_NODE_PROMPT_TEMPLATE)
        self.assertIn("list each red chain",
                      main.GOAL_FIRST_NODE_PROMPT_TEMPLATE)

    def test_mycheck_example_carries_source_rule(self):
        stage = main.stage_selftest_dir(Path(self.id().replace(".", "_")),
                                        43101)
        try:
            text = (stage / "MY-CHECK.spec.ts.example").read_text()
            self.assertIn("SOURCE RULE", text)
            self.assertIn("never edit the spec to match",
                          text)
        finally:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


class SessionGroupTests(unittest.TestCase):
    """会话=依赖环:SCC 共组,组间拓扑序(文档序 tiebreak)。"""

    def test_synthetic_tree_groups_per_family(self):
        batches = main.group_families(TREE)
        groups = main.session_group_batches(TREE, batches,
                                            main.family_index(TREE))
        self.assertEqual([[0], [1], [2]], groups)

    def test_cycle_families_share_one_group(self):
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [
                node("REQ-1-1", description="a",
                     dependencies=["REQ-2-1"])]),
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="b",
                     dependencies=["REQ-1-1"])]),
        ])
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree),
                                            policy="scc")
        self.assertEqual(1, len(groups))
        self.assertEqual({0, 1}, set(groups[0]))

    def test_family_policy_splits_cycle_in_doc_order(self):
        # family 档(默认):环也拆成每家族一会话,文档序破环。
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [
                node("REQ-1-1", description="a",
                     dependencies=["REQ-2-1"])]),
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="b",
                     dependencies=["REQ-1-1"])]),
        ])
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree))
        self.assertEqual([[0], [1]], groups)

    def test_forward_dependency_reorders_groups(self):
        # 文档序 REQ-1 在前但依赖 REQ-2 → 拓扑序 REQ-2 组先行。
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [
                node("REQ-1-1", description="a",
                     dependencies=["REQ-2-1"])]),
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="b")]),
        ])
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree))
        self.assertEqual([[1], [0]], groups)

    def test_real_sheet_two_groups(self):
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon--sheet"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("sheet task not present")
        tree = main.load_requirement_tree(req_dir)
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree),
                                            policy="scc")
        # 2↔5 环经 5→3→2 把 3/4 吸入同一 SCC:{REQ-1} | {REQ-2,3,4,5}
        self.assertEqual(2, len(groups))
        self.assertEqual([0], groups[0])
        self.assertEqual([1, 2, 3, 4], groups[1])

    def test_real_sheet_family_policy_five_groups(self):
        # family 档(默认):每家族一会话,文档序(环在 REQ-2 处破,与
        # 三跑执行顺序一致)。
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon--sheet"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("sheet task not present")
        tree = main.load_requirement_tree(req_dir)
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree))
        self.assertEqual([[0], [1], [2], [3], [4]], groups)

    def test_real_github_singleton_groups_in_doc_order(self):
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon--github"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("github task not present")
        tree = main.load_requirement_tree(req_dir)
        fam_of = main.family_index(tree)
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches, fam_of)
        # github 家族图无环 → 6 个单家族组;大家族拆的多批留在自己组内,
        # 组间保持文档序(依赖无前向冲突)。
        self.assertEqual(6, len(groups))
        fam_seq = []
        for group in groups:
            fams = {str(fam_of[str(batches[bi][0]["id"])]["id"])
                    for bi in group}
            self.assertEqual(1, len(fams), "one family per group")
            fam_seq.append(next(iter(fams)))
        self.assertEqual(
            [f"REQ-{i}" for i in range(1, 7)], fam_seq)


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

    def test_system_row_dedupes_fixture_contract(self):
        """装配层去重:rules 内嵌的通用 FIXTURE 段裁掉,任务专属
        fixture_rules 是唯一承载(llm.jsonl 实证裁前 ×2)。"""
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        fx = "FIXTURE CONTRACT (graded, not demo data): task-specific seed details."
        ctx = main.build_system_row_context(TREE, rules, fx)
        self.assertEqual(ctx.count("FIXTURE CONTRACT (graded"), 1)
        # 指针行保住核心语义
        self.assertIn("seed details bind VERBATIM", ctx)
        self.assertIn("db.run.json never ships", ctx)
        # RULES_MUST_NAME 路径拼写不受影响(scaffold_test 钉在常量上)
        self.assertIn("backend/data/db.json", rules)

    def test_system_row_is_byte_stable(self):
        a = main.build_system_row_context(TREE, self.RULES,
                                          self.FIXTURE_RULES)
        b = main.build_system_row_context(TREE, self.RULES,
                                          self.FIXTURE_RULES)
        self.assertEqual(a, b)

    def test_load_strips_reference_image_marks(self):
        # 09-29 裁决:参考图记号在 yaml 载入时整体剥离(自动附图通道
        # 退役,内核视觉支持保留给显式媒体通道的调用方)——下游渲染器
        # (describe_node/骨架 blob/系统行)天然无图,防线正则随之删除。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            req = Path(td)
            (req / "requirements.yaml").write_text(
                "id: ROOT\n"
                "name: root\n"
                "type: FOLDER\n"
                "description: Global rules. See "
                "![image](reference/sign-up.png) for looks.\n"
                "children:\n"
                "  - id: REQ-1\n"
                "    name: Family One\n"
                "    type: FOLDER\n"
                "    description: \"Page reference: "
                "![image](reference/home.png)\"\n"
                "    children:\n"
                "      - id: REQ-1-1\n"
                "        name: N1\n"
                "        type: ATOMIC\n"
                "        description: \"Build it. Page reference: "
                "![image](reference/grid.png)\"\n"
                "        scenarios:\n"
                "          - name: s1\n"
                "            steps:\n"
                "              - keyword: THEN\n"
                "                content: works "
                "![shot](reference/after.png)\n"
                "      - id: REQ-1-2\n"
                "        name: N2\n"
                "        type: ATOMIC\n"
                "        description: |\n"
                "          Real body.\n"
                "\n"
                "          Page reference:\n"
                "          ![image](reference/sort.png)\n"
                "      - id: REQ-1-3\n"
                "        name: N3\n"
                "        type: ATOMIC\n"
                "        description: |\n"
                "          Shot body.\n"
                "\n"
                "          Screenshot reference:\n"
                "          ![image](reference/shot.png)\n",
                encoding="utf-8")
            tree = main.load_requirement_tree(req)
        blob = main.render_verbatim_nodes(tree)
        ctx = main.build_system_row_context(tree, "rules", "fx")
        for text in (blob, ctx):
            self.assertNotIn("![", text)
            self.assertNotIn("](reference/", text)
        # 悬空的整行 "Page reference:" 一并消失,正文其余保留。
        self.assertNotIn("Page reference:", blob)
        # 标签变体同律剥离(09-30:github 真实 yaml 用 "Screenshot
        # reference:",旧正则只认 Page 变体,26/47 节点留悬空标签)。
        self.assertNotIn("Screenshot reference:", blob)
        self.assertIn("Global rules.", ctx)
        self.assertIn("Build it.", blob)
        self.assertIn("works", blob)
        # 按 "Page reference:" 节点整段剥离:前置空行分隔随之收走,
        # 描述尾部不留空白残行(旧实现留 "\n\n\n \n")。
        nodes = {}

        def collect(n):
            nodes[n["id"]] = n
            for c in n.get("children") or []:
                collect(c)
        collect(tree)
        self.assertEqual(nodes["REQ-1-1"]["description"], "Build it.")
        self.assertEqual(nodes["REQ-1-2"]["description"], "Real body.\n")
        self.assertEqual(nodes["REQ-1-3"]["description"], "Shot body.\n")

    def test_system_row_is_compact(self):
        ctx = main.build_system_row_context(TREE, self.RULES,
                                            self.FIXTURE_RULES)
        self.assertLess(len(ctx), 4000)

    def test_session_injects_into_per_profile_data_dir(self):
        """AGENTS.md 必须落在 per-profile data 目录(内核系统行读取点)。

        serve.rs:344 把 resolve_data_dir(profile)=<data_dir>/profiles/
        <pid>/data 传给 build_system_prompt 的 data_dir/project_dir 双位
        (实读核实)——早前写到 <data_dir>/ 根是错位注入,内核读不到。"""
        import tempfile
        import octos_stdio
        with tempfile.TemporaryDirectory() as td:
            pid = "arc-test-1"
            data_dir = Path(td)
            # 复现 session._inject_system_context 的路径逻辑
            target = (data_dir / "profiles" / pid / "data")
            target.mkdir(parents=True, exist_ok=True)
            (target / "AGENTS.md").write_text("stable content",
                                              encoding="utf-8")
            self.assertEqual(
                (target / "AGENTS.md").read_text(encoding="utf-8"),
                "stable content")
            # session 注入方法(时序在 create 后,不经 RPC)
            session_like = octos_stdio.OctosStdioSession.__new__(
                octos_stdio.OctosStdioSession)
            session_like.data_dir = data_dir
            session_like.profile_id = pid
            session_like.system_context = "stable content"
            session_like._stderr_lines = []
            session_like._inject_system_context()
            self.assertEqual(
                (target / "AGENTS.md").read_text(encoding="utf-8"),
                "stable content")


class FixtureRulesTruncationTests(unittest.TestCase):
    """FIXTURE 段常驻系统行(字节稳定层),必须整句收尾:截断上限只做
    失控保护,不允许静默腰斩内容——github 全量段 ~2930 字符,2900 帽
    曾恰好把尾句 "scratch test scripts belong in /tmp, never inside
    .arc/acceptance." 断半。"""

    def _render(self, stats):
        return fixture_manifest.render_extra_rules({"e2e_envs": []}, stats)

    def test_graph_text_ends_with_complete_sentence(self):
        text = self._render({"kind": "graph", "seeded": 1,
                             "uncovered": 0, "arrange": 0,
                             "canonical": False})
        self.assertIn("scratch test scripts belong in /tmp, never "
                      "inside .arc/acceptance.", text)
        self.assertTrue(text.endswith("."), text[-80:])
        self.assertLess(len(text), fixture_manifest._MAX_EXTRA_RULES)

    def test_sheet_text_unaffected_and_complete(self):
        text = self._render({"kind": "sheet", "seeded": 0,
                             "uncovered": 0, "arrange": 9,
                             "canonical": True})
        self.assertTrue(text.endswith("."), text[-80:])
        self.assertLess(len(text), fixture_manifest._MAX_EXTRA_RULES)


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
        # 数据面基座说明保留;09-28 CRUD 生成器退役:API 面归模型
        # (backend/src/routes/),schema 声明协议整段删除
        for section in ("Host-owned (never rewrite)",
                        "ALL API endpoints are YOURS", "FIXTURE CONTRACT"):
            self.assertIn(section, rules)
        self.assertNotIn("SCHEMA-DRIVEN BACKEND", rules)
        self.assertNotIn("engine.js", rules)
        self.assertNotIn("schema.json", rules)
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
        self.assertIn("test --config ./ui-smoke/playwright.config.ts",
                      note)
        # 命令形状不得指向 .arc(平台命名空间);边界句提及 .arc/ 是允许的
        self.assertNotIn("--config .arc", note)
        self.assertIn("never inside .arc/", note)
        self.assertIn("scenarios ARE the test oracle", note)
        note2 = main.build_selftest_note(None)
        self.assertIn("npx --yes @playwright/test test", note2)

    def test_selftest_note_pins_new_fidelity_and_freedom_rules(self):
        # 09-30 github REQ-1/2/3 法医四件套:双 Sign in(strict 定位器)、
        # session restore、Playwright 全面自由化、调试 spec 残留。
        note = main.build_selftest_note("/opt/cli.js")
        # 保真规则 4:歧义=应用缺陷(awareness,不限制 spec 写法)
        self.assertIn("GRADER LOCATOR IS STRICT", note)
        self.assertIn("That is an APP defect", note)
        # 保真规则 3 扩充:签入态 reload 存活(session restore)
        self.assertIn("assert the signed-in identity survives", note)
        # Playwright 自由化:撤禁令(2026-09-30 运营拍板)
        self.assertNotIn("raw browser", note)
        self.assertNotIn("browserless guessing", note)
        self.assertIn("How you invoke Playwright is yours to choose", note)
        # 调试 spec 残留纪律
        self.assertIn("zz-debug-", note)

    def test_skeleton_prompt_has_no_failure_class_rules(self):
        # 09-30 终裁:失败模式类提示词硬编码禁止(如已删的 CLIENT ARITY
        # "URL 拼接会出 undefined"条款)——问题由测试驱动闭环发现:自测
        # spec 断言可观察状态 → 红 → 修。平台机制事实(strict 定位器/
        # 超时墙/种子契约)不受此限。
        for banned in ("CLIENT ARITY", "undefined"):
            self.assertNotIn(banned, main.GOAL_FIRST_SKELETON_PROMPT)

    def test_rehearsal_repair_prompt_carries_retry_note_fork(self):
        # P1-2a:第 2 次修复换策略话术——同错复发时明示"上一种修法没
        # 生效,换方式重推导",不再重复同一补丁形状(sheet 法医
        # "重复失败不升级")。
        fmt = {"error": "E", "port": 1, "smoke": 2}
        first = main.REHEARSAL_REPAIR_PROMPT.format(
            retry_note=main.REHEARSAL_REPAIR_FIRST_NOTE, **fmt)
        retry = main.REHEARSAL_REPAIR_PROMPT.format(
            retry_note=main.REHEARSAL_REPAIR_RETRY_NOTE, **fmt)
        self.assertNotIn("SAME failure", first)
        self.assertIn("SAME failure", retry)
        self.assertIn("different approach", retry)
        # 负向场景零专门措辞(09-30 定则:不为负向场景新增机制/措辞)。
        for text in (first, retry):
            self.assertNotIn("rejection", text.lower())

    def test_selftest_nudge_is_process_neutral(self):
        # T1 nudge 只陈述过程事实(改了文件但零自测运行),不预设任何
        # 失败形态;"expected outcome" 措辞天然涵盖预期拒绝类场景。
        self.assertIn("no self-test run was recorded",
                      main.SELFTEST_NUDGE_PROMPT)
        self.assertIn("expected outcome", main.SELFTEST_NUDGE_PROMPT)
        self.assertNotIn("rejection", main.SELFTEST_NUDGE_PROMPT.lower())

    def test_frontend_rules_pin_accessible_name_uniqueness(self):
        # 09-30 法医:登录页双 "Sign in" → 评测器 strict 定位器歧义即死,
        # ~39/44 场景的第一死因;应用侧义务钉在 HOST CONTRACT。
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("ACCESSIBLE-NAME UNIQUENESS", rules)
        self.assertIn("share the same accessible name", rules)

    def test_browser_script_guard_is_not_armed_by_default(self):
        # 09-30 拍板:Playwright 用法全面放开——env 不再默认武装内核
        # guard(代码保留,运维可显式 re-enable)。
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = main.build_octos_env(Path(tmp))
        self.assertNotIn("OCTOS_DENY_BROWSER_SCRIPTS", env)

    def test_scenario_level_boilerplate_detection(self):
        # 09-30 github 法医:47 节点里 23 个混合形态——场景 1 真步骤、
        # 场景 2+ 是跨节点占位骨架,节点级检测器按设计放行(标题回声),
        # 36/100 占位场景曾以 "VERIFICATION REQUIREMENTS (verbatim)" 名义
        # 进工单。场景级判据(通用零词表):非 GIVEN 步骤剥种子尾/引号值/
        # 标题短语后跨 ≥4 节点逐字相同 = 占位;场景全部步骤皆占位才打标。
        def make_node(nid, name, tab):
            boiler_when = (f"The user signs in as `alice-dev` with "
                           f"`Valid-password-123!` and follows the visible "
                           f"controls for the {name.lower()} workflow. "
                           f"Seed values: account `alice-dev`.")
            boiler_then = ("The page displays the required headings, "
                           "controls, values, and status for the "
                           f"{name.lower()} workflow.")
            return main_node(nid, name, scenarios=[
                {"name": f"{nid} real", "steps": [
                    {"keyword": "GIVEN", "content": "A seeded org exists."},
                    {"keyword": "WHEN", "content":
                        f"The user clicks the {tab} tab, enters the name "
                        "in the filter box, and clicks the result."},
                    {"keyword": "THEN", "content":
                        f"The list shows only matching {tab} entries with "
                        "their visibility and update time."}]},
                {"name": f"{nid} filler", "steps": [
                    {"keyword": "GIVEN", "content": "A seeded org exists."},
                    {"keyword": "WHEN", "content": boiler_when},
                    {"keyword": "THEN", "content": boiler_then}]}])

        def main_node(nid, name, scenarios=None):
            return {"id": nid, "name": name, "type": "ATOMIC",
                    "dependencies": [], "children": [],
                    **({"scenarios": scenarios} if scenarios else {})}

        root = {"id": "ROOT", "name": "Root", "type": "FOLDER",
                "children": [
                    {"id": "F1", "name": "Family One", "type": "FOLDER",
                     "children": [
                         make_node("A-1", "Alpha Workflow", "Ledgers"),
                         make_node("A-2", "Beta Workflow", "Troves"),
                         make_node("A-3", "Gamma Workflow", "Archives"),
                         make_node("A-4", "Delta Workflow", "Cellars"),
                     ]}]}
        flagged = main._mark_boilerplate_scenarios(root)
        self.assertEqual(flagged, 4, "each node's filler scenario flags")
        node = root["children"][0]["children"][0]
        scens = node["scenarios"]
        self.assertFalse(scens[0].get("_steps_templated"),
                         "real scenario must stay unflagged")
        self.assertTrue(scens[1].get("_steps_templated"))
        text = main.describe_node(node, None, "full")
        self.assertIn("The user clicks the Ledgers tab", text,
                      "real WHEN/THEN must render verbatim")
        self.assertIn("WHEN/THEN steps omitted", text)
        self.assertNotIn("follows the visible controls", text,
                         "boilerplate must not render as acceptance criteria")
        # 单节点无跨节点重复 → 零误标
        single = {"id": "ROOT", "name": "Root", "type": "FOLDER",
                  "children": [{"id": "F1", "name": "F", "type": "FOLDER",
                                "children": [make_node("B-1", "Solo Job",
                                                       "Ledgers")]}]}
        self.assertEqual(main._mark_boilerplate_scenarios(single), 0)

    def test_requirement_message_bridge_is_retired(self):
        # 09-28 CRUD 生成器退役:引号消息 sidecar 桥(_collect_requirement_
        # messages + req-messages.json)无消费方,须保持删除状态(防回退)。
        import scaffold as sc_pkg
        self.assertFalse(hasattr(main, "_collect_requirement_messages"))
        self.assertFalse(hasattr(main, "SCHEMA_DIRECTIVE"))
        self.assertFalse(hasattr(sc_pkg, "Watcher"))
        self.assertFalse(hasattr(sc_pkg, "compile_schema"))

    def test_selftest_evidence_collects_last_result_per_spec(self):
        # 09-30:平台 runner 上 Tier-2 恒不可用——final 链从内核
        # context-logs 复原模型自测的末次结果作取证锚点。
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".octos" / "context-logs" / "20260930-100000-1"
            root.mkdir(parents=True)

            def row(ts, cmd, out):
                return _json.dumps({"kind": "tool_call", "tool": "bash",
                                    "args": {"cmd": cmd}, "output": out,
                                    "ts": ts}, ensure_ascii=False)

            lines = [
                row("2026-09-30 10:00:00",
                    "node cli.js test --config ./ui-smoke/playwright.config.ts req-1.spec.ts",
                    "  1 passed (2.0s)"),
                # 同 spec 红了又修绿:末次结果必须覆盖首次
                row("2026-09-30 10:01:00",
                    "node cli.js test --config ./ui-smoke/playwright.config.ts req-1.spec.ts",
                    "  1 failed │ 0 passed (2.0s)"),
                row("2026-09-30 10:02:00",
                    "node cli.js test --config ./ui-smoke/playwright.config.ts req-1.spec.ts",
                    "  1 passed (2.1s)"),
                # 非 playwright 的 bash 调用不收
                row("2026-09-30 10:03:00",
                    "npm run build --prefix frontend",
                    "built in 3s"),
            ]
            (root / "tools.jsonl").write_text("\n".join(lines) + "\n",
                                              encoding="utf-8")
            st = main._collect_selftest_evidence(Path(td))
            self.assertIsNotNone(st)
            self.assertEqual(st["runs"], 3)
            self.assertEqual(st["specs"]["req-1.spec.ts"],
                             {"passed": 1, "failed": None,
                              "ts": "2026-09-30 10:02:00"})
            self.assertEqual(st["last"]["passed"], 1)
            # postflight 之后的落点也兜
            import shutil
            shutil.rmtree(Path(td) / ".octos")
            moved = (Path(td) / "octos" / "dot-octos" / "context-logs"
                     / "20260930-100000-1")
            moved.mkdir(parents=True)
            (moved / "tools.jsonl").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")
            self.assertIsNotNone(main._collect_selftest_evidence(Path(td)))
        # 无任何自测运行 → None(不造键)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(main._collect_selftest_evidence(Path(td)))

    def test_selftest_presence_gate_reads_only_the_delta(self):
        # T1:基线偏移之后的增量才计——批 turn 前的自测运行不救批 turn
        # 后的"零运行";新出现的会话文件按偏移 0 全量计;只认 playwright
        # 命令形状,构建/探活不算。
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".octos" / "context-logs" / "r1"
            root.mkdir(parents=True)
            log_file = root / "tools.jsonl"

            def row(cmd):
                return _json.dumps({"kind": "tool_call", "tool": "bash",
                                    "args": {"cmd": cmd},
                                    "output": "ok"})

            log_file.write_text(row("node cli.js test --config "
                                    "./ui-smoke/playwright.config.ts "
                                    "old.spec.ts") + "\n",
                                encoding="utf-8")
            baseline = main._selftest_baseline(Path(td))
            # 基线后只有构建 → 未运行
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(row("npm run build --prefix frontend") + "\n")
            self.assertFalse(main._selftest_ran_since(Path(td), baseline))
            # 基线后无自测时,新出现的会话文件(偏移 0)里有自测 → 已运行
            root2 = Path(td) / ".octos" / "context-logs" / "r2"
            root2.mkdir()
            (root2 / "tools.jsonl").write_text(
                row("node cli.js test --config "
                    "./ui-smoke/playwright.config.ts a.spec.ts") + "\n",
                encoding="utf-8")
            self.assertTrue(main._selftest_ran_since(Path(td), baseline))
            # 基线后出现自测命令 → 已运行(增量识别,历史不误报)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(row("npx --yes @playwright/test test --config "
                             "ui-smoke/playwright.config.ts req-2.spec.ts")
                         + "\n")
            self.assertTrue(main._selftest_ran_since(Path(td), baseline))

    def test_frontend_fingerprint_tracks_sources_only(self):
        # rehearsal 构建缓存判定面:指纹只跟源文件走(node_modules/dist
        # 不计),源未变则稳定,touch 源则变化。
        import os
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as td:
            fe = Path(td) / "frontend"
            (fe / "src").mkdir(parents=True)
            (fe / "src" / "App.jsx").write_text("x", encoding="utf-8")
            (fe / "node_modules").mkdir()
            (fe / "node_modules" / "dep.js").write_text("y", encoding="utf-8")
            (fe / "dist").mkdir()
            (fe / "dist" / "index.html").write_text("z", encoding="utf-8")
            fp1 = main._frontend_fingerprint(fe)
            self.assertEqual(main._frontend_fingerprint(fe), fp1)
            (fe / "dist" / "index.html").write_text("changed", encoding="utf-8")
            self.assertEqual(main._frontend_fingerprint(fe), fp1,
                             "dist 产物不计入指纹")
            (fe / "node_modules" / "dep.js").write_text("c2", encoding="utf-8")
            self.assertEqual(main._frontend_fingerprint(fe), fp1,
                             "node_modules 不计入指纹")
            src = fe / "src" / "App.jsx"
            os.utime(src, (time.time() + 10, time.time() + 10))
            self.assertNotEqual(main._frontend_fingerprint(fe), fp1,
                                "源变更必须改变指纹")
            (fe / "src" / "new.css").write_text("body{}", encoding="utf-8")
            self.assertNotEqual(main._frontend_fingerprint(fe), fp1,
                                "新增源文件必须改变指纹")

    def test_prompt_templates_format_without_keyerror(self):
        # 模板字面量花括号必须保持 {{}} 转义(存量 KeyError 雷),后端
        # 所有权行指向 routes(生成器词汇不回流)。09-29 减法重构:
        # TOOLBOX 段删除(系统行 HOST CONTRACT 已有),spec_anchor 成
        # 为必填 format 字段(逐批判定推导锚点)。
        sk = main.GOAL_FIRST_SKELETON_PROMPT.format(port=3000, smoke=3100)
        self.assertIn("seed-accounts.json", sk)
        self.assertNotIn("schema.json", sk)
        self.assertIn("ONLY if the requirements name sign-in", sk)
        node_p = main.GOAL_FIRST_NODE_PROMPT_TEMPLATE.format(
            node_id="REQ-1", node_spec="NS", port=3000, smoke=3100,
            spec_anchor="the VERIFICATION REQUIREMENTS text above")
        self.assertIn("backend/src/routes/", node_p)
        self.assertNotIn("schema.json", node_p)
        # TOOLBOX 不再随工单重发(系统行常驻);纪律句齐备
        self.assertNotIn("TOOLBOX (optional, unwired)", node_p)
        self.assertIn("SPEC DISCIPLINE", node_p)
        self.assertIn("the TEXT wins", node_p)
        self.assertIn("list each red chain", node_p)
        self.assertIn("FLOW FIDELITY", node_p)
        self.assertIn("VALIDATION SCOPE", node_p)
        self.assertIn("WRITE THE SPEC BEFORE IMPLEMENTING", node_p)
        # curl 判定通道已关(func 200 ≠ 浏览器流绿)
        self.assertNotIn("(or curl the API directly)", node_p)

    def test_spec_anchor_forks_by_batch_templated_flag(self):
        # 模板批:锚 description;真步骤批:锚 VERIFICATION 文本。
        # 锚点分叉逻辑由批组装处消费 _scenarios_templated 标记——
        # 这里钉住两档锚点文本都可在模板中承载(format 不炸即可)。
        for anchor in ("the REQ CONTENT descriptions above — this batch's "
                       "scenario steps are template placeholders; the "
                       "descriptions are the authoritative flow contract",
                       "the VERIFICATION REQUIREMENTS text above"):
            p = main.GOAL_FIRST_NODE_PROMPT_TEMPLATE.format(
                node_id="R", node_spec="NS", port=1, smoke=2,
                spec_anchor=anchor)
            self.assertIn(anchor, p)

    def test_gateway_override_excludes_phantom_tools(self):
        # 09-29 减法重构(最大单项):基础提示词与 7 工具 allowlist 对齐
        # ——Subagents/Memory 整节与全部幻影工具名出局。
        text = main._GATEWAY_PROMPT_OVERRIDE
        for tool in ("read_file", "edit_file", "write_file", "grep",
                     "list_dir", "update_plan", "bash"):
            self.assertIn(f"`{tool}`", text, f"missing real tool {tool}")
        for phantom in ("spawn", "read_task_output", "check_background_tasks",
                        "save_memory", "recall", "ask_user_question",
                        "glob", "code_structure", "diff_edit", "apply_patch",
                        "view_image"):
            self.assertNotIn(phantom, text, f"phantom tool {phantom} leaked")
        self.assertNotIn("## Subagents", text)
        self.assertNotIn("## Memory", text)
        self.assertNotIn("Match the user's language", text)
        self.assertIn("NEVER invent, simulate", text)
        self.assertIn("READ BEFORE WRITE", text)

    def test_install_gateway_prompt_override_writes_file(self):
        import tempfile
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("main.Path") as mp:
                mp.home.return_value = Path(td)
                logs: list[str] = []
                main.install_gateway_prompt_override(logs.append)
                f = Path(td) / ".octos" / "prompts" / "gateway.md"
                self.assertTrue(f.exists())
                self.assertEqual(f.read_text(encoding="utf-8"),
                                 main._GATEWAY_PROMPT_OVERRIDE)
                # 幂等:二次调用不重写(内容一致直接返回)
                logs.clear()
                main.install_gateway_prompt_override(logs.append)
                self.assertTrue(all("installed" not in m for m in logs)
                                or not logs)

    def test_build_octos_env_thinking_tiers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "c1"
            env = main.build_octos_env(cfg)
            conf = json.loads((cfg / "config.json").read_text())
            self.assertEqual(conf["model_thinking"], main.THINKING)
            self.assertEqual(conf["model_reasoning_effort"],
                             main.REASONING_EFFORT)
            cfg2 = Path(td) / "c2"
            main.build_octos_env(cfg2, thinking=main.THINKING_REPAIR,
                                 effort=main.REASONING_EFFORT_REPAIR)
            conf2 = json.loads((cfg2 / "config.json").read_text())
            self.assertEqual(conf2["model_thinking"], main.THINKING_REPAIR)
            self.assertEqual(conf2["model_reasoning_effort"],
                             main.REASONING_EFFORT_REPAIR)
        self.assertNotEqual(main.REASONING_EFFORT,
                            main.REASONING_EFFORT_REPAIR,
                            "修复档必须与工作档不同,否则分档是摆设")

    def test_describe_node_data_only_mode_drops_then(self):
        nd = node(
            "REQ-8-8-8", "n", "desc text",
            scenarios=[{"name": "s1", "steps": [
                {"keyword": "GIVEN", "content": "a seeded workbook"},
                {"keyword": "WHEN", "content": "user clicks X"},
                {"keyword": "THEN", "content": "Y shows"}]}])
        full = main.describe_node(nd)
        self.assertIn("WHEN user clicks X", full)
        data_only = main.describe_node(nd, verification="data-only")
        self.assertNotIn("WHEN user clicks X", data_only)
        self.assertNotIn("THEN Y shows", data_only)
        self.assertIn("GIVEN a seeded workbook", data_only)
        self.assertIn("SCENARIO DATA CONTRACTS", data_only)

    def test_describe_node_templated_placeholder(self):
        nd = node(
            "REQ-7-7-7", "n",
            "Users create a blank workbook from the home page.",
            scenarios=[{"name": "s -the requested workflow",
                        "steps": [
                            {"keyword": "GIVEN",
                             "content": "seed contains workbook `Q3`"},
                            {"keyword": "WHEN",
                             "content": "the requested workflow,the "
                                        "requested workflow page entry "
                                        "the requested workflow with "
                                        "concrete values"},
                            {"keyword": "THEN",
                             "content": "the requested workflow,the "
                                        "requested workflow using the "
                                        "same seeded names and values"},
                            {"keyword": "THEN",
                             "content": "after the user refreshes the "
                                        "page the requested workflow "
                                        "the requested workflow remains "
                                        "persisted the requested "
                                        "workflow"}]}])
        self.assertTrue(main._node_scenarios_templated(nd),
                        "真垃圾样本必须命中模板检测")
        nd["_scenarios_templated"] = True  # load 期 _mark_templated_scenarios 打标
        rendered = main.describe_node(nd)
        self.assertNotIn("WHEN the requested workflow", rendered)
        self.assertIn("template placeholders", rendered)
        self.assertIn("authoritative flow contract", rendered)
        # GIVEN 数据契约保留
        self.assertIn("GIVEN seed contains workbook `Q3`", rendered)
        clean = node("REQ-6-6-6", "m", "plain",
                           scenarios=[{"name": "s", "steps": [
                               {"keyword": "WHEN",
                                "content": "user clicks the save button"},
                               {"keyword": "THEN",
                                "content": "the row appears"},
                               {"keyword": "THEN",
                                "content": "values persist"},
                               {"keyword": "THEN",
                                "content": "state remains"},
                               {"keyword": "THEN",
                                "content": "nothing else changes"}]}])
        self.assertFalse(main._node_scenarios_templated(clean),
                         "干净场景文本不得误伤")
        self.assertIn("WHEN user clicks the save button",
                      main.describe_node(clean))

    def test_mycheck_example_is_task_neutral(self):
        # 09-28 审计 P1:示例曾内嵌 sheet 任务串('Q3 Sales'/'Pivot table
        # editor'/'Workbook name'),每个任务都会收到别的任务的先验。
        # LOCATOR/GESTURE 仪器规则(B 级)保留;任务词汇不得回流。
        # 09-30 追加:gridcell A1/C5/B3 同属 sheet 味——换 role 中性形。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            main.stage_selftest_dir(Path(td), 43101)
            text = (Path(td) / "ui-smoke" / "MY-CHECK.spec.ts.example"
                    ).read_text(encoding="utf-8")
        for word in ("Q3 Sales", "Pivot table", "Workbook name", "pivot",
                     "gridcell", "'A1'", "'C5'", "'B3'"):
            self.assertNotIn(word, text)
        for keep in ("LOCATOR RULE", "GESTURE RULE", "getByRole('region'",
                     "getByRole('option'", "Shift"):
            self.assertIn(keep, text)

    def test_segment_digest_seeds_line_is_gated(self):
        # 09-28 审计 P3:种子声明行只在宿主真落过种子时附带——空种子
        # 任务里它是假陈述。
        notes = {"REQ-1": "did X"}
        with_seeds = main._segment_digest(
            notes, {"REQ-1": "done"}, has_seeds=True)
        without = main._segment_digest(
            notes, {"REQ-1": "done"}, has_seeds=False)
        self.assertIn("seeds were compiled by the host", with_seeds)
        self.assertNotIn("seeds were compiled by the host", without)
        self.assertIn("REQ-1", without, "digest body itself stays")

    def test_lazy_gate_ignores_requirements_dir(self):
        # 09-28 审计:需求原文是只读真值,模型往 requirements/ 写文件
        # 不得洗白零工作轮;backend/frontend 的真实变化仍计数。
        import os
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            req = out / "requirements"
            req.mkdir(parents=True)
            p = req / "requirements.yaml"
            p.write_text("scratch\n", encoding="utf-8")
            os.utime(p, (time.time() + 5, time.time() + 5))
            self.assertFalse(main.project_files_changed(out, time.time()))
            backend = out / "backend" / "src"
            backend.mkdir(parents=True)
            (backend / "index.js").write_text("x\n", encoding="utf-8")
            self.assertTrue(main.project_files_changed(out, time.time() - 10))


class HostRuntimeRegressionTests(unittest.TestCase):
    """09-28 评审修复回归:final 重放后 SPEC RUN 装置不残、懒人门不误判。"""

    def test_final_replay_restores_spec_run_runtime(self):
        # 防篡改重放(stage_specs)只回填 *.spec.ts+support——config 必须
        # 由 stage_acceptance_runtime 重建,否则 final 轮的 verbatim SPEC
        # RUN 命令指向不存在的 config(09-28 评审 P0-1)。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            tests = out / "tests-src"
            tests.mkdir()
            (tests / "REQ-1.spec.ts").write_text(
                "import { test, expect } from '@playwright/test';\n",
                encoding="utf-8")
            stage = plan_envelope.stage_specs(tests, out)
            main.stage_acceptance_runtime(stage, 43101, None)
            cfg = (stage / "playwright.config.ts").read_text()
            self.assertIn("http://127.0.0.1:43101", cfg)
            self.assertIn("timeout: 10_000", cfg)
            # 重放清目录 → 重建(final 调用点同一路径)
            stage2 = plan_envelope.stage_specs(tests, out)
            self.assertFalse((stage2 / "playwright.config.ts").exists())
            main.stage_acceptance_runtime(stage2, 43101, None)
            self.assertTrue((stage2 / "playwright.config.ts").is_file())

    def test_project_files_changed_ignores_runtime_run_file(self):
        # 首批 turn 的时间窗覆盖宿主首次起服(store 把种子拷成
        # db.run.json):运行态文件不得计入懒人轮门禁的工作证据。
        import os
        import tempfile
        import time as _time
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            run_file = out / "backend" / "data" / "db.run.json"
            run_file.parent.mkdir(parents=True)
            run_file.write_text("{}", encoding="utf-8")
            t = _time.time()
            os.utime(run_file, (t + 10, t + 10))  # 未来 mtime=必比 since 新
            self.assertFalse(main.project_files_changed(out, t))
            real = out / "frontend" / "src" / "app.jsx"
            real.parent.mkdir(parents=True)
            real.write_text("x", encoding="utf-8")
            self.assertTrue(main.project_files_changed(out, t))


class WorkOrderSpecTests(unittest.TestCase):
    """build_work_order_spec:批 turn 与 blocked-retry pass 共用的工单装配
    (此前 retry 侧手工复制且缺系统行 NOTE——漂移修复的回归钉)。"""

    def _spec(self, batch_ids, statuses=None, scope=None, tree=None):
        src = tree or TREE
        batch = [n for n in main.flatten_atomic(src)
                 if str(n["id"]) in batch_ids]
        return main.build_work_order_spec(
            src, batch, main.family_index(src), frozenset(),
            statuses or {}, scope)

    def test_sections_in_order_with_note_and_ledger(self):
        spec, anchor = self._spec(["REQ-2-1"],
                                  statuses={"REQ-2-1": "active"})
        # 段序:家族契约 → 依赖闭包 → 批节点 → NOTE → 账本行
        self.assertLess(spec.index("THIS FAMILY"),
                        spec.index("CROSS-FAMILY"))
        self.assertLess(spec.index("CROSS-FAMILY"), spec.index("THIS BATCH"))
        self.assertLess(spec.index("THIS BATCH"), spec.index("NOTE:"))
        self.assertLess(spec.index("NOTE:"),
                        spec.index("PROGRESS (family ledger"))
        # 家族契约逐字 + 依赖闭包逐字 + 批节点逐字,NOTE 恒在场
        # (blocked-retry 此前缺 NOTE 的漂移由此钉住)
        self.assertIn("family two module contract", spec)
        self.assertIn("family one module contract", spec)
        self.assertIn("d21", spec)
        self.assertEqual(anchor, "the VERIFICATION REQUIREMENTS text above")

    def test_anchor_forks_on_templated_batch(self):
        tree = json.loads(json.dumps(TREE))
        for n in main.flatten_atomic(tree):
            n["_scenarios_templated"] = True
        _, anchor = self._spec(["REQ-2-1"], tree=tree)
        self.assertIn("REQ CONTENT descriptions above", anchor)
        self.assertIn("template placeholders", anchor)

    def test_scope_limits_ledger_families(self):
        spec, _ = self._spec(["REQ-1-1-1"], scope=frozenset({"REQ-1"}))
        self.assertIn("REQ-1:pending", spec)
        self.assertNotIn("REQ-2:", spec)


if __name__ == "__main__":
    unittest.main(verbosity=1)
