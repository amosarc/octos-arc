"""ARC 宿主工具箱(纯标准库)——09-27 planctl 退役后瘦身。

原职责(构建 [arc-plan] envelope/CONTRACT/FOCUS 系统行)已随 plan 模式
整体退役(与 pi 对齐):契约与需求全量印进工单正文(arc/main.py),内核
只做驱动与治理。保留的六件是仍被 main.py 消费的确定性工具:

- HOST_STACK_RULES / HOST_STACK_RULES_GOAL_FIRST  契约文案(工单正文)
- stage_specs                验收 specs 只读暂存
- spec_to_node_map           spec→需求节点归属(批过滤/回归范围)
- build_validation_probes    POST 路由坏载荷探针(5xx=RED,4xx 记事实)
- boot_env                   Tier-2/page-health/dev-server boot 环境(PORT+EXTRA)
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
    "Your spec's clicks replay each scenario's WHEN steps in order, "
    "exactly as written — never insert a step the scenario does not "
    "name, never skip or reorder one; a literal step the app cannot "
    "perform is an APP bug — fix the app, never detour the walk.",
    "Every alternative a THEN or the description lists (refresh, "
    "browser back navigation, direct reopen of a recorded protected "
    "page) gets its own walk in the spec.",
    "Every behavioral rule a description states gets its own assertion "
    "in your spec — a quoted rule the spec never exercises is a rule "
    "never verified.",
    "Every write survives a refresh; sessions survive a server restart.",
    "On any page no two controls share an accessible name — the grader's "
    "getByRole(role, { name }) dies on ambiguity, so ambiguity is an APP "
    "defect.",
    "Navigation chrome — tab bars, named links, menus — renders "
    "synchronously, never gated behind a data fetch; only the data area "
    "may wait.",
    "Every entity the seed pre-provisions and every feature page that "
    "serves it is reachable from the application home page by clicking "
    "visible links or tabs — a search box or a typed URL is not an "
    "entry point; seeded data exists to be used.",
    "An entity's name renders at most once per page as visible text — "
    "breadcrumb crumb or page heading, never both; the grader's "
    "getByText(name, { exact: true }) dies on the second match, so "
    "duplication is an APP defect.",
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
    "requirements/requirements.yaml = the requirement source; the "
    "children map's (lines N-M) ranges address it — read ONLY those "
    "mapped ranges, via read_file start_line/end_line (shell sed -n "
    "'N,Mp' acceptable for narrow ranges). NEVER read the whole file: "
    "no full-file reads, no paging from line 1, no wholesale cat of "
    "the source.",
    "backend/src/app.js + backend/src/index.js bootstrap the app: index "
    "binds PORT + ARC_EXTRA_PORTS, app serves frontend/dist (SPA fallback "
    "to index.html) — keep that contract when you change them.",
    f"Ports: the host boots and grades the app on port {WEB_PORT} — "
    f"never bind or restart it; if you start the app yourself to verify, "
    f"bind port {SMOKE_PORT} only. Restart the dev server after backend "
    f"changes with `sh scripts/restart-server.sh` from the workspace root "
    f"— never kill server processes by pattern.",
    "scripts/ = host-provided helper scripts (e.g. restart-server.sh); "
    "run them from the workspace root, do not put your own files there "
    "(scratch belongs in /tmp).",
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
    "UI skeleton (the structural shape the platform's reference screens "
    "share): every page renders inside the host frame "
    "(frontend/src/app/frame.jsx — one persistent top header with brand, "
    "breadcrumb, and an actions slot); the page that owns the current "
    "view declares its header context with useFrame({ crumbs, actions }).",
    "Home page = the entity list: every seeded top-level entity is "
    "reachable from it as a clickable row or link whose exact accessible "
    "name is the entity's name.",
    "Entity pages keep the frame, show the entity's name in the header "
    "breadcrumb or page title — never both — and expose sub-views as "
    "named navigation (TabNav tabs or labeled links using the "
    "requirement's own words).",
    "Sign-in is a centered card with labeled Username and Password "
    "inputs and a Sign in submit; deep links must render their page "
    "directly (the router falls back to the URL pathname when the hash "
    "is empty).",
    "frontend/src/app/api.js = the backend client; frontend/src/app/"
    "store.jsx = optional DataProvider helper.",
    "Kit (optional; each file's header documents itself — read before "
    "use): frontend/src/app/ui.jsx, frontend/src/app/frame.jsx, "
    "frontend/src/app/grid.js, "
    "frontend/src/app/formula.js, frontend/src/app/csv.js, "
    "frontend/src/app/undo.js, frontend/src/app/router.js; "
    "frontend/src/features/README.md = feature notes + grid editor "
    "kernel pattern.",
    "Grid pages address cells beyond the data extent (40 rows x 20 cols "
    "minimum).",
    "Self-tests live ONLY under e2e/ (config: "
    "e2e/playwright.config.ts) — never inside .arc/. Run them with "
    "`sh scripts/run-e2e.sh <spec files>` from the workspace root — it "
    "cold-starts the app and pins the @playwright/test module path; do "
    "not hand-roll your own Playwright CLI invocations.",
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



def boot_env(base_env: dict, smoke_port: int, extra_ports: list) -> dict:
    """Tier-2/page-health/dev-server boot 环境:PORT + ARC_EXTRA_PORTS 一起设。

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


