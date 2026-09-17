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
        self.assertTrue({"frontend/package.json", "frontend/src/main.jsx"}
                        <= frontend_files)


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
