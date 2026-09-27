"""Tests for arc/fixture_manifest.py + 夹具注入(plan_envelope 协作)。

语料直接取仓内两个真实比赛任务(arc/tasks/{github,sheet}),断言锚点即
2026-09-24 复盘确认的失分点(REQ-2-2-1 的 "Remove me"、REQ-3-1-1 的
A1=2/=A1*2、github 角色账号图)——提取器/编译器对这些锚点失手即 FAIL。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_manifest as fx  # noqa: E402
import plan_envelope as pe  # noqa: E402

_TASKS = Path(__file__).resolve().parent / "tasks"


def _tree(task: str) -> dict:
    # 任务目录 2026-09 更名为 hackathon--<task>;兼容两种拼写。
    for cand in (task, f"hackathon--{task}"):
        path = (_TASKS / cand / "template" / "requirements"
                / "requirements.yaml")
        if path.is_file():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"requirements.yaml not found for {task!r} "
                            f"under {_TASKS}")


def _workbook_cells(compiled: dict, marker: str) -> dict:
    for wb in compiled["db"].get("workbooks", []):
        for sheet in wb["state"]["sheets"]:
            if marker in json.dumps(sheet["cells"], ensure_ascii=False):
                return sheet["cells"]
    return {}


def _hackathon_tree(task: str) -> dict:
    import yaml as _yaml
    return _yaml.safe_load(
        (_TASKS / task / "requirements.yaml").read_text(encoding="utf-8"))


class HackathonSheetCompileTests(unittest.TestCase):
    """hackathon--sheet:同工作簿名 → 单一规范种子(Q3 Sales),冲突态进
    arrange。这些断言就是验收 spec 断言的种子数据源——这里丢一格,
    spec 与实现同时失真。"""

    @classmethod
    def setUpClass(cls):
        cls.manifest = fx.extract(_hackathon_tree("hackathon--sheet"))
        cls.compiled = fx.compile_seed(cls.manifest)

    def test_kind_and_single_canonical_workbook(self):
        self.assertEqual(self.compiled["kind"], "sheet")
        wbs = self.compiled["db"]["workbooks"]
        self.assertEqual(len(wbs), 1, "canonical seed must be ONE workbook")
        self.assertEqual(wbs[0]["name"], "Q3 Sales")

    def test_canonical_sheet1_dataset(self):
        s1 = next(s for s in self.compiled["db"]["workbooks"][0]
                  ["state"]["sheets"] if s["name"] == "Sheet1")
        c = s1["cells"]
        self.assertEqual(c.get("A1"), {"v": "Region"})
        self.assertEqual(c.get("B1"), {"v": "Sales"})
        self.assertEqual(c.get("C1"), {"v": "Status"})
        self.assertEqual(c.get("A2"), {"v": "East"})
        self.assertEqual(c.get("B2"), {"v": 1200})
        self.assertEqual(c.get("C2"), {"v": "Open"})
        self.assertEqual(c.get("A3"), {"v": "North"})
        self.assertEqual(c.get("B3"), {"v": 800})
        self.assertEqual(c.get("C3"), {"v": "Closed"})
        self.assertEqual(c.get("A4"), {"v": "South"})
        self.assertEqual(c.get("B4"), {"v": 700})
        self.assertEqual(c.get("C4"), {"v": "Open"})

    def test_canonical_sheet2_rows(self):
        sheets = self.compiled["db"]["workbooks"][0]["state"]["sheets"]
        s2 = next((s for s in sheets if s["name"] == "Sheet2"), None)
        self.assertIsNotNone(s2, "REQ-2 需要 Sheet2 存在")
        self.assertEqual(s2["cells"].get("A1"), {"v": "East"})
        self.assertEqual(s2["cells"].get("B1"), {"v": 1200})
        self.assertEqual(s2["cells"].get("A2"), {"v": "North"})
        self.assertEqual(s2["cells"].get("B2"), {"v": 800})

    def test_conflicting_states_arrange_not_seeded(self):
        # REQ-3 的 A1:B2=Item/Qty、REQ-4 的 A1=2 与 A1=Region 冲突:
        # 不进种子,由测试经 UI 布置。
        blob = json.dumps(self.compiled["db"], ensure_ascii=False)
        self.assertNotIn("Item", blob)
        self.assertNotIn('"f": "=A1+B1"', blob)
        for nid in ("REQ-3-1-1", "REQ-4-1-1"):
            self.assertIn(nid, self.compiled["arrange"])
        for nid in ("REQ-1-1-1", "REQ-2-1-1", "REQ-5-3-1"):
            self.assertIn(nid, self.compiled["covered"])

    def test_extra_rules_canonical_without_suffix_noise(self):
        stats = {"kind": self.compiled["kind"],
                 "uncovered": len(self.compiled["uncovered"]),
                 "arrange": len(self.compiled["arrange"]),
                 "seeded": len(self.compiled["db"].get("users", [])),
                 "canonical": True}
        text = fx.render_extra_rules(self.manifest, stats)
        self.assertLessEqual(len(text), 2400)
        self.assertIn("CANONICAL and FINAL", text)
        self.assertNotIn("unique suffix", text)
        self.assertNotIn(fx.FIXTURE_PASSWORD, text)


class HackathonGithubCompileTests(unittest.TestCase):
    """hackathon--github:裸 Q3 不再误判 sheet;逐字种子账号入 seed。"""

    @classmethod
    def setUpClass(cls):
        cls.manifest = fx.extract(_hackathon_tree("hackathon--github"))
        cls.compiled = fx.compile_seed(cls.manifest)

    def test_kind_is_graph_despite_quarter_tokens(self):
        self.assertEqual(self.compiled["kind"], "graph")

    def test_verbatim_seed_account(self):
        users = self.compiled["db"]["users"]
        alice = next((u for u in users
                      if u["username"] == "alice-dev"), None)
        self.assertIsNotNone(alice, "alice-dev must be seeded verbatim")
        self.assertEqual(alice["email"], "alice.dev@example.test")
        # 密码 scrypt 落库;明文只进 logins(登录探针用)
        import hashlib
        salt, digest = alice["password"].split(":")
        again = hashlib.scrypt(b"Valid-password-123!", salt=salt.encode(),
                               dklen=32, n=16384, r=8, p=1,
                               maxmem=64 * 1024 * 1024)
        self.assertEqual(again.hex(), digest)
        self.assertIn("alice-dev", [l["username"] for l in self.compiled["logins"]])

    def test_seed_entities_present(self):
        ents = self.compiled["db"]
        self.assertIn("Acme Demo", [o["name"] for o in ents["organizations"]])
        self.assertIn("acme-docs", [r["name"] for r in ents["repositories"]])
        self.assertIn("frontend-team", [t["name"] for t in ents["teams"]])
        self.assertIn("bob-reviewer",
                      [m["username"] for m in ents["members"]])

    def test_repo附属种子_分支issue_pr_标签里程碑(self):
        ents = self.compiled["db"]
        self.assertIn("main", [b["name"] for b in ents["branches"]])
        self.assertIn("feature-search", [b["name"] for b in ents["branches"]])
        self.assertIn("Improve onboarding",
                      [i["title"] for i in ents["issues"]])
        self.assertIn("Fix search", [p["title"] for p in ents["pulls"]])
        self.assertIn("bug", [l["name"] for l in ents["labels"]])
        self.assertIn("Q3 launch",
                      [m["title"] for m in ents["milestones"]])

    def test_starter_schema_covers_req456_entities(self):
        names = {e["name"] for e in self.compiled["schema"]["entities"]}
        self.assertTrue({"branches", "issues", "pulls", "labels",
                         "milestones", "comments"} <= names)
        self.assertIn("commits", names,
                      "commits 实体必须入 schema(REQ-4-2 历史/对比页)")

    def test_repo_visibility_and_org_attachment(self):
        """属性级保真(hg-github-001):private 修饰语落库、逐字仓库挂逐字 org。"""
        ents = self.compiled["db"]
        repos = {r["name"]: r for r in ents["repositories"]}
        self.assertEqual(repos["acme-docs"]["visibility"], "public")
        self.assertEqual(repos["secret-research"]["visibility"], "private",
                         "需求原文 private repository `secret-research`")
        self.assertEqual(repos["acme-docs"]["orgName"], "Acme Demo")
        self.assertEqual(repos["secret-research"]["orgName"], "Acme Demo")
        self.assertNotIn("fixture-org",
                         [o["name"] for o in ents["organizations"]],
                         "有逐字 org 时不再落合成 fixture-org")

    def test_owner_membership_role(self):
        members = self.compiled["db"]["members"]
        owner = next((m for m in members
                      if m["username"] == "alice-dev"), None)
        self.assertIsNotNone(owner, "owner `alice-dev` 须成为组织成员")
        self.assertEqual(owner["role"], "owner")

    def test_issue_attributes_labels_description_closed_companion(self):
        issues = {i["title"]: i for i in self.compiled["db"]["issues"]}
        main = issues["Improve onboarding"]
        self.assertEqual(main["labels"], "bug, documentation")
        self.assertEqual(main["description"],
                         "Describe the onboarding improvement.")
        self.assertEqual(main["author"], "alice-dev")
        closed = issues.get("Legacy welcome text")
        self.assertIsNotNone(closed, "closed 伴随 issue 须入种子")
        self.assertEqual(closed["status"], "closed")

    def test_pull_states_authors_reviewers_and_comment(self):
        pulls = {p["title"]: p for p in self.compiled["db"]["pulls"]}
        self.assertEqual(pulls["Improve onboarding"]["status"], "open")
        self.assertEqual(pulls["Fix search"]["status"], "closed",
                         "one Open PR ... one Closed PR → 列表序首开次闭")
        self.assertEqual(pulls["Improve onboarding"]["author"], "alice-dev",
                         "created by `alice` 前缀解析到逐字账号")
        self.assertEqual(pulls["Improve onboarding"]["reviewers"],
                         "bob-reviewer")
        comments = self.compiled["db"]["comments"]
        self.assertTrue(any(c["parentType"] == "pull" for c in comments),
                        "PR discussion comment 种子(REQ-6-3-1)")
        self.assertTrue(any(c["parentType"] == "issue" for c in comments),
                        "issue comment 种子(REQ-5-1-2)")

    def test_commits_seeded_not_dropped(self):
        commits = self.compiled["db"]["commits"]
        self.assertIn("Document search flow", [c["message"] for c in commits])
        self.assertEqual(commits[0]["repoName"], "acme-docs")

    def test_no_synthetic_fixture_users_with_literal_accounts(self):
        """门控:需求已给逐字账号矩阵时,fixture-* 合成号是需求外实体。"""
        users = [u["username"] for u in self.compiled["db"]["users"]]
        for synth in ("fixture-write", "fixture-maintain",
                      "fixture-admin", "fixture-reviewer"):
            self.assertNotIn(synth, users)
        self.assertEqual(users, ["alice-dev"])


class InteractionContractTests(unittest.TestCase):
    """B1(run-002 剩余 2 红复盘):Escape/Enter 键位语义与逐字文案
    必须以独立小节进 FOCUS 头部,不再埋在 4000 字 validation 中部。"""

    def test_sheet_edit_node_gets_keyboard_semantics(self):
        tree = _hackathon_tree("hackathon--sheet")
        target: dict = {}

        def walk(n):
            if str(n.get("id")) == "REQ-3-1-1":
                target.update(n)
            for c in n.get("children") or []:
                walk(c)

        walk(tree)
        rules = pe.extract_interaction_contract(
            str(target.get("description") or ""))
        joined = "\n".join(rules)
        self.assertIn("Escape cancels", joined)
        self.assertIn("Enter", joined)

    def test_error_messages_rank_before_control_names(self):
        rules = pe.extract_interaction_contract(
            'Clicking "Save" shows "Worksheet name cannot be empty" then '
            'the "Rename workbook" button stays.')
        self.assertTrue(rules[0].endswith('cannot be empty" (copy exactly)'))

    def test_envelope_validation_leads_with_interaction_section(self):
        nodes = [{"id": "REQ-3", "type": "ATOMIC", "name": "n",
                  "description": 'Pressing Escape cancels; the dialog '
                  '"Sort range" opens. Error "Invalid input." shown.'}]
        env = pe.build_envelope(
            seq=1, phase="node", task="t", atomic_nodes=nodes,
            statuses={}, focus="REQ-3",
            ports={"grade": 1, "smoke": 2, "extra": []}, spec_map={})
        v = env["steps"][0]["validation"] or ""
        self.assertTrue(v.startswith("INTERACTION SEMANTICS"))
        self.assertIn("Escape cancels", v)
        self.assertIn("Invalid input.", v)


class SpecReqPrefixTests(unittest.TestCase):
    def test_dash_and_dot_ids(self):
        self.assertEqual(pe.spec_req_prefix(
            "REQ-1-1-1-add-worksheet.spec.ts"), "REQ-1-1-1")
        self.assertEqual(pe.spec_req_prefix(
            "REQ-1.1-user-registration.spec.ts"), "REQ-1.1")
        self.assertIsNone(pe.spec_req_prefix("helpers.spec.ts"))

    def test_hackathon_spec_map_exact(self):
        tree = _hackathon_tree("hackathon--sheet")
        atomic = []
        def walk(n):
            if str(n.get("type", "")).upper() == "ATOMIC":
                atomic.append(n)
            for ch in n.get("children") or []:
                walk(ch)
        walk(tree)
        tests_dir = _TASKS.parent / "public-tests" / "hackathon--sheet"
        if not tests_dir.is_dir():
            return
        mapping = pe.spec_to_node_map(tests_dir, atomic)
        self.assertIn("REQ-1-1-1", mapping)
        self.assertIn("REQ-5-3-1", mapping)
        self.assertTrue(all(v for v in mapping.values()))


class ExtractTests(unittest.TestCase):
    def test_github_global_and_e2e(self):
        m = fx.extract(_tree("github"))
        self.assertGreaterEqual(len(m["global"]), 24)
        self.assertGreaterEqual(len(m["e2e_envs"]), 10)
        self.assertIn("E2E_DRAFT_PULL_REQUEST_URL", m["e2e_envs"])
        # 需求原文是通配 `E2E_ORGANIZATION_NEW_MEMBER_*`——按字面量提取
        self.assertIn("E2E_ORGANIZATION_NEW_MEMBER_", m["e2e_envs"])

    def test_github_node_givens_and_seed_sentences(self):
        m = fx.extract(_tree("github"))
        givens = "\n".join(m["nodes"]["REQ-6-5"]["given"])
        self.assertIn("approv", givens.lower())
        seeds = "\n".join(s for info in m["nodes"].values()
                          for s in info["seed_sentences"])
        self.assertIn("Seed data supplies", seeds)

    def test_sheet_scenario_givens(self):
        m = fx.extract(_tree("sheet"))
        node = m["nodes"]["REQ-2-2-1"]
        joined = "\n".join(node["given"])
        self.assertIn("Remove me", joined)
        edit = "\n".join(m["nodes"]["REQ-3-1-1"]["given"])
        self.assertIn("A1", edit)
        self.assertIn("=A1*2", edit)


class SheetCompileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = fx.extract(_tree("sheet"))
        cls.compiled = fx.compile_seed(cls.manifest)

    def test_kind_and_volume(self):
        self.assertEqual(self.compiled["kind"], "sheet")
        self.assertGreaterEqual(len(self.compiled["db"]["workbooks"]), 16)

    def test_delete_row_fixture_exact_cells(self):
        cells = _workbook_cells(self.compiled, "Remove me")
        self.assertEqual(cells.get("A2"), {"v": "Alpha"})
        self.assertEqual(cells.get("A3"), {"v": "Remove me"})
        self.assertEqual(cells.get("A4"), {"v": "Beta"})

    def test_edit_fixture_value_and_formula(self):
        cells = _workbook_cells(self.compiled, '"A1": {"v": 2}')
        self.assertEqual(cells.get("A1"), {"v": 2})
        self.assertEqual(cells.get("B1"), {"f": "=A1*2", "result": 4})

    def test_move_source_fixture_formulas(self):
        cells = _workbook_cells(self.compiled, '"f": "=B1*2"')
        self.assertEqual(cells.get("C1"), {"f": "=B1*2", "result": 6})
        self.assertEqual(cells.get("D1"), {"f": "=C1+1", "result": 7})
        self.assertEqual(cells.get("A1"), {"v": 2})
        self.assertEqual(cells.get("B1"), {"v": 3})

    def test_range_rows_fixture(self):
        cells = _workbook_cells(self.compiled, '"v": "Pending"')
        self.assertEqual(cells.get("A1"), {"v": "Region"})
        self.assertEqual(cells.get("B2"), {"v": 1200})
        self.assertEqual(cells.get("C2"), {"v": "Open"})
        self.assertEqual(cells.get("A4"), {"v": "South"})
        self.assertEqual(cells.get("E1"), {"v": "Outside"})

    def test_trailing_punctuation_and_status_values(self):
        blob = json.dumps(self.compiled["db"], ensure_ascii=False)
        self.assertNotIn('"700."', blob)
        self.assertIn('"v": "Open"', blob)

    def test_multi_sheet_split(self):
        # REQ-4-2-1: "on Sheet2, A1 is Other and B1 is =1+1 and displays 2"
        blob = json.dumps(self.compiled["db"], ensure_ascii=False)
        self.assertIn('"f": "=1+1"', blob)
        for wb in self.compiled["db"]["workbooks"]:
            for sheet in wb["state"]["sheets"]:
                if sheet["cells"].get("A1") == {"v": "Other"}:
                    if sheet["name"] != "Sheet1":
                        return  # 分桶成功
        self.fail("Sheet2 bucket not split (A1=Other landed only on Sheet1)")

    def test_unparseable_scenarios_reported_not_invented(self):
        blob = json.dumps(self.compiled["db"], ensure_ascii=False)
        # 撤销类场景("user has just committed...")不可编译 → 不自编
        self.assertNotIn("just committed", blob)
        # 新解析器已覆盖旧 sheet 题全部带坐标场景(uncovered 空);报告
        # 机制本身用合成用例锁定:有坐标语境但句式不可解析 → 必须报告。
        synthetic = {"global": [], "e2e_envs": [], "nodes": {
            "REQ-9-9-9": {"given": [
                "The worksheet holds A1 B2 C3 D4 E5 in unknown fashion."],
                "scenarios": [{"name": "mystery", "given": [
                    "The worksheet holds A1 B2 C3 D4 E5 in unknown "
                    "fashion."]}], "seed_sentences": []}}}
        c2 = fx.compile_seed(synthetic)
        self.assertTrue(c2["uncovered"])
        self.assertEqual(c2["uncovered"][0]["node"], "REQ-9-9-9")
        self.assertFalse(c2["db"])

    def test_workbooks_are_independent_instances(self):
        names = [wb["name"] for wb in self.compiled["db"]["workbooks"]]
        self.assertEqual(len(names), len(set(names)),
                         "scenario workbooks must be distinct instances")


class GraphCompileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = fx.extract(_tree("github"))
        cls.compiled = fx.compile_seed(cls.manifest)

    def test_kind_and_role_accounts(self):
        self.assertEqual(self.compiled["kind"], "graph")
        users = [u["username"] for u in self.compiled["db"]["users"]]
        self.assertGreaterEqual(len(users), 10)
        for expected in ("fixture-org-owner", "fixture-org-member",
                         "fixture-repo-read", "fixture-triage",
                         "fixture-write", "fixture-maintain",
                         "fixture-admin", "fixture-pr-author",
                         "fixture-reviewer", "fixture-no-access"):
            self.assertIn(expected, users)

    def test_org_and_visibility_pair(self):
        repos = {r["name"]: r for r in self.compiled["db"]["repositories"]}
        self.assertIn("fixture-public-repo", repos)
        self.assertIn("fixture-private-repo", repos)
        self.assertEqual(repos["fixture-public-repo"]["visibility"],
                         "public")
        self.assertEqual(repos["fixture-private-repo"]["visibility"],
                         "private")
        self.assertEqual(len(self.compiled["db"]["organizations"]), 1)

    def test_scrypt_hash_matches_store_verify_format(self):
        row = self.compiled["db"]["users"][0]
        salt, digest = row["password"].split(":")
        self.assertRegex(salt, r"^[0-9a-f]{16}$")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        # 重算逐位一致(与 backend store.js 的 scryptSync(pw, salt, 32) 同参)
        import hashlib
        again = hashlib.scrypt(
            fx.FIXTURE_PASSWORD.encode(), salt=salt.encode(),
            dklen=32, n=16384, r=8, p=1, maxmem=64 * 1024 * 1024)
        self.assertEqual(again.hex(), digest)

    def test_starter_schema_exposes_seeded_entities(self):
        names = {e["name"] for e in self.compiled["schema"]["entities"]}
        self.assertTrue({"users", "organizations", "repositories",
                         "members", "teams"} <= names)
        auth = next(e for e in self.compiled["schema"]["entities"]
                    if e.get("auth"))
        self.assertEqual(auth["name"], "users")


class RenderExtraRulesTests(unittest.TestCase):
    def test_deterministic_and_capped(self):
        m = fx.extract(_tree("github"))
        c = fx.compile_seed(m)
        stats = {"uncovered": len(c["uncovered"]), "kind": c["kind"]}
        first = fx.render_extra_rules(m, stats)
        self.assertEqual(first, fx.render_extra_rules(m, stats))
        self.assertLessEqual(len(first), 2400)
        self.assertIn(fx.FIXTURE_PASSWORD, first)
        self.assertIn("E2E_DRAFT_PULL_REQUEST_URL", first)

    def test_sheet_rules_without_password_noise(self):
        m = fx.extract(_tree("sheet"))
        c = fx.compile_seed(m)
        text = fx.render_extra_rules(
            m, {"uncovered": len(c["uncovered"]), "kind": c["kind"]})
        self.assertLessEqual(len(text), 2400)


class EnvelopeInjectionTests(unittest.TestCase):
    def _env(self, **kw):
        base = dict(
            seq=1, phase="node", task="t",
            atomic_nodes=[{"id": "REQ-1", "type": "ATOMIC",
                           "name": "n1", "description": "Validation contract: do x"},
                          {"id": "REQ-2", "type": "ATOMIC",
                           "name": "n2", "description": "Validation contract: do y"}],
            statuses={"REQ-1": "active", "REQ-2": "pending"},
            focus="REQ-1", ports={"grade": 3000, "smoke": 3100, "extra": []},
            spec_map={})
        base.update(kw)
        return pe.build_envelope(**base)

    def test_seeded_node_gets_pointer_prefix(self):
        env = self._env(
            fixture_given={"REQ-1": ["A1 is 2, B1 is Keep"]},
            seeded_nodes={"REQ-1"})
        v = env["steps"][0]["validation"]
        self.assertIn("host-seeded", v)
        self.assertIn("A1 is 2, B1 is Keep", v)
        self.assertEqual(env["steps"][0]["preconditions"],
                         ["A1 is 2, B1 is Keep"])

    def test_unseeded_node_gets_hand_seed_list(self):
        env = self._env(fixture_given={"REQ-2": ["A2 is Alpha"]})
        v = env["steps"][1]["validation"]
        self.assertIn("graded fixtures", v)
        self.assertIn("A2 is Alpha", v)

    def test_validation_capped_and_keeps_given(self):
        env = self._env(
            fixture_given={"REQ-1": ["G%d is %s" % (i, "x" * 200)
                                     for i in range(20)]},
            seeded_nodes=set())
        v = env["steps"][0]["validation"]
        self.assertLessEqual(len(v), 3000)
        self.assertIn("G0 is", v)

    def test_focus_step_survives_step_cap(self):
        nodes = [{"id": f"REQ-{i}", "type": "ATOMIC", "name": f"n{i}",
                  "description": "d"} for i in range(1, 46)]
        env = pe.build_envelope(
            seq=1, phase="node", task="t", atomic_nodes=nodes,
            statuses={}, focus="REQ-45",
            ports={"grade": 1, "smoke": 2, "extra": []}, spec_map={})
        ids = {s["id"] for s in env["steps"]}
        self.assertIn("REQ-45", ids)
        # 上限已放宽到内核 MAX_STEPS=64:47 节点级任务全量入列,不再截尾。
        self.assertEqual(len(env["steps"]), 45)
        self.assertLessEqual(len(env["steps"]), 64)

    def test_extra_rules_passthrough(self):
        env = self._env(extra_rules="FIXTURE CONTRACT: x" * 10)
        self.assertTrue(env["extra_rules"].startswith("FIXTURE CONTRACT: x"))
        self.assertLessEqual(len(env["extra_rules"]), 2400)


class MaterializeTests(unittest.TestCase):
    def test_writes_on_placeholder_and_skips_model_seed(self):
        m = fx.extract(_tree("sheet"))
        with TemporaryDirectory() as td:
            out = Path(td)
            data = out / "backend" / "data"
            data.mkdir(parents=True)
            (data / "db.json").write_text('{"collections":{}}\n',
                                          encoding="utf-8")
            compiled = fx.materialize(out, m)
            self.assertIsNotNone(compiled)
            seeded = json.loads((data / "db.json").read_text())
            self.assertGreaterEqual(
                len(seeded["collections"]["workbooks"]), 16)
            self.assertTrue((data / "schema.json").exists())
            # 再跑:模型/宿主已写过 → 幂等跳过
            seeded["collections"]["workbooks"][0]["name"] = "model edit"
            (data / "db.json").write_text(
                json.dumps(seeded), encoding="utf-8")
            self.assertIsNone(fx.materialize(out, m))

    def test_no_fixture_task_writes_nothing(self):
        m = fx.extract({"id": "ROOT", "type": "FOLDER", "name": "x",
                        "children": []})
        with TemporaryDirectory() as td:
            compiled = fx.materialize(Path(td), m)
            self.assertEqual(compiled["db"], {})
            self.assertFalse((Path(td) / "backend").exists())


class FixtureProbeTests(unittest.TestCase):
    def test_login_and_entity_probes(self):
        probes = pe.build_fixture_probes(
            [{"username": "fixture-org-owner", "role": "org-owner"}],
            ["users", "organizations", "repositories"])
        logins = [p for p in probes if p["kind"] == "login"]
        ents = [p for p in probes if p["kind"] == "entity"]
        self.assertEqual(len(logins), 1)
        self.assertEqual(logins[0]["path"], "/api/auth/login")
        self.assertEqual(
            json.loads(logins[0]["body"])["password"], fx.FIXTURE_PASSWORD)
        # users 是 auth 实体(无引擎 GET 路由)——由 login 探针代管
        self.assertEqual([p["entity"] for p in ents],
                         ["organizations", "repositories"])

    def test_empty_when_no_seed(self):
        self.assertEqual(pe.build_fixture_probes([], []), [])


if __name__ == "__main__":
    unittest.main()
