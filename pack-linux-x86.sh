#!/bin/sh
# 自包含 linux x86_64 运行包：octos(musl 静态) + arc 运行时 + 启动脚本。
# 用法：sh pack-linux-x86.sh   （需先完成 cross/docker musl 编译）
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
BIN=target/x86_64-unknown-linux-gnu/release/octos   # cargo zigbuild, glibc 2.17
[ -f "$BIN" ] || { echo "缺少 $BIN；先跑容器交叉编译"; exit 1; }
file "$BIN" | grep -q "ELF 64-bit LSB.*x86-64" || { echo "$BIN 不是 linux x86-64 ELF"; exit 1; }
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/octos-arc-linux-x86/bin"
# 平台规范：main.py / requirements.txt 必须在 zip 根目录（与 pack.sh 一致），
# 二进制放 bin/，由 run-ticket.sh 设 OCTOS_BIN 指向。
cp "$BIN" "$STAGE/octos-arc-linux-x86/bin/octos"
chmod +x "$STAGE/octos-arc-linux-x86/bin/octos"
cp -R arc/. "$STAGE/octos-arc-linux-x86/"
cd "$STAGE/octos-arc-linux-x86"
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete
rm -rf arc-output local-grader/node_modules local-grader/run \
       local-grader/test-results .env octos-arc-bundle.zip
# arc/template/ 是 pack.sh「打包即物化」留在仓库的可浏览副本，随 cp -R
# 进了暂存区；scaffold 只写不删，先清空再生成（与 pack.sh 同一语义），
# 否则 copytree 撞上已存在的 template/frontend 直接 FileExistsError。
rm -rf template
cat > run-ticket.sh <<'EOF'
#!/bin/sh
# 用法: OCTOS_MODEL=<model> OPENAI_API_KEY=<key> OPENAI_BASE_URL=<url> ./run-ticket.sh <task-dir> [输出目录]
set -e
cd "$(dirname "$0")"
export OCTOS_BIN="$(pwd)/bin/octos"
: "${ARCBENCH_WEB_PORT:=43100}" "${OCTOS_SMOKE_PORT:=43101}"
export ARCBENCH_WEB_PORT OCTOS_SMOKE_PORT
exec python3 run-task-local.py "$@"
EOF
chmod +x run-ticket.sh
# 平台规范：zip 根需含 template/ 目录(frontend/ + backend/ + README.md + template.yaml)，
# 平台以其为初始工作区模板。内容用包内 scaffold 现生成，与宿主运行时写入字节一致。
python3 - <<PYEOF
import sys, pathlib, shutil, tempfile
sys.path.insert(0, ".")
import scaffold
tmp = tempfile.mkdtemp()
try:
    scaffold.scaffold(pathlib.Path(tmp))
    tpl = pathlib.Path("template")
    tpl.mkdir(exist_ok=True)
    for d in ("frontend", "backend"):
        shutil.copytree(f"{tmp}/{d}", tpl / d)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
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
  api_root: backend/src/app.js
  function_root: backend/src
  db_root: backend/src/database
  schema_source: backend/data/schema.json
  e2e_test_root: .arc/acceptance
EOF
cat > template/README.md <<'EOF'
# Web Template

Single-origin web application: Express serves `/api/*` routes and the built
frontend from `frontend/dist` (SPA fallback), listening on `PORT` plus every
port in `ARC_EXTRA_PORTS`.

- `frontend/`: React + Vite; `npm run build` produces `frontend/dist/`.
- `backend/`: Express; data model is declared in `backend/data/schema.json`
  and compiled by the host into `backend/src/database/schema.js`; JSON store
  at `backend/data/db.json`; auth (register/login/logout/me) and CRUD are
  engine-driven; task-specific endpoints go in `backend/src/routes/index.js`.
- npm dependencies are prewarmed by the host; never run npm install in turns.
EOF
cd "$STAGE/octos-arc-linux-x86"
rm -f "$ROOT/octos-arc-linux-x86.zip"
# 先 import 后清理（同 pack.sh 的坑注释）：上面的 python 生成步骤会重新
# 产生 __pycache__/pyc，收尾统一扫掉，否则 pyc 回流进包。
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete
find . -name .DS_Store -delete
zip -qr "$ROOT/octos-arc-linux-x86.zip" .
echo "打包完成: $ROOT/octos-arc-linux-x86.zip"
shasum -a 256 "$ROOT/octos-arc-linux-x86.zip"
