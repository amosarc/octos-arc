"""evolve 模式单测钉(10-06 设计文档 arc/docs/evolve-design.md §12)。

Run: python3 -m unittest tests.evolve_test
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import plan_envelope  # noqa: E402
import scaffold  # noqa: E402

TASKS = Path(__file__).resolve().parent.parent / "tasks"


class EvolveNarrativeTests(unittest.TestCase):
    def test_evolve_rules_replace_fresh_scaffold_claims(self):
        r = plan_envelope.render_rules(mode="evolve")
        self.assertIn("EXTEND it in place", r)
        self.assertIn("never delete or rename a baseline entity", r)
        self.assertIn("edit the existing route files in place", r)
        self.assertIn("surgical changes", r)          # 进化守则
        self.assertIn("Verify before building", r)    # 验证优先教义(统一策略)
        self.assertNotIn("ships EMPTY", r)            # create 假叙事不得进场
        self.assertNotIn("fill the stubs in place", r)

    def test_create_rules_unchanged(self):
        r = plan_envelope.render_rules(mode="create")
        self.assertIn("ships EMPTY", r)
        self.assertNotIn("surgical changes", r)
        self.assertEqual(plan_envelope.HOST_STACK_RULES_GOAL_FIRST, r)

    def test_turn_seed_line_evolve_suffix(self):
        tree = main.load_requirement_tree(
            TASKS / "hackathon-evolution--sheet")
        parent_of: dict = {}
        main._node_family_index(tree)

        def idx(node, parent):
            for c in node.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type", "")).upper() == "ATOMIC":
                    parent_of[str(c.get("id"))] = parent
                else:
                    idx(c, c)

        idx(tree, tree)
        batch = main.group_families(tree)[0]
        evo = main.build_turn_features(batch, parent_of, evolve=True)
        crt = main.build_turn_features(batch, parent_of, evolve=False)
        self.assertIn("never remove existing entities", evo)
        self.assertNotIn("never remove existing entities", crt)
        # 逐字保真面不受 evolve 影响:场景原文两版一致。
        self.assertIn("SCENARIOS (verbatim):", evo)


class VerifyBudgetNoteTests(unittest.TestCase):
    """收轮义务+共享面纪律(evolve 专属注记,批循环追加;sheet 轮 REQ-6
    实证:自报全量绿+宿主冷测 20 红——"至多一次"改"必须一次且全绿")。"""

    def test_obligation_and_discipline_present(self):
        n = main.EVOLVE_VERIFY_BUDGET_NOTE
        self.assertIn("run the FULL own suite ONCE, cold", n)
        self.assertIn("blocks the end of this turn", n)
        self.assertIn("first suspect", n)
        self.assertIn("SHARED-SURFACE DISCIPLINE", n)
        self.assertIn("scratch/debug spec files", n)

    def test_note_not_baked_into_turn_features(self):
        # 分层:工单正文不含预算注记(由批循环追加),create 工单天然不带。
        tree = main.load_requirement_tree(
            TASKS / "hackathon-evolution--sheet")
        parent_of: dict = {}

        def _idx(node, parent):
            for c in node.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type", "")).upper() == "ATOMIC":
                    parent_of[str(c.get("id"))] = parent
                else:
                    _idx(c, c)

        _idx(tree, tree)
        batch = main.group_families(tree)[0]
        blob = main.build_turn_features(batch, parent_of, evolve=True)
        self.assertNotIn("VERIFICATION BUDGET", blob)


class RoundPreambleTests(unittest.TestCase):
    def test_dossier_and_lessons_injected_only_when_present(self):
        with_dossier = main.build_round_preamble(
            "ISSUE-A: nav target X missing", "## Round 1\n- red")
        self.assertIn("ISSUE-A", with_dossier)
        self.assertIn("## Round 1", with_dossier)
        self.assertIn("EVOLVE ROUND START", with_dossier)
        self.assertIn("extend backend/data/db.json IN PLACE",
                      with_dossier)
        self.assertIn("NEVER", with_dossier)
        # 统一策略教义:验证优先,已绿不重复实现。
        self.assertIn("do NOT redo what walks green", with_dossier)
        empty = main.build_round_preamble("", "")
        self.assertNotIn("BASELINE ISSUES", empty)
        self.assertNotIn("LESSONS", empty)


class DanglingDependencyTests(unittest.TestCase):
    """sheet evolution 的 dependencies 悬空指向旧题 REQ——全链优雅跳过。"""

    def test_sheet_evolution_full_chain(self):
        tree = main.load_requirement_tree(
            TASKS / "hackathon-evolution--sheet")
        dangling = main._dangling_dependencies(tree)
        self.assertIn("REQ-1-2-2 -> REQ-1-1-1", dangling)
        batches = main.group_families(tree)
        fam_of = main.family_index(tree)
        parent_of: dict = {}

        def idx(node, parent):
            for c in node.get("children") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type", "")).upper() == "ATOMIC":
                    parent_of[str(c.get("id"))] = parent
                else:
                    idx(c, c)

        idx(tree, tree)
        for batch in batches:  # 全批工单装配无异常
            main.build_turn_features(batch, parent_of, evolve=True)
        groups = main.session_group_batches(tree, batches, fam_of)
        self.assertEqual(len(groups), 7)  # 悬空零边=7 单家族组


class NoWipePinTests(unittest.TestCase):
    """evolve 承诺的无清空钉:宿主装配面不触碰模型已有文件。"""

    def _pseudo_project(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="evolve-nowipe-"))
        (tmp / "backend/data").mkdir(parents=True)
        (tmp / "backend/src/routes").mkdir(parents=True)
        (tmp / "e2e").mkdir()
        (tmp / "scripts").mkdir()
        sentinel = {
            "backend/src/routes/index.js":
                "// model work\nexport default 1;\n",
            "backend/src/routes/req1.js": "// model req1 routes\n",
            "frontend/src/features/req1.jsx": "// model feature\n",
            "e2e/req1-identity.spec.ts": "// model spec\ntest('x', () => {});\n",
            "backend/data/db.json": '{"users": [{"u": "evo-owner"}]}',
        }
        for rel, body in sentinel.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        self._sentinel = sentinel
        return tmp

    def test_scaffold_and_stubs_never_overwrite_model_work(self):
        tmp = self._pseudo_project()
        tree = main.load_requirement_tree(
            TASKS / "hackathon-evolution--github")
        fam_of = main.family_index(tree)
        scaffold.scaffold(tmp)  # write-if-absent:只补缺失宿主件
        main.scaffold_family_stubs(tmp, tree, frozenset(), fam_of,
                                   log=lambda *a, **k: None)
        main.scaffold.stage_selftest_dir(tmp, 43101, subdir="e2e",
                                         entity_names=["Acme Demo"])
        for rel, body in self._sentinel.items():
            self.assertEqual(
                (tmp / rel).read_text(encoding="utf-8"), body,
                f"host assembly overwrote model file {rel}")
        # 宿主自有件落位(_host-walk 属宿主,不在哨兵面)。
        self.assertTrue((tmp / "e2e/_host-walk.spec.ts").exists())

    def test_detect_evolve_project(self):
        tmp = self._pseudo_project()
        self.assertTrue(main.detect_evolve_project(tmp))
        empty = Path(tempfile.mkdtemp(prefix="evolve-empty-"))
        self.assertFalse(main.detect_evolve_project(empty))
        no_seed = Path(tempfile.mkdtemp(prefix="evolve-noseed-"))
        (no_seed / "backend/src/routes").mkdir(parents=True)
        (no_seed / "e2e").mkdir()
        (no_seed / "e2e/req1.spec.ts").write_text("test();", encoding="utf-8")
        self.assertFalse(main.detect_evolve_project(no_seed))


class SuiteParsingTests(unittest.TestCase):
    def test_parse_playwright_json(self):
        # 真实形状(23:08 法医件实证):suite{title,file,specs} 嵌套,
        # spec{title,file,tests},test{status:expected/unexpected/…,
        # results[status: passed/failed]};test 无 title,标题=祖先
        # suite title 链 + spec.title。
        report = {
            "suites": [{
                "title": "", "file": "/w/e2e/req6-pr.spec.ts",
                "specs": [{
                    "title": "ok case",
                    "file": "/w/e2e/req6-pr.spec.ts",
                    "tests": [{"status": "expected",
                               "results": [{"status": "passed"}]}]},
                    {"title": "bad case",
                     "file": "/w/e2e/req6-pr.spec.ts",
                     "tests": [{"status": "unexpected",
                                "results": [{"status": "failed"}]}]}],
                "suites": [{
                    "title": "nested describe",
                    "specs": [{
                        "title": "flaky case",
                        "file": "/w/e2e/req5-issues.spec.ts",
                        "tests": [{"status": "flaky",
                                   "results": [{"status": "passed"},
                                               {"status": "failed"}]}]}],
                }],
            }]}
        passed, failed, titles, files, passed_titles = \
            main._parse_playwright_json(report)
        self.assertEqual((passed, failed), (1, 2))
        self.assertEqual(files, ["req6-pr.spec.ts", "req5-issues.spec.ts"])
        self.assertIn("nested describe › flaky case", titles)
        self.assertEqual(passed_titles, ["ok case"])

    def test_parse_real_postmortem_report(self):
        # 真 109+ 场景报告回归钉(轮 1 三次 0/0 的根因=词汇表错误)。
        f = (Path(__file__).resolve().parent.parent / "hackathon-projects"
             / "github" / "octos" / "evolve-verdicts" / "suite-postmortem")
        reports = sorted(f.glob("suite-zero-*.json")) if f.is_dir() else []
        if not reports:
            self.skipTest("no postmortem report on disk")
        rep = json.loads(reports[-1].read_text(encoding="utf-8"))
        passed, failed, titles, files, p_titles = \
            main._parse_playwright_json(rep)
        self.assertGreater(passed, 50)     # 该报告实测 118 过/3 挂
        self.assertEqual(passed + failed, 121)

    def test_own_suite_inventory_and_family(self):
        tmp = Path(tempfile.mkdtemp(prefix="evolve-inv-"))
        e2e = tmp / "e2e"
        e2e.mkdir()
        for name in ("req1-identity.spec.ts", "req6-change.spec.ts",
                     "_host-walk.spec.ts", "MY-CHECK.spec.ts.example",
                     "smoke-random.spec.ts"):
            (e2e / name).write_text("test();", encoding="utf-8")
        inv = main._own_suite_specs(tmp)
        self.assertEqual(inv, ["req1-identity.spec.ts",
                               "req6-change.spec.ts",
                               "smoke-random.spec.ts"])
        self.assertEqual(main._spec_family("req6-change.spec.ts"), "REQ-6")
        self.assertEqual(main._spec_family("req1-1-1-x.spec.ts"), "REQ-1")
        self.assertIsNone(main._spec_family("smoke-random.spec.ts"))

    def test_scenario_coverage(self):
        tree = {"children": [{
            "id": "REQ-1", "children": [{
                "id": "REQ-1-1", "type": "ATOMIC",
                "scenarios": [
                    {"name": "alpha flow"},
                    {"name": "beta flow"}]}]}]}
        cov = main._scenario_coverage(tree, ["spec › alpha flow done"])
        self.assertEqual((cov["covered"], cov["total"]), (1, 2))
        self.assertEqual(cov["by_family"]["REQ-1"],
                         {"covered": 1, "total": 2})


class LoopTests(unittest.TestCase):
    """evolve_loop 外层闭环:教训提炼/收窄/裁剪(fixture 驱动,零 LLM)。"""

    def _verdict(self):
        return main.EvolveVerdict(
            ok=False, round_index=1,
            batch_verdicts={"REQ-1-1-1": "done", "REQ-6-2-1": "blocked"},
            failed_nodes=["REQ-6-2-1"],
            suite={"passed": 5, "failed": 2,
                   "failed_titles": ["req6-pr.spec.ts › merge red",
                                     "req5.spec.ts › issue red"],
                   "failed_files": ["req6-change.spec.ts",
                                    "req5-issues.spec.ts"],
                   "passed_titles": []},
            walk={"errors": [], "missing": ["Compare"]},
            coverage={"covered": 3, "total": 30},
            phase0={"turn_ok": True, "turn_reason": "model end_turn",
                    "repair_turns": 1,
                    "suite": {"passed": 4, "failed": 3}},
        )

    def test_extract_lessons(self):
        import evolve_loop
        from datetime import datetime, timedelta
        v = self._verdict()
        lines = evolve_loop.extract_lessons(
            v, Path(tempfile.mkdtemp()), 1,
            datetime.now() - timedelta(minutes=5))
        blob = "\n".join(lines)
        self.assertIn("req6-pr.spec.ts › merge red", blob)
        self.assertIn("unreachable from home: Compare", blob)
        self.assertIn("batches not done: REQ-6-2-1", blob)
        self.assertIn("phase0:", blob)
        self.assertIn("coverage: 3/30", blob)

    def test_append_lessons_trims_old_rounds(self):
        import evolve_loop
        from datetime import datetime
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "octos" / "evolution-lessons.md"
            for n in range(1, 6):
                evolve_loop.append_lessons(
                    p, n, [f"- round{n} line"], keep_rounds=2,
                    now=datetime(2026, 10, 6, 12, n))
            text = p.read_text(encoding="utf-8")
            self.assertIn("Round 5", text)
            self.assertIn("Round 4", text)
            self.assertNotIn("Round 3", text)
            self.assertNotIn("Round 1", text)
            self.assertIn("round5 line", text)


class RegressionEnforcementTests(unittest.TestCase):
    """回归强制力四件套钉(10-07,对 sheet 轮完整日志核实的修正)。"""

    def test_new_reds_file_level(self):
        # 旧树 id/无 id 标题全数计入(曾 20 红喂成 6 条)。
        titles = [
            "req1-workbook.spec.ts › REQ-1-1-1 view and open seeded workbook",
            "req4-formula.spec.ts › REQ-4-1-1 basic expressions",
            "probe.spec.ts › probe5",
            "req1-2-2-rename.spec.ts › REQ-1-2-2 rename trims spaces",
        ]
        reds = main._regression_new_reds(titles)
        self.assertEqual(len(reds), 4)
        known = main._regression_new_reds(titles, {titles[3]})
        self.assertEqual(known, titles[:3])

    def test_repair_stop_loss(self):
        # 修复轮被切断=半改在场,恒停。
        self.assertTrue(main._repair_should_stop(20, 5, True))
        # 恶化超 30% 停(REQ-8 形态:23→51)。
        self.assertTrue(main._repair_should_stop(23, 51, False))
        self.assertTrue(main._repair_should_stop(20, 27, False))
        # 改善/持平/小幅波动继续。
        self.assertFalse(main._repair_should_stop(20, 26, False))
        self.assertFalse(main._repair_should_stop(20, 20, False))
        self.assertFalse(main._repair_should_stop(20, 0, False))

    def test_max_iterations_cut_detection(self):
        self.assertTrue(main._is_max_iterations_cut("max iterations"))
        self.assertTrue(
            main._is_max_iterations_cut("Turn ended: max iterations"))
        self.assertFalse(main._is_max_iterations_cut("doom loop"))
        self.assertFalse(main._is_max_iterations_cut("loop thrash"))
        self.assertFalse(main._is_max_iterations_cut(None))

    def test_prompts_carry_reconciliation_context(self):
        p = main.EVOLVE_REGRESSION_REPAIR_PROMPT.format(
            failed=3, titles="- t1\n- t2\n- t3", files="req1.spec.ts",
            changed="backend/src/routes/index.js, sheet-kernel.js")
        self.assertIn("Files this batch changed since its start", p)
        self.assertIn("sheet-kernel.js", p)
        c = main.fresh_session_repair_prompt(
            "TASK BODY", "cut by the per-turn iteration cap", None,
            changed_files=["frontend/src/features/sheet-kernel.js"])
        self.assertIn("cut MID-EDIT", c)
        self.assertIn("sheet-kernel.js", c)
        self.assertIn("Reconcile that half-state FIRST", c)
        # 不带清单时无对账段(向后兼容)。
        self.assertNotIn(
            "MID-EDIT",
            main.fresh_session_repair_prompt("T", "r", None))


class StableSuiteMeasureTests(unittest.TestCase):
    """稳定红门钉(10-07 sheet 法医):高红率复测一次,只认两轮都红的
    标题——瞬时噪声红不进修复链;低红率直接过。"""

    def test_transient_noise_dropped(self):
        readings = [
            {"passed": 30, "failed": 20,
             "failed_titles": [f"noise{i}" for i in range(15)]
             + [f"real{i}" for i in range(5)],
             "failed_files": ["a.spec.ts"] * 20, "passed_titles": []},
            {"passed": 45, "failed": 5,
             "failed_titles": [f"real{i}" for i in range(5)],
             "failed_files": ["a.spec.ts"] * 5, "passed_titles": []},
        ]
        # 第二轮的 files 是去重集(短于标题数)——不得 zip 配对截断
        readings[1]["failed_files"] = ["a.spec.ts"]
        out = main.stable_suite_measure(
            lambda: readings.pop(0), log=lambda *a: None)
        self.assertEqual(out["failed"], 5)
        self.assertTrue(all(t.startswith("real") for t in out["failed_titles"]))
        self.assertEqual(out["failed_files"], ["a.spec.ts"])

    def test_low_red_rate_no_remeasure(self):
        calls = []

        def once():
            calls.append(1)
            return {"passed": 40, "failed": 2, "failed_titles": ["x", "y"],
                    "failed_files": ["a.spec.ts"], "passed_titles": []}
        out = main.stable_suite_measure(once, log=lambda *a: None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["failed"], 2)

    def test_green_or_none_passthrough(self):
        self.assertIsNone(main.stable_suite_measure(
            lambda: None, log=lambda *a: None))
        g = {"passed": 5, "failed": 0, "failed_titles": [],
             "failed_files": [], "passed_titles": []}
        self.assertEqual(main.stable_suite_measure(
            lambda: g, log=lambda *a: None)["failed"], 0)


class RollbackContainmentTests(unittest.TestCase):
    """伤害遏制回滚钉:失败的修复轮半改回滚到 HEAD;运行态文件豁免。"""

    def test_rollback_only_uncommitted(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            def git(*a):
                sp.run(["git", "-C", str(root), *a], capture_output=True,
                       check=True)
            git("init", "-q")
            git("config", "user.email", "t@t"); git("config", "user.name", "t")
            (root / "src").mkdir()
            (root / "src/a.js").write_text("committed\n", encoding="utf-8")
            (root / "backend/data").mkdir(parents=True)
            (root / "backend/data/db.run.json").write_text(
                "{}", encoding="utf-8")
            git("add", "-A"); git("commit", "-qm", "base")
            # 修复轮半改:源码 + 运行态
            (root / "src/a.js").write_text("half-edit poison\n",
                                           encoding="utf-8")
            (root / "backend/data/db.run.json").write_text(
                "{poison}", encoding="utf-8")
            rolled = main.rollback_uncommitted(
                root, ["src/a.js", "backend/data/db.run.json"],
                log=lambda *a: None)
            self.assertEqual(rolled, ["src/a.js"])  # 运行态豁免
            self.assertEqual((root / "src/a.js").read_text(
                encoding="utf-8"), "committed\n")   # 半改已撤


class CliExitCodeTests(unittest.TestCase):
    """__main__ 退出码契约钉:EvolveVerdict 对象→0(sys.exit(非int)
    =exit 1,曾把成功轮害成非零、打断评测器);int 原样。"""

    def test_run_cli_swallows_crash_exits_zero(self):
        # __main__ 终极兜底钉:try 外表面崩溃 → traceback 落 stderr、
        # 退出码 0(评测器不被打断);SystemExit 放行。注入崩溃验证守卫,
        # 绝不真调 main(免污染工作区)。
        import contextlib
        import io

        def _boom():
            raise RuntimeError("pre-try surface crash")

        err = io.StringIO()
        orig = main.main
        main.main = _boom
        try:
            with contextlib.redirect_stderr(err):
                rc = main._run_cli()
        finally:
            main.main = orig
        self.assertEqual(rc, 0)
        self.assertIn("pre-try surface crash", err.getvalue())

        def _se():
            raise SystemExit(2)

        main.main = _se
        try:
            with self.assertRaises(SystemExit):
                main._run_cli()
        finally:
            main.main = orig

    def test_verdict_object_maps_to_zero(self):
        v = main.EvolveVerdict(ok=False, dry_run=True)
        self.assertEqual(main._cli_exit_code(v), 0)
        self.assertEqual(main._cli_exit_code(main.EvolveVerdict(ok=True)), 0)
        self.assertEqual(main._cli_exit_code(0), 0)
        self.assertEqual(main._cli_exit_code(1), 1)


class HostScriptsNoOverwriteTests(unittest.TestCase):
    """脚手架落盘策略钉(10-07 拍板):已建成项目上仅 selftest 骨架与
    AGENTS.md 覆写刷新,宿主辅助脚本存在即跳过(不覆写)。"""

    def test_existing_scripts_kept_fresh_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            sentinel_rs = "# operator-tuned restart\nexit 0\n"
            sentinel_e2e = "# model-known run-e2e\nexit 7\n"
            (root / "scripts/restart-server.sh").write_text(
                sentinel_rs, encoding="utf-8")
            (root / "scripts/run-e2e.sh").write_text(
                sentinel_e2e, encoding="utf-8")
            written = main.stage_host_scripts(root, 43101,
                                              log=lambda *a: None)
            self.assertEqual(written, [])  # 存在即跳过:零写入
            self.assertEqual(
                (root / "scripts/restart-server.sh").read_text(
                    encoding="utf-8"), sentinel_rs)
            self.assertEqual(
                (root / "scripts/run-e2e.sh").read_text(
                    encoding="utf-8"), sentinel_e2e)
        with tempfile.TemporaryDirectory() as td2:
            fresh = Path(td2)
            written = main.stage_host_scripts(fresh, 43101,
                                              log=lambda *a: None)
            self.assertEqual(
                written, ["scripts/restart-server.sh",
                          "scripts/run-e2e.sh"])
            rs = (fresh / "scripts/restart-server.sh").read_text(
                encoding="utf-8")
            e2e = (fresh / "scripts/run-e2e.sh").read_text(
                encoding="utf-8")
            self.assertIn("/api/health", rs)
            self.assertIn("@playwright/test/cli.js", e2e)
            # 幂等:二次调用零写入。
            self.assertEqual(
                main.stage_host_scripts(fresh, 43101,
                                        log=lambda *a: None), [])


class GitignoreGuardTests(unittest.TestCase):
    def test_append_only_missing_lines(self):
        with tempfile.TemporaryDirectory() as td:
            gi = Path(td) / ".gitignore"
            gi.write_text("node_modules/\noctos/\n.octos/\n",
                          encoding="utf-8")
            added = main._ensure_evolve_gitignore(Path(td),
                                                  log=lambda *a: None)
            self.assertEqual(added, [])
            self.assertEqual(
                gi.read_text(encoding="utf-8"),
                "node_modules/\noctos/\n.octos/\n")  # 只增不删、不重排
            gi.write_text("node_modules/\n", encoding="utf-8")
            added = main._ensure_evolve_gitignore(Path(td),
                                                  log=lambda *a: None)
            self.assertEqual(added, [".octos/", "octos/"])
            self.assertIn("octos/", gi.read_text(encoding="utf-8"))

    def test_archive_context_logs_is_rename(self):
        with tempfile.TemporaryDirectory() as td:
            cl = Path(td) / ".octos" / "context-logs"
            cl.mkdir(parents=True)
            (cl / "llm.jsonl").write_text("{}", encoding="utf-8")
            name = main._archive_stale_context_logs(
                Path(td), log=lambda *a: None)
            self.assertIsNotNone(name)
            moved = Path(td) / ".octos" / name
            self.assertTrue(moved.is_dir())
            self.assertFalse(cl.exists())
            self.assertEqual(
                (moved / "llm.jsonl").read_text(encoding="utf-8"), "{}")


class VisualCheckDryRunTests(unittest.TestCase):
    """视觉核验轮(10-07,模型自截制)dry-run 预览:核验轮 prompt 随
    dry-run 落盘;Phase 0 修复句点名截图义务(指示模型,宿主不代拍)。"""

    def test_dry_run_writes_visual_check_prompt(self):
        req = TASKS / "hackathon-evolution--sheet"
        if not (req / "requirements.yaml").exists():
            self.skipTest("sheet evolution task not present")
        with tempfile.TemporaryDirectory() as td:
            main._evolve_dry_run(req, Path(td), main.EvolveOptions())
            preview = (Path(td) / "octos" / "evolve-dry-run"
                       / "visual-check-prompt.txt")
            self.assertTrue(preview.is_file())
            text = preview.read_text(encoding="utf-8")
            self.assertIn("VISUAL NAV CHECK", text)
            self.assertIn("ATTACHED", text)
            self.assertIn("NEVER the scenario text", text)
            self.assertIn("YOU captured", text)
            self.assertNotIn("{shots}", text)
            # Phase 0 修复句点名截图义务(拍什么归模型;义务来自宿主
            # 发明的 Phase 0 批,须在 dry-run 可预览)。
            phase0 = (Path(td) / "octos" / "evolve-dry-run"
                      / "phase0-prompt.txt").read_text(encoding="utf-8")
            self.assertIn("(d) capture", phase0)
            self.assertIn("octos/screenshots/", phase0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
