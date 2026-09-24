"""夹具契约提取器 + 宿主种子编译器(纯标准库,单测覆盖)。

设计动机(2026-09-24 github 2分/sheet 5分复盘):需求的 ROOT `data:` 段、
每条场景 GIVEN 步骤与内联 "Seed data supplies..." 句子就是评分夹具契约,
此前全链无人消费——提示词只说 "demo data",模型自编种子导致夹具错配。
本模块宿主尽算三件事:

1. extract(tree): 契约清单结构化(data: 条目 / E2E_* 环境项 / 每节点 GIVEN
   与种子句),落盘 .arc/fixture-manifest.json 供对表;
2. compile_seed(manifest): 把可机械解析的部分直接编译成 db.json 行集
   (sheet 型: "A1/B1 are Item/Amount" → cells 字典,每场景一个独立工作簿,
   天然满足隔离契约;graph 型: 角色清单 → 账号/组织/仓库/团队骨架行,
   密码统一约定,scrypt 与 backend store.verify 逐位兼容);
3. render_extra_rules(manifest, stats): ≤2400 字符、确定性(字节稳定,
   满足缓存律)的全局契约文本,经 envelope.extra_rules 渲染进稳定 CONTRACT 段。

解析不了的条目一律进 uncovered 报告降级为清单注入,绝不自编替代品。
"""

from __future__ import annotations

import hashlib
import re

# 种子账号统一密码约定:extra_rules 下发给模型,登录探针按它验证;
# 大小写+数字+特殊字符,满足 validate.js 的 password 格式(≥12 位)。
FIXTURE_PASSWORD = "Fixture-password-123!"

E2E_RE = re.compile(r"E2E_[A-Z0-9_]+")
SEED_SENTENCE_RE = re.compile(
    r"[^.]*[Ss]eed data (?:supplies|provides)[^.]*\.")
COORD_RE = re.compile(r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\b")

_MAX_EXTRA_RULES = 2400


# ---------------------------------------------------------------- 提取

def _walk_atomic(node: dict, out: list | None = None) -> list[dict]:
    """与 main.flatten_atomic 同序遍历(此处独立实现,避免 import main)。"""
    if out is None:
        out = []
    node_type = str(node.get("type") or "").upper()
    if node_type == "ATOMIC" or (not node.get("children")
                                 and node_type != "FOLDER"):
        out.append(node)
    for child in node.get("children") or []:
        if isinstance(child, dict):
            _walk_atomic(child, out)
    return out


def extract(tree: dict) -> dict:
    """requirements 树 → 结构化夹具清单(确定性,无 IO)。"""
    global_items: list[dict] = []
    for cat in tree.get("data") or []:
        if not isinstance(cat, dict):
            continue
        for item in cat.get("items") or []:
            text = str(item)
            global_items.append({
                "category": str(cat.get("category") or ""),
                "text": text,
                "e2e_envs": E2E_RE.findall(text),
            })
    nodes: dict[str, dict] = {}
    for node in _walk_atomic(tree):
        nid = str(node.get("id"))
        givens: list[str] = []
        scenarios: list[dict] = []
        for sc in node.get("scenarios") or []:
            sc_givens = [
                str(step.get("content", ""))
                for step in (sc.get("steps") or [])
                if isinstance(step, dict)
                and str(step.get("keyword", "")).upper() == "GIVEN"]
            if sc_givens:
                scenarios.append({
                    "name": str(sc.get("name") or "scenario"),
                    "given": sc_givens})
                givens.extend(sc_givens)
        desc = str(node.get("description") or "")
        seeds = [m.group(0).strip() for m in SEED_SENTENCE_RE.finditer(desc)]
        if givens or seeds:
            nodes[nid] = {"given": givens, "scenarios": scenarios,
                          "seed_sentences": seeds}
    return {
        "global": global_items,
        "nodes": nodes,
        "e2e_envs": sorted({e for g in global_items for e in g["e2e_envs"]}),
    }


# ------------------------------------------------- GIVEN → cells(sheet 型)

_RANGE_HEADERS_RE = re.compile(
    r"\b([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)\s+are\s+([^;,]+)")
_ROWS_RE = re.compile(
    r"(?:rows\s+(\d+)\s*[-–]\s*(\d+)\s+are|with\s+data)\s+([^;]+)")
_SLASH_LIST_RE = re.compile(
    r"\b([A-Z]{1,3}[1-9][0-9]{0,3}(?:/[A-Z]{1,3}[1-9][0-9]{0,3})+)\s+are\s+"
    r"([^;,]+)")
_FORMULA_CONTAINS_RE = re.compile(
    r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\s+(?:contains|has)\s+(?:the\s+)?"
    r"(?:saved\s+)?formula\s+(=\S+?)\s+with\s+result\s+([^\s,;.]+)",
    re.IGNORECASE)
_FORMULA_IS_RE = re.compile(
    r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\s+is\s+(=\S+?)\s*(?:with\s+result|and\s+"
    r"displays?)\s+([^\s,;.]+)")
_SELECTED_VALUE_RE = re.compile(
    r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\s+is\s+selected\s+with\s+saved\s+value\s+"
    r"([^,;.]+)", re.IGNORECASE)
_SAVED_VALUE_RE = re.compile(
    r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\s+(?:on\s+Sheet\d+\s+)?has\s+saved\s+"
    r"value\s+([^,;.]+)", re.IGNORECASE)
_SINGLE_RE = re.compile(
    r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\s+(?:on\s+Sheet\d+\s+)?"
    r"(?:is|contains)\s+([^,;.]+)")
_SKIP_VALUE = {"empty"}
_AND_TAIL_RE = re.compile(
    r"\s+and\s+(?:has|have|provides?|displays?|contains?|accepts?|remains?|"
    r"still|does|is\s|[A-Z]{1,3}[1-9][0-9]{0,3}\s+(?:is|contains|has)\b)"
    r".*$", re.IGNORECASE)


def _clean_value(raw: str):
    """'Alpha, ' → 'Alpha';'Empty'/'empty' → None;数字文本 → 数字。"""
    v = raw.strip().strip('"“”').strip()
    v = re.sub(r"\s+(respectively|and)$", "", v).strip()
    v = _AND_TAIL_RE.sub("", v).strip()
    v = v.rstrip(".,;:").strip()
    if not v or v.lower() in _SKIP_VALUE:
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _split_top_commas(text: str, arity: int) -> list[list[str]] | None:
    """按 ', ' 切出等宽斜杠组;带引号逗号的值会破坏等宽 → 返回 None。"""
    groups: list[list[str]] = []
    for chunk in re.split(r",\s+", text):
        chunk = chunk.strip()
        chunk = re.sub(r"^(and|And)\s+", "", chunk)
        parts = chunk.split("/")
        if len(parts) != arity:
            return None
        groups.append([p for p in parts])
    return groups


def parse_given_cells(text: str) -> dict[str, dict]:
    """单条 GIVEN 文本 → {"Sheet1": {A1: {v:..}, ...}, "Sheet2": {...}}。

    支持的规律句式(覆盖 sheet 类任务的高频形态,逐条实测):
    - "A1/B1 are Item/Amount"、"A2 is Remove me"
    - "C2 contains (the saved) formula =A2*B2 with result 20"
    - "B1 is =A1*2 (and displays|with result) 4"、"A1 is selected with
      saved value 2"
    - "A1:C5 are Region/Sales/Status; rows 2-5 are East/1200/Open, ..."
      以及 "... with data Alpha/3, Bravo/1"(等宽斜杠组,引号内逗号 → 不解析)
    - "on Sheet2, A1 is Other"(按 sheet 前缀分桶)
    解析不到任何单元格 → 返回空 dict(调用方按 uncovered 处理)。
    """
    sheets: dict[str, dict] = {}

    def bucket(name: str) -> dict:
        return sheets.setdefault(name, {})

    # 按 "on Sheet2," 切段,前段归 Sheet1。
    parts = re.split(r"\bon\s+(Sheet\d+)\s*,", text)
    segments: list[tuple[str, str]] = [("", text)]
    if len(parts) >= 3:
        segments = [("", parts[0])]
        for i in range(1, len(parts), 2):
            segments.append((parts[i], parts[i + 1] if i + 1 < len(parts) else ""))

    for sheet_name, seg in segments:
        target_name = sheet_name or "Sheet1"
        cells = bucket(target_name)
        consumed: list[tuple] = []

        for m in _FORMULA_CONTAINS_RE.finditer(seg):
            coord, expr, result = m.group(1), m.group(2), m.group(3)
            cells[coord] = {"f": expr, "result": _clean_value(result) or expr}
            consumed.append(m.span())
        for m in _FORMULA_IS_RE.finditer(seg):
            coord, expr, result = m.group(1), m.group(2), m.group(3)
            cells[coord] = {"f": expr, "result": _clean_value(result) or expr}
            consumed.append(m.span())
        for m in _SELECTED_VALUE_RE.finditer(seg):
            v = _clean_value(m.group(2))
            if v is not None:
                cells[m.group(1)] = {"v": v}
            consumed.append(m.span())
        for m in _SAVED_VALUE_RE.finditer(seg):
            v = _clean_value(m.group(2))
            if v is not None:
                cells.setdefault(m.group(1), {"v": v})
            consumed.append(m.span())
        for m in _SLASH_LIST_RE.finditer(seg):
            coords = m.group(1).split("/")
            values = m.group(2).split("/")
            if len(coords) != len(values):
                continue
            for coord, raw in zip(coords, values):
                if coord in cells:
                    continue
                v = _clean_value(raw)
                if v is None:
                    continue
                if isinstance(v, str) and v.startswith("="):
                    cells[coord] = {"f": v}
                else:
                    cells[coord] = {"v": v}
            consumed.append(m.span())
        for m in _SINGLE_RE.finditer(seg):
            coord, raw = m.group(1), m.group(2)
            if coord in cells:
                continue
            if raw.strip().lower().startswith(("selected", "empty")):
                continue
            v = _clean_value(raw)
            if v is None:
                continue
            if isinstance(v, str) and v.startswith("="):
                cells[coord] = {"f": v}
            else:
                cells[coord] = {"v": v}
            consumed.append(m.span())

        # 表头区间 + 行组("A1:C5 are R/S/T; rows 2-5 are E/1/Open, ...")
        for m in _RANGE_HEADERS_RE.finditer(seg):
            c1, r1, c2, r2 = (_col_index(m.group(1)), int(m.group(2)),
                              _col_index(m.group(3)), int(m.group(4)))
            headers = m.group(5).split("/")
            if len(headers) != c2 - c1 + 1:
                continue
            for i, raw in enumerate(headers):
                v = _clean_value(raw)
                if v is not None:
                    cells[f"{chr(64 + c1 + i)}{r1}"] = {"v": v}
        for m in _ROWS_RE.finditer(seg):
            body = m.group(3)
            arity = None
            first = re.split(r",\s+", body)[0]
            arity = len(first.strip().split("/"))
            if arity < 2:
                continue
            groups = _split_top_commas(body, arity)
            if groups is None:
                continue
            start_row = int(m.group(1)) if m.group(1) else 2
            for offset, group in enumerate(groups):
                for ci, raw in enumerate(group):
                    v = _clean_value(raw)
                    if v is None:
                        continue
                    coord = f"{chr(64 + 1 + ci)}{start_row + offset}"
                    if isinstance(v, str) and v.startswith("="):
                        cells[coord] = {"f": v}
                    else:
                        cells[coord] = {"v": v}
    return {k: v for k, v in sheets.items() if v}


# ------------------------------------------------ 角色 → 实体(graph 型)

_ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"organization owner|org owner", "org-owner"),
    (r"organization member|org member", "org-member"),
    (r"repository read|\bread\b role|role of read", "repo-read"),
    (r"\btriage\b", "triage"),
    (r"\bwrite\b", "write"),
    (r"\bmaintain\b", "maintain"),
    (r"\badmin\b", "admin"),
    (r"pull-request author|pr author", "pr-author"),
    (r"non-author reviewer|reviewer", "reviewer"),
    (r"no-repository-access|no repository access|no-access", "no-access"),
]


def _scrypt_hex(password: str, salt_hex: str) -> str:
    """与 backend store.js 的 hash/verify 逐位兼容:`salt:hash`,salt 为
    十六进制字符串本身作 UTF-8 盐(Node scryptSync 字符串盐语义),
    N=16384/r=8/p=1(=Node 默认参数)。确定性盐 → 种子文件字节稳定。"""
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt_hex.encode("utf-8"),
        dklen=32, n=16384, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f"{salt_hex}:{digest.hex()}"


def fixture_user_row(role: str) -> dict:
    username = f"fixture-{role}"
    salt = hashlib.md5(role.encode()).hexdigest()[:16]
    return {
        "id": f"u-fixture-{role}",
        "username": username,
        "email": f"{username}@example.test",
        "password": _scrypt_hex(FIXTURE_PASSWORD, salt),
        "verifiedEmail": True,
        "terms": True,
        "createdAt": "2026-01-01T00:00:00.000Z",
    }


def _detect_roles(texts: list[str]) -> list[str]:
    blob = "\n".join(texts).lower()
    roles: list[str] = []
    for pattern, role in _ROLE_PATTERNS:
        if re.search(pattern, blob) and role not in roles:
            roles.append(role)
    return roles


# -------------------------------------------------------------- 编译入口

def _kind(manifest: dict) -> str:
    given_blob = "\n".join(
        g for info in manifest["nodes"].values() for g in info["given"])
    global_blob = "\n".join(g["text"] for g in manifest["global"])
    cellish = len(COORD_RE.findall(given_blob))
    roles = _detect_roles([global_blob])
    if cellish >= 4 and roles:
        return "mixed"
    if cellish >= 4:
        return "sheet"
    if roles:
        return "graph"
    return "none"


def compile_seed(manifest: dict) -> dict:
    """夹具清单 → 宿主可落盘产物(确定性,无 IO)。

    返回 {"kind", "db": {collection: [rows]}, "schema": starter|None,
          "covered": {nid: [scenario 名]}, "uncovered": [...], "logins": [...]}
    covered 的场景已生成独立实例(隔离契约);uncovered 一律报告、不自编。
    """
    kind = _kind(manifest)
    db: dict[str, list] = {}
    covered: dict[str, list[str]] = {}
    uncovered: list[dict] = []
    logins: list[dict] = []

    if kind in ("sheet", "mixed"):
        used_names: set[str] = set()
        for nid, info in manifest["nodes"].items():
            groups = info.get("scenarios") or (
                [{"name": "default", "given": info["given"]}]
                if info["given"] else [])
            for sc in groups:
                sname = sc["name"]
                cells = parse_given_cells("\n".join(sc["given"]))
                if not cells:
                    if COORD_RE.findall("\n".join(sc["given"])):
                        uncovered.append({"node": nid, "scenario": sname,
                                          "reason": "given cells unparseable"})
                    continue
                name = sname if sname not in used_names else f"{sname} ({nid})"
                used_names.add(sname)
                key = f"{nid}::{sname}"
                sheet_rows = [
                    {"name": sname_, "cells": cells_, "selection": "A1"}
                    for sname_, cells_ in cells.items()]
                db.setdefault("workbooks", []).append({
                    "id": "wb-" + hashlib.sha1(key.encode()).hexdigest()[:10],
                    "name": name,
                    "lastUpdated": "2026-01-01T00:00:00.000Z",
                    "state": {"activeSheet": sheet_rows[0]["name"],
                              "sheets": sheet_rows,
                              "focus": {s["name"]: "A1" for s in sheet_rows}},
                })
                covered.setdefault(nid, []).append(sname)

    if kind in ("graph", "mixed"):
        global_blob = [g["text"] for g in manifest["global"]]
        seed_blob = [s for info in manifest["nodes"].values()
                     for s in info["seed_sentences"]]
        roles = _detect_roles(global_blob + seed_blob)
        blob = "\n".join(global_blob + seed_blob).lower()
        for role in roles:
            row = fixture_user_row(role)
            db.setdefault("users", []).append(row)
            logins.append({"username": row["username"], "role": role})
        if "organization" in blob:
            db.setdefault("organizations", []).append({
                "id": "o-fixture-org", "name": "fixture-org",
                "displayName": "Fixture Organization", "public": True,
                "createdAt": "2026-01-01T00:00:00.000Z"})
            if "org-owner" in roles:
                db.setdefault("members", []).append({
                    "id": "m-fixture-owner", "orgName": "fixture-org",
                    "username": "fixture-org-owner", "role": "owner"})
            if "org-member" in roles:
                db.setdefault("members", []).append({
                    "id": "m-fixture-member", "orgName": "fixture-org",
                    "username": "fixture-org-member", "role": "member"})
        if "repository" in blob and "organization" in blob:
            db.setdefault("repositories", []).append({
                "id": "r-fixture-public", "orgName": "fixture-org",
                "name": "fixture-public-repo",
                "description": "Fixture public repository",
                "visibility": "public", "defaultBranch": "main",
                "updatedAt": "2026-01-01T00:00:00.000Z"})
            if "private repositor" in blob or "private repository" in blob:
                db.setdefault("repositories", []).append({
                    "id": "r-fixture-private", "orgName": "fixture-org",
                    "name": "fixture-private-repo",
                    "description": "Fixture private repository",
                    "visibility": "private", "defaultBranch": "main",
                    "updatedAt": "2026-01-01T00:00:00.000Z"})
        if "team" in blob:
            db.setdefault("teams", []).append({
                "id": "t-fixture-team", "orgName": "fixture-org",
                "name": "fixture-team", "description": "Fixture team",
                "createdAt": "2026-01-01T00:00:00.000Z"})
            if "org-owner" in roles:
                db.setdefault("teamMembers", []).append({
                    "id": "tm-fixture-owner", "orgName": "fixture-org",
                    "teamName": "fixture-team",
                    "username": "fixture-org-owner"})

    schema = None
    if kind in ("graph", "mixed"):
        schema = {"entities": [
            {"name": "users", "auth": True, "label": "User", "fields": [
                {"name": "username", "label": "Username", "type": "string",
                 "required": True, "unique": True, "min": 3, "max": 39},
                {"name": "email", "label": "Email", "type": "string",
                 "required": True, "unique": True, "format": "email"},
                {"name": "password", "label": "Password", "type": "password",
                 "required": True},
                {"name": "verifiedEmail", "label": "Email verified",
                 "type": "boolean"},
                {"name": "terms", "label": "Agree to the terms",
                 "type": "boolean"}]},
            {"name": "organizations", "label": "Organization", "fields": [
                {"name": "name", "label": "Name", "type": "string",
                 "required": True, "unique": True},
                {"name": "displayName", "label": "Display name",
                 "type": "string"},
                {"name": "public", "label": "Public", "type": "boolean"}]},
            {"name": "repositories", "label": "Repository", "fields": [
                {"name": "orgName", "label": "Owner", "type": "string"},
                {"name": "name", "label": "Name", "type": "string",
                 "required": True},
                {"name": "description", "label": "Description",
                 "type": "string"},
                {"name": "visibility", "label": "Visibility", "type": "string",
                 "enum": ["public", "private"]},
                {"name": "defaultBranch", "label": "Default branch",
                 "type": "string"}]},
            {"name": "members", "label": "Member", "fields": [
                {"name": "orgName", "label": "Organization", "type": "string"},
                {"name": "username", "label": "Username", "type": "string"},
                {"name": "role", "label": "Role", "type": "string"}]},
            {"name": "teams", "label": "Team", "fields": [
                {"name": "orgName", "label": "Organization", "type": "string"},
                {"name": "name", "label": "Name", "type": "string"},
                {"name": "description", "label": "Description",
                 "type": "string"}]},
            {"name": "teamMembers", "label": "Team member", "fields": [
                {"name": "orgName", "label": "Organization", "type": "string"},
                {"name": "teamName", "label": "Team", "type": "string"},
                {"name": "username", "label": "Username", "type": "string"}]},
        ]}
    elif kind == "sheet":
        schema = {"entities": [
            {"name": "workbooks", "label": "Workbook", "fields": [
                {"name": "name", "label": "Name", "type": "string",
                 "required": True, "min": 1, "max": 200},
                {"name": "lastUpdated", "label": "Last updated",
                 "type": "string"}]},
        ]}

    return {"kind": kind, "db": db, "schema": schema, "covered": covered,
            "uncovered": uncovered, "logins": logins}


# ------------------------------------------------- extra_rules(稳定段)

def render_extra_rules(manifest: dict, stats: dict | None = None) -> str:
    """全局夹具契约文本:确定性渲染(同输入 → 逐字节相同,缓存律),≤2400。"""
    stats = stats or {}
    e2e = manifest["e2e_envs"]
    e2e_str = ", ".join(e2e[:12]) + (f" (+{len(e2e) - 12} more)"
                                     if len(e2e) > 12 else "") if e2e \
        else "none named"
    uncovered_n = int(stats.get("uncovered", 0))
    parts = [
        "FIXTURE CONTRACT (graded, not demo data): the requirement's data: "
        "section and every scenario GIVEN line are the graded seed "
        "contract — names may carry a unique suffix, but roles, ownership, "
        "visibility, and relationships must match VERBATIM. The host "
        "seeded the machine-compilable part into backend/data/db.json: "
        "NEVER delete or rename seeded rows/collections; extend "
        "backend/data/schema.json so every seeded field is exposed.",
        f'Seeded fixture accounts share the password "'
        f'{FIXTURE_PASSWORD}" (username fixture-<role>, email '
        "fixture-<role>@example.test) — auth must verify them from the "
        "seed and sign-in pages must accept these credentials.",
        "Scenarios that MODIFY data each need their OWN fresh "
        "preconfigured instance (never share one mutable fixture across "
        "scenarios; the graders restore state between runs).",
        f"E2E_* harness fixtures named by the requirement: {e2e_str} — "
        "every one must map to a concrete seeded object you create.",
        ("Files/commits are the single source of truth: branches store "
         "head references only, NEVER duplicate whole file trees per "
         "branch or fork.") if stats.get("kind") in ("graph", "mixed")
        else "",
        (f"{uncovered_n} scenario fixture(s) could NOT be auto-compiled — "
         "they appear per-node under GIVEN; hand-seed those exact "
         "pre-states before implementing the node.") if uncovered_n else "",
        "Runtime state (backend/data/db.run.json) never ships; it resets "
        "to the seed on every server restart.",
    ]
    text = " ".join(p for p in parts if p)
    return text[:_MAX_EXTRA_RULES]


# ------------------------------------------------ 落盘/读取(宿主 IO 面最小)

def materialize(output_dir, manifest: dict) -> dict | None:
    """宿主种子落盘(幂等,保守):db.json 仍为空占位时才写,schema.json
    缺失且编译器给出 starter 时才写;返回 compile_seed 结果供流控记账,
    跳过时返回 None。模型已写过种子(非空 collections)绝不覆盖。"""
    import json
    from pathlib import Path

    compiled = compile_seed(manifest)
    if not compiled["db"]:
        return compiled  # 无可编译行(仅报告用),不落盘

    data_dir = Path(output_dir) / "backend" / "data"
    db_file = data_dir / "db.json"
    existing = {}
    if db_file.exists():
        try:
            existing = json.loads(db_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 占位/坏 JSON 同样按空处理
            existing = {}
    if any(existing.get("collections", {}).values()):
        return None  # 已有种子(模型或上一轮写过)——不覆盖
    merged = {"collections": dict(existing.get("collections") or {})}
    for name, rows in compiled["db"].items():
        merged["collections"].setdefault(name, [])
        have = {r.get("id") for r in merged["collections"][name]}
        merged["collections"][name].extend(
            r for r in rows if r.get("id") not in have)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    schema_file = data_dir / "schema.json"
    if compiled["schema"] and not schema_file.exists():
        schema_file.write_text(
            json.dumps(compiled["schema"], ensure_ascii=False, indent=2)
            + "\n", encoding="utf-8")
    return compiled
