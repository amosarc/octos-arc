"""ARC host plan envelope builder (纯标准库).

构建发往内核 `[arc-plan]` marker 的计划 envelope:需求树 → steps、spec→节点
映射(含碰撞组分配)、端口契约、host_signals 透传;以及宿主尽算的两件基础设施:
stage_specs(specs 只读暂存)与 host_scaffold(幂等五件套)。

设计律(与内核 planctl 对齐):
- 宿主尽算:凡宿主能算的不交模型;
- 线格式:`[arc-plan]\\n{单行紧凑 JSON}\\n\\n{prompt}`,payload ≤256KB;
- spec→节点映射:原子 id 精确命中 > 最长点分前缀 > 碰撞组分配 > 全局桶。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

RULESET = "arc_web_v1"
SPEC_STAGE_SUBDIR = ".arc/acceptance"
WIPE_GLOBS = ("*.spec.ts", "support")

# 宿主环境事实(grading 形状)——经 envelope.rules_text 下发,内核逐字渲染。
# 刻意不含任何 UI 行为规则:任务的验收规则来自 requirements.yaml 的
# Validation 契约(见 split_validation),宿主不发明规则。
HOST_STACK_RULES = """\
HOST CONTRACT (graded Playwright specs are ground truth — where the \
requirement text disagrees, the spec wins):
STACK: React(Vite) frontend + Express backend. backend/src/app.js exports \
the app; backend/src/server.js listens on process.env.PORT plus every \
port in ARC_EXTRA_PORTS. Serve frontend/dist; JSON APIs under /api/. \
Persistence: backend/data/db.json (load at startup, rewrite per mutation, \
seed the requirement's records when empty). Hash passwords with Node \
crypto scrypt. Dependencies: backend express; frontend \
react/react-dom/vite; vitest as a backend devDependency (test marker). No \
TypeScript in app code; no native-binding packages; installs use the \
China mirror (npmmirror, already in .npmrc). The staged specs \
.arc/acceptance/ are READ-ONLY (never edit/copy/delete).
SERVER-SIDE VALIDATION (generic discipline, every task): the backend \
re-validates ALL input and never trusts the client. Enforce the \
contract's required/length/format/uniqueness rules EXACTLY, boundaries \
included (min, min+1, max, max+1). Any rejected submit stays on the \
page, shows exactly ONE visible error, and creates no records or \
sessions. Empty, oversized, or wrong-type payloads must be rejected \
(4xx) — a 5xx on any input is a bug. Before declaring done, walk the \
FAILURE cases (empty/missing/oversized/duplicate), not just the happy \
path."""

# 契约前缀双语表(中文全角冒号 / 英文冒号,英文大小写不敏感)
_VALIDATION_PREFIXES = ("Validation 契约：", "Validation contract:")


def rehearsal_boot_env(base_env: dict, smoke_port: int, extra_ports: list) -> dict:
    """彩排/Tier-2 boot 环境:PORT + ARC_EXTRA_PORTS 一起设。

    F5 修复:检查项要求 extra 端口在听,boot 就必须把同一份清单交给应用
    (env 驱动双听),否则「按契约正确实现」的应用也会被判 RED。extra 为空
    时不设该键,legacy 行为不变。
    """
    env = dict(base_env or {}, PORT=str(smoke_port))
    if extra_ports:
        env["ARC_EXTRA_PORTS"] = ",".join(str(int(p)) for p in extra_ports)
    return env


def build_validation_probes(routes: list) -> list[dict]:
    """通用入参校验探针(确定性、零业务语义)。

    对 POST 类探针路由派生两种坏载荷:空对象 `{}`、单字段 10KB 超长串。
    判定约定:5xx = RED(任何输入下 500 都是不可接受的校验行为);
    4xx/2xx 记录为事实(前端校验型应用可能合法返回 2xx,由模型对照契约
    自查)。每路由 ≤2 探针,总量 ≤8。
    """
    probes: list[dict] = []
    oversized = "x" * 10240
    for route in routes:
        if str(route.get("method", "")).upper() != "POST":
            continue
        path = str(route.get("path", ""))
        if not path:
            continue
        probes.append({"method": "POST", "path": path, "variant": "empty",
                       "body": "{}"})
        probes.append({"method": "POST", "path": path, "variant": "oversized",
                       "body": json.dumps({"f": oversized}, ensure_ascii=False)})
        if len(probes) >= 8:
            break
    return probes


def split_validation(description: str) -> tuple[str, str]:
    """Split a yaml node description into (validation, notes).

    契约 = description 首行(`|-` 字面块的首行即任务作者写的完整 Validation
    契约;`>-` 折叠块整体折叠为一行);剥离「Validation 契约:」前缀。
    其余行 = 实现指引(notes,不进 envelope 的规则位)。
    """
    text = (description or "").strip()
    if not text:
        return "", ""
    lines = text.splitlines()
    validation = lines[0].strip()
    for prefix in _VALIDATION_PREFIXES:
        if validation.lower().startswith(prefix.lower()):
            validation = validation[len(prefix):].strip()
            break
    notes = "\n".join(l.strip() for l in lines[1:] if l.strip())
    return validation, notes

# ── spec → 节点映射 ────────────────────────────────────────────────────

_REQ_ID_RE = re.compile(r"^(REQ-\d+(?:\.\d+)*)")


def spec_req_prefix(basename: str) -> str | None:
    """`REQ-1.1-user-registration.spec.ts` → `REQ-1.1`."""
    m = _REQ_ID_RE.match(basename)
    return m.group(1) if m else None


def spec_to_node_map(tests_dir: Path, atomic_nodes: list[dict]) -> dict[str, list[str]]:
    """Map each spec file to the requirement node that owns it.

    Resolution order per spec file (by its REQ-x.y prefix):
    1. exact atomic id match;
    2. longest dotted-prefix atomic id (REQ-1.1 → REQ-1);
    3. collision group: when one atomic id collected MULTIPLE
       prefix-assigned files while later atomic nodes have none, the files
       are distributed in filename order over the prefix node followed by
       those spec-less nodes (ticket-booking: REQ-1.1/REQ-1.2 specs ↔
       REQ-1/REQ-2 atoms);
    4. helper files (support/) and unknown prefixes stay unmapped (global).

    Returns {node_id: [spec basenames]}.
    """
    atomic_ids = [str(n.get("id", "")) for n in atomic_nodes]
    spec_files = (
        sorted(p.name for p in tests_dir.glob("*.spec.ts"))
        if tests_dir.is_dir()
        else []
    )
    if not atomic_ids or not spec_files:
        return {}

    exact: dict[str, list[str]] = {}
    by_prefix: dict[str, list[str]] = {}
    for basename in spec_files:
        prefix = spec_req_prefix(basename)
        if prefix is None:
            continue
        if prefix in atomic_ids:
            exact.setdefault(prefix, []).append(basename)
            continue
        candidates = [aid for aid in atomic_ids if prefix.startswith(aid + ".")]
        if candidates:
            best = max(candidates, key=lambda a: (a.count("."), len(a)))
            by_prefix.setdefault(best, []).append(basename)

    mapping = {aid: list(files) for aid, files in exact.items()}
    for owner, files in by_prefix.items():
        # Nodes later in document order that hold no specs of their own.
        spec_less = [
            aid
            for aid in atomic_ids
            if aid != owner and aid not in mapping and not by_prefix.get(aid)
        ]
        if len(files) > 1 and spec_less:
            targets = [owner] + spec_less[: len(files) - 1]
            for target, basename in zip(targets, files):
                mapping[target] = [basename]
        else:
            mapping.setdefault(owner, []).extend(files)
    return mapping


# ── envelope 构建 ─────────────────────────────────────────────────────


def build_envelope(
    *,
    seq: int,
    phase: str,
    task: str,
    atomic_nodes: list[dict],
    statuses: dict[str, str],
    focus: str | None,
    ports: dict,
    spec_map: dict[str, list[str]],
    extra_rules: str | None = None,
    host_signals: dict | None = None,
) -> dict:
    """Build one envelope dict (snake_case, mirrors planctl::Envelope)."""
    steps = []
    for node in atomic_nodes:
        nid = str(node.get("id", ""))
        desc = str(node.get("description") or node.get("name") or "")
        validation, _notes = split_validation(desc)
        targets = {"files": [], "routes": [], "spec_files": spec_map.get(nid, [])}
        steps.append({
            "id": nid,
            "action": "modify",
            # PLAN 清单短摘要;完整契约走 validation(FOCUS 段逐字渲染)。
            "input": (validation or nid)[:200],
            "validation": validation[:3000] or None,
            "targets": targets,
            "expected_output": f"Playwright specs for {nid} pass",
            "status": statuses.get(nid, "pending"),
        })
    envelope = {
        "v": 1,
        "seq": seq,
        "phase": phase,
        "stack": "web_react_express",
        "task": task[:500],
        "spec_dir": SPEC_STAGE_SUBDIR,
        "rules": RULESET,
        "rules_text": HOST_STACK_RULES,
        "extra_rules": (extra_rules[:2400] if extra_rules else None),
        "ports": {
            "grade": int(ports.get("grade", 0)),
            "smoke": int(ports.get("smoke", 0)),
            "extra": [int(p) for p in ports.get("extra", [])],
        },
        "steps": steps[:40],
        "focus": focus,
        "host_signals": host_signals,
    }
    return envelope


def render_marked_prompt(envelope: dict, prompt: str) -> str:
    """`[arc-plan]\\n{json}\\n\\n{prompt}` — compact, non-ASCII kept."""
    payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    if len(payload.encode("utf-8")) > 256 * 1024:
        raise ValueError(f"plan envelope payload exceeds 256KB ({len(payload)} chars)")
    if prompt.startswith("[arc-plan]"):
        # idempotence: never double-wrap
        return prompt
    return f"[arc-plan]\n{payload}\n\n{prompt}"


def is_marked(prompt: str) -> bool:
    return prompt.lstrip().startswith("[arc-plan]")


# ── stage_specs:specs 只读暂存 ────────────────────────────────────────


def stage_specs(tests_dir: Path, output_dir: Path) -> Path:
    """Copy `*.spec.ts` + `support/` into `<output>/.arc/acceptance/`.

    Clears the previous staging first (retries must not see stale specs).
    Non-.ts files are filtered. Returns the staging dir.
    """
    stage = output_dir / SPEC_STAGE_SUBDIR
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    if tests_dir.is_dir():
        for p in sorted(tests_dir.glob("*.spec.ts")):
            shutil.copy2(p, stage / p.name)
        support = tests_dir / "support"
        if support.is_dir():
            shutil.copytree(support, stage / "support")
    return stage


# ── host_scaffold:幂等五件套(E 翻转)─────────────────────────────────

BACKEND_PACKAGE_JSON = {
    "name": "backend",
    "version": "1.0.0",
    "private": True,
    "main": "src/server.js",
    "scripts": {
        "start": "node src/server.js",
        "test": "vitest run --config vitest.config.js",
    },
    "dependencies": {"express": "^4.19.2"},
    "devDependencies": {"vitest": "^1.6.0"},
}

BACKEND_VITEST_CONFIG = """\
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.js'],
    environment: 'node',
  },
});
"""

FRONTEND_PACKAGE_JSON = {
    "name": "frontend",
    "version": "1.0.0",
    "private": True,
    "scripts": {"build": "vite build"},
    "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
    "devDependencies": {"@vitejs/plugin-react": "^4.3.1", "vite": "^5.2.0"},
}

FRONTEND_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
"""

FRONTEND_MAIN_JSX = """\
import React from 'react';
import { createRoot } from 'react-dom/client';

function App() {
  return <div>App scaffold</div>;
}

createRoot(document.getElementById('root')).render(<App />);
"""

NPMRC = "registry=https://registry.npmmirror.com\n"


def host_scaffold(output_dir: Path) -> list[str]:
    """Idempotently lay down the five fixed infrastructure files.

    Never touches db.json / app.js / server.js (business semantics stay
    with the model). Returns the list of paths written THIS call.
    """
    written: list[str] = []

    def write(rel: str, content: str) -> None:
        target = output_dir / rel
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)

    write("backend/package.json", json.dumps(BACKEND_PACKAGE_JSON, indent=2) + "\n")
    write("backend/vitest.config.js", BACKEND_VITEST_CONFIG)
    write("frontend/package.json", json.dumps(FRONTEND_PACKAGE_JSON, indent=2) + "\n")
    write("frontend/index.html", FRONTEND_INDEX_HTML)
    write("frontend/src/main.jsx", FRONTEND_MAIN_JSX)
    write(".npmrc", NPMRC)
    return written


# ── probe_routes:从 staged specs 提取探测清单(A 翻转)───────────────

_API_RE = re.compile(r"/api/[A-Za-z0-9/_:.\-{}]+")
_POST_HINT_RE = re.compile(r"register|login|create|add|submit|update|delete|book|order|save|post", re.I)


def probe_routes(tests_dir: Path) -> list[dict]:
    """Harvest `/api/…` literals from the specs → probe list.

    Method defaults to GET; POST when the path or surrounding context
    hints at a mutating verb. Sorted, deduped.
    """
    probes: dict[str, dict] = {}
    files = sorted(tests_dir.rglob("*.spec.ts")) if tests_dir.is_dir() else []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            for m in _API_RE.finditer(line):
                route = m.group(0).rstrip("/.,;:'\"")
                # Method hint: the path itself, or the SAME line (a wider
                # window lets a later `request.post` mislabel `/api/health`).
                method = (
                    "POST"
                    if _POST_HINT_RE.search(route) or _POST_HINT_RE.search(line)
                    else "GET"
                )
                key = f"{method} {route}"
                probes.setdefault(key, {"method": method, "path": route})
    return [probes[k] for k in sorted(probes)]


# ── 版本门控 ──────────────────────────────────────────────────────────


def parse_version_tuple(version_output: str) -> tuple[int, int, int]:
    """`octos 2.0.3-rc.11` → (2, 0, 3); rc suffixes ignored (2.0.3-rc ≥ 2.0.3)."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_output)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def plan_capable(version_output: str) -> bool:
    """Kernel capability gate: tuple ≥ (2, 0, 3), or OCTOS_ASSUME_ARC_PLAN.

    `OCTOS_ASSUME_ARC_PLAN=0` forces the legacy path (baseline runs);
    `=1` overrides the gate for dev builds.
    """
    assume = os.environ.get("OCTOS_ASSUME_ARC_PLAN", "")
    if assume == "0":
        return False
    if assume == "1":
        return True
    return parse_version_tuple(version_output) >= (2, 0, 3)
