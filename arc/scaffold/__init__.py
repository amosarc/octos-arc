"""宿主脚手架包——唯一公开面(参考 agent-arc-based 的组织:清单单源 +
小工厂面)。模板为 Python 字符串常量(backend.py/frontend.py),路径与
身份知识收敛在 manifest.py;调用方只 import 本包,不再触碰具体文件路径。

外部入口:
    scaffold(output_dir)              幂等落盘 27 件模板,返回本次写入 rel
    template_ready(output_dir)        宿主模板已就位(frontend/ + app.js)
    prewarm_deps(output_dir, log)     宿主代装 backend/frontend 依赖
    task_stubs / scaffold_task_features   需求树→家族 stub 预生成(taskshape)
    stage_selftest_dir(output_dir, smoke_port)  e2e/ 自测骨架(config+示例)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import backend, frontend, manifest  # noqa: F401  (re-export 便于排查)
from .taskshape import scaffold_task_features, task_stubs
from .selftest import stage_selftest_dir  # noqa: F401
from .manifest import (  # noqa: F401
    INSTALL_REGISTRY,
    MODEL_FILES,
    NPM_REGISTRY,
    READY_KEYS,
    RULES_MUST_NAME,
    SCAFFOLD_FILES,
    STACK_ID,
)

__all__ = [
    "scaffold", "template_ready", "prewarm_deps",
    "task_stubs", "scaffold_task_features",
    "SCAFFOLD_FILES", "READY_KEYS",
    "NPM_REGISTRY", "INSTALL_REGISTRY", "STACK_ID",
    "RULES_MUST_NAME",
    "backend", "frontend", "manifest",
]


# 宿主自有件=运行时必覆盖(增量演化下随 kit 升级):frame.jsx 的页头
# chrome 契约是宿主决策——10-07 16 分案实证模型曾自植 HeaderAuth 把
# "Sign in"入口放上页头,评分器 main 域定位 24 场景全灭;kit 收口后
# 每轮以当前版覆盖,入口链接归位 <main> 的义务由 RULES 承载。
HOST_REFRESH_FILES = {"frontend/src/app/frame.jsx"}


def scaffold(output_dir: Path, log=print) -> list[str]:
    """幂等落盘完整可启动模板(write-if-absent),返回本次写入的相对路径。

    backend 基座(HTTP/cookie/存储/遥测 + routes 空扩展点)与 frontend
    五件套由宿主预置;业务语义(routes 里的全部 API、种子数据、UI)留给
    模型——见 manifest 内各文件角色。例外:HOST_REFRESH_FILES(页头
    chrome 契件 frame.jsx)无条件覆盖——宿主自有面,随 kit 演进刷新。
    """
    root = Path(output_dir)
    written: list[str] = []
    for rel, (content, _role) in SCAFFOLD_FILES.items():
        target = root / rel
        if target.exists() and rel not in HOST_REFRESH_FILES:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            log(f"[scaffold] host-owned {rel} refreshed (kit upgrade)")
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def _key_present(root: Path, rel: str) -> bool:
    target = root / rel
    return target.is_dir() if "." not in target.name else target.is_file()


def template_ready(output_dir: Path) -> bool:
    """宿主模板已落盘(main.py 的就绪判定收敛于此)。"""
    root = Path(output_dir)
    return all(_key_present(root, rel) for rel in READY_KEYS)


def _npm_bin() -> str:
    """npm 可执行文件:PATH 优先,回退 NODE_BIN 目录。"""
    npm = shutil.which("npm")
    if npm:
        return npm
    node_bin = os.environ.get("NODE_BIN", "").strip().rstrip("/")
    if node_bin:
        cand = os.path.join(node_bin, "npm")
        if os.path.isfile(cand):
            return cand
    return "npm"


def prewarm_deps(output_dir: Path, log=print) -> None:
    """宿主代装依赖(plan §3):任何模型轮都不再跑 npm install。

    非致命:失败只告警,rehearsal 与模型轮仍可重试。
    """
    env = dict(os.environ,
               npm_config_registry=INSTALL_REGISTRY,
               NPM_CONFIG_REGISTRY=INSTALL_REGISTRY)
    for sub in ("backend", "frontend"):
        d = Path(output_dir) / sub
        if not (d / "package.json").is_file():
            continue
        t0 = time.time()
        try:
            r = subprocess.run(
                [_npm_bin(), "install", "--no-audit", "--no-fund",
                 "--loglevel", "error"],
                cwd=d, env=env, capture_output=True, text=True, timeout=300)
            log(f"[prewarm] npm install {sub}: rc={r.returncode} "
                f"in {time.time() - t0:.0f}s"
                + ("" if r.returncode == 0 else f" stderr={r.stderr[-200:]!r}"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"[prewarm] npm install {sub} failed (non-fatal): {exc}")
