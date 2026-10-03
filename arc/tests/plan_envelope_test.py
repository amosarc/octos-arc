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
        env = pe.boot_env({"A": "1"}, 43101, [3301, 3002])
        self.assertEqual(env["PORT"], "43101")
        self.assertEqual(env["ARC_EXTRA_PORTS"], "3301,3002")
        self.assertEqual(env["A"], "1")

    def test_no_extra_ports_means_no_key(self):
        env = pe.boot_env({}, 3000, [])
        self.assertEqual(env, {"PORT": "3000"})
        self.assertNotIn("ARC_EXTRA_PORTS", env)

    def test_does_not_mutate_base(self):
        base = {"PORT": "9999"}
        pe.boot_env(base, 3000, [3301])
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
        for needle in ["scrypt", "ARC_EXTRA_PORTS",
                       "backend/src/routes", "ALL API endpoints are YOURS",
                       # 09-30:FIXTURE 机制段迁任务渲染层(任务专属),
                       # 09-28 技术选型层:库式基座在契约中逐一点名
                       "src/lib/session.js", "src/lib/crud.js",
                       "frontend/src/app/csv.js", "frontend/src/app/undo.js",
                       "frontend/src/app/router.js",
                       "tx with rollback", "nextSeq",
                       # 09-30 端口契约在 ARCHITECTURE(评测口宿主管理/
                       # 开发口自验专用),main.py 缺省从常量取
                       "never bind or restart it", "bind port 3100 only",
                       # 10-02:重启唯一通道=scripts/restart-server.sh
                       # (pkill 自匹配 doom 两 turn 全灭后的三重修复之一)
                       "sh scripts/restart-server.sh",
                       "scripts/ = host-provided helper scripts"]:
            self.assertIn(needle, r)
        # Hygiene 一刀切禁令整体退役(用户拍板;grading-port/db.run/
        # .arc 三条由 ARCHITECTURE Ports 行与任务 FIXTURE 段承载)
        self.assertNotIn("Hygiene", r)
        self.assertNotIn("npm install", r)
        self.assertNotIn("never commit", r)
        # 账号三义务迁 BOOTSTRAP_PREAMBLE(首 turn 载体),系统行不回流
        self.assertNotIn("seed-accounts", r)
        self.assertNotIn("ROLE-ACTOR", r)
        # 端口契约单一出处:main.py 的 OCTOS_SMOKE_PORT/web_port 缺省同源。
        self.assertEqual((pe.WEB_PORT, pe.SMOKE_PORT), (3000, 3100))
        # 预算帽(防契约无限膨胀):386ed58 加 ACCESSIBLE-NAME UNIQUENESS
        # 段后常量 6033,6000 旧帽是提交时漏改的既有失败——帽随内容走,
        # 不裁法医根因修复文本;2c8f1b4 步进保真三律+10-03 sheet 文本
        # 身义/描述句断言两律后 6676;bba53e1 首页可达律后 6915;
        # 10-03 template-3 REQ-2 全灭法医(菜单瞬显/NativeSelect/引号名
        # 链接/会话头真相)后 7685;同日提示词审计去重(菜单律并回同步
        # 渲染律/home 行回缩/头部失真句删除)后 7424,帽再次随内容走。
        self.assertLessEqual(len(r), 8000)
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


