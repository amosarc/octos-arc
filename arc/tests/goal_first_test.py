"""goal-first 转向单测(09-27 宿主优先→目标优先)。

覆盖:家族调度(group_families)、需求地图(render_requirements_map)、
家族契约逐字(describe_family_context)、goal-first 契约文案路径点名
(RULES_MUST_NAME 双断言)、envelope rules_text 覆写、红项 probe 过滤。

Run: python3 arc/goal_first_test.py
"""
from __future__ import annotations

import json
import os
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture_manifest  # noqa: E402
import gherkin  # noqa: E402
import main  # noqa: E402
import scaffold  # noqa: E402
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
        for task in ("hackathon-evolution--github",
                     "hackathon-evolution--sheet"):
            req = Path(__file__).resolve().parent.parent / "tasks" / task
            if not (req / "requirements.yaml").exists():
                continue
            tree = main.load_requirement_tree(req)
            rmap = main.render_requirements_map(tree)
            # 空白归一比较:evolution 双描述节点以 "Original Feature
            # Description" 起头,首 40 字符窗跨换行;地图渲染带缩进,
            # 逐字匹配误报——意图钉是"描述在场",非缩进形状。
            flat = " ".join(rmap.split())
            atomics = main.flatten_atomic(tree)
            lost = [str(n.get("id")) for n in atomics
                    if " ".join(str(n.get("description") or "")
                                .split())[:60] not in flat]
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


class TurnBlockTests(unittest.TestCase):
    """build_turn_features 统一块骨架:TASK → SEED → SCENARIOS(本批
    Feature 原文)。DEPENDENCIES/REUSED UI CONTRACT 依赖块已整体退役
    (装配与闭包机器全删);SEED CONTRACT 暂停装配待确认是否有用(代码
    保留,_TURN_SEED_CONTRACT 旋钮);SEED 行=种子自愈指令(_TURN_SEED_
    LINE,SCENARIOS 空时一并省略)。本批节点自己的 GIVEN 仍逐字(LAW 4,
    SeedOwnershipTests)。"""

    def _blob(self, batch_ids, tree=None):
        src_tree = tree or TREE
        batch = [n for n in main.flatten_atomic(src_tree)
                 if str(n["id"]) in batch_ids]
        parent_of: dict[str, dict] = {}

        def _walk(nd, parent):
            for c in nd.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type", "")).upper() == "ATOMIC":
                    parent_of[str(c.get("id"))] = parent
                else:
                    _walk(c, c)

        _walk(src_tree, src_tree)
        return main.build_turn_features(batch, parent_of)

    def test_only_task_and_scenarios_sections(self):
        # 退役后骨架:TASK → SEED → SCENARIOS;依赖/UI/种子引用三段
        # 一律不进场。TASK 行只列编号不列标题(id↔场景绑定由 `# <id>`
        # 注释承担);SEED 行=每 turn 种子自愈指令(批间无回种,修补
        # 必须导向持久面 db.json 而非活本)。
        blob = self._blob(["REQ-2-1"])
        self.assertIn("TASK: implement the requirements below (REQ-2-1)",
                      blob)
        self.assertNotIn("REQ-2-1 (", blob)
        self.assertIn("SEED: backend/data/db.json", blob)
        self.assertIn("delete backend/data/db.run.json", blob)
        # 10-06 REQ-4-4 doom 法医:契约只教删、没讲"删后重现=正常",
        # 模型把 e2e 重建的活本当异常,陷入删↔验乒乓(50s 13 连发
        # 被内核杀)。防循环句与删除指令同载体进场。
        self.assertIn("never loop on deleting and re-verifying", blob)
        self.assertLess(blob.index("TASK:"), blob.index("SEED:"))
        self.assertLess(blob.index("SEED:"),
                        blob.index("SCENARIOS (verbatim):"))
        self.assertIn("SCENARIOS (verbatim):", blob)
        self.assertNotIn("DEPENDENCIES:", blob)
        self.assertNotIn("REUSED UI CONTRACT", blob)
        self.assertNotIn("SEED CONTRACT", blob)

    def test_seeded_given_verbatim_in_scenarios(self):
        # SEED CONTRACT 暂停后,种子契约唯一载体=SCENARIOS 的 GIVEN 原文。
        tree = node("ROOT", "FOLDER", [
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="d21",
                     scenarios=[{"name": "s", "steps": [
                         {"keyword": "GIVEN",
                          "content": "The seeded data is account "
                                     "`alice-dev`, email `a@x.test`, "
                                     "password `pw`; repository "
                                     "`acme-docs`, branches `main` and "
                                     "`feature-search`; pull request "
                                     "`Improve onboarding`; check "
                                     "`test`."}]}])]),
        ])
        blob = self._blob(["REQ-2-1"], tree=tree)
        self.assertIn(
            "The seeded data is account `alice-dev`, email `a@x.test`, "
            "password `pw`; repository `acme-docs`, branches `main` and "
            "`feature-search`; pull request `Improve onboarding`; check "
            "`test`.", blob)
        self.assertNotIn("SEED CONTRACT", blob)

    def test_real_sheet_turn_sections(self):
        # sheet 真树烟测:装配面只剩 TASK+SCENARIOS。
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon-evolution--sheet"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("sheet task not present")
        tree = main.load_requirement_tree(req_dir)
        batch = main.group_families(tree)[0]
        blob = self._blob([str(n["id"]) for n in batch], tree=tree)
        self.assertIn("SCENARIOS (verbatim):", blob)
        self.assertNotIn("DEPENDENCIES:", blob)
        self.assertNotIn("REUSED UI CONTRACT", blob)
        self.assertNotIn("SEED CONTRACT", blob)


    def test_empty_scenarios_omits_field(self):
        # SCENARIOS 正文为空(批节点在父索引中缺席 → Feature 渲染为空)
        # → 整块省略(连同 SEED 行,"GIVEN lines below" 不悬空),不留
        # 悬空字段头;TASK 照常在场。
        batch = [n for n in main.flatten_atomic(TREE)
                 if str(n["id"]) in ("REQ-2-1",)]
        blob = main.build_turn_features(batch, parent_of={})
        self.assertIn("TASK: implement REQ-2-1", blob)
        self.assertNotIn("SCENARIOS (verbatim):", blob)
        self.assertNotIn("SEED:", blob)


class MyCheckExampleTests(unittest.TestCase):
    """MY-CHECK 示例仍随 e2e 骨架落盘,SOURCE RULE 钉住。"""

    def test_mycheck_example_carries_source_rule(self):
        # stage_selftest_dir 返回的是 root/e2e 子目录——曾经的
        # rmtree(stage) 只删子目录,空壳根目录留在 CWD(arc/ 下两例)。
        # 与其余调用点同风格:TemporaryDirectory 连根回收。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stage = scaffold.stage_selftest_dir(Path(td), 43101)
            text = (stage / "MY-CHECK.spec.ts.example").read_text()
            self.assertIn("SOURCE RULE", text)
            self.assertIn("never edit the spec to match", text)
            # 10-02 sheet 56/100 法医:组合框双驱动示例(点击式整族
            # 超时的教训进 GESTURE RULE;示例用中性词汇,禁任务味)。
            self.assertIn("combo box BOTH ways", text)
            self.assertIn("getByRole('combobox', { name: 'Field' }", text)
            # 10-06 github 全题 42/100 法医:自测导航全走 goto 捷径、
            # 只验便利态(登出态点 Acme Demo 全绿),判分器登录态行走
            # 全灭——WALK RULE 把"像首次使用者一样验证"钉进示例。
            self.assertIn("WALK RULE", text)
            self.assertIn("the session state the scenario starts in", text)
            self.assertIn("page.goto is only for the initial load", text)


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
        # 10-06 evolution 换代重锚:覆盖层 dependencies 全部悬空指向
        # 旧题 REQ(树外 id)→ 家族级依赖边全部消失 → SCC=7 个单家族组
        # (旧题的 2↔5 环随旧 yaml 退役;悬空优雅跳过正是本钉)。
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon-evolution--sheet"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("sheet task not present")
        tree = main.load_requirement_tree(req_dir)
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree),
                                            policy="scc")
        self.assertEqual(7, len(groups))
        self.assertEqual([[i] for i in range(7)], groups)

    def test_real_sheet_family_policy_five_groups(self):
        # family 档(默认):每家族一会话,文档序(悬空依赖零边)。
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon-evolution--sheet"
        if not (req_dir / "requirements.yaml").is_file():
            self.skipTest("sheet task not present")
        tree = main.load_requirement_tree(req_dir)
        batches = main.group_families(tree)
        groups = main.session_group_batches(tree, batches,
                                            main.family_index(tree))
        self.assertEqual([[i] for i in range(7)], groups)

    def test_real_github_singleton_groups_in_doc_order(self):
        req_dir = Path(__file__).resolve().parent.parent / "tasks" / \
            "hackathon-evolution--github"
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

    def test_system_row_map_scoped_to_run_families(self):
        # 范围 run:AGENTS.md(children map)只描述范围内家族
        with_scope = main.build_system_row_context(
            TREE, "rules", "fx", scope=frozenset({"REQ-1"}))
        self.assertIn("- REQ-1:", with_scope)
        self.assertNotIn("- REQ-2:", with_scope)
        self.assertNotIn("- REQ-3:", with_scope)
        # 全量 run:地图保持全家族
        without = main.build_system_row_context(TREE, "rules", "fx")
        self.assertIn("- REQ-1:", without)
        self.assertIn("- REQ-2:", without)
        self.assertIn("- REQ-3:", without)

    def test_map_outline_scope_keeps_family_from_atomic_scope(self):
        # 范围 id 是原子节点(REQ-1-1-1)⇒ 其顶层家族(REQ-1)整体保留
        outline = main.render_map_outline(
            TREE, scope=frozenset({"REQ-1-1-1"}))
        self.assertIn("- REQ-1:", outline)
        self.assertNotIn("- REQ-2:", outline)
        self.assertNotIn("- REQ-3:", outline)
        # 子家族 id(REQ-2-1)⇒ REQ-2 保留,REQ-1/REQ-3 淘汰
        outline = main.render_map_outline(
            TREE, scope=frozenset({"REQ-2-1"}))
        self.assertNotIn("- REQ-1:", outline)
        self.assertIn("- REQ-2:", outline)
        self.assertNotIn("- REQ-3:", outline)

    def test_map_outline_lists_family_direct_atomic_children(self):
        # evolution 任务形状:二级 FOLDER/ATOMIC 混挂——家族直挂的
        # ATOMIC=子家族同级特性,必须进地图(跳过=模型从地图看不见
        # 该特性,而 plan_envelope 只许读 map 映射的行号区间,导航
        # 面随之断裂)。更深层的 ATOMIC 仍随各自工单到达,不入图。
        tree = node("ROOT", "FOLDER", [
            node("REQ-1", "FOLDER", [
                node("REQ-1-1", "FOLDER", [
                    node("REQ-1-1-1", description="deep"),
                ], name="Sub Family"),
                node("REQ-1-2", description="direct atomic",
                     name="Direct Feature"),
            ], name="Family"),
        ])
        outline = main.render_map_outline(tree)
        self.assertIn("    REQ-1-2: Direct Feature", outline)
        self.assertNotIn("REQ-1-1-1:", outline)
        # AGENTS.md 装配面(build_system_row_context 地图段)同源
        ctx = main.build_system_row_context(tree, "rules", "fx")
        self.assertIn("REQ-1-2: Direct Feature", ctx)

    def test_map_outline_direct_atomic_carries_line_span(self):
        # 行号区间随二级 ATOMIC 给出——"读 ONLY mapped ranges"定点
        # 导航的主载荷
        tree = node("ROOT", "FOLDER", [
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="d21", name="D21"),
            ], name="F2"),
        ], description="root")
        tree["children"][0]["children"][0]["_lines"] = (12, 34)
        outline = main.render_map_outline(tree)
        self.assertIn("    REQ-2-1: D21 (lines 12-34)", outline)

    def test_family_stubs_scope_prunes_generation(self):
        # taskshape 家族 stub(误删恢复):家族宇宙=范围裁剪树,
        # REQ-1 范围只生成 req1 家族的 feature/route stub
        import main
        family_of = main.family_index(TREE)
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            main.scaffold_family_stubs(td, TREE, frozenset({"REQ-1"}),
                                       family_of)
            feats = td / "frontend" / "src" / "features"
            routes = td / "backend" / "src" / "routes"
            slugs = {p.stem for p in feats.glob("*.jsx")}
            self.assertEqual(len(slugs), 1)
            self.assertTrue(next(iter(slugs)).startswith("req1"))
            self.assertEqual(
                {p.stem for p in routes.glob("*.js")
                 if p.name != "index.js"}, slugs)
            # 头注释=该家族 REQ 工单清单
            head = (feats / f"{next(iter(slugs))}.jsx").read_text(
                encoding="utf-8")
            self.assertIn("REQ-1-1-1", head)
            # 幂等:重跑零新增(stub 只在文件不存在时写)
            main.scaffold_family_stubs(td, TREE, frozenset({"REQ-1"}),
                                       family_of)
            self.assertEqual(
                len(list(feats.glob("*.jsx"))), 1)

    def test_family_stubs_empty_scope_generates_all_families(self):
        import main
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            main.scaffold_family_stubs(td, TREE)
            slugs = {p.stem for p in (td / "frontend" / "src"
                                      / "features").glob("*.jsx")}
            self.assertEqual(len(slugs), 3)


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
        # 09-30 两形式:总纲 RULES 段已无 FIXTURE 散文,任务段唯一承载
        self.assertEqual(ctx.count("FIXTURE CONTRACT (graded"), 1)
        self.assertIn("Background:", ctx)
        self.assertIn("children map:", ctx)
        self.assertIn("RULES & ARCHITECTURE", ctx)
        # 需求源文件指针:children map 行号区间 + shell 直取(用户拍板)
        self.assertIn("requirements/requirements.yaml", ctx)
        # RULES_MUST_NAME 路径拼写不受影响(scaffold_test 钉在常量上)
        self.assertIn("backend/data/db.json", rules)

    def test_system_row_is_byte_stable(self):
        a = main.build_system_row_context(TREE, self.RULES,
                                          self.FIXTURE_RULES)
        b = main.build_system_row_context(TREE, self.RULES,
                                          self.FIXTURE_RULES)
        self.assertEqual(a, b)

    def test_load_strips_reference_image_marks(self):
        # 09-29 裁决:参考图记号在 yaml 载入时整体剥离——下游渲染器
        # (Gherkin/系统行/工单)天然无图,防线正则随之删除。
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
                "    name: N1\n"
                "    type: FOLDER\n"
                "    description: |\n"
                "      Build it.\n"
                "      Page reference:\n"
                "      ![image](reference/sort.png)\n"
                "    children:\n"
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
        ctx = main.build_system_row_context(tree, "rules", "fx")
        feats = gherkin.feature_files(tree)
        blob = "\n".join(f["text"] for f in feats)
        for text in (blob, ctx):
            self.assertNotIn("![", text)
            self.assertNotIn("](reference/", text)
        self.assertNotIn("Page reference:", blob)
        # 标签变体同律剥离(09-30:github 真实 yaml 用 "Screenshot
        # reference:",旧正则只认 Page 变体,26/47 节点留悬空标签)。
        self.assertNotIn("Screenshot reference:", blob)
        self.assertIn("Global rules.", ctx)
        self.assertIn("Build it.", blob)
        # 原子节点锚与 Flow contract(description 剥图后)在场;N3 无
        # scenarios,不渲染空 Scenario 行(10-02 全量恢复后的形态)。
        self.assertIn("# REQ-1-3", blob)
        self.assertIn("# Flow contract: Shot body.", blob)

    def test_prune_reference_imagery_blind_lane(self):
        # 10-01 拍板:盲 lane 不读参考截图——workspace 副本图片删除、
        # requirements.yaml 记号原地剥成与 host 渲染一致;非图片文件
        # 保留,剥后 load 幂等。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            req = Path(td)
            (req / "requirements.yaml").write_text(
                "id: ROOT\nname: root\ntype: FOLDER\n"
                "description: Global rules.\n"
                "children:\n"
                "  - id: REQ-1\n"
                "    name: N1\n"
                "    type: ATOMIC\n"
                "    description: |\n"
                "      Build it.\n"
                "      Page reference:\n"
                "      ![image](reference/sort.png)\n",
                encoding="utf-8")
            ref = req / "reference"
            ref.mkdir()
            (ref / "sort.png").write_bytes(b"\x89PNG")
            (ref / "notes.txt").write_text("keep me", encoding="utf-8")
            removed = main.prune_reference_imagery(req)
            self.assertIn("reference/sort.png", removed)
            self.assertIn("requirements.yaml (page-reference marks)", removed)
            self.assertFalse((ref / "sort.png").exists())
            self.assertTrue((ref / "notes.txt").exists())
            self.assertTrue(ref.is_dir())  # 非图片文件在,目录不收
            raw = (req / "requirements.yaml").read_text(encoding="utf-8")
            self.assertNotIn("Page reference:", raw)
            self.assertNotIn("](reference/", raw)
            self.assertIn("Build it.", raw)
            # 剥后 load 正常且幂等(再次 prune 无事可做)。
            tree = main.load_requirement_tree(req)
            self.assertTrue(tree.get("children"))
            self.assertEqual(main.prune_reference_imagery(req), [])

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
        # db.run.json 不交付的义务由任务 FIXTURE 段承载(总纲 Hygiene
        # 行已退役,用户拍板)
        self.assertIn("db.run.json) never ships", text)
        self.assertTrue(text.endswith("."), text[-80:])
        self.assertLess(len(text), fixture_manifest._MAX_EXTRA_RULES)


class GoalFirstRulesTests(unittest.TestCase):
    def test_goal_first_rules_name_every_contract_path(self):
        for name in sc.RULES_MUST_NAME:
            self.assertIn(
                name, plan_envelope.HOST_STACK_RULES_GOAL_FIRST,
                f"goal-first rules missing {name!r}")

    def test_rules_carry_laws_and_architecture(self):
        """09-30 两形式定则:AGENTS.md 第三段 = 行为总纲 + 架构说明。"""
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("RULES & ARCHITECTURE", rules)
        self.assertIn("The requirement text wins", rules)
        # 拒绝接地:只按原文拒绝,无引用句=中性默认(注释机制已退役)
        self.assertIn("only where the text states it", rules)
        self.assertIn("must SUCCEED with a neutral default", rules)
        self.assertIn("Every GIVEN line is a binding pre-state", rules)
        self.assertIn("no two controls share an accessible name", rules)
        self.assertIn("Never regress an already-green family", rules)
        # Hygiene 一刀切禁令(npm install/commit)整体退役,不回流
        self.assertNotIn("npm install", rules)
        self.assertNotIn("never commit", rules)
        # 架构说明零禁令:需求要求基座没提供的能力时就该改
        self.assertIn("when the requirement needs more than it provides, "
                      "change it", rules)
        self.assertNotIn("never rewrite", rules)
        self.assertNotIn("never touch", rules)
        # 死目录不复流:TOOLBOX 逐助手目录/宿主代述播种
        self.assertNotIn("TOOLBOX", rules)
        self.assertNotIn("AccountTrigger", rules)
        self.assertNotIn("org-owner", rules)
        # 禁回流串(历史定则)
        self.assertNotIn("SCHEMA-DRIVEN", rules)
        self.assertNotIn("engine.js", rules)
        self.assertNotIn("schema.json", rules)
        self.assertNotIn("SERVER-SIDE VALIDATION", rules)
        self.assertNotIn("UI CONTRACT HYGIENE", rules)

    def test_stage_writes_config_example_and_host_walk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stage = scaffold.stage_selftest_dir(Path(td), 43101)
            names = sorted(p.name for p in stage.iterdir())
            self.assertEqual(
                names, [".gitignore", "MY-CHECK.spec.ts.example",
                        "_host-walk.json", "_host-walk.spec.ts",
                        "node_modules", "playwright.config.ts"])
            # node_modules 桥接是相对软链(REQ-6 doom 根因的暂存期闭环),
            # backend 依赖安装前悬空无害
            self.assertTrue((stage / "node_modules").is_symlink())
            cfg = (stage / "playwright.config.ts").read_text()
            self.assertIn("http://127.0.0.1:43101", cfg)
            self.assertIn("workers: 1", cfg)
            # 宿主只写 _host-walk(零任务知识行走);其余 spec 均为模型自写
            self.assertFalse(
                any(n.endswith(".spec.ts") and n != "_host-walk.spec.ts"
                    for n in names))

    def test_host_walk_spec_stages_entity_names(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            stage = scaffold.stage_selftest_dir(
                Path(td), 43101, entity_names=["acme-docs", "第二工作簿"])
            names = json.loads(
                (stage / "_host-walk.json").read_text(encoding="utf-8"))
            self.assertEqual(names, ["acme-docs", "第二工作簿"])
            spec = (stage / "_host-walk.spec.ts").read_text()
            self.assertIn("_host-walk.json", spec)
            self.assertIn("seed entity not visible anywhere on the walk",
                          spec)
            # 无实体名时 walk 退化为纯 page-error 检查,spec 仍落盘
            stage2 = scaffold.stage_selftest_dir(Path(td), 43101,
                                                 entity_names=[])
            self.assertEqual(
                json.loads(
                    (stage2 / "_host-walk.json").read_text("utf-8")), [])

    def test_rules_carry_selftest_surface(self):
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("Self-tests live ONLY under e2e/", rules)
        self.assertIn("config: e2e/playwright.config.ts", rules)
        self.assertNotIn("SELF-TEST CONTRACT", rules)

    def test_frontend_rules_pin_accessible_name_uniqueness(self):
        # 09-30 法医:登录页双 "Sign in" → 评测器 strict 定位器歧义即死;
        # 义务钉在 RULES & ARCHITECTURE LAW 7。
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("no two controls share an accessible name", rules)
        self.assertIn("ambiguity is an APP defect", rules)

    def test_rules_pin_walk_fidelity_and_instant_nav(self):
        # 10-03 评测法医:自测 spec 插步(改密后私加 Sign out 三步)遮蔽
        # 应用缺口(会话不销毁)+漏走 back-nav 腿 → 4 挂;helper 在
        # Loading 态找 tab → 2 挂。三律+完成锚从句钉死。
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("replay each scenario's WHEN steps in order", rules)
        self.assertIn("never detour the walk", rules)
        self.assertIn("gets its own walk in the spec", rules)
        self.assertIn("Navigation chrome", rules)
        self.assertIn("never gated behind a data fetch", rules)
        turn = main.GOAL_FIRST_TURN_COMPLETION.format(smoke=3100)
        self.assertIn("each walk replaying its scenario's steps exactly "
                      "as written", turn)

    def test_rules_pin_text_identity_and_description_coverage(self):
        # 10-03 sheet 法医:工作簿名面包屑+页头双渲染 → grader
        # getByText strict×2 炸(5 挂);导出公式求值空上下文=描述规范句
        # 未被自测覆盖(1 挂)。文本身义一律+描述句断言义务钉死。
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("at most once per page as visible text", rules)
        self.assertIn("breadcrumb crumb or page heading, never both", rules)
        self.assertIn("dies on the second match", rules)
        self.assertIn("gets its own assertion in your spec", rules)
        self.assertIn("a rule never verified", rules)
        # ARCHITECTURE 实体页行:or 硬化为排他
        self.assertIn("header breadcrumb or page title — never both", rules)

    def test_rules_pin_home_reachability(self):
        # 10-03 github-stage-03 0/41 法医:评分器行走=点可见导航目标,
        # search-only 首页零可点入口 → 40/41 全灭(评测窗零 search/零
        # 仓库 fetch);种子数据写进去就是为了被使用,入口必须可见可达。
        # 10-06 github 全题 42/100 法医:模型只给登出态建了实体目录,
        # 登录态首页缩成仓库面板 → 判分器登录后行走全灭;可达律补
        # 会话态不变式(登录不得缩小登出态可见的入口面)。
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("reachable from the application home page by "
                      "clicking visible links or tabs", rules)
        self.assertIn("in the session state each scenario starts from",
                      rules)
        self.assertIn("signing in never hides an entry", rules)
        self.assertIn("a search box or a typed URL is not an entry point",
                      rules)
        self.assertIn("seeded data exists to be used", rules)

    def test_rules_pin_grader_walk_mechanics(self):
        # 10-03 template-3 法医:首页 slug≠yaml 引号名"Acme Demo"→
        # REQ-2-1-1 全灭;下拉菜单打开后异步 fetch 重插菜单项→点击落空,
        # REQ-2-1-2/REQ-2-2 十一连超时;裸 <select> 判分器点不动→
        # REQ-2-3 卡死;Sign in 登录后仍显示→会话态不可读。四律+架构
        # home 行硬化钉死。
        rules = plan_envelope.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("never gated behind a data fetch or rebuilt on "
                      "open", rules)
        self.assertIn("a rebuilt item is a detached miss", rules)
        self.assertIn("scaffold's NativeSelect", rules)
        self.assertIn("never a bare <select>", rules)
        self.assertIn("the name the requirement quotes for it", rules)
        self.assertIn("the display name when one exists, never a slug or "
                      "id", rules)
        self.assertIn("signed in shows the account menu and no Sign in",
                      rules)
        self.assertIn("signed out shows Sign in and no account menu", rules)
        # 架构 home 行已回缩为结构事实(义务由引号名律承载)
        self.assertIn("renders on it as a clickable row or link", rules)

    def test_seed_entity_names_cover_small_collections(self):
        # 10-03 template-3 法医:旧行"行数最多两集合×各前3"把
        # organizations(1 行)挤掉,walk 只查 repo/team 名——判分器点
        # "Acme Demo" 全灭而宿主行走全绿。改全集合覆盖(仍零语义:只读
        # name/title,username 行自然跳过)。
        cols = {"organizations": [{"name": "Acme Demo"}],
                "repositories": [{"name": "acme-docs"},
                                 {"name": "secret-research"}],
                "teams": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
                "members": [{"username": "bob"}]}
        names = main._seed_entity_names(cols)
        self.assertIn("Acme Demo", names)
        self.assertEqual(len(names), 6)
        self.assertNotIn("bob", names)

    def test_browser_script_guard_is_not_armed_by_default(self):
        # 09-30 拍板:Playwright 用法全面放开——env 不再默认武装内核
        # guard(代码保留,运维可显式 re-enable)。
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = main.build_octos_env(Path(tmp))
        self.assertNotIn("OCTOS_DENY_BROWSER_SCRIPTS", env)

    def test_scenario_level_boilerplate_detection_retired(self):
        # 10-02 全量恢复拍板:"the requested workflow" 是"所请求的工作流"
        # (LLM 须按顺序走完的测试本体),占位判定是误读——节点级/场景级
        # 检测器整链退役,须保持删除状态(防回退);渲染恒全场景全步骤。
        self.assertFalse(hasattr(main, "_mark_boilerplate_scenarios"))
        self.assertFalse(hasattr(main, "_mark_templated_scenarios"))
        self.assertFalse(hasattr(main, "_node_scenarios_templated"))
        self.assertFalse(hasattr(main, "_norm_step_skeleton"))
        self.assertFalse(hasattr(main, "_iter_atomic_nodes"))

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
                    "node cli.js test --config ./e2e/playwright.config.ts req-1.spec.ts",
                    "  1 passed (2.0s)"),
                # 同 spec 红了又修绿:末次结果必须覆盖首次
                row("2026-09-30 10:01:00",
                    "node cli.js test --config ./e2e/playwright.config.ts req-1.spec.ts",
                    "  1 failed │ 0 passed (2.0s)"),
                row("2026-09-30 10:02:00",
                    "node cli.js test --config ./e2e/playwright.config.ts req-1.spec.ts",
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

    def test_prompt_templates_format_without_keyerror(self):
        # 模板字面量花括号必须保持 {{}} 转义(存量 KeyError 雷)。两形式:
        # TURN = {features}/{smoke} 两个槽;BOOTSTRAP 前言无槽。
        turn = main.GOAL_FIRST_TURN_COMPLETION.format(smoke=3100)
        self.assertIn("generate Playwright automation tests", turn)
        pre = main.BOOTSTRAP_PREAMBLE
        self.assertIn("seed", pre)
        self.assertNotIn("schema.json", turn + pre)

    def test_gateway_override_excludes_phantom_tools(self):
        # 09-29 减法重构(最大单项):基础提示词与 6 工具 allowlist 对齐
        # ——Subagents/Memory 整节与全部幻影工具名出局(update_plan
        # 10-01 退役:弱 lane 把它当伪进度出口,零值)。
        text = main._GATEWAY_PROMPT_OVERRIDE
        for tool in ("read_file", "edit_file", "write_file", "grep",
                     "list_dir", "bash"):
            self.assertIn(f"`{tool}`", text, f"missing real tool {tool}")
        for phantom in ("spawn", "read_task_output", "check_background_tasks",
                        "save_memory", "recall", "ask_user_question",
                        "glob", "code_structure", "diff_edit", "apply_patch",
                        "view_image"):
            self.assertNotIn(phantom, text, f"phantom tool {phantom} leaked")
        self.assertNotIn("## Subagents", text)
        self.assertNotIn("## Memory", text)
        self.assertNotIn("Match the user's language", text)
        # 09-30:防伪造句折叠进身份与 Tool Discipline(Verification/Communication 段退役)
        self.assertIn("Never claim or fabricate results you did not run",
                      text)
        self.assertIn("Read before write", text)

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
        # 10-03 拍板:工作/修复两档同 max(工作轮推理强度优先),分档
        # 管道仍由上面两条 config 断言钉死;两档必须不同的人工律退役。
        self.assertEqual(main.REASONING_EFFORT, "max")
        self.assertEqual(main.REASONING_EFFORT_REPAIR, "max")

    def test_describe_node_data_only_mode_drops_then(self):
        nd = node(
            "REQ-8-8-8", "n", "desc text",
            scenarios=[{"name": "s1", "steps": [
                {"keyword": "GIVEN", "content": "a seeded workbook"},
                {"keyword": "WHEN", "content": "user clicks X"},
                {"keyword": "THEN", "content": "Y shows"}]}])
        full = gherkin.render_scenarios(nd, "full")
        self.assertIn("When user clicks X", full)
        data_only = gherkin.render_scenarios(nd, "data-only")
        self.assertNotIn("When user clicks X", data_only)
        self.assertNotIn("Then Y shows", data_only)
        self.assertIn("Given a seeded workbook", data_only)

    def test_workflow_reference_steps_render_verbatim(self):
        # 10-02 全量恢复:步骤里的 "the requested workflow" 是"所请求的
        # 工作流"——LLM 须按顺序走完的测试本体,逐字进场,不再判占位。
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
        rendered = gherkin.render_scenarios(nd, "full")
        self.assertIn("When the requested workflow", rendered)
        self.assertIn("the requested workflow remains persisted",
                      rendered)
        self.assertNotIn("platform placeholder boilerplate", rendered)
        # GIVEN 数据契约照常在场。
        self.assertIn("seed contains workbook `Q3`", rendered)
        # 干净真步骤照常逐字(真步骤的合法重复不受任何检测牵连)。
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
        self.assertIn("When user clicks the save button",
                      gherkin.render_scenarios(clean, "full"))

    def test_mycheck_example_is_task_neutral(self):
        # 09-28 审计 P1:示例曾内嵌 sheet 任务串('Q3 Sales'/'Pivot table
        # editor'/'Workbook name'),每个任务都会收到别的任务的先验。
        # LOCATOR/GESTURE 仪器规则(B 级)保留;任务词汇不得回流。
        # 09-30 追加:gridcell A1/C5/B3 同属 sheet 味——换 role 中性形。
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            scaffold.stage_selftest_dir(Path(td), 43101)
            text = (Path(td) / "e2e" / "MY-CHECK.spec.ts.example"
                    ).read_text(encoding="utf-8")
        for word in ("Q3 Sales", "Pivot table", "Workbook name", "pivot",
                     "gridcell", "'A1'", "'C5'", "'B3'"):
            self.assertNotIn(word, text)
        for keep in ("LOCATOR RULE", "GESTURE RULE", "getByRole('region'",
                     "getByRole('option'", "Shift",
                     "PAGE-ERROR RULE", "pageerror", "unhandledrejection",
                     "WALK RULE",
                     "expect(pageErrors.concat(rejections)"):
            self.assertIn(keep, text)

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
    """build_turn_features:批 turn 与 blocked-retry pass 共用的 TURN 装配
    (统一块骨架:TASK → SCENARIOS 本批 Feature 原文)。"""

    def _spec(self, batch_ids, tree=None):
        src_tree = tree or TREE
        batch = [n for n in main.flatten_atomic(src_tree)
                 if str(n["id"]) in batch_ids]
        parent_of: dict[str, dict] = {}

        def _walk(node, parent):
            for c in node.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type", "")).upper() == "ATOMIC":
                    parent_of[str(c.get("id"))] = parent
                else:
                    _walk(c, c)

        _walk(src_tree, src_tree)
        return main.build_turn_features(batch, parent_of)

    def test_sections_in_order(self):
        spec = self._spec(["REQ-2-1"])
        # 段序:TASK → SCENARIOS(本批 Feature);NOTE/账本废话已删
        self.assertLess(spec.index("TASK:"),
                        spec.index("SCENARIOS (verbatim):"))
        self.assertLess(spec.index("SCENARIOS (verbatim):"),
                        spec.index("Feature:"))
        self.assertNotIn("DEPENDENCIES:", spec)
        self.assertNotIn("REUSED UI CONTRACT", spec)
        self.assertNotIn("SEED CONTRACT", spec)
        self.assertNotIn("NOTE:", spec)
        self.assertNotIn("PROGRESS (family ledger", spec)


class RuntimeSeedResetTests(unittest.TestCase):
    """验证性 boot 的探测基线=种子契约:宿主每次彩排/Tier-2 boot 前删
    db.run.json——模型自测留下的运行态(如 REQ-1-1-3 场景按 GIVEN 改掉
    的种子账号密码)不得让 login-probe 把「需求要求的旧行为」(旧密码
    登录失败)误判成 declared seed/auth 损坏而进修复轮(09-30 用户核验:
    Tier-2 两路径本就删,Tier-1 彩排补齐)。"""

    def test_reset_removes_run_file_and_tolerates_absence(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as td:
            out = Path(td)
            run_db = out / "backend" / "data" / "db.run.json"
            run_db.parent.mkdir(parents=True)
            run_db.write_text("{}", encoding="utf-8")
            main._reset_runtime_seed(out)
            self.assertFalse(run_db.exists())
            main._reset_runtime_seed(out)  # 缺席不抛
            # 种子文件本身不动(它是交付原件)
            self.assertFalse((out / "backend" / "data" / "db.json").exists())


class DumpRuntimePromptsTests(unittest.TestCase):
    """真实运行时上下文采集(main.dump_runtime_prompts):唯一数据源=内核
    llm.jsonl(OCTOS_CONTEXT_LOG=1 常开,逐字节 messages);产出
    prompts/{system-prompt,AGENTS,turn-NNN}.md。"""

    REQ_LINE = ('{"kind":"llm_request","seq":%d,"iteration":%d,'
                '"messages":[{"role":"system","content":%s},'
                '{"role":"user","content":%s},'
                '{"role":"assistant","content":"ok"}]}')

    @staticmethod
    def _sys_text():
        return ("gateway base text\n\nCurrent date: 2026-09-30\n\n"
                "## AGENTS.md\n\nROOT CONTRACT (verbatim)\nchildren map\n"
                "\n## TOOL USE DISCIPLINE\nnever claim results")

    def _write_llm(self, root: Path, run_id: str, rows) -> None:
        d = root / ".octos" / "context-logs" / run_id
        d.mkdir(parents=True)
        body = "\n".join(
            self.REQ_LINE % (seq, it, json.dumps(sys_text),
                             json.dumps(user))
            for seq, it, sys_text, user in rows) + "\n"
        (d / "llm.jsonl").write_text(body, encoding="utf-8")

    def _fake_workspace(self, td: Path) -> None:
        sys_text = self._sys_text()
        # 会话 A:两个回合(iteration 1 各一条;iteration 2 忽略)
        self._write_llm(td, "20260930-100000-1", [
            (1, 1, sys_text, "TASK: implement REQ-1 (alpha)."),
            (2, 2, sys_text, "TASK: implement REQ-1 (alpha)."),
            (3, 1, sys_text, "TASK: implement REQ-2 (beta)."),
        ])
        # 会话 B(修复会话):一个回合;另一 run_id 坏行跳过
        self._write_llm(td, "20260930-110000-9", [
            (50, 1, sys_text, "REHEARSAL REPAIR: build failed"),
        ])
        bad = td / ".octos" / "context-logs" / "20260930-090000-0"
        bad.mkdir()
        (bad / "llm.jsonl").write_text(
            "{not json}\n{\"kind\":\"llm_response\",\"seq\":9}\n",
            encoding="utf-8")
        # 会话 A 先动、会话 B 后动(mtime 序=B 在后)
        import os as _os
        import time as _time
        a = td / ".octos" / "context-logs" / "20260930-100000-1" / "llm.jsonl"
        b = td / ".octos" / "context-logs" / "20260930-110000-9" / "llm.jsonl"
        t0 = _time.time() - 60
        _os.utime(a, (t0, t0))

    def test_full_capture_three_kinds(self):
        import main
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            self._fake_workspace(td)
            summary = main.dump_runtime_prompts(td)
            prompts = td / "octos" / "prompts"
            self.assertEqual(summary["turns"], 3)
            self.assertTrue(summary["system_prompt"])
            self.assertTrue(summary["agents_md"])
            sys_out = (prompts / "system-prompt.md").read_text(
                encoding="utf-8")
            self.assertEqual(sys_out, self._sys_text())
            agents = (prompts / "AGENTS.md").read_text(encoding="utf-8")
            # 段提取含包裹头、止于下一个 "## " 头
            self.assertTrue(agents.startswith("## AGENTS.md"))
            self.assertIn("ROOT CONTRACT (verbatim)", agents)
            self.assertNotIn("TOOL USE DISCIPLINE", agents)
            # turn 顺序:会话 A 两回合 → 会话 B 一回合;iteration 2 忽略
            t1 = (prompts / "turn-001.md").read_text(encoding="utf-8")
            t2 = (prompts / "turn-002.md").read_text(encoding="utf-8")
            t3 = (prompts / "turn-003.md").read_text(encoding="utf-8")
            self.assertIn("REQ-1 (alpha)", t1)
            self.assertIn("REQ-2 (beta)", t2)
            self.assertIn("REHEARSAL REPAIR", t3)
            self.assertEqual(
                sorted(p.name for p in prompts.glob("turn-*.md")),
                ["turn-001.md", "turn-002.md", "turn-003.md"])

    def test_no_context_logs_is_noop_never_raises(self):
        import main
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            summary = main.dump_runtime_prompts(td)
        self.assertEqual(summary, {"turns": 0, "system_prompt": False,
                                   "agents_md": False})
        self.assertFalse((Path(td_s) / "octos" / "prompts").exists())


    def test_mirror_hidden_logs_copies_arc_and_context_logs(self):
        # 点前缀目录(.arc/.octos)平台取回会丢:镜像进 octos/logs/ 后齐全
        import main
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            arc = td / ".arc" / "traceability"
            arc.mkdir(parents=True)
            (td / ".arc" / "debug.log").write_text("dbg", encoding="utf-8")
            (arc / "scenarios.json").write_text("{}", encoding="utf-8")
            self._write_llm(td, "20261001-130000-1",
                            [(1, 1, "sys", "user")])
            counts = main._mirror_hidden_logs(td)
            self.assertEqual(counts["arc_files"], 2)
            self.assertEqual(counts["context_log_files"], 1)
            logs = td / "octos" / "logs"
            self.assertTrue((logs / "arc" / "debug.log").is_file())
            self.assertTrue(
                (logs / "arc" / "traceability" / "scenarios.json").is_file())
            self.assertTrue((logs / "context-logs" / "20261001-130000-1"
                             / "llm.jsonl").is_file())
            # 幂等:重跑计数不变、文件仍在
            self.assertEqual(main._mirror_hidden_logs(td),
                             {"arc_files": 2, "context_log_files": 1})

    def test_mirror_hidden_logs_noop_without_hidden_dirs(self):
        import main
        with tempfile.TemporaryDirectory() as td_s:
            counts = main._mirror_hidden_logs(Path(td_s))
        self.assertEqual(counts, {"arc_files": 0, "context_log_files": 0})

    def test_agents_fallback_to_profile_file_when_section_missing(self):
        import main
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            self._write_llm(td, "r1", [
                (1, 1, "system prompt without any section header",
                 "TASK: x")])
            profile = (td / "octos" / "state" / "profiles" / "p1" / "data")
            profile.mkdir(parents=True)
            (profile / "AGENTS.md").write_text(
                "injected system row", encoding="utf-8")
            summary = main.dump_runtime_prompts(td)
            self.assertTrue(summary["agents_md"])
            self.assertEqual(
                (td / "octos" / "prompts" / "AGENTS.md").read_text(
                    encoding="utf-8"),
                "injected system row")

    def test_idempotent_rebuild_and_consecutive_dedupe(self):
        import main
        with tempfile.TemporaryDirectory() as td_s:
            td = Path(td_s)
            # 同一回合重试 → 两条相同 iteration-1 行,只计一个 turn
            self._write_llm(td, "r1", [
                (1, 1, self._sys_text(), "TASK: retry me"),
                (2, 1, self._sys_text(), "TASK: retry me"),
                (3, 1, self._sys_text(), "TASK: next turn")])
            main.dump_runtime_prompts(td)
            main.dump_runtime_prompts(td)  # 幂等:整体重建,不翻倍
            prompts = td / "octos" / "prompts"
            self.assertEqual(sorted(p.name for p in prompts.glob("turn-*")),
                             ["turn-001.md", "turn-002.md"])
            self.assertIn("TASK: retry me",
                          (prompts / "turn-001.md").read_text(
                              encoding="utf-8"))


class NodeIsolationTests(unittest.TestCase):
    """节点级 spec 证据桥=感知层(10-03 重审定则)。

    只读:失败批尾一行节点裁决,回答"blocked 批到底落了什么"(法医
    面)。requeue 执行层同日重审撤销——A/C/B 续跑梯子已含 C 级新会话
    重跑本批,触发源已被 run-e2e.sh 根修,且粒度收益场景(半程
    blocked 批)真实 run 未出现、聚合证据可能重做已绿节点。
    BOOTSTRAP 诚实钉同轮落地:种子义务批次化,in-scope 全量承诺禁
    回流(最小读取纪律)。"""

    def test_node_spec_evidence_verdicts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            e2e = root / "e2e"
            e2e.mkdir()
            (e2e / "req6-2-1.spec.ts").write_text(
                "// REQ-6-2-1: Create PR scenario\ntest('a', () => {});")
            (e2e / "req6-2-2.spec.ts").write_text(
                "// REQ-6-2-2: Edit PR scenario\ntest('b', () => {});")
            (e2e / "_host-walk.spec.ts").write_text(
                "// REQ-6-2-3 must NOT attribute via the host walk")
            evidence = {"specs": {
                "e2e/req6-2-1.spec.ts": {"passed": 2, "failed": 0,
                                         "ts": "t1"},
                "e2e/req6-2-2.spec.ts": {"passed": None, "failed": 3,
                                         "ts": "t2"},
            }}
            v = main._node_spec_evidence(
                root, ["REQ-6-2-1", "REQ-6-2-2", "REQ-6-2-3"], evidence)
            self.assertEqual(v["REQ-6-2-1"]["verdict"], "done")
            self.assertEqual(v["REQ-6-2-2"]["verdict"], "blocked")
            self.assertEqual(v["REQ-6-2-3"]["verdict"], "unattempted")
            self.assertEqual(v["REQ-6-2-1"]["specs"], ["req6-2-1.spec.ts"])

    def test_node_spec_evidence_without_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            e2e = root / "e2e"
            e2e.mkdir()
            (e2e / "req7-1.spec.ts").write_text("// REQ-7-1 never ran")
            v = main._node_spec_evidence(root, ["REQ-7-1"], None)
            self.assertEqual(v["REQ-7-1"]["verdict"], "unattempted")

    def test_bootstrap_seed_obligation_is_batch_honest(self):
        # 种子义务批次化(10-03 重审拍板):BOOTSTRAP 只承诺本批 GIVEN,
        # 后批由每批 SEED 行自愈(extend in place)——禁止 "in-scope
        # 全量承诺" 回流:它不可兑现且诱导 turn 1 通读全 yaml,违反
        # 最小读取纪律。
        self.assertIn("every GIVEN line below", main.BOOTSTRAP_PREAMBLE)
        self.assertNotIn("in-scope GIVEN line", main.BOOTSTRAP_PREAMBLE)
        self.assertIn("never rebuild it from scratch",
                      main.BOOTSTRAP_PREAMBLE)

    def test_prompt_states_nondeterministic_hash_and_reset_once(self):
        # 10-06 github 42/100 两真 doom 根因落句(context-logs 逐 iter
        # 还原):①store.hash 随机盐——同一密码每次输出不同,REQ-1-1-1
        # 模型 8 连复读求"稳定值"被杀;②db.run.json 删后重现=正常
        # (每 boot 从种子重拷),REQ-4-4 模型删↔验乒乓被杀。两信息
        # 缺口在诱发点旁(SEED 行/两 BOOTSTRAP)补齐。
        self.assertIn("differs on every call by design",
                      main.BOOTSTRAP_PREAMBLE)
        self.assertIn("compute each hash once and reuse the value",
                      main.BOOTSTRAP_PREAMBLE)
        self.assertIn("never loop on deleting and re-verifying",
                      main._TURN_SEED_LINE)
        ev = main.EVOLVE_BOOTSTRAP_PREAMBLE
        self.assertIn("differs on every call by design", ev)
        self.assertIn("never loop on deleting and re-verifying", ev)


class SeedOwnershipTests(unittest.TestCase):
    """09-30 定则:宿主播种退役——GIVEN 原样进上下文,种子(db.json)
    由模型按需求的 data: 段与 GIVEN 行实现;宿主只留 compile_seed 纯
    函数做探针/完整性基线。真凶回放:seeded GIVEN 曾被换成一行指针,
    演员断言离开模型上下文,REQ-2/3 整族死在登录第一步。"""

    def _parent_of(self, tree):
        parent_of: dict[str, dict] = {}

        def _walk(node, parent):
            for c in node.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type", "")).upper() == "ATOMIC":
                    parent_of[str(c.get("id"))] = parent
                else:
                    _walk(c, c)

        _walk(tree, tree)
        return parent_of

    def test_turn_features_keep_given_verbatim(self):
        # 09-30 定则:GIVEN 恒原文,不许指针化——演员断言必须在场。
        tree = node("ROOT", "FOLDER", [
            node("REQ-2", "FOLDER", [
                node("REQ-2-1", description="d",
                     scenarios=[{"name": "s", "steps": [
                         {"keyword": "GIVEN",
                          "content": "An organization Owner is signed in"},
                         {"keyword": "WHEN", "content": "act"}]}])],
        )])
        parent_of = self._parent_of(tree)
        batch = main.flatten_atomic(tree)
        blob = main.build_turn_features(batch, parent_of)
        self.assertIn("An organization Owner is signed in", blob)
        self.assertNotIn("seeded by host", blob)

    def test_bootstrap_preamble_carries_seed_obligation(self):
        pre = main.BOOTSTRAP_PREAMBLE
        self.assertIn("implement the seed", pre)
        self.assertIn("backend/data/db.json", pre)
        self.assertNotIn("never rewrite", pre)
        # 09-30 深夜:账号三义务从系统行迁入首 turn 前言(用户拍板
        # "turn 应该关注的")
        self.assertIn("exact emails and passwords", pre)
        self.assertIn("scrypt-hashed via store.hash", pre)
        self.assertIn("role phrases", pre)
        # seed-accounts.json/login-probe 是 rehearsal 退役后的幽灵机制
        # (宿主代码零消费方,10-02 拍板),不得回流任何提示词层。
        self.assertNotIn("seed-accounts", pre)
        self.assertNotIn("login-probe", pre)

    def test_github_given_verbatim_smoke(self):
        req_dir = (Path(__file__).resolve().parent.parent / "tasks"
                   / "hackathon-evolution--github")
        if not (req_dir / "requirements.yaml").exists():
            self.skipTest("github task file not present")
        tree = main.load_requirement_tree(req_dir)
        parent_of = self._parent_of(tree)
        batches = main.group_families(tree)
        hit = next(b for b in batches
                   if any(str(n.get("id")) == "REQ-2-2-1" for n in b))
        blob = main.build_turn_features(hit, parent_of)
        # 10-03 yaml 换代重锚:GIVEN 从 "Owner is signed in" 句式改写为
        # pre-provisions 句式;种子实质(账号+邮箱+密码+归属)逐字在场。
        self.assertIn("pre-provisions the verified account `org-owner`", blob)
        self.assertIn("org-owner@example.test", blob)
        self.assertNotIn("seeded by host", blob)


class VisualCheckTests(unittest.TestCase):
    """视觉核验轮(10-07,模型自截制):拍什么页、拍不拍归模型决定
    (MY-CHECK SCREENSHOT RULE 教手法);宿主只做跨轮机制——归集本批
    新图、帽内 FileRef 附回下一轮 User 消息,带图请求由 VISUAL_MODEL
    路由切视觉模型。内核零改动。"""

    def test_image_file_refs_shape_and_filters(self):
        import octos_stdio
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "home.png"
            png.write_bytes(b"\x89PNG\r\n")
            refs = octos_stdio.image_file_refs(
                [png, Path(tmp) / "missing.png", Path(tmp) / "notes.txt"])
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["mime"], "image/png")
            self.assertEqual(refs[0]["size_bytes"], 6)
            self.assertTrue(Path(refs[0]["path"]).is_absolute())
        self.assertEqual(octos_stdio.image_file_refs([]), [])

    def test_turn_start_params_media_wire_shape(self):
        import octos_stdio
        legacy = octos_stdio.build_turn_start_params("s1", "t1", "hello")
        self.assertEqual(legacy, {
            "session_id": "s1", "turn_id": "t1",
            "input": [{"kind": "text", "text": "hello"}]})
        self.assertNotIn("media", legacy)
        media = [{"path": "/tmp/x.png", "mime": "image/png",
                  "size_bytes": 3}]
        with_media = octos_stdio.build_turn_start_params(
            "s1", "t2", "look", media=media)
        self.assertEqual(with_media["media"], media)

    def test_collect_model_screenshots_mtime_and_cap(self):
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "octos" / "screenshots"
            new = root / "e2e" / "octos" / "screenshots"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            t0 = _time.time() - 60
            for name, parent, mtime in (
                    ("stale.png", old, t0 - 30),
                    ("a.png", old, t0 + 1),
                    ("b.png", new, t0 + 2),
                    ("c.txt", old, t0 + 3)):
                f = parent / name
                f.write_bytes(b"\x89PNG\r\n")
                os.utime(f, (mtime, mtime))
            shots = main._collect_model_screenshots(root, t0, cap=2)
            self.assertEqual([p.name for p in shots], ["b.png", "a.png"])
            # 帽是 token 治理:超帽截断归集面,不裁内容决策。
            self.assertLessEqual(len(shots), 2)

    def test_visual_check_prompt_and_media_contract(self):
        self.assertEqual(main._visual_check_prompt_and_media([]),
                         (None, []))
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "home.png"
            shot.write_bytes(b"\x89PNG\r\n")
            prompt, media = main._visual_check_prompt_and_media([shot])
            self.assertIsNotNone(prompt)
            self.assertIn("home.png", prompt)
            self.assertIn("YOU captured", prompt)
            self.assertIn("NEVER the scenario text", prompt)
            self.assertEqual(len(media), 1)

    def test_nav_visual_check_prompt_unique_source(self):
        # 唯一出处:dry-run 预览与真跑核验轮共用同一常量(文案改动
        # 两处同步,不许出现第二份手抄)。
        self.assertEqual(main.NAV_VISUAL_CHECK_PROMPT.count("{shots}"), 1)
        self.assertNotIn("{targets}", main.NAV_VISUAL_CHECK_PROMPT)
        self.assertNotIn("{misses}", main.NAV_VISUAL_CHECK_PROMPT)
        self.assertIn("scripts/run-e2e.sh _host-walk.spec.ts",
                      main.NAV_VISUAL_CHECK_PROMPT)
        self.assertIn("never the scenario text",
                      main._VISUAL_PIGGYBACK_NOTE)

    def test_mycheck_example_teaches_screenshot_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = scaffold.stage_selftest_dir(Path(tmp), 43101,
                                                entity_names=["X"])
            example = (stage / "MY-CHECK.spec.ts.example").read_text(
                encoding="utf-8")
        self.assertIn("SCREENSHOT RULE", example)
        self.assertIn("octos/screenshots/", example)
        self.assertIn("fullPage: true", example)
        # 机制边界:拍什么页由模型决定(定则:宿主不写死)。
        self.assertIn("decide yourself", example)

    def test_changed_source_paths_shared_filter(self):
        self.assertEqual(main._changed_source_paths([
            "frontend/src/App.jsx", "backend/src/routes/issues.js",
            "octos/screenshots/b01/home.png", "e2e/MY-CHECK.spec.ts",
            "frontend/index.html", "frontend/dist/index.html"]),
            ["frontend/src/App.jsx", "backend/src/routes/issues.js",
             "frontend/index.html"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
