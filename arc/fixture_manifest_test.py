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
    return yaml.safe_load(
        (_TASKS / task / "template" / "requirements" / "requirements.yaml")
        .read_text(encoding="utf-8"))


def _workbook_cells(compiled: dict, marker: str) -> dict:
    for wb in compiled["db"].get("workbooks", []):
        for sheet in wb["state"]["sheets"]:
            if marker in json.dumps(sheet["cells"], ensure_ascii=False):
                return sheet["cells"]
    return {}


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
        self.assertTrue(self.compiled["uncovered"],
                        "uncovered report must be non-empty for sheet")

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
        self.assertLessEqual(len(env["steps"]), 40)

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
