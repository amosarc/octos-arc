#!/bin/sh
# 把 arc/ 打成 ARC 平台要的提交包。
# 平台规范：zip 根必须有 main.py / requirements.txt /
# template/(frontend+backend+README.md+template.yaml)；平台把 template/ 铺成
# 初始工作区，再调 python3 main.py <requirements-source> --output-dir <workspace>。
# 布局参考 agent-arc-based：入口在根、模板在 template/，开发件(测试脚本、
# 打分器、本地题目副本)一律不进包。
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

# 平台跑 Linux x86_64：内核二进制必须先就位（README「Linux x86_64 发布」流程）。
[ -f bin/octos ] || { echo "缺少 bin/octos；先交叉编译并拷入（见 README）"; exit 1; }
file bin/octos | grep -q "ELF 64-bit LSB.*x86-64" || { echo "bin/octos 不是 linux x86-64 ELF"; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/octos-arc-bundle"
mkdir -p "$PKG"

# template/ 打包时由 scaffold 单源直接现生成到 arc/template/（打包即物化：
# 仓库里始终有可浏览的模板目录，内容永远与单源一致；scaffold 只写不删，
# 故先清空再生成，manifest 删掉的条目不会残留）。
rm -rf template
python3 - <<'PYEOF'
import sys, pathlib
sys.path.insert(0, ".")
import scaffold
written = scaffold.scaffold(pathlib.Path("template"))
print("template/: 由 scaffold 现生成 %d 件" % len(written))
PYEOF
cat > template/template.yaml <<'EOF'
schema_version: "1.0"
id: web-react-express
name: Web Application (React + Express)
type: web_app
version: 1.0.0
description: Full-stack React and Express application template for generation agents.
stack:
  frontend:
    framework: Vite
    library: React
    language: JavaScript
  backend:
    framework: Express
    language: JavaScript
    database: JSON file store (backend/data/db.json)
  tests:
    e2e: Playwright
copy:
  exclude:
    - template.yaml
    - frontend/node_modules
    - frontend/dist
    - backend/node_modules
preview:
  html: frontend/dist/index.html
  assets: frontend/dist/assets
agent_guidance:
  ui_root: frontend/src
  api_root: backend/src/routes
  function_root: backend/src
  db_root: backend/src/database
  e2e_test_root: .arc/acceptance
EOF
cat > template/README.md <<'EOF'
# Web Template

Single-origin web application: Express serves `/api/*` routes and the built
frontend from `frontend/dist` (SPA fallback), listening on `PORT` plus every
port in `ARC_EXTRA_PORTS`.

- `frontend/`: React + Vite; `npm run build` produces `frontend/dist/`.
- `backend/`: Express; JSON store at `backend/data/db.json` (the graded
  seed — runtime state goes to `db.run.json`, which resets to the seed on
  every restart and never ships). The host owns only the substrate
  (`src/app.js`, `src/index.js`, `src/lib/util.js`, `src/database/store.js`);
  ALL API endpoints — auth included — are implemented by the agent in
  `backend/src/routes/`.
- npm dependencies are prewarmed by the host; never run npm install in turns.
EOF

# 运行时必需：main.py 顶层 import(plan_envelope/fixture_manifest/scaffold/
# arcbench_agent_runtime) + 惰性 import(octos_stdio) + 内核(bin/) + 初始
# 模板(template/)
# (fixture_manifest.py:夹具契约提取/宿主种子编译——main.py 顶层依赖,漏打即启动崩)
# (clock.py:时间戳单一时钟源(北京时间UTC+8)——main.py/事件流/追溯表/
#  遥测共享,main.py 顶层依赖,漏打即启动崩)
# (visual_ref.py 已退役(2026-09-28):参考图由内核在含图工单的 turn 直接
#  作为 user 消息媒体附件,宿主预描述通道删除)
# (09-27 目标优先定则:ui_smoke/flow_suite 宿主自写验收套件已删除——
#  测试用例由模型根据需求自生成,宿主只落盘 .arc/ui-smoke 最小骨架)
# public-tests 不进包(2026-09-26 正式比赛决策):平台挂载 /workspace/tests
# 或 ARCBENCH_TESTS_DIR 优先生效;两者皆缺的任务走 no-staged-specs 的
# 模型自测契约(main.py locate_acceptance_tests 的包内回落分支因此休眠)。
cp main.py plan_envelope.py fixture_manifest.py octos_stdio.py clock.py requirements.txt "$PKG/"
cp -R arcbench_agent_runtime scaffold bin "$PKG/"
# 自验工具链零打包(2026-09-27 决策):生产/仿真 runner 镜像自带全局
# @playwright/test@1.57.0 + chromium-1200,_ensure_host_toolchain /
# ui_smoke._lg_cli 直接解析全局 cli(与评测引擎同一份二进制,零安装
# 零网络);平台评测只用镜像全局工具链,与提交包零交互——local-grader
# 彻底移出比赛包(2026-09-20 的 ENOENT 教训随之失效:兜底路径改为
# tmpdir + 钉死 PLATFORM_PW_VERSION 安装,不再依赖包内清单)。
cp -R template "$PKG/template"

# 先 import 后清理：python 生成步骤会重新产生 __pycache__，收尾统一扫掉
# （先清理后生成会让 pyc 回流进包——历史踩坑：linux 包脚本曾因此翻车。）
cd "$PKG"
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -o -name .DS_Store -delete

rm -f "$ROOT/../octos-arc-bundle.zip"
zip -qr "$ROOT/../octos-arc-bundle.zip" .
echo "打包完成：$ROOT/../octos-arc-bundle.zip"
shasum -a 256 "$ROOT/../octos-arc-bundle.zip"
