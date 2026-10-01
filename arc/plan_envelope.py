"""ARC 宿主工具箱(纯标准库)——09-27 planctl 退役后瘦身。

原职责(构建 [arc-plan] envelope/CONTRACT/FOCUS 系统行)已随 plan 模式
整体退役(与 pi 对齐):契约与需求全量印进工单正文(arc/main.py),内核
只做驱动与治理。保留的六件是仍被 main.py 消费的确定性工具:

- HOST_STACK_RULES / HOST_STACK_RULES_GOAL_FIRST  契约文案(工单正文)
- stage_specs                验收 specs 只读暂存
- spec_to_node_map           spec→需求节点归属(批过滤/回归范围)
- probe_routes               staged specs → /api 探测清单
- build_validation_probes    POST 路由坏载荷探针
- build_fixture_probes       种子可达性探针(login/entity)
- rehearsal_boot_env         彩排/Tier-2 boot 环境(PORT+EXTRA)
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import fixture_manifest

SPEC_STAGE_SUBDIR = ".arc/acceptance"
WIPE_GLOBS = ("*.spec.ts", "support")

# 端口契约单一出处:评测口宿主管理(模型绝不 bind/重启),开发口=模型
# 自验专用;main.py 的 OCTOS_SMOKE_PORT 与 web_port 缺省都从这里取。
WEB_PORT = 3000
SMOKE_PORT = 3100

# 全局总纲(两形式之一:AGENTS.md 系统行第三段)。pi buildRules 同构:
# 规则由多源贡献(全局法则 + 任务层 fixture_manifest.task_rules),集中
# 经 build_rules 去重装配为单行 bullet——重复贡献自动坍缩,新增来源零
# 改动装配点。ARCHITECTURE 是事实清单(逐文件一行),不是规则。

CHARTER_RULES = [
    "The requirement text wins over the app, over your specs, over seeded "
    "data, and over this file; quoted names are exact accessible names "
    "and quoted rules bind verbatim.",
    "The grader walks ONLY the operations the requirement names — never "
    "make a step mandatory that the text does not describe.",
    "Reject (a 4xx, a validation error, a disabled submit) only where "
    "the text states it: use the text's quoted message verbatim when it "
    "quotes one, a short clear message of your own when it mandates the "
    "rejection without quoting one; no mandating sentence = the "
    "operation must SUCCEED with a neutral default.",
    "Every GIVEN line is a binding pre-state: its actor can sign in, its "
    "entities exist verbatim, and the seed covers them.",
    "Write the Playwright spec for a node BEFORE implementing it, watch "
    "it red, then fix the app until it walks green on the RUNNING app — "
    "a green API response is not a walking flow.",
    "Every write survives a refresh; sessions survive a server restart.",
    "On any page no two controls share an accessible name — the grader's "
    "getByRole(role, { name }) dies on ambiguity, so ambiguity is an APP "
    "defect.",
    "Never regress an already-green family; after editing shared files, "
    "re-run every green family's spec.",
]

# 架构事实清单:逐文件一行,路径与角色按脚手架/taskshape 真实运行后的
# 目录(09-30 用户纠错:旧版写的 src/app.js 等缩写路径从第一天起就是
# 错的——真实路径是 backend/src/app.js;stub 机制按 taskshape 契约描述)。
ARCHITECTURE_LINES = [
    "Layout: two app roots — backend/ = the Express server (npm start "
    "boots it; src/, data/) and frontend/ = the Vite+React client (npm "
    "run build emits dist/; index.html, src/); all paths below are "
    "workspace-relative.",
    "backend/src/app.js + backend/src/index.js bootstrap the app: index "
    "binds PORT + ARC_EXTRA_PORTS, app serves frontend/dist (SPA fallback "
    "to index.html) — keep that contract when you change them.",
    f"Ports: the host boots and grades the app on port {WEB_PORT} — "
    f"never bind or restart it; if you start the app yourself to verify, "
    f"bind port {SMOKE_PORT} only.",
    "backend/src/lib/util.js = responses/cookies/ids; "
    "backend/src/lib/eval-telemetry.js = platform traffic/exception "
    "self-telemetry.",
    "backend/src/lib/session.js = session mechanics on the store — you "
    "wire the login/logout/me routes in backend/src/routes/.",
    "backend/src/lib/crud.js = optional REST factory: mount(app, "
    "'<collection>', {publicRow, validate, prepare, listQuery}); custom "
    "routes registered before mount take precedence.",
    "backend/src/database/store.js = seed/run dual-file store: "
    "coll/one/get/all/dup/insert/update/remove/save, query/sort/page, "
    "tx with rollback, nextSeq, scrypt hash/verify.",
    "ALL API endpoints are YOURS: backend/src/routes/index.js mounts one "
    "pre-created stub per requirement family (routes/<family>.js — REQ "
    "ids in the header); fill the stubs in place, add sibling files "
    "freely — routes, payload shapes, validation, and error messages "
    "follow the requirement text, nothing else.",
    "backend/data/db.json ships EMPTY: implement the seed from the "
    "GIVEN lines (plus a data: section if present) — never drop a GIVEN "
    "entity; expose every requirement-named entity through your API.",
    "The substrate boots out of the box; when the requirement demands "
    "something they do not provide, change them.",
    "frontend/src/main.jsx = React entry; frontend/src/app/App.jsx = the "
    "assembly layer mounting one frontend/src/features/<family>.jsx per "
    "family (stubs pre-created, REQ ids in the headers — fill in place, "
    "add siblings freely); styles in frontend/src/app/styles.css.",
    "frontend/src/app/api.js = the backend client; frontend/src/app/"
    "store.jsx = optional DataProvider helper.",
    "Kit (optional; each file's header documents itself — read before "
    "use): frontend/src/app/ui.jsx, frontend/src/app/grid.js, "
    "frontend/src/app/formula.js, frontend/src/app/csv.js, "
    "frontend/src/app/undo.js, frontend/src/app/router.js; "
    "frontend/src/features/README.md = feature notes + grid editor "
    "kernel pattern.",
    "Grid pages address cells beyond the data extent (40 rows x 20 cols "
    "minimum).",
    "Self-tests live ONLY under e2e/ (config: "
    "e2e/playwright.config.ts) — never inside .arc/.",
]


def build_rules(*sources: list[str]) -> str:
    """pi buildRules 同构:多源贡献单行规则,规范化去重,bullet 输出。"""
    rules: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for rule in source:
            r = " ".join(str(rule).split()).strip()
            if not r or r.lower() in seen:
                continue
            seen.add(r.lower())
            rules.append(r)
    return "\n".join(f"- {r}" for r in rules)


def render_rules(task_rules: list[str] | None = None) -> str:
    """系统行第三段:全局法则 + 任务贡献规则(去重合并)+ 架构清单。"""
    header = ("RULES & ARCHITECTURE (global laws that bind every turn of "
              "every family; each derives from the requirement text — the "
              "host invents none of it).")
    return (header + "\n" + build_rules(CHARTER_RULES, task_rules or [])
            + "\nARCHITECTURE:\n" + build_rules(ARCHITECTURE_LINES))


HOST_STACK_RULES_GOAL_FIRST = render_rules()



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


def build_fixture_probes(logins: list[dict]) -> list[dict]:
    """夹具探针(宿主 0-token 验证,复用 rehearsal-repair 回路)。

    login:POST 到声明路径(条目可带 path,默认 /api/auth/login——纯
    缺省约定,声明可覆盖;亦可带 body 逐字覆写请求体——模型对自己
    的声明有完全控制权,宿主不设形状假设)期待 200;400/401 = 声明
    的账号/密码与实际认证不符,404 = 声明路径无路由。清单优先取模型
    自报的 backend/data/seed-accounts.json(角色账号由模型推理决定,
    宿主只验证其声明),逐字转录种子殿后(同名去重,声明条目优先);
    无密码条目跳过——宿主不再有统一密码兜底(09-28 退役)。
    种子集合的 API 可达性不再走 HTTP 形状假设(CRUD 生成器退役后
    端点形状归模型)——由 main.py 的文件级种子完整性检查承接
    (db.json 种子行 vs 宿主编译清单)。判定语义在 _probe_routes_on 落地。
    """
    probes: list[dict] = []
    seen: set[str] = set()
    for login in (logins or []):
        if len(probes) >= 8:
            break
        username = str(login.get("username") or "")
        password = str(login.get("password") or "")
        if not username or not password:
            continue
        # 按用户名去重,首现优先——调用点 declared 在前:模型声明条目
        # (可带 path/body)压过 compiled 缺省条目,消灭"声明了真实路由、
        # compiled 副本仍探测缺省路径"的永久红陷阱(09-29 审计)。
        if username in seen:
            continue
        seen.add(username)
        # 声明条目可用 body 逐字覆写请求体——宿主缺省形状
        # ({"username","password"} JSON)对自报声明不设假设。
        body = login.get("body")
        if not isinstance(body, str) or not body.strip():
            body = json.dumps({"username": username, "password": password})
        probes.append({
            "method": "POST",
            "path": str(login.get("path") or "/api/auth/login"),
            "kind": "login",
            "user": username,
            "body": body})
    return probes[:8]


# ── spec → 节点映射 ────────────────────────────────────────────────────

# 点分(REQ-1.1,ticket 系)与连笔(REQ-1-1-1,hackathon 系)ID 都接受。
_REQ_ID_RE = re.compile(r"^(REQ-\d+(?:[.-]\d+)*)")


def spec_req_prefix(basename: str) -> str | None:
    """`REQ-1.1-user-registration.spec.ts` → `REQ-1.1`;
    `REQ-1-1-1-add-worksheet.spec.ts` → `REQ-1-1-1`."""
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
