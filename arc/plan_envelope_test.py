"""Tests for arc/plan_envelope.py (stdlib unittest only)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    def test_contains_stack_facts_and_generic_validation(self):
        r = pe.HOST_STACK_RULES
        for needle in ["scrypt", "ARC_EXTRA_PORTS", "never run npm install",
                       "schema.json", "/api/auth/register",
                       "SERVER-SIDE VALIDATION", "min, min+1, max, max+1",
                       "a 5xx on any input is a bug"]:
            self.assertIn(needle, r)
        self.assertLessEqual(len(r), 6000)  # wire 上限(planctl mod.rs:99)
        # 通用内核文案撤销后不得回流:无普适 UI 行为条
        self.assertNotIn("RE-RENDER", r)
        self.assertNotIn("bilingual", r)
        self.assertNotIn("getByLabel", r)


class SplitValidationTests(unittest.TestCase):
    def _ticket_desc(self):
        # verbatim 形态:ticket-booking REQ-1 的 |- 块(首行=完整契约)。
        contract = ("Validation 契约：应用必须支持直接访问 `/register`，"
                    "有效数据提交后必须调用注册接口、创建登录会话并导航回 `/`，"
                    "首页显示提交的用户名和 accessible name 为 `Sign out` 的退出链接，"
                    "刷新后仍保持登录；证件类型初始值为“请选择证件类型”。")
        notes = "实现旅客注册页面和注册接口，从首页点击 `Register` 进入。\n前端负责必填提示。"
        return contract + "\n" + notes

    def test_ticket_style_first_line_is_contract_with_prefix_stripped(self):
        v, notes = pe.split_validation(self._ticket_desc())
        self.assertTrue(v.startswith("应用必须支持直接访问"))
        self.assertNotIn("Validation 契约", v)
        self.assertIn("导航回 `/`", v)
        self.assertIn("Sign out", v)
        self.assertIn("刷新后仍保持登录", v)
        self.assertIn("请选择证件类型", v)
        self.assertTrue(notes.startswith("实现旅客注册页面"))

    def test_smoke_style_folded_description_is_all_contract(self):
        smoke = ("The home page displays the current count, initially 0, and "
                 "buttons named \"Increment\" and \"Decrement\".")
        v, notes = pe.split_validation(smoke)
        self.assertEqual(v, smoke)
        self.assertEqual(notes, "")

    def test_english_prefix_is_stripped_case_insensitively(self):
        v, notes = pe.split_validation(
            "Validation contract: The app must support direct access to `/register`.\n"
            "Implementation notes here.")
        self.assertTrue(v.startswith("The app must support"))
        self.assertNotIn("Validation contract", v)
        self.assertEqual(notes, "Implementation notes here.")

        v2, _ = pe.split_validation("validation CONTRACT: lowercase variant.")
        self.assertEqual(v2, "lowercase variant.")

    def test_empty_description(self):
        self.assertEqual(pe.split_validation(""), ("", ""))
        self.assertEqual(pe.split_validation(None), ("", ""))


class EnvelopeTests(unittest.TestCase):
    def _nodes(self):
        return [
            {"id": "REQ-1", "type": "ATOMIC", "name": "注册",
             "description": "注册旅客账号\n详细描述"},
            {"id": "REQ-2", "type": "ATOMIC", "name": "登录", "description": "登录和退出账号"},
        ]

    def test_shape_focus_statuses_and_ports(self):
        env = pe.build_envelope(
            seq=3,
            phase="node",
            task="火车票预订演示系统",
            atomic_nodes=self._nodes(),
            statuses={"REQ-1": "done", "REQ-2": "active"},
            focus="REQ-2",
            ports={"grade": 3000, "smoke": 3001, "extra": [3301]},
            spec_map={"REQ-1": ["REQ-1.1-user-registration.spec.ts"]},
        )
        self.assertEqual(env["v"], 1)
        self.assertEqual(env["seq"], 3)
        self.assertEqual(env["phase"], "node")
        self.assertEqual(env["stack"], "web_react_express")
        self.assertEqual(env["rules"], "arc_web_v1")
        self.assertEqual(env["spec_dir"], ".arc/acceptance")
        self.assertEqual(env["ports"], {"grade": 3000, "smoke": 3001, "extra": [3301]})
        self.assertEqual(env["focus"], "REQ-2")
        self.assertEqual(len(env["steps"]), 2)
        s1, s2 = env["steps"]
        self.assertEqual(s1["id"], "REQ-1")
        self.assertEqual(s1["status"], "done")
        # input = 契约前 200 字摘要;validation = 契约全文(规则通道)
        self.assertTrue(s1["input"].startswith("注册旅客账号"))
        self.assertEqual(s1["validation"], s1["input"] if len(s1["input"]) < 200 else s1["input"])
        self.assertTrue(len(s1["validation"] or "") > 0)
        self.assertIn("rules_text", env)
        self.assertIn("HOST CONTRACT", env["rules_text"])
        self.assertIn("scrypt", env["rules_text"])
        # 宿主规则不含 UI 行为条(那些来自 yaml 契约)
        self.assertNotIn("getByLabel", env["rules_text"])
        self.assertNotIn("RE-RENDER", env["rules_text"])
        self.assertEqual(s1["targets"]["spec_files"], ["REQ-1.1-user-registration.spec.ts"])
        self.assertEqual(s2["status"], "active")

    def test_host_signals_pass_through(self):
        hs = {
            "rehearsal": {"build_ok": True, "boot_ok": True,
                          "routes": [{"method": "GET", "path": "/api/health", "status": 200}],
                          "digest": ""},
            "scaffolded_by_host": ["backend/package.json"],
            "spec_run": None,
        }
        env = pe.build_envelope(
            seq=4, phase="node", task="t", atomic_nodes=self._nodes(),
            statuses={}, focus=None, ports={}, spec_map={}, host_signals=hs,
        )
        self.assertEqual(env["host_signals"], hs)

    def test_final_phase_has_no_focus(self):
        env = pe.build_envelope(
            seq=9, phase="final", task="t", atomic_nodes=self._nodes(),
            statuses={}, focus=None, ports={}, spec_map={},
        )
        self.assertIsNone(env["focus"])
        self.assertEqual(env["phase"], "final")


class MarkerTests(unittest.TestCase):
    def test_round_trip_wrap_and_detect(self):
        env = pe.build_envelope(
            seq=1, phase="skeleton", task="t", atomic_nodes=[node("REQ-1")],
            statuses={}, focus="REQ-1", ports={}, spec_map={},
        )
        marked = pe.render_marked_prompt(env, "Build it.")
        self.assertTrue(pe.is_marked(marked))
        lines = marked.split("\n")
        self.assertEqual(lines[0], "[arc-plan]")
        payload = json.loads(lines[1])
        self.assertEqual(payload["seq"], 1)
        self.assertTrue(marked.endswith("\n\nBuild it."))
        # non-ASCII kept as-is (ensure_ascii=False)
        env2 = dict(env, task="火车票")
        marked2 = pe.render_marked_prompt(env2, "x")
        self.assertIn("火车票", marked2)

    def test_never_double_wraps(self):
        once = pe.render_marked_prompt({"v": 1}, "go")
        twice = pe.render_marked_prompt({"v": 2}, once)
        self.assertEqual(once, twice)

    def test_oversized_payload_raises(self):
        big = {"v": 1, "task": "x" * 300_000}
        with self.assertRaises(ValueError):
            pe.render_marked_prompt(big, "p")


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
                "backend/src/lib/validate.js",
                "backend/src/lib/auth.js",
                "backend/src/database/store.js",
                "backend/src/database/engine.js",
                "backend/src/database/schema.js",
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


class VersionGateTests(unittest.TestCase):
    def test_tuple_parses_rc_as_release(self):
        self.assertEqual(pe.parse_version_tuple("octos 2.0.3-rc.11"), (2, 0, 3))
        self.assertEqual(pe.parse_version_tuple("octos 2.0.2"), (2, 0, 2))
        self.assertEqual(pe.parse_version_tuple("garbage"), (0, 0, 0))

    def test_gate_matrix(self):
        import os
        old = os.environ.pop("OCTOS_ASSUME_ARC_PLAN", None)
        try:
            self.assertTrue(pe.plan_capable("octos 2.0.3"))
            self.assertTrue(pe.plan_capable("octos 2.0.3-rc.11"))
            self.assertTrue(pe.plan_capable("octos 2.1.0"))
            self.assertFalse(pe.plan_capable("octos 2.0.2"))
            self.assertFalse(pe.plan_capable("nonsense"))
            os.environ["OCTOS_ASSUME_ARC_PLAN"] = "1"
            self.assertTrue(pe.plan_capable("octos 0.0.1"))
            os.environ["OCTOS_ASSUME_ARC_PLAN"] = "0"
            self.assertFalse(pe.plan_capable("octos 9.9.9"))
        finally:
            os.environ.pop("OCTOS_ASSUME_ARC_PLAN", None)
            if old is not None:
                os.environ["OCTOS_ASSUME_ARC_PLAN"] = old


if __name__ == "__main__":
    unittest.main()
