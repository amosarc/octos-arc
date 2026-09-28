"""taskshape — 需求树 → 任务形状的目录预生成。
初始化时直接落盘每家族的
feature stub 与路由模块 stub,头注释就是该家族的 REQ 工单清单;
节点轮提示词点名本批的家族文件。模型从"推理划分+实现"降为"填空"。

纪律:stub 是模型独占路径的宿主首次播种——只在文件不存在时写
(重跑安全,模型已写的内容永不覆盖);App.jsx/routes/index.js 只在
仍是脚手架原始字节时才改写为装配版(模型动过就留着,模型自己接)。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import backend, frontend

FEATURES_DIR = "frontend/src/features"
ROUTES_DIR = "backend/src/routes"
_MAX_FAMILIES = 12
_MAX_NODE_LINES = 12


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:48].strip("-")
    return s or fallback


def _component(text: str, fallback: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", str(text or "")) if w]
    name = "".join(w[:1].upper() + w[1:] for w in words[:5]) or fallback
    return re.sub(r"[^A-Za-z0-9_]", "", name)


def _atomics_with_family(tree: dict) -> list[tuple[str, str, list[dict]]]:
    """(family_slug, family_component, [atomic nodes]) 按文档序。

    家族 = ROOT 直接子 FOLDER;其子树内的全部 ATOMIC 归该家族。
    挂在 ROOT 下的散原子归入 fallback 家族(罕见)。"""
    families: list[tuple[str, str, list[dict]]] = []

    def collect(node) -> list[dict]:
        out: list[dict] = []
        if str(node.get("type", "")).upper() == "ATOMIC":
            out.append(node)
        for child in node.get("children") or []:
            out.extend(collect(child))
        return out

    loose: list[dict] = []
    for folder in (tree or {}).get("children") or []:
        nodes = collect(folder)
        if not nodes:
            continue
        fid = str(folder.get("id") or "")
        fname = str(folder.get("name") or fid or "domain")
        prefix = fid.replace("REQ-", "req").replace("-", "").lower()
        slug = (prefix + "-" + _slug(fname, "domain")) if prefix \
            else _slug(fname, "domain")
        families.append((slug, _component(fname, "Domain"), nodes))
    if loose:
        families.append(("app-misc", "AppMisc", loose))
    return families[:_MAX_FAMILIES]


def _work_order(nodes: list[dict]) -> list[str]:
    lines = []
    for n in nodes[:_MAX_NODE_LINES]:
        nid = str(n.get("id") or "")
        name = " ".join(str(n.get("name") or "").split())
        lines.append(f"//   {nid} {name}"[:110])
    if len(nodes) > _MAX_NODE_LINES:
        lines.append(f"//   … +{len(nodes) - _MAX_NODE_LINES} more node(s) "
                     "in this family (see your CONTRACT/FOCUS sections)")
    return lines


def task_stubs(tree: dict) -> tuple[list[dict], dict[str, str]]:
    """需求树 → (stub 描述列表, {atomic_id: feature 文件名})。

    stub 描述:{slug, component, nodes}。确定性:同一棵树输出相同。
    """
    out: list[dict] = []
    node_map: dict[str, str] = {}
    for slug, component, nodes in _atomics_with_family(tree):
        out.append({"slug": slug, "component": component, "nodes": nodes})
        fname = f"{slug}.jsx"
        for n in nodes:
            nid = str(n.get("id") or "")
            if nid:
                node_map[nid] = fname
    return out, node_map


def _feature_stub(stub: dict) -> str:
    order = _work_order(stub["nodes"])
    return "\n".join([
        f"// {stub['slug']}.jsx — HOST-PRE-CREATED stub for one requirement",
        "// family (fill this file IN PLACE; keep the default export; add",
        "// sibling files when a sub-domain warrants, and wire/rearrange",
        "// mounts in src/app/App.jsx). The grid kernel pattern for grid-",
        "// like families is in frontend/src/features/README.md.",
        "// This family covers:",
        *order,
        "import React from 'react';",
        "",
        f"export default function {stub['component']}() {{",
        "  return null;",
        "}",
    ]) + "\n"


def _route_stub(stub: dict) -> str:
    order = _work_order(stub["nodes"])
    return "\n".join([
        f"// routes/{stub['slug']}.js — HOST-PRE-CREATED stub for this",
        "// requirement family's task-specific endpoints. Register them on",
        "// the passed app; store/lib helpers per the HOST CONTRACT. This",
        "// family covers:",
        *order,
        "module.exports = (app) => {",
        "  // this family's endpoints",
        "};",
    ]) + "\n"


def _app_wiring(stubs: list[dict]) -> str:
    imports = "\n".join(
        f"import {s['component']} from '../features/{s['slug']}.jsx';"
        for s in stubs)
    mounts = "\n".join(
        f"        <{s['component']} />" for s in stubs)
    return f"""\
import React from 'react';
import {{ DataProvider }} from './store.jsx';
{imports}

// ASSEMBLY LAYER — the only file that wires features together.
// Stubs are pre-mounted per requirement family in requirement order;
// rearrange the layout/mounts freely as the app takes shape (business
// logic NEVER lives here — it goes in frontend/src/features/<name>.jsx).
export default function App() {{
  return (
    <DataProvider>
      <div className="app">
{mounts}
      </div>
    </DataProvider>
  );
}}
"""


def _routes_wiring(stubs: list[dict]) -> str:
    requires = "\n".join(
        f"  require('./{s['slug']}')(app); // {s['nodes'][0].get('id', '')}"
        if s["nodes"] else
        f"  require('./{s['slug']}')(app);"
        for s in stubs)
    return f"""\
// routes/index.js — task-specific endpoints, pre-split per requirement
// family (host-seeded). Each family module is a plain (app) => {{}}
// registering its own routes; implement families in their files.
module.exports = (app) => {{
{requires}
}};
"""


def scaffold_task_features(output_dir: Path, tree: dict,
                           log=print) -> list[str]:
    """落盘每需求家族的 feature/route stub + 装配版 App.jsx/index.js。

    只在文件不存在时写 stub(模型独占路径的首次播种);App.jsx 与
    routes/index.js 仅当仍是脚手架原始字节时才改写(模型动过不碰)。
    返回写入的相对路径列表。"""
    stubs, _node_map = task_stubs(tree)
    if not stubs:
        return []
    out = Path(output_dir)
    written: list[str] = []
    feats = out / FEATURES_DIR
    routes = out / ROUTES_DIR
    feats.mkdir(parents=True, exist_ok=True)
    routes.mkdir(parents=True, exist_ok=True)
    for s in stubs:
        fjsx = feats / f"{s['slug']}.jsx"
        if not fjsx.exists():
            fjsx.write_text(_feature_stub(s), encoding="utf-8")
            written.append(f"{FEATURES_DIR}/{s['slug']}.jsx")
        rjs = routes / f"{s['slug']}.js"
        if not rjs.exists():
            rjs.write_text(_route_stub(s), encoding="utf-8")
            written.append(f"{ROUTES_DIR}/{s['slug']}.js")
    # 装配层改写:仅当仍是原始字节(幂等 + 不覆盖模型工作)。
    app_jsx = out / "frontend/src/app/App.jsx"
    try:
        pristine = app_jsx.read_text(encoding="utf-8") == \
            frontend.FRONTEND_APP_JSX
    except OSError:
        pristine = False
    if app_jsx.exists() and pristine:
        app_jsx.write_text(_app_wiring(stubs), encoding="utf-8")
        written.append("frontend/src/app/App.jsx")
    routes_index = out / "backend/src/routes/index.js"
    try:
        pristine_r = routes_index.read_text(encoding="utf-8") == \
            backend.ROUTES_INDEX_JS
    except OSError:
        pristine_r = False
    if routes_index.exists() and pristine_r:
        routes_index.write_text(_routes_wiring(stubs), encoding="utf-8")
        written.append("backend/src/routes/index.js")
    if written:
        log(f"[scaffold] task shape pre-generated: {len(stubs)} family "
            f"stub(s) ({', '.join(s['slug'] for s in stubs[:4])}"
            f"{'…' if len(stubs) > 4 else ''})")
    return written
