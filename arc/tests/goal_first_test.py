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
                "![shot](reference/after.png)\n",
                encoding="utf-8")
            tree = main.load_requirement_tree(req)
        blob = main.render_verbatim_nodes(tree)
        ctx = main.build_system_row_context(tree, "rules", "fx")
        for text in (blob, ctx):
            self.assertNotIn("![", text)
            self.assertNotIn("](reference/", text)
        # 悬空的整行 "Page reference:" 一并消失,正文其余保留。
        self.assertNotIn("Page reference:", blob)
        self.assertIn("Global rules.", ctx)
        self.assertIn("Build it.", blob)
        self.assertIn("works", blob)

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

    def test_requirement_message_bridge_is_retired(self):
        # 09-28 CRUD 生成器退役:引号消息 sidecar 桥(_collect_requirement_
        # messages + req-messages.json)无消费方,须保持删除状态(防回退)。
        import scaffold as sc_pkg
        self.assertFalse(hasattr(main, "_collect_requirement_messages"))
        self.assertFalse(hasattr(main, "SCHEMA_DIRECTIVE"))
        self.assertFalse(hasattr(sc_pkg, "Watcher"))
        self.assertFalse(hasattr(sc_pkg, "compile_schema"))

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
        # {grid} 字面量未转义曾让每批工单组装 KeyError('grid')(存量雷,
        # 09-28 随 CRUD 生成器退役顺手修复)——模板字面量花括号必须
        # 保持 {{}} 转义,且后端所有权行指向 routes(生成器词汇不回流)。
        sk = main.GOAL_FIRST_SKELETON_PROMPT.format(port=3000, smoke=3100)
        self.assertIn("seed-accounts.json", sk)
        self.assertNotIn("schema.json", sk)
        node_p = main.GOAL_FIRST_NODE_PROMPT_TEMPLATE.format(
            node_id="REQ-1", node_spec="NS", port=3000, smoke=3100)
        self.assertIn("grid={grid}", node_p)
        self.assertIn("backend/src/routes/", node_p)
        self.assertNotIn("schema.json", node_p)

    def test_mycheck_example_is_task_neutral(self):
        # 09-28 审计 P1:示例曾内嵌 sheet 任务串('Q3 Sales'/'Pivot table
        # editor'/'Workbook name'),每个任务都会收到别的任务的先验。
        # LOCATOR/GESTURE 仪器规则(B 级)保留;任务词汇不得回流。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            main.stage_selftest_dir(Path(td), 43101)
            text = (Path(td) / "ui-smoke" / "MY-CHECK.spec.ts.example"
                    ).read_text(encoding="utf-8")
        for word in ("Q3 Sales", "Pivot table", "Workbook name", "pivot"):
            self.assertNotIn(word, text)
        for keep in ("LOCATOR RULE", "GESTURE RULE", "getByRole('region'",
                     "gridcell", "Shift"):
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


if __name__ == "__main__":
    unittest.main(verbosity=1)


class HiddenDirRelocationTests(unittest.TestCase):
    """postflight 隐藏目录收编(09-29):workspace 根散落的隐藏运行时目录
    统一收进 octos/,契约目录(.git/.arc)与可见交付件不动。"""

    def test_relocates_hidden_dirs_into_octos(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for d in (".octos", ".npm", ".cache"):
                (out / d).mkdir()
                (out / d / "f.txt").write_text("x", encoding="utf-8")
            for keep in (".git", ".arc"):
                (out / keep).mkdir()
                (out / keep / "contract.txt").write_text("keep",
                                                         encoding="utf-8")
            main._relocate_hidden_dirs(out)
            self.assertTrue((out / "octos/dot-octos/f.txt").is_file())
            self.assertTrue((out / "octos/dot-npm/f.txt").is_file())
            self.assertTrue((out / "octos/dot-cache/f.txt").is_file())
            for d in (".octos", ".npm", ".cache"):
                self.assertFalse((out / d).exists())
            # 契约目录原位保留
            self.assertTrue((out / ".git/contract.txt").is_file())
            self.assertTrue((out / ".arc/contract.txt").is_file())

    def test_collision_appends_counter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "octos" / "dot-npm").mkdir(parents=True)
            (out / "octos" / "dot-npm" / "old.txt").write_text(
                "old", encoding="utf-8")
            (out / ".npm").mkdir()
            (out / ".npm" / "new.txt").write_text("new", encoding="utf-8")
            main._relocate_hidden_dirs(out)
            self.assertTrue((out / "octos/dot-npm/old.txt").is_file())
            self.assertTrue((out / "octos/dot-npm-2/new.txt").is_file())
            self.assertFalse((out / ".npm").exists())

    def test_no_hidden_dirs_is_noop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "frontend").mkdir()
            main._relocate_hidden_dirs(out)
            self.assertFalse((out / "octos").exists())
