"""Tests for arc/plan_envelope.py (stdlib unittest only)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plan_envelope as pe  # noqa: E402
import scaffold as sc  # noqa: E402


def node(nid: str) -> dict:
    return {"id": nid, "type": "ATOMIC", "name": nid, "description": f"{nid} desc"}


class SpecToNodeMapTests(unittest.TestCase):
    def test_exact_atomic_id_match(self):
        with TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "REQ-1.spec.ts").write_text("test('x', () => {});")
            (tdir / "REQ-2.spec.ts").write_text("test('y', () => {});")
            m = pe.spec_to_node_map(tdir, [node("REQ-1"), node("REQ-2")])
        self.assertEqual(m, {"REQ-1": ["REQ-1.spec.ts"], "REQ-2": ["REQ-2.spec.ts"]})

    def test_ticket_collision_group_splits_in_filename_order(self):
        # ticket-booking shape: atoms REQ-1/REQ-2, specs REQ-1.1/REQ-1.2.
        with TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "REQ-1.1-user-registration.spec.ts").write_text("a")
            (tdir / "REQ-1.2-user-login.spec.ts").write_text("b")
            m = pe.spec_to_node_map(tdir, [node("REQ-1"), node("REQ-2")])
        self.assertEqual(
            m,
            {
                "REQ-1": ["REQ-1.1-user-registration.spec.ts"],
                "REQ-2": ["REQ-1.2-user-login.spec.ts"],
            },
        )

    def test_prefix_owned_files_stay_together_without_spec_less_nodes(self):
        with TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "REQ-1.1-a.spec.ts").write_text("a")
            (tdir / "REQ-1.2-b.spec.ts").write_text("b")
            m = pe.spec_to_node_map(tdir, [node("REQ-1")])
        self.assertEqual(m, {"REQ-1": ["REQ-1.1-a.spec.ts", "REQ-1.2-b.spec.ts"]})

    def test_unknown_prefix_and_helpers_go_global(self):
        with TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "OTHER-9.spec.ts").write_text("x")
            (tdir / "support").mkdir()
            (tdir / "support" / "e2e.ts").write_text("export const x = 1;")
            m = pe.spec_to_node_map(tdir, [node("REQ-1")])
        self.assertEqual(m, {})

    def test_exact_hits_are_never_redistributed(self):
        # REQ-1 has its own exact spec; REQ-1.1 prefix-hits REQ-1 too.
        # The prefix file joins REQ-1 (no collision: REQ-2 has none either way,
        # but exact files are untouchable).
        with TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "REQ-1.spec.ts").write_text("exact")
            (tdir / "REQ-1.1-extra.spec.ts").write_text("prefix")
            m = pe.spec_to_node_map(tdir, [node("REQ-1"), node("REQ-2")])
        self.assertIn("REQ-1.spec.ts", m["REQ-1"])
        self.assertIn("REQ-1.1-extra.spec.ts", m["REQ-1"])


class RehearsalBootEnvTests(unittest.TestCase):
    def test_sets_port_and_extra_ports(self):
        env = pe.rehearsal_boot_env({"A": "1"}, 43101, [3301, 3002])
        self.assertEqual(env["PORT"], "43101")
        self.assertEqual(env["ARC_EXTRA_PORTS"], "3301,3002")
        self.assertEqual(env["A"], "1")

    def test_no_extra_ports_means_no_key(self):
        env = pe.rehearsal_boot_env({}, 3000, [])
        self.assertEqual(env, {"PORT": "3000"})
        self.assertNotIn("ARC_EXTRA_PORTS", env)

    def test_does_not_mutate_base(self):
        base = {"PORT": "9999"}
        pe.rehearsal_boot_env(base, 3000, [3301])
        self.assertEqual(base["PORT"], "9999")


class ValidationProbeTests(unittest.TestCase):
    def test_post_routes_get_empty_and_oversized_probes(self):
        routes = [
            {"method": "GET", "path": "/api/health"},
            {"method": "POST", "path": "/api/register"},
            {"method": "POST", "path": "/api/login"},
        ]
        probes = pe.build_validation_probes(routes)
        self.assertEqual(len(probes), 4)
        by_path = {}
        for pr in probes:
            by_path.setdefault(pr["path"], []).append(pr["variant"])
        self.assertEqual(by_path["/api/register"], ["empty", "oversized"])
        self.assertEqual(by_path["/api/login"], ["empty", "oversized"])
        oversized = next(p for p in probes if p["variant"] == "oversized")
        self.assertGreater(len(oversized["body"]), 10240)
        self.assertEqual(probes[0]["body"], "{}")

    def test_cap_and_skips(self):
        routes = [{"method": "POST", "path": f"/api/r{i}"} for i in range(10)]
        routes.append({"method": "GET", "path": "/api/x"})
        probes = pe.build_validation_probes(routes)
        self.assertEqual(len(probes), 8)
        self.assertEqual(pe.build_validation_probes([]), [])


class HostStackRulesTests(unittest.TestCase):
    def test_goal_first_rules_carry_substrate_and_model_ownership(self):
        r = pe.HOST_STACK_RULES_GOAL_FIRST
        for needle in ["scrypt", "ARC_EXTRA_PORTS", "never run npm install",
                       "backend/src/routes", "ALL API endpoints are YOURS",
                       "seed-accounts.json", "db.run.json must NEVER ship",
                       "FIXTURE CONTRACT",
                       # 09-28 技术选型层:库式基座在契约中逐一点名
                       "src/lib/session.js", "src/lib/crud.js",
                       "frontend/src/app/csv.js", "frontend/src/app/undo.js",
                       "frontend/src/app/router.js",
                       "tx (atomic", "nextSeq"]:
            self.assertIn(needle, r)
        # 预算帽(防契约无限膨胀):386ed58 加 ACCESSIBLE-NAME UNIQUENESS
        # 段后常量 6033,6000 旧帽是提交时漏改的既有失败——帽随内容走,
        # 不裁法医根因修复文本。
        self.assertLessEqual(len(r), 6500)
        # 09-28 CRUD 生成器退役:声明协议/生成层词汇不得回流
        self.assertNotIn("SCHEMA-DRIVEN", r)
        self.assertNotIn("schema.json", r)
        self.assertNotIn("engine.js", r)
        self.assertNotIn("validate.js", r)
        self.assertNotIn("lib/auth.js", r)
        # 通用内核文案撤销后不得回流:无普适 UI 行为条
        self.assertNotIn("RE-RENDER", r)
        self.assertNotIn("bilingual", r)
        self.assertNotIn("getByLabel", r)


class StageSpecsTests(unittest.TestCase):
    def test_copies_specs_and_support_and_filters_non_ts(self):
        with TemporaryDirectory() as td:
            src = Path(td, "src")
            out = Path(td, "out")
            src.mkdir()
            (src / "REQ-1.spec.ts").write_text("a")
            (src / "README.md").write_text("nope")
            (src / "run.sh").write_text("echo")
            (src / "support").mkdir()
            (src / "support" / "e2e.ts").write_text("helper")
            (src / "support" / "node_modules").mkdir()
            stage = pe.stage_specs(src, out)
            self.assertEqual(stage, out / ".arc/acceptance")
            names = sorted(p.name for p in stage.iterdir())
            self.assertEqual(names, ["REQ-1.spec.ts", "support"])
            self.assertTrue((stage / "support" / "e2e.ts").exists())

    def test_clears_previous_staging(self):
        with TemporaryDirectory() as td:
            src = Path(td, "src")
            out = Path(td, "out")
            src.mkdir()
            (src / "REQ-1.spec.ts").write_text("a")
            pe.stage_specs(src, out)
            (src / "REQ-1.spec.ts").unlink()
            stage = pe.stage_specs(src, out)
            self.assertFalse(any(stage.iterdir()))


class HostScaffoldTests(unittest.TestCase):
    def test_writes_full_template_and_is_idempotent(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            written = sc.scaffold(out)
            for rel in [
                "backend/package.json",
                "backend/src/app.js",
                "backend/src/index.js",
                "backend/src/lib/util.js",
                "backend/src/database/store.js",
                "backend/src/routes/index.js",
                "backend/data/db.json",
                "frontend/package.json",
                "frontend/index.html",
                "frontend/src/main.jsx",
                ".npmrc",
                "backend/.npmrc",
                "frontend/.npmrc",
            ]:
                self.assertTrue((out / rel).exists(), rel)
                self.assertIn(rel, written)
            pkg = json.loads((out / "backend" / "package.json").read_text())
            self.assertEqual(pkg["dependencies"], {"express": "^4.19.2"})
            self.assertNotIn("vitest", json.dumps(pkg))
            fpkg = json.loads((out / "frontend" / "package.json").read_text())
            self.assertEqual(fpkg["scripts"]["build"], "vite build")
            # 09-27 镜像对齐:船载 .npmrc 与 runner 镜像默认 registry 一致
            # (npmjs.org);agent 期安装走 INSTALL_REGISTRY env 覆盖。
            self.assertIn("registry.npmjs.org",
                          (out / "backend" / ".npmrc").read_text())

            # idempotent: second call writes nothing
            self.assertEqual(sc.scaffold(out), [])
            # existing files are NOT overwritten
            (out / ".npmrc").write_text("custom\n")
            sc.scaffold(out)
            self.assertEqual((out / ".npmrc").read_text(), "custom\n")

    def test_business_files_stay_with_the_model(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            # 模型独占:数据声明、种子记录、业务端点。
            self.assertFalse((out / "backend/data/schema.json").exists())
            db = json.loads(
                (out / "backend/data/db.json").read_text())
            self.assertEqual(db, {"collections": {}})
            stub = (out / "backend/src/routes/index.js").read_text()
            self.assertIn("module.exports = () => {};", stub)

    def test_template_js_has_no_blank_lines(self):
        # 无空行律只约束 backend JS(会被模型经 repomap 反复消费);
        # frontend.jsx 本就含空行。
        for rel, (content, _role) in sc.SCAFFOLD_FILES.items():
            if rel.startswith("backend/") and rel.endswith(".js"):
                self.assertNotIn("\n\n", content, rel)


class ProbeRoutesTests(unittest.TestCase):
    def test_collects_api_routes_with_method_hints(self):
        with TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "REQ-3-api.spec.ts").write_text(
                "const a = await request.get('http://127.0.0.1:3301/api/health');\n"
                "const b = await request.post('/api/trains/search', {data: {}});\n"
                "const c = await request.post('/api/register', {data: {}});\n"
            )
            probes = pe.probe_routes(tdir)
        by = {f"{p['method']} {p['path']}" for p in probes}
        self.assertIn("GET /api/health", by)
        self.assertIn("POST /api/trains/search", by)
        self.assertIn("POST /api/register", by)
        self.assertEqual(len(probes), 3)




class FixtureProbeTests(unittest.TestCase):
    def test_declared_logins_first_and_passwordless_skipped(self):
        # 模型自报账号(seed-accounts.json)优先;无密码条目跳过——
        # 宿主不再有统一密码兜底(09-28 合成账号退役)。
        logins = [{"username": "org-owner", "password": "pw"},
                  {"username": "ghost"},
                  {"username": "alice-dev", "password": "quoted-pw"}]
        probes = pe.build_fixture_probes(logins)
        self.assertEqual([p["user"] for p in probes],
                         ["org-owner", "alice-dev"])
        for p in probes:
            self.assertEqual(json.loads(p["body"])["password"] in
                             ("pw", "quoted-pw"), True)

    def test_declared_path_overrides_default_login_route(self):
        # CRUD 生成器退役:登录路由形状归模型,声明条目的 path 字段
        # 覆盖缺省约定 /api/auth/login。
        probes = pe.build_fixture_probes([
            {"username": "u", "password": "p", "path": "/api/login"},
            {"username": "v", "password": "p"}])
        self.assertEqual(probes[0]["path"], "/api/login")
        self.assertEqual(probes[1]["path"], "/api/auth/login")

    def test_declared_entry_supersedes_compiled_duplicate(self):
        # 09-29 审计:同名去重、声明优先——模型声明了真实路由后,
        # compiled 缺省条目不得再探测死路径造成永久红。
        probes = pe.build_fixture_probes([
            {"username": "alice", "password": "p", "path": "/api/login"},
            {"username": "alice", "password": "p"},
            {"username": "bob", "password": "p"}])
        self.assertEqual([p["user"] for p in probes], ["alice", "bob"])
        self.assertEqual(probes[0]["path"], "/api/login")

    def test_declared_body_overrides_default_shape(self):
        # 声明条目可用 body 逐字覆写请求体——宿主对模型自报声明
        # 不设字段形状假设。
        probes = pe.build_fixture_probes([
            {"username": "u", "password": "p",
             "body": '{"identifier":"u","secret":"p"}'}])
        self.assertEqual(probes[0]["body"],
                         '{"identifier":"u","secret":"p"}')
        # 无 body 声明仍走缺省形状
        probes = pe.build_fixture_probes([{"username": "v", "password": "p"}])
        self.assertEqual(json.loads(probes[0]["body"]),
                         {"username": "v", "password": "p"})

    def test_login_probe_cap(self):
        logins = [{"username": f"u{i}", "password": "pw"} for i in range(9)]
        probes = pe.build_fixture_probes(logins)
        self.assertEqual(len(probes), 8)
        self.assertEqual(pe.build_fixture_probes([]), [])
