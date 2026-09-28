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

# 宿主环境事实(grading 形状)——现印进每个工单正文(=0 回退路径用)。
# 刻意不含任何 UI 行为规则:任务的验收规则来自 requirements.yaml 本身,
# 宿主不发明规则(09-27 定则)。
HOST_STACK_RULES = """\
HOST CONTRACT (graded Playwright specs are ground truth — where the \
requirement text disagrees, the spec wins):
STACK: React(Vite) frontend + Express backend, PREBUILT BY THE HOST — \
the backend already runs. Host-owned (never rewrite): src/app.js + \
src/index.js (bootstrap; PORT + ARC_EXTRA_PORTS; serves frontend/dist), \
src/lib/util.js (responses/cookies/ids), src/lib/validate.js \
(schema-driven validators, English contract-family error defaults), \
src/database/store.js (atomic JSON store: coll/one/all/dup/insert/\
remove/save + scrypt hash/verify), src/database/engine.js (schema-driven \
CRUD factory), src/lib/auth.js (register/login/logout/me + sessions). Dependencies are PREINSTALLED — never run npm install, \
never add npm packages.
FRONTEND LAYOUT (isolation contract): every requirement's UI lives in \
its OWN self-contained file frontend/src/features/<name>.jsx (default \
export; component+state+styles colocated). Graded UI shapes are CODE: \
frontend/src/app/ui.jsx ships NativeSelect (native <select>; graders \
selectOption), AppDialog (native <dialog>), AccountTrigger (username \
its own exact-text node + the requirement's trigger label), \
SingleError (ONE visible error), Region ("A region named X" = \
<Region label="X">) and FixedGrid (fixed-size role=grid; pass the \
useGrid kernel); frontend/src/app/grid.js = the useGrid kernel (click \
/ Shift-click-extend / drag / aria-selected built in); \
frontend/src/app/formula.js = the cell formula engine — import, \
never hand-roll. frontend/src/features/README.md (host-owned) \
carries the GRID EDITOR KERNEL PATTERN — one shared grid kernel, \
never per-feature re-implementations. \
frontend/src/app/App.jsx is the ASSEMBLY layer only — \
per feature one import+mount; the FIRST requirement may lay out the \
page structure, never business logic after that. \
Shared entities go through frontend/src/app/store.jsx \
(useResource('<entity>')) — never duplicate shared state inside \
features. NEVER edit another requirement's feature file; keep \
frontend/src/main.jsx, frontend/src/app/api.js and \
frontend/src/app/styles.css as-is (colocate feature styles with the \
feature, or reuse the global classes).
SCHEMA-DRIVEN BACKEND: to add data, write backend/data/schema.json = \
{"entities":[{"name":"users","auth":true,"label":"用户名","fields":[...]}]}; \
field keys: name, label (used in error messages), type \
(string|number|boolean|password), required, unique, min, max, pattern \
(JS regex), enum, match (mirror field, e.g. confirmPassword), format \
(email|username|password), message (single) or messages \
{required,invalid,duplicate} per reason (quoted "<X> is required"/ \
"<X> already exists" auto-map; copy quoted error strings verbatim). \
The host compiles it \
to src/database/schema.js and mounts CRUD at /api/<entity> \
automatically; entities with auth:true additionally get \
/api/auth/register (validates ALL fields, scrypt-hashes the secret, \
auto-login session), /api/auth/login (accepts any unique identifier \
(username or email; trimmed, email case-insensitive), uniform failure message (quoted credential string wins via the codegen \
bridge, default "Invalid username or password"; never reveals existence), /api/auth/logout, /api/me (session restore; sessions \
survive server restart). Non-plain-CRUD endpoints (search/booking flows) go in \
src/routes/index.js using store + lib helpers.
FIXTURE CONTRACT (graded, not demo data): the `data:` section + every \
scenario GIVEN line are the graded seed contract, already compiled into \
backend/data/db.json. Names may carry a unique suffix, but roles, \
ownership, visibility, relationships match VERBATIM; NEVER delete or rename seeded rows \
or collections, and extend schema.json so every seeded field is \
exposed. Scenarios that MODIFY data each need their OWN fresh \
preconfigured instance (never share one mutable fixture across \
scenarios). Files/commits are the single source of truth: branches \
store head references only, NEVER duplicate whole file trees per \
branch or fork. db.json is the SEED: runtime state goes to db.run.json \
and every server restart resets to the seed (sh .arc/host/\
restart-server.sh); db.run.json must NEVER ship.
SERVER-SIDE VALIDATION (generic discipline, every task): the backend \
re-validates ALL input and never trusts the client. Enforce the \
contract's required/length/format/uniqueness rules EXACTLY, boundaries \
included (min, min+1, max, max+1). Any rejected submit stays on the \
page, shows exactly ONE visible error, and creates no records or \
sessions. Empty, oversized, or wrong-type payloads must be rejected \
(4xx) — a 5xx on any input is a bug. Before declaring done, walk the \
FAILURE cases (empty/missing/oversized/duplicate), not just the happy \
path.

UI CONTRACT HYGIENE (graded in a real browser by the host smoke and \
the hidden tests): every quoted string in a requirement is a VISIBLE \
control with that EXACT accessible name — substring variants \
("Delete worksheet" vs the required "Delete") fail, and hidden \
<select> options are not visible controls (use a native <select> the \
grader can selectOption, or visible radios/buttons); WIDGETS are \
NATIVE-FIRST — dropdowns are always native <select> with real <option> \
labels (a custom div dropdown is invisible to every selector), modals \
prefer the native <dialog> \
element (showModal() gives Escape/backdrop close and focus trapping \
for free), and custom is built ONLY where HTML has no native widget \
(menus/tabs/grids) with the requirement's exact ARIA roles mandatory; a DATA GRID renders its \
COMPLETE structure — role="grid" with visible columnheader/rowheader \
cells and gridcells named by coordinate (A1), never the data region \
alone; a SUBMIT/SAVE control must become enabled once required fields \
are filled — a permanently disabled submit can never be clicked by a \
test; every dialog closes on Escape AND on cancel/overlay click — \
an undismissable overlay blocks every later grader step; modifier \
clicks keep selection (Shift-click extends the range — never \
collapse it); downloads are \
byte-exact: no UTF-8 BOM, exact CSV quoting."""

# goal-first 变体(09-27 宿主优先→目标优先转向):后端数据面/夹具各段与
# host-first 完全一致;差异只在 FRONTEND 段——frontend/src 全域归模型,
# UI kit 从"必须用"降为"可选工具箱",需求原文是唯一权威。
# 宿主不制定行为规则(09-27 定则):SERVER-SIDE VALIDATION 与 UI CONTRACT
# HYGIENE 两段宿主发明纪律已裁——验收用例由模型根据需求自生成;
# 留下的三段全是数据面基座的 API 说明书,非任务规则。
# RULES_MUST_NAME 的路径拼写同样必须逐一点名(scaffold_test 双断言)。
HOST_STACK_RULES_GOAL_FIRST = """\
HOST CONTRACT (goal-first: the requirement text is the authoritative \
product contract — quoted names are exact accessible names, quoted \
rules bind verbatim, and the scenarios are the test oracle; where \
staged specs exist they remain ground truth. The host documents its \
data-plane substrate below and authors NO behavioral rules — you \
derive the behavior and the tests from the requirement text):
STACK: React(Vite) frontend + Express backend, PREBUILT BY THE HOST — \
the backend already runs. Host-owned (never rewrite): src/app.js + \
src/index.js (bootstrap; PORT + ARC_EXTRA_PORTS; serves frontend/dist), \
src/lib/util.js (responses/cookies/ids), src/lib/validate.js \
(schema-driven validators, English contract-family error defaults), \
src/database/store.js (atomic JSON store: coll/one/all/dup/insert/\
remove/save + scrypt hash/verify), src/database/engine.js (schema-driven \
CRUD factory), src/lib/auth.js (register/login/logout/me + sessions). Dependencies are PREINSTALLED — never run npm install, \
never add npm packages.
FRONTEND (goal-first — the model owns it): frontend/src is yours to \
shape as the requirements demand: frontend/src/app/App.jsx, the shell, \
frontend/src/app/styles.css, and frontend/src/features/ — restructure \
freely (a proven shape: one self-contained file per requirement family \
under frontend/src/features/<name>.jsx, App.jsx a thin assembly layer). \
Keep frontend/src/main.jsx, frontend/src/app/store.jsx \
(useResource('<entity>')) and frontend/src/app/api.js working as the \
backend client — extend them, never fork shared state into features. \
TOOLBOX (optional, unwired): frontend/src/app/ui.jsx ships proven \
shapes — NativeSelect (dropdowns = native <select>, graders use \
selectOption), AppDialog (native <dialog> modals), AccountTrigger \
(username its own exact-text node + the requirement's trigger label — \
the observed full-score shape), SingleError (one visible error \
element), Region (render <Region label="X"> for a requirement phrase \
"A region named X" — a section, so getByRole('region', {name}) \
resolves; a div with aria-label has no region role) and FixedGrid \
(fixed-size role=grid; pass the useGrid kernel as grid={...} and \
click / Shift-click-extend / drag / aria-selected are handled for \
you) — use them or roll your own; the requirement text is the only \
authority. frontend/src/app/grid.js carries the useGrid selection/\
editing kernel plus rectOf/inRectangle/rangeCoords geometry; \
frontend/src/app/formula.js carries a cell formula engine \
(displayValue/evalFormula/adjustFormula/shiftRefsOnRowChange/\
shiftRefsOnColChange — =SUM(A1:A3)*2, =A1 + B1, =-A1, nested calls \
and the #DIV/0!/#REF!/#NAME?/#ERROR! vocabulary included). \
frontend/src/features/README.md carries the GRID EDITOR KERNEL \
PATTERN — grid-like families build on ONE shared selection/editing \
state model instead of re-implementing it per feature.
SCHEMA-DRIVEN BACKEND: to add data, write backend/data/schema.json = \
{"entities":[{"name":"users","auth":true,"label":"用户名","fields":[...]}]}; \
field keys: name, label (used in error messages), type \
(string|number|boolean|password), required, unique, min, max, pattern \
(JS regex), enum, match (mirror field, e.g. confirmPassword), format \
(email|username|password), message (single) or messages \
{required,invalid,duplicate} per reason (quoted "<X> is required"/ \
"<X> already exists" auto-map; copy quoted error strings verbatim). \
The host compiles it \
to src/database/schema.js and mounts CRUD at /api/<entity> \
automatically; entities with auth:true additionally get \
/api/auth/register (validates ALL fields, scrypt-hashes the secret, \
auto-login session), /api/auth/login (accepts any unique identifier \
(username or email; trimmed, email case-insensitive), uniform failure message (quoted credential string wins via the codegen \
bridge, default "Invalid username or password"; never reveals existence), /api/auth/logout, /api/me (session restore; sessions \
survive server restart). Non-plain-CRUD endpoints (search/booking flows) go in \
src/routes/index.js using store + lib helpers.
FIXTURE CONTRACT (graded, not demo data): the `data:` section + every \
scenario GIVEN line are the graded seed contract, already compiled into \
backend/data/db.json. Names may carry a unique suffix, but roles, \
ownership, visibility, relationships match VERBATIM; NEVER delete or rename seeded rows \
or collections, and extend schema.json so every seeded field is \
exposed. Scenarios that MODIFY data each need their OWN fresh \
preconfigured instance (never share one mutable fixture across \
scenarios). Files/commits are the single source of truth: branches \
store head references only, NEVER duplicate whole file trees per \
branch or fork. db.json is the SEED: runtime state goes to db.run.json \
and every server restart resets to the seed (sh .arc/host/\
restart-server.sh); db.run.json must NEVER ship."""


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


def build_fixture_probes(logins: list[dict], collections: list[str]) -> list[dict]:
    """夹具探针(宿主 0-token 验证,复用 rehearsal-repair 回路)。

    - login: POST /api/auth/login(宿主固定 auth 路由)期待 200;
      400/401 = 声明的账号/密码与实际认证不符,404 = auth 实体未声明。
      登录清单优先取模型自报的 backend/data/seed-accounts.json(角色
      账号由模型推理决定,宿主只验证其声明),逐字转录种子殿后。
      无密码条目跳过——宿主不再有统一密码兜底(09-28 退役)。
    - entity: GET /api/<name> 期待 200 且非空;空 = 宿主种子行被删/集合被
      改名,404 = schema.json 未暴露该实体。users(auth 实体)没有引擎
      GET 路由,由 login 探针代管。判定语义在 _probe_routes_on 落地。
    """
    probes: list[dict] = []
    for login in (logins or []):
        if len(probes) >= 4:
            break
        username = str(login.get("username") or "")
        password = str(login.get("password") or "")
        if not username or not password:
            continue
        probes.append({
            "method": "POST", "path": "/api/auth/login", "kind": "login",
            "user": username,
            "body": json.dumps({
                "username": username,
                "password": password})})
    for name in (collections or []):
        if name == "users" or len(probes) >= 8:
            continue
        probes.append({"method": "GET", "path": f"/api/{name}",
                       "kind": "entity", "entity": name})
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
