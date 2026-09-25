"""ARC host plan envelope builder (纯标准库).

构建发往内核 `[arc-plan]` marker 的计划 envelope:需求树 → steps、spec→节点
映射(含碰撞组分配)、端口契约、host_signals 透传;以及宿主尽算的 specs
只读暂存(stage_specs)。宿主模板落盘已迁至 scaffold 包(HOST_STACK_RULES
文字契约保留在此)。

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

import fixture_manifest

RULESET = "arc_web_v1"
SPEC_STAGE_SUBDIR = ".arc/acceptance"
WIPE_GLOBS = ("*.spec.ts", "support")

# 宿主环境事实(grading 形状)——经 envelope.rules_text 下发,内核逐字渲染。
# 刻意不含任何 UI 行为规则:任务的验收规则来自 requirements.yaml 的
# Validation 契约(见 split_validation),宿主不发明规则。
HOST_STACK_RULES = """\
HOST CONTRACT (graded Playwright specs are ground truth — where the \
requirement text disagrees, the spec wins):
STACK: React(Vite) frontend + Express backend, PREBUILT BY THE HOST — the \
backend is already a complete running app. Host-owned files (never \
rewrite): src/app.js + src/index.js (bootstrap; listens on PORT plus \
every port in ARC_EXTRA_PORTS; serves frontend/dist), src/lib/util.js \
(responses/cookies/ids), src/lib/validate.js (schema-driven validators, \
CN/EN error text), src/database/store.js (atomic JSON store: coll/one/\
all/dup/insert/remove/save + scrypt hash/verify), src/database/engine.js \
(schema-driven CRUD factory), src/lib/auth.js (register/login/logout/me \
+ sessions). Dependencies are PREINSTALLED — never run npm install, \
never add npm packages.
FRONTEND LAYOUT (isolation contract): every requirement's UI lives in \
its OWN self-contained file frontend/src/features/<name>.jsx (default \
export; the component, its local state/hooks, and its scoped styles all \
in that one file). frontend/src/features/README.md (host-owned) carries \
the GRID EDITOR KERNEL PATTERN — grid-like tasks must build on ONE \
shared selection/editing state model per the README instead of \
re-implementing it per feature. frontend/src/app/App.jsx is the ASSEMBLY layer only — \
per feature add one import and one mount; the FIRST requirement may lay \
out the page structure there, after that never put business logic in it. \
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
(email|username|password), message (custom error). The host compiles it \
to src/database/schema.js and mounts CRUD at /api/<entity> \
automatically; entities with auth:true additionally get \
/api/auth/register (validates ALL entity fields, scrypt-hashes the \
secret, creates a session = auto-login), /api/auth/login (accepts any \
unique field as identifier: username or email, trimmed, email \
case-insensitive, uniform failure message that never reveals account \
existence), /api/auth/logout, /api/me (session restore; sessions \
survive server restart). Endpoints that are NOT plain CRUD (search / \
booking flows) go in src/routes/index.js using store + lib helpers.
FIXTURE CONTRACT (graded, not demo data): the requirement's `data:` \
section and every scenario GIVEN line are the graded seed contract — \
the host has already compiled them into backend/data/db.json. Names \
may carry a unique suffix, but roles, ownership, visibility, and \
relationships must match VERBATIM; NEVER delete or rename seeded rows \
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
path."""

# 契约前缀双语表(中文全角冒号 / 英文冒号,英文大小写不敏感)
_VALIDATION_PREFIXES = ("Validation 契约：", "Validation contract:")

# 与内核 planctl 上限保持一致(mod.rs MAX_VALIDATION_CHARS / MAX_STEPS)。
MAX_VALIDATION_LOCAL = 4000
MAX_STEPS_LOCAL = 64


def _trim_sentence(text: str, budget: int) -> str:
    """Sentence-boundary preferred truncation; a hard cut is the last
    resort but never mid-token for the trailing data (grades read the
    tail of GIVEN bullets)."""
    if len(text) <= budget:
        return text
    head = text[:budget]
    cut = head.rfind(". ")
    if cut > budget // 2:
        return head[:cut + 1]
    return head.rstrip() + "…"


# ── 微行为契约抽取(逐字交互语义,直指剩余失分点)─────────────────────
#
# run-002 复盘:剩余 2 红(Escape 取消、验证报错逐字文案)的契约埋在
# 4000 字 validation 段中部,模型读到即忘。此处从节点描述确定性抽取
# "键位/逐字文案"级微行为,以独立小节插在 FOCUS 头部——每批 3-6 行、
# 字节稳定、纯文本规则。
_INTERACTION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Pressing Escape|press Escape|Escape cancels", re.I),
     "Escape cancels an uncommitted cell edit (grid value unchanged, "
     "formula bar shows the original value)"),
    (re.compile(r"Pressing Enter|Enter or clicking", re.I),
     "Enter (or clicking another cell) commits an edit"),
    (re.compile(r"Ctrl\+Z|Ctrl\+Y|keyboard shortcut", re.I),
     "Keyboard shortcuts Ctrl+Z / Ctrl+Y drive Undo / Redo alongside the "
     "toolbar buttons"),
    (re.compile(r"case-insensitive", re.I),
     "Formula function names are case-insensitive"),
    (re.compile(r"exactly one visible error|EXACTLY ONE", re.I),
     "Render EXACTLY ONE visible error element per failure (no per-field "
     "+ form-level duplication)"),
    (re.compile(r"as the user types|without.{0,40}(Enter|submit)", re.I),
     "Live filtering applies as the user types (no Enter/submit needed)"),
    (re.compile(r"aria-selected", re.I),
     "Maintain aria-selected=true/false on every gridcell per the current "
     "selection"),
]
_VERBATIM_QUOTED_RE = re.compile(
    r'"([A-Z][^"]{6,80})"', re.MULTILINE)


def extract_interaction_contract(description: str, limit: int = 8) -> list[str]:
    """节点描述 → 逐字交互语义清单(确定性,≤limit 条)。

    两类来源:①模板规则(Escape/Enter/aria-selected 等键位与状态语义);
    ②双引号逐字文案(错误消息/按钮名,首字母大写的英文串)。
    """
    out: list[str] = []
    for regex, rule in _INTERACTION_RULES:
        if regex.search(description):
            out.append(rule)
        if len(out) >= limit:
            return out
    # 逐字文案:错误消息(句号/感叹号结尾)排序在前——它们是最容易
    # 被改写的失分点;去重保序。
    verbatim: list[str] = []
    for m in _VERBATIM_QUOTED_RE.finditer(description):
        quoted = m.group(1).strip()
        if quoted and quoted not in verbatim:
            verbatim.append(quoted)
    is_message = lambda s: s.endswith((".", "!", "?", ":")) or s.startswith(("Please", "Invalid", "Error"))
    verbatim.sort(key=lambda s: not is_message(s))
    for quoted in verbatim:
        if len(out) >= limit:
            break
        out.append(f'VERBATIM string: "{quoted}" (copy exactly)')
    return out


def _merge_given(head: str, given: list[str], validation: str) -> str:
    """GIVEN 子弹 + validation 合成 FOCUS validation 字段。

    优先级:GIVEN 子弹(评分种子数据常在句尾,硬截断=丢分)整条优先;
    超预算时先挤压 validation 段(保底 400 字符,句子边界截断)——文件夹
    级细节由 shared_contract 兜底,这里丢的只是重复叙述。
    """
    bullets = given[:12]
    blines = "".join(f"\n  - {g}" for g in bullets)
    budget = MAX_VALIDATION_LOCAL - len(head) - 1
    if len(blines) + len(validation or "") <= budget:
        out = head + blines
        if validation:
            out += "\n" + validation
        return out[:MAX_VALIDATION_LOCAL]
    v_text = _trim_sentence(validation or "", max(400, budget - len(blines)))
    fixed = len(head) + 1 + len(v_text)
    per = max(160, (MAX_VALIDATION_LOCAL - fixed) // len(bullets))
    merged = head + "".join(
        f"\n  - {_trim_sentence(g, per)}" for g in bullets)
    if v_text:
        merged += "\n" + v_text
    return merged[:MAX_VALIDATION_LOCAL]


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
      400/401 = 种子账号或统一密码约定被破坏,404 = auth 实体未声明。
    - entity: GET /api/<name> 期待 200 且非空;空 = 宿主种子行被删/集合被
      改名,404 = schema.json 未暴露该实体。users(auth 实体)没有引擎
      GET 路由,由 login 探针代管。判定语义在 _probe_routes_on 落地。
    """
    probes: list[dict] = []
    for login in (logins or [])[:2]:
        password = str(login.get("password") or fixture_manifest.FIXTURE_PASSWORD)
        probes.append({
            "method": "POST", "path": "/api/auth/login", "kind": "login",
            "user": str(login.get("username") or ""),
            "body": json.dumps({
                "username": login.get("username"),
                "password": password})})
    for name in (collections or []):
        if name == "users" or len(probes) >= 8:
            continue
        probes.append({"method": "GET", "path": f"/api/{name}",
                       "kind": "entity", "entity": name})
    return probes[:8]


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


# ── envelope 构建 ─────────────────────────────────────────────────────


# ── shared_contract(文件夹级需求契约)────────────────────────────────

_SHARED_CONTRACT_BUDGET = 12000  # 内核 MAX_SHARED_CONTRACT_CHARS


def build_shared_contract(tree: dict, budget: int = _SHARED_CONTRACT_BUDGET,
                          ) -> str | None:
    """非原子(文件夹)节点描述 → 稳定 CONTRACT 段文本。

    这些描述承载跨节点的共享 UI 契约(ARIA 角色/可访问名/持久化纪律),
    此前从未进入模型上下文(hackathon--sheet 的 grid 契约、hackathon
    --github 的 24.5KB 权限契约全链丢失)。确定性渲染、字节稳定,超预算
    时按文件夹均匀做句子边界截断。
    """
    parts: list[str] = []

    def walk(node: dict) -> None:
        if str(node.get("type", "")).upper() != "ATOMIC":
            desc = str(node.get("description") or "").strip()
            if desc:
                parts.append(f"[{node.get('id')}] {desc}")
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    walk(tree)
    if not parts:
        return None
    text = "\n\n".join(parts)
    if len(text) <= budget:
        return text
    soft = 1500
    trimmed = [_trim_sentence(p, soft) for p in parts]
    if sum(len(p) for p in trimmed) <= budget:
        return "\n\n".join(trimmed)
    per = max(300, budget // len(parts))
    return "\n\n".join(_trim_sentence(p, per) for p in parts)


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
    fixture_given: dict[str, list[str]] | None = None,
    seeded_nodes: set[str] | frozenset[str] | None = None,
    shared_contract: str | None = None,
    specs_staged: bool = False,
    arrange_nodes: set[str] | frozenset[str] | None = None,
) -> dict:
    """Build one envelope dict (snake_case, mirrors planctl::Envelope).

    fixture_given/seeded_nodes 挂接夹具契约:前者把每节点场景 GIVEN
    (评分前置态)以 FOCUS validation 前缀的形式结构化下发——宿主已播种
    的节点给"verify addressable"指针,未播种的给逐字清单(模型手工补种)。
    step 额外附 preconditions 数组(当前内核 unknown-field 忽略,前向兼容)。
    """
    seeded = seeded_nodes or frozenset()
    arranged = arrange_nodes or frozenset()
    steps = []
    for node in atomic_nodes:
        nid = str(node.get("id", ""))
        desc = str(node.get("description") or node.get("name") or "")
        validation, _notes = split_validation(desc)
        micro = extract_interaction_contract(desc)
        if micro:
            validation = (
                "INTERACTION SEMANTICS (graded verbatim):\n  - "
                + "\n  - ".join(micro)
                + ("\n" + validation if validation else "")
            )
        given = (fixture_given or {}).get(nid) or []
        if given:
            if nid in seeded:
                head = ("GIVEN fixtures (host-seeded into backend/data/"
                        "db.json — verify each is addressable on the "
                        "RUNNING app: the object opens, values match "
                        "verbatim):")
            elif nid in arranged:
                head = ("GIVEN pre-states CONFLICT with the canonical seed "
                        "and are NOT seeded — the grader arranges them "
                        "through the UI before the scenario; accept them "
                        "like any other edit, never hand-seed them:")
            else:
                head = ("GIVEN (graded fixtures — the graders assert these "
                        "exact pre-states; seed them into backend/data/"
                        "db.json before this scenario):")
            merged = _merge_given(head, given, validation)
            validation = merged[:MAX_VALIDATION_LOCAL]
        targets = {"files": [], "routes": [], "spec_files": spec_map.get(nid, [])}
        steps.append({
            "id": nid,
            "action": "modify",
            # PLAN 清单短摘要;完整契约走 validation(FOCUS 段逐字渲染)。
            "input": (validation or nid)[:200],
            "validation": validation[:MAX_VALIDATION_LOCAL] or None,
            "preconditions": [g[:300] for g in given[:12]] or None,
            "targets": targets,
            "expected_output": f"Playwright specs for {nid} pass",
            "status": statuses.get(nid, "pending"),
        })
    # steps 上限对齐内核 MAX_STEPS=64:47 节点的 hackathon 任务全量入列,
    # 不再截尾;focus 顶替逻辑保留作最后一道守卫(wire 校验 focus 精确
    # 匹配 step id,失配即丢整个 envelope)。
    trimmed = steps[:MAX_STEPS_LOCAL]
    if focus is not None:
        fid = str(focus)
        if fid not in {s["id"] for s in trimmed}:
            fstep = next((s for s in steps if s["id"] == fid), None)
            if fstep is not None:
                trimmed = (trimmed[:-1] + [fstep] if len(trimmed) >= MAX_STEPS_LOCAL
                           else trimmed + [fstep])
    envelope = {
        "v": 1,
        "seq": seq,
        "phase": phase,
        "stack": "web_react_express",
        "task": task[:500],
        "spec_dir": SPEC_STAGE_SUBDIR,
        "specs_staged": bool(specs_staged),
        "shared_contract": (shared_contract[:12000]
                            if shared_contract else None),
        "rules": RULESET,
        "rules_text": HOST_STACK_RULES,
        "extra_rules": (extra_rules[:2400] if extra_rules else None),
        "ports": {
            "grade": int(ports.get("grade", 0)),
            "smoke": int(ports.get("smoke", 0)),
            "extra": [int(p) for p in ports.get("extra", [])],
        },
        "steps": trimmed,
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
