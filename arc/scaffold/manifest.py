"""脚手架单一事实源(参考 agent-arc-based 的 template.yaml 职责)。

路径知识此前散布 4 层——host_template.FILES(生成)、host_codegen(编译)、
main.py 的就绪判定、HOST_STACK_RULES 的文字契约——此处收敛为唯一清单:
文件清单/角色、就绪与完整判定键、schema 编译路径、cookie 名、npm 源。
防漂移一致性测试见 arc/scaffold_test.py。
"""

from __future__ import annotations

import json

from . import backend, frontend
from .frontend_libs import ROUTER_JS, UNDO_JS, CSV_JS  # noqa: F401
from .formula_template import FORMULA_JS
from .grid_template import GRID_JS

STACK_ID = "web-react-express"
# 09-27 版本对齐(镜像取证):runner 基础镜像默认 registry=npmjs.org——
# 船载 .npmrc 只在评测期被消费,必须与镜像一致;agent 期安装走
# prewarm/toolchain 的 env 覆盖(下方 INSTALL_REGISTRY,国内/本地提速),
# 不受船载 .npmrc 影响。
NPM_REGISTRY = "https://registry.npmjs.org/"
# agent 期安装(prewarm/toolchain/npm 探活)的 env 覆盖源
INSTALL_REGISTRY = "https://registry.npmmirror.com"
NPMRC = f"registry={NPM_REGISTRY}\n"

# 就绪判定键(宿主模板已落盘)——原 main.py 硬编码。
READY_KEYS = ("frontend", "frontend/src/app/App.jsx", "backend/src/app.js")

# 全部脚手架文件:rel → (内容, 角色)。
# 角色:host=宿主独占(模型禁改); seed=种子数据; model=模型独占
# (宿主绝不落盘,清单内仅作契约锚点)。
SCAFFOLD_FILES: dict[str, tuple[str, str]] = {
    "backend/.npmrc": (NPMRC, "host"),
    "backend/package.json": (json.dumps(backend.BACKEND_PACKAGE_JSON, indent=2) + "\n", "host"),
    "backend/src/index.js": (backend.INDEX_JS, "host"),
    "backend/src/app.js": (backend.APP_JS, "host"),
    "backend/src/lib/util.js": (backend.UTIL_JS, "host"),
    "backend/src/lib/session.js": (backend.SESSION_JS, "host"),
    "backend/src/lib/crud.js": (backend.CRUD_JS, "host"),
    "backend/src/lib/eval-telemetry.js": (backend.EVAL_TELEMETRY_JS, "host"),
    "backend/src/database/store.js": (backend.STORE_JS, "host"),
    "backend/src/routes/index.js": (backend.ROUTES_INDEX_JS, "host"),
    "backend/data/db.json": (backend.DB_JSON, "seed"),
    "frontend/package.json": (json.dumps(frontend.FRONTEND_PACKAGE_JSON, indent=2) + "\n", "host"),
    "frontend/index.html": (frontend.FRONTEND_INDEX_HTML, "host"),
    "frontend/src/main.jsx": (frontend.FRONTEND_MAIN_JSX, "host"),
    "frontend/src/app/App.jsx": (frontend.FRONTEND_APP_JSX, "host"),
    "frontend/src/app/store.jsx": (frontend.FRONTEND_STORE_JSX, "host"),
    "frontend/src/app/api.js": (frontend.FRONTEND_API_JS, "host"),
    # UI kit:满分形态代码化(NativeSelect/AppDialog/AccountTrigger/
    # SingleError/FixedGrid)——提示词规则的方差靠默认路径消零
    "frontend/src/app/ui.jsx": (frontend.FRONTEND_UI_JSX, "host"),
    # 09-28:网格选区/编辑内核 + 单元格公式引擎(纯函数、零任务语义,
    # 手写形态两跑翻车后下沉为宿主代码;未 import 时 vite 不打包)
    "frontend/src/app/grid.js": (GRID_JS, "host"),
    "frontend/src/app/formula.js": (FORMULA_JS, "host"),
    # 09-28 技术选型层:csv(RFC4180/字节精确/TSV/下载)、undo(快照栈)、
    # router(hash 深链)——两题取证的高频造轮子点,库式基座非生成式代答
    "frontend/src/app/csv.js": (CSV_JS, "host"),
    "frontend/src/app/undo.js": (UNDO_JS, "host"),
    "frontend/src/app/router.js": (ROUTER_JS, "host"),
    "frontend/src/app/styles.css": (frontend.FRONTEND_STYLES_CSS, "host"),
    "frontend/.npmrc": (NPMRC, "host"),
    ".npmrc": (NPMRC, "host"),
    # B2:网格编辑器内核模式契约(只读参考,模型在此目录落 feature 文件)
    "frontend/src/features/README.md": (frontend.FEATURES_README, "host"),
}
# 模型独占文件:宿主不落盘,清单内仅登记路径供 prompt/契约引用。
# frontend/src/features/ 是模型的 UI 扩展面(每需求域一个自包含文件);
# backend/src/routes/ 是模型的全部 API 面(auth+业务,09-28 CRUD 生成器
# 退役后数据模型/端点形状全归模型)——宿主只落 routes/index.js 空扩展点。
# 例外:taskshape 首次播种的家族 stub(仅文件不存在时写,永不覆盖)。
MODEL_FILES = ("frontend/src/features/*.jsx", "backend/src/routes/*.js")

# HOST_STACK_RULES(plan_envelope 文字契约)必须逐一点名的路径拼写。
# 拼写按契约原文(backend 相对路径),防漂移测试逐条断言覆盖。
RULES_MUST_NAME = (
    "src/app.js",
    "src/index.js",
    "src/lib/util.js",
    "src/lib/session.js",
    "src/lib/crud.js",
    "src/database/store.js",
    "src/routes",
    "backend/data/db.json",
    "backend/data/seed-accounts.json",
    "frontend/dist",
    "frontend/src/main.jsx",
    "frontend/src/app/App.jsx",
    "frontend/src/app/store.jsx",
    "frontend/src/app/api.js",
    "frontend/src/app/ui.jsx",
    "frontend/src/app/grid.js",
    "frontend/src/app/formula.js",
    "frontend/src/app/csv.js",
    "frontend/src/app/undo.js",
    "frontend/src/app/router.js",
    "frontend/src/app/styles.css",
    "frontend/src/features",
)
