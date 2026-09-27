"""scaffold 包测试:load/compile/template-static/template-smoke + 防漂移一致性。

用法: python3 arc/scaffold_test.py -v
模板冒烟会真实 npm install express(npmmirror)并跑 node 端到端流程;
node/npm 缺失时对应用例跳过。

一致性组(ManifestConsistencyTests)是本次封装的核心回归面:路径知识此前
散布 4 层(生成/编译/就绪判定/文字契约),收敛到 manifest 后用测试钉住
"清单 ↔ 模板内容 ↔ 文字契约 ↔ 冒烟脚本"四者不漂移——这正是参考项目
agent-arc-built(template.yaml 无代码执行)踩过的坑。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import plan_envelope as pe  # noqa: E402
import scaffold as sc  # noqa: E402

VALID = {
    "entities": [
        {"name": "users", "auth": True, "fields": [
            {"name": "username", "label": "用户名", "type": "string",
             "required": True, "unique": True, "format": "username"},
            {"name": "email", "type": "email", "unique": True},
            {"name": "password", "type": "password", "required": True},
            {"name": "confirmPassword", "match": "password"},
            {"name": "idType", "enum": ["护照", "居民身份证"]},
        ]},
        {"name": "things", "fields": [
            {"name": "name", "type": "string", "required": True, "max": 255},
        ]},
    ]
}


def _node_bin() -> str | None:
    return shutil.which("node")


class LoadSchemaTests(unittest.TestCase):
    def _load(self, raw: str | None):
        with TemporaryDirectory() as td:
            out = Path(td)
            p = sc.schema_path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            if raw is not None:
                p.write_text(raw, encoding="utf-8")
            return sc.load_schema(out)

    def test_valid_schema_round_trips(self):
        s, err = self._load(json.dumps(VALID))
        self.assertIsNone(err)
        self.assertEqual([e["name"] for e in s["entities"]],
                         ["users", "things"])

    def test_absent_and_invalid_json(self):
        s, err = self._load(None)
        self.assertIsNone(s)
        self.assertEqual(err, "absent")
        s, err = self._load("{nope")
        self.assertIsNone(s)
        self.assertIn("invalid json", err)

    def test_structure_violations(self):
        cases = [
            ('{"x":1}', "entities"),
            ('{"entities":[{"name":"1bad","fields":[]}]}', "identifier"),
            ('{"entities":[{"name":"a","fields":[{"name":"f"},{"name":"f"}]}]}', "duplicate field"),
            ('{"entities":[{"name":"a","fields":[{"name":"f","type":"blob"}]}]}', "unknown type"),
            ('{"entities":[{"name":"a","fields":[{"name":"f","format":"uuid"}]}]}', "unknown format"),
            ('{"entities":[{"name":"a","fields":[{"name":"f","min":"3"}]}]}', "must be int"),
            ('{"entities":[{"name":"a","fields":[{"name":"f","match":"g"}]}]}', "unknown field"),
            ('{"entities":[{"name":"a","fields":[]},{"name":"a","fields":[]}]}', "duplicate entity"),
        ]
        for raw, needle in cases:
            s, err = self._load(raw)
            self.assertIsNone(s, raw)
            self.assertIn(needle, err, raw)

    def test_match_forward_reference_ok(self):
        s, err = self._load('{"entities":[{"name":"a","fields":['
                            '{"name":"c","match":"p"},{"name":"p","type":"string"}]}]}')
        self.assertIsNone(err)
        self.assertEqual(s["entities"][0]["fields"][0]["match"], "p")


class CompileSchemaTests(unittest.TestCase):
    def test_compile_route_literals_and_idempotence(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            p = sc.schema_path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(VALID), encoding="utf-8")
            written = sc.compile_schema(out)
            self.assertEqual(written, [str(sc.codegen.SCHEMA_JS)])
            js = (out / sc.codegen.SCHEMA_JS).read_text(encoding="utf-8")
            self.assertIn("/api/users", js)
            self.assertIn("/api/things", js)
            self.assertIn('"auth": true', js)
            self.assertEqual(sc.compile_schema(out), [], "unchanged → skip")
            p.write_text(json.dumps({"entities": []}), encoding="utf-8")
            self.assertEqual(sc.compile_schema(out), [str(sc.codegen.SCHEMA_JS)])
            self.assertIn("entities", (out / sc.codegen.SCHEMA_JS).read_text())

    def test_invalid_schema_compiles_nothing(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            p = sc.schema_path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"entities":[{"name":"a","fields":[{"name":"f","type":"blob"}]}]}',
                         encoding="utf-8")
            self.assertEqual(sc.compile_schema(out), [])
            self.assertFalse((out / sc.codegen.SCHEMA_JS).read_text().count("blob"))


class ScaffoldLifecycleTests(unittest.TestCase):
    """scaffold/template_ready/template_complete 的幂等与判定语义。"""

    def test_scaffold_writes_all_and_is_idempotent(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            written = sc.scaffold(out)
            self.assertEqual(sorted(written), sorted(sc.SCAFFOLD_FILES))
            for rel in sc.SCAFFOLD_FILES:
                self.assertTrue((out / rel).is_file(), rel)
            self.assertEqual(sc.scaffold(out), [], "second call writes nothing")
            # write-if-absent:已有文件不被覆写
            (out / ".npmrc").write_text("custom\n")
            sc.scaffold(out)
            self.assertEqual((out / ".npmrc").read_text(), "custom\n")

    def test_readiness_gates(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            self.assertFalse(sc.template_ready(out))
            self.assertFalse(sc.template_complete(out))
            sc.scaffold(out)
            self.assertTrue(sc.template_ready(out))
            self.assertTrue(sc.template_complete(out))
            # 缺 schema.js 占位 → 不完整(compiled 或被模型删过都算)
            (out / sc.SCHEMA_JSON_REL).parent.mkdir(parents=True, exist_ok=True)
            (out / "backend/src/database/schema.js").unlink()
            self.assertTrue(sc.template_ready(out))
            self.assertFalse(sc.template_complete(out))

    def test_model_owned_files_never_scaffolded(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            for rel in sc.MODEL_FILES:
                self.assertFalse((out / rel).exists(),
                                 f"{rel} 属模型独占,宿主不得落盘")


class TemplateStaticTests(unittest.TestCase):
    def test_js_no_blank_lines(self):
        # 无空行律只约束 backend JS(被模型经 repomap 反复消费)。
        for rel, (content, _role) in sc.SCAFFOLD_FILES.items():
            if rel.startswith("backend/") and rel.endswith(".js"):
                self.assertNotIn("\n\n", content, rel)

    def test_js_passes_node_check(self):
        node = _node_bin()
        if not node:
            self.skipTest("node not available")
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            for js in sorted(out.glob("backend/**/*.js")):
                r = subprocess.run([node, "--check", str(js)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{js}: {r.stderr}")


class ManifestConsistencyTests(unittest.TestCase):
    """防漂移:清单 ↔ 文字契约 ↔ 冒烟脚本 ↔ 模板内容互相咬合。"""

    def test_host_stack_rules_names_every_contract_path(self):
        # HOST_STACK_RULES 是发给模型的文字契约;清单里登记的契约路径
        # 必须逐一点名,否则模型会改写宿主文件或漏掉扩展点。
        for name in sc.RULES_MUST_NAME:
            self.assertIn(name, pe.HOST_STACK_RULES,
                          f"HOST_STACK_RULES 缺少 {name!r}")

    def test_cookie_name_single_sourced(self):
        # cookie 名在 AUTH_JS 模板与 template_smoke.js 断言链两处出现,
        # manifest 是唯一字面源。
        auth_js = sc.SCAFFOLD_FILES["backend/src/lib/auth.js"][0]
        self.assertIn(f"const COOKIE = '{sc.COOKIE_NAME}'", auth_js)
        smoke = (Path(__file__).resolve().parent / "template_smoke.js").read_text(
            encoding="utf-8")
        self.assertIn(sc.COOKIE_NAME, smoke)

    def test_manifest_covers_backend_js_and_frontend(self):
        backend_js = {rel for rel in sc.SCAFFOLD_FILES
                      if rel.startswith("backend/src") and rel.endswith(".js")}
        self.assertTrue({"backend/src/app.js", "backend/src/lib/auth.js",
                         "backend/src/database/schema.js"} <= backend_js)
        frontend_files = {rel for rel in sc.SCAFFOLD_FILES
                          if rel.startswith("frontend/")}
        self.assertTrue({"frontend/package.json", "frontend/src/main.jsx",
                         "frontend/src/app/App.jsx",
                         "frontend/src/app/store.jsx",
                         "frontend/src/app/api.js",
                         "frontend/src/app/styles.css"} <= frontend_files)

    def test_frontend_modular_contract_pieces_exist(self):
        # run8 教训:单文件 main.jsx 叠改造成 12 个回归失败。模块化契约的
        # 关键件必须在模板与文字契约两侧同时成立。
        app_jsx = sc.SCAFFOLD_FILES["frontend/src/app/App.jsx"][0]
        self.assertIn("ASSEMBLY LAYER", app_jsx)
        main_jsx = sc.SCAFFOLD_FILES["frontend/src/main.jsx"][0]
        self.assertIn("./app/App.jsx", main_jsx)
        self.assertNotIn("function App", main_jsx,
                         "main.jsx must stay a thin bootstrap, not carry App")
        store_jsx = sc.SCAFFOLD_FILES["frontend/src/app/store.jsx"][0]
        self.assertIn("useResource", store_jsx)
        rules = pe.HOST_STACK_RULES
        self.assertIn("frontend/src/features", rules)
        self.assertIn("NEVER edit another requirement's feature file", rules)

    def test_store_seed_runtime_split(self):
        # 种子/运行时双文件:db.json 是模型种子,db.run.json 是运行态,
        # 重启回种子(gitops 忽略 run 文件)。
        store_js = sc.SCAFFOLD_FILES["backend/src/database/store.js"][0]
        self.assertIn("db.run.json", store_js)
        self.assertIn("copyFileSync", store_js)
        from arcbench_agent_runtime.gitops import GitClient  # noqa: local check
        import inspect
        self.assertIn("backend/data/db.run.json",
                      inspect.getsource(GitClient.ensure_arc_gitignore))


class TelemetryTests(unittest.TestCase):
    """评测期遥测(2026-09-27):平台 func/e2e 失败明细不下发,应用自身把
    评测流量与前端异常落 octos-logs/eval-traffic.jsonl 随 zip 带回。
    三件套 = lib/eval-telemetry.js + app.js require 行 + index.html 钩子;
    scaffold 常规路径落盘,telemetry.ensure 在交付前兜底回补。"""

    def test_manifest_carries_telemetry_pieces(self):
        self.assertIn("backend/src/lib/eval-telemetry.js", sc.SCAFFOLD_FILES)
        self.assertEqual(
            sc.SCAFFOLD_FILES["backend/src/lib/eval-telemetry.js"][1], "host")
        app_js = sc.SCAFFOLD_FILES["backend/src/app.js"][0]
        self.assertIn("require('./lib/eval-telemetry').mount(app)", app_js)
        index_html = sc.SCAFFOLD_FILES["frontend/index.html"][0]
        self.assertIn("__arcTelemetryHook", index_html)
        self.assertIn("/api/__telemetry", index_html)

    def test_ensure_reinjects_after_model_rewrite(self):
        # 模型重写 app.js(保留 express 锚点)与 index.html(丢钩子)后,
        # 交付前兜底必须把 require 行与错误钩子注回,并写 marker 行。
        from scaffold import telemetry
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            (out / "backend/src/app.js").write_text(
                "// model rewrite\nconst express = require('express');\n"
                "const app = express();\nmodule.exports = app;\n",
                encoding="utf-8")
            (out / "frontend/index.html").write_text(
                "<!doctype html><html><head><title>x</title></head>"
                "<body><div id=\"root\"></div></body></html>",
                encoding="utf-8")
            self.assertTrue(telemetry.ensure(out, log=lambda *a: None))
            app_js = (out / "backend/src/app.js").read_text(encoding="utf-8")
            self.assertIn("eval-telemetry", app_js)
            self.assertIn("const app = express();", app_js)
            html = (out / "frontend/index.html").read_text(encoding="utf-8")
            self.assertIn("__arcTelemetryHook", html)
            self.assertLess(html.index("__arcTelemetryHook"),
                            html.index("</head>"))
            marker = (out / "octos-logs/eval-traffic.jsonl").read_text(
                encoding="utf-8").strip().splitlines()[-1]
            self.assertIn('"kind": "marker"', marker)
            # 幂等:第二次 ensure 不重复注入
            telemetry.ensure(out, log=lambda *a: None)
            self.assertEqual(
                (out / "frontend/index.html").read_text(encoding="utf-8"),
                html)

    def test_ensure_warns_when_anchor_missing(self):
        # app.js 无 express 锚点(极端重写)→ 返回 False 只告警,marker 仍写,
        # 其余件不受影响。
        from scaffold import telemetry
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            (out / "backend/src/app.js").write_text(
                "module.exports = require('express')();\n", encoding="utf-8")
            self.assertFalse(telemetry.ensure(out, log=lambda *a: None))
            self.assertTrue(
                (out / "octos-logs/eval-traffic.jsonl").is_file())


class TemplateSmokeTests(unittest.TestCase):
    def test_full_backend_flow(self):
        node = _node_bin()
        npm = shutil.which("npm")
        if not node or not npm:
            self.skipTest("node/npm not available")
        # output_dir 必须是工作区根(SCHEMA_JSON/SCHEMA_JS 为根相对路径)。
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            p = sc.schema_path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(VALID), encoding="utf-8")
            self.assertEqual(sc.compile_schema(out), [str(sc.codegen.SCHEMA_JS)])
            backend = out / "backend"
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
                   "npm_config_registry": sc.NPM_REGISTRY}
            r = subprocess.run([npm, "install", "--no-audit", "--no-fund",
                                "--loglevel", "error"],
                               cwd=backend, env=env, capture_output=True,
                               text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            smoke = Path(__file__).resolve().parent / "template_smoke.js"
            r = subprocess.run([node, str(smoke)], cwd=backend,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0,
                             f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()
