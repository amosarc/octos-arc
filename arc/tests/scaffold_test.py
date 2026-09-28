"""scaffold 包测试:template-static/template-smoke + 防漂移一致性。

用法: python3 arc/tests/scaffold_test.py -v
模板冒烟会真实 npm install express(npmmirror)并跑 node 端到端流程;
node/npm 缺失时对应用例跳过。

一致性组(ManifestConsistencyTests)是封装的核心回归面:路径知识此前
散布 4 层(生成/编译/就绪判定/文字契约),收敛到 manifest 后用测试钉住
"清单 ↔ 模板内容 ↔ 文字契约 ↔ 冒烟脚本"四者不漂移——这正是参考项目
agent-arc-built(template.yaml 无代码执行)踩过的坑。
09-28 CRUD 生成器退役:schema 编译组(LoadSchema/CompileSchema)随
codegen.py 删除;基座冒烟只承诺装配/健康/存储/重启回种子。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plan_envelope as pe  # noqa: E402
import scaffold as sc  # noqa: E402


def _node_bin() -> str | None:
    return shutil.which("node")


class TaskShapeTests(unittest.TestCase):
    """taskshape:需求树 → 家族 stub 预生成(幂等/不覆盖/工单头)。"""

    MINI_TREE = {
        "id": "ROOT", "type": "FOLDER", "children": [
            {"id": "REQ-1", "type": "FOLDER", "name": "Identity & Access",
             "children": [
                 {"id": "REQ-1-1", "type": "ATOMIC", "name": "Sign Up",
                  "description": "d", "children": []},
                 {"id": "REQ-1-2", "type": "ATOMIC", "name": "Sign In",
                  "description": "d", "children": []}]},
            {"id": "REQ-2", "type": "FOLDER", "name": "Dashboards/Views",
             "children": [
                 {"id": "REQ-2-1", "type": "ATOMIC", "name": "Overview",
                  "description": "d", "children": []}]},
        ]}

    def test_task_stubs_families_and_map(self):
        stubs, nmap = sc.task_stubs(self.MINI_TREE)
        slugs = [s["slug"] for s in stubs]
        self.assertEqual(len(slugs), 2)
        self.assertTrue(slugs[0].startswith("req1-"))
        self.assertIn("identity", slugs[0])
        # 非法字符入 slug(斜杠/&)必须被清洗
        self.assertNotIn("/", slugs[1]); self.assertNotIn("&", slugs[0])
        self.assertEqual(nmap["REQ-1-1"], slugs[0] + ".jsx")
        self.assertEqual(nmap["REQ-2-1"], slugs[1] + ".jsx")
        comp = stubs[0]["component"]
        self.assertTrue(comp[0].isalpha() and comp[0].isupper())

    def test_stub_headers_carry_work_order(self):
        from scaffold import taskshape
        stubs, _ = sc.task_stubs(self.MINI_TREE)
        text = taskshape._feature_stub(stubs[0])
        self.assertIn("REQ-1-1 Sign Up", text)
        self.assertIn("host-pre-created", text.lower())
        self.assertIn("export default function", text)
        rtext = taskshape._route_stub(stubs[0])
        self.assertIn("module.exports = (app)", rtext.replace(" => {", " = (app)").replace("};", "")) or \
            self.assertIn("module.exports = (app) => {", rtext)

    def test_scaffold_task_features_idempotent_no_clobber(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            first = sc.scaffold_task_features(out, self.MINI_TREE)
            self.assertGreater(len(first), 2)
            stub = out / "frontend/src/features" / (
                sc.task_stubs(self.MINI_TREE)[0][0]["slug"] + ".jsx")
            self.assertTrue(stub.is_file())
            # 幂等:第二遍零写入
            self.assertEqual(sc.scaffold_task_features(out, self.MINI_TREE), [])
            # 模型已写内容永不覆盖
            stub.write_text("// model work\n", encoding="utf-8")
            app = out / "frontend/src/app/App.jsx"
            app.write_text("// model app\n", encoding="utf-8")
            sc.scaffold_task_features(out, self.MINI_TREE)
            self.assertEqual(stub.read_text(encoding="utf-8"), "// model work\n")
            self.assertEqual(app.read_text(encoding="utf-8"), "// model app\n")

    def test_wiring_is_pristine_only(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            sc.scaffold_task_features(out, self.MINI_TREE)
            routes = out / "backend/src/routes/index.js"
            text = routes.read_text(encoding="utf-8")
            self.assertIn("require('./", text)
            import subprocess, sys as _sys
            node = _node_bin()
            if node:
                for js in routes.parent.glob("*.js"):
                    r = subprocess.run([node, "--check", str(js)],
                                       capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"{js}: {r.stderr}")


class ScaffoldLifecycleTests(unittest.TestCase):
    """scaffold/template_ready 的幂等与判定语义。"""

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

    def test_readiness_gate(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            self.assertFalse(sc.template_ready(out))
            sc.scaffold(out)
            self.assertTrue(sc.template_ready(out))

    def test_generator_files_are_gone(self):
        # 09-28 CRUD 生成器退役:生成层文件不再是模板的一部分,
        # scaffold 包也不再暴露编译面(防回退)。
        for rel in ("backend/src/lib/validate.js", "backend/src/lib/auth.js",
                    "backend/src/database/engine.js",
                    "backend/src/database/schema.js"):
            self.assertNotIn(rel, sc.SCAFFOLD_FILES)
        for name in ("compile_schema", "load_schema", "schema_path",
                     "Watcher", "template_complete"):
            self.assertFalse(hasattr(sc, name), name)
        app_js = sc.SCAFFOLD_FILES["backend/src/app.js"][0]
        self.assertNotIn("database/schema", app_js)
        self.assertNotIn("lib/auth", app_js)
        self.assertNotIn("database/engine", app_js)

    def test_model_owned_files_never_scaffolded(self):
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            # 模型独占面:features 业务组件(README 契约除外)与 schema
            # 声明(已随生成器退役,宿主不再落 starter)。
            self.assertFalse((out / "backend/data/schema.json").exists())
            self.assertFalse(
                any((out / "frontend/src/features").glob("*.jsx")),
                "features 业务文件属模型,宿主只落 README.md")
            # routes/ 宿主只落空扩展点 index.js;家族业务模块(taskshape
            # stub)只在 scaffold_task_features 时播种。
            self.assertEqual(
                [p.name for p in (out / "backend/src/routes").glob("*.js")],
                ["index.js"])


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
        # HOST_STACK_RULES_GOAL_FIRST 是发给模型的文字契约;清单里登记的
        # 契约路径必须逐一点名,否则模型会改写宿主文件或漏掉扩展点。
        for name in sc.RULES_MUST_NAME:
            self.assertIn(name, pe.HOST_STACK_RULES_GOAL_FIRST,
                          f"goal-first rules 缺少 {name!r}")

    def test_manifest_covers_backend_js_and_frontend(self):
        backend_js = {rel for rel in sc.SCAFFOLD_FILES
                      if rel.startswith("backend/src") and rel.endswith(".js")}
        self.assertTrue({"backend/src/app.js", "backend/src/index.js",
                         "backend/src/lib/util.js",
                         "backend/src/lib/session.js",
                         "backend/src/lib/crud.js",
                         "backend/src/database/store.js",
                         "backend/src/routes/index.js"} <= backend_js)
        frontend_files = {rel for rel in sc.SCAFFOLD_FILES
                          if rel.startswith("frontend/")}
        self.assertTrue({"frontend/package.json", "frontend/src/main.jsx",
                         "frontend/src/app/App.jsx",
                         "frontend/src/app/store.jsx",
                         "frontend/src/app/api.js",
                         "frontend/src/app/csv.js",
                         "frontend/src/app/undo.js",
                         "frontend/src/app/router.js",
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
        rules = pe.HOST_STACK_RULES_GOAL_FIRST
        self.assertIn("frontend/src/features", rules)
        self.assertIn("GRID EDITOR KERNEL", rules)

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
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
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

    def test_frontend_build_and_csv_lib(self):
        """vite 集成构建(含 undo/router 的 react import)+ csv.js 纯函数
        行为单测(ESM,frontend package.json type=module 使 node 直测)。"""
        node = _node_bin()
        npm = shutil.which("npm")
        if not node or not npm:
            self.skipTest("node/npm not available")
        with TemporaryDirectory() as td:
            out = Path(td)
            sc.scaffold(out)
            frontend = out / "frontend"
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
                   "npm_config_registry": sc.NPM_REGISTRY}
            r = subprocess.run([npm, "install", "--no-audit", "--no-fund",
                                "--loglevel", "error"],
                               cwd=frontend, env=env, capture_output=True,
                               text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            # vite build:App/store 集成 sanity(注:未接线工具箱库不在
            # import 图里,vite 不会碰它们——语法门在下方 esbuild)。
            r = subprocess.run([npm, "run", "build"], cwd=frontend, env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0,
                             f"stdout:\n{r.stdout[-800:]}\nstderr:\n{r.stderr[-800:]}")
            # 语法门:工具箱库不在 vite 构建图内,esbuild transform 逐个
            # 解析(undo/router 的 react import 在 transform 模式不解析)。
            esbuild = frontend / "node_modules" / ".bin" / "esbuild"
            libs = ["src/app/undo.js", "src/app/router.js",
                    "src/app/grid.js", "src/app/formula.js",
                    "src/app/csv.js", "src/app/ui.jsx"]
            r = subprocess.run([str(esbuild), *libs,
                                f"--outdir={out / 'esb-out'}"],
                               cwd=frontend, env=env, capture_output=True,
                               text=True, timeout=120)
            self.assertEqual(r.returncode, 0,
                             f"stdout:\n{r.stdout[-500:]}\nstderr:\n{r.stderr[-500:]}")
            # JSX/ESM 语法门(esbuild 就位于本用例 npm install 之后):
            # 截断的 .jsx 与截断的 ESM .js 必须被 js_syntax_errors 抓到,
            # 正常模板件必须零误报。
            import main as arc_main
            (frontend / "src" / "Broken.jsx").write_text(
                "export default function Broken() {\n  return (<div>\n",
                encoding="utf-8")
            (frontend / "src" / "app" / "Broken.js").write_text(
                "export const x = (\n", encoding="utf-8")
            errs = arc_main.js_syntax_errors(out)
            self.assertTrue(
                any("Broken.jsx" in e for e in errs), errs)
            self.assertTrue(
                any("Broken.js" in e for e in errs), errs)
            self.assertTrue(
                all("Broken" in e for e in errs),
                f"false positives on pristine template files: {errs}")
            (frontend / "src" / "Broken.jsx").unlink()
            (frontend / "src" / "app" / "Broken.js").unlink()
            # csv.js 行为单测 + grid.js compareValues 单测:零依赖纯函数,
            # node ESM 直测(grid 的 react import 从 frontend/node_modules 解析)。
            csv_url = (frontend / "src" / "app" / "csv.js").as_uri()
            grid_url = (frontend / "src" / "app" / "grid.js").as_uri()
            harness = r'''
import { parseCSV, stringifyCSV, parseTSV } from "__CSV_URL__";
import { compareValues } from "__GRID_URL__";
const ok = (n, c) => { if (!c) { console.error('FAIL ' + n); process.exit(1); } console.log('PASS ' + n); };
let r = parseCSV('a,b\n1,2');
ok('basic', !r.error && JSON.stringify(r.rows) === '[["a","b"],["1","2"]]');
r = parseCSV('"x,y","he said ""hi""","line1\nline2"');
ok('quoted', !r.error && r.rows[0][0] === 'x,y' && r.rows[0][1] === 'he said "hi"' && r.rows[0][2] === 'line1\nline2');
r = parseCSV('\uFEFFa,b');
ok('bom', !r.error && r.rows[0][0] === 'a');
ok('unterminated', !!parseCSV('"oops').error);
r = parseCSV('a,b\n1,2\n');
ok('trailing-newline', !r.error && r.rows.length === 2);
const out = stringifyCSV([['a','b'],['x,y','he said "hi"']]);
ok('stringify-byte-exact', out === 'a,b\r\n"x,y","he said ""hi"""\r\n');
ok('roundtrip', !parseCSV(out).error);
r = parseTSV('a\tb\nc\td');
ok('tsv', !r.error && JSON.stringify(r.rows) === '[["a","b"],["c","d"]]');
r = parseCSV('a,,c');
ok('empty-cells', !r.error && r.rows[0][1] === '' && r.rows[0][2] === 'c');
ok('cmp-numeric-text', compareValues('9', '10') < 0 && compareValues(2, 10) < 0);
ok('cmp-date', compareValues('2026-01-02', '2026-03-01') < 0);
ok('cmp-text', compareValues('abc', 'abd') < 0);
ok('cmp-mixed-falls-to-text', compareValues('abc', '5') > 0);
ok('cmp-empty-vs-value', compareValues('', 'x') < 0);
const sorted = ['10', '9', 'abc', '2026-02-01', '2025-12-31', '2']
  .sort(compareValues);
ok('cmp-full-sort', JSON.stringify(sorted) === JSON.stringify(
  ['2', '9', '10', '2025-12-31', '2026-02-01', 'abc']));
console.log('libs harness OK');
'''.replace("__CSV_URL__", csv_url).replace("__GRID_URL__", grid_url) + "\n"
            harness_path = Path(td) / "csv_harness.mjs"
            harness_path.write_text(harness, encoding="utf-8")
            r = subprocess.run([node, str(harness_path)], cwd=frontend,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0,
                             f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()
