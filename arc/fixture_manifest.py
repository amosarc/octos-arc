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
# 无仓库种子句时的挂靠仓(hackathon--github 的公开仓库锚点)。
PUBLIC_REPO_DEFAULT = "acme-docs"

E2E_RE = re.compile(r"E2E_[A-Z0-9_]+")
SEED_SENTENCE_RE = re.compile(
    r"[^.]*[Ss]eed data (?:supplies|provides)[^.]*\.")
COORD_RE = re.compile(r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\b")
# 季度/编号语境的 "Q3"(后随非数字,如 "Q3 Sales")与真实单元格坐标同形——
# 统计与解析前一律剔除(hackathon--github 曾因此 32 处裸 Q3 全部误计为
# 坐标,kind 判定被带偏成 sheet、种子账号零编译)。
_QUARTER_TOKEN_RE = re.compile(r"\b[Qq][1-4]\b(?!\d)")

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


# ------------------------------------------- hackathon 系新句式种子解析
#
# hackathon--sheet 的 GIVEN 与旧 sheet 题句式不同("cell A1 value `Region`"
# / "cells `A1=2`" / "range `A1:C6` with headers `H/H/H` and rows `E/1/O`,
# ..." / "workbook `Q3 Sales` with `Sheet1` and `Sheet2`, rows `E/1` and
# `N/2`")。本节解析这些形态,产出与 parse_given_cells 同形的 cells 字典,
# 另携带 workbook/sheet 归属供规范种子合并。

_NEW_WORKBOOK_RE = re.compile(
    r"(?:seeded\s+)?workbook\s+`([^`]+)`", re.IGNORECASE)
_NEW_CELL_VALUE_RE = re.compile(
    r"\bcell\s+([A-Z]{1,3}[1-9][0-9]{0,3})\s+value\s+`([^`]+)`",
    re.IGNORECASE)
_NEW_CELLS_EQ_RE = re.compile(
    r"`([A-Z]{1,3}[1-9][0-9]{0,3})=([^`]+)`")
_NEW_SHEET_NAME_RE = re.compile(r"`(Sheet\d+)`")
_NEW_ROWS_TUPLES_RE = re.compile(r"rows\s+((?:`[^`]+`)(?:\s+and\s+`[^`]+`)*)",
                                 re.IGNORECASE)
_NEW_RANGE_START_RE = re.compile(
    r"range\s+`([A-Z]{1,3})([1-9][0-9]{0,3}):", re.IGNORECASE)
_NEW_HEADERS_RE = re.compile(r"headers\s+`([^`]+)`", re.IGNORECASE)
_NEW_RANGE_CONTAINING_RE = re.compile(
    r"range\s+`[A-Z]{1,3}[1-9][0-9]{0,3}:[A-Z]{1,3}[1-9][0-9]{0,3}`\s+"
    r"containing\s+", re.IGNORECASE)
_BACKTICKED_GROUP_RE = re.compile(r"`([^`]+)`")


def _cell_from(coord: str, raw: str) -> dict:
    v = _clean_value(raw)
    if v is None:
        return {}
    if isinstance(v, str) and v.startswith("="):
        return {"f": v}
    return {"v": v}


def _row_tuple_to_cells(raw: str, row: int, start_col: int = 1) -> dict:
    parts = [p.strip() for p in raw.split("/")]
    cells: dict = {}
    for i, part in enumerate(parts):
        cell = _cell_from("", part)
        if cell:
            cells[f"{chr(64 + start_col + i)}{row}"] = cell
    return cells


def parse_seed_sentence(text: str) -> dict:
    """hackathon 新句式 GIVEN → {"workbook", "sheets", "arrange_only"}。

    - workbook: "seeded workbook `Q3 Sales`"(规范种子合并的归属键);
    - sheets: {sheet_name: {coord: cell}},未知归属落 Sheet1;
    - arrange_only: 场景语义要求单元格处于与全局种子冲突的状态
      (REQ-3 的 A1:B2=Item/Qty、REQ-4 的 A1=2)时为 True——这类状态由
      测试经 UI 自行布置,不进规范种子。

    注意:此处不做季度词剔除——工作簿名本身就叫 "Q3 Sales";各解析
    正则均为动词/结构锚定,裸 "Q3" 不会误入 cells。
    """
    plain = text
    sheets: dict[str, dict] = {}
    wb_match = _NEW_WORKBOOK_RE.search(plain)
    workbook = wb_match.group(1) if wb_match else None

    def bucket(name: str) -> dict:
        return sheets.setdefault(name, {})

    # "cell A1 value `Region`"
    for m in _NEW_CELL_VALUE_RE.finditer(plain):
        bucket("Sheet1")[f"{m.group(1)}"] = _cell_from(m.group(1), m.group(2))
    # "`A1=2`, `B1=3`"
    for m in _NEW_CELLS_EQ_RE.finditer(plain):
        bucket("Sheet1")[m.group(1)] = _cell_from(m.group(1), m.group(2))
    # "with `Sheet1` and `Sheet2`, rows `East/1200` and `North/800`"
    sheet_names = _NEW_SHEET_NAME_RE.findall(plain)
    for m in _NEW_ROWS_TUPLES_RE.finditer(plain):
        tuples = _BACKTICKED_GROUP_RE.findall(m.group(1))
        target = sheet_names[-1] if sheet_names else "Sheet1"
        row = 1
        for t in tuples:
            if ":" in t and not "/" in t:
                continue  # 区间引用不是行数据
            for coord, cell in _row_tuple_to_cells(t, row).items():
                bucket(target)[coord] = cell
            row += 1
    # "range `A1:C6` with headers `Region/Sales/Status` and rows `E/1/O`, ..."
    for m in _NEW_HEADERS_RE.finditer(plain):
        header_cells = _row_tuple_to_cells(m.group(1), 1)
        start = _NEW_RANGE_START_RE.search(plain[:m.start()])
        if start and start.group(2) != "1":
            header_cells = _row_tuple_to_cells(
                m.group(1), int(start.group(2)))
        for coord, cell in header_cells.items():
            bucket("Sheet1")[coord] = cell
        tail = plain[m.end():]
        row = (int(start.group(2)) + 1) if start and start.group(2) != "1" else 2
        for t in _BACKTICKED_GROUP_RE.findall(tail):
            if "/" not in t:
                break  # rows 序列结束(遇到非行数据引号)
            for coord, cell in _row_tuple_to_cells(t, row).items():
                bucket("Sheet1")[coord] = cell
            row += 1
    # "range `A1:B2` containing `Item/Qty` and `Pen/4`"(矩形预置态,
    # 与规范种子常冲突 → 合并时按冲突处理,不进规范种子)
    arrange_only = False
    for m in _NEW_RANGE_CONTAINING_RE.finditer(plain):
        arrange_only = True
        start = _NEW_RANGE_START_RE.search(plain[:m.start()])
        row = int(start.group(2)) if start else 1
        # "containing" 之后的引号组(以 and/, 连接)逐条落格
        tail = plain[m.end():]
        for t in _BACKTICKED_GROUP_RE.findall(tail):
            if "/" not in t:
                break
            for coord, cell in _row_tuple_to_cells(t, row).items():
                bucket("Sheet1")[coord] = cell
            row += 1
    return {
        "workbook": workbook,
        "sheets": {k: v for k, v in sheets.items() if v},
        "arrange_only": arrange_only,
    }


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
    # 先剔除季度/编号语境的 "Q3" 再数坐标——"Q3 Sales" 与单元格 Q3 同形,
    # 不剔除会把 graph 型任务误判成 sheet(hackathon--github 32/32 全误计)。
    given_blob = _QUARTER_TOKEN_RE.sub("", given_blob)
    global_blob = "\n".join(g["text"] for g in manifest["global"])
    cellish = len(COORD_RE.findall(given_blob))
    roles = _detect_roles([global_blob])
    # hackathon 新句式的图谱种子实体("account `x`, email `y`, password `z`"
    # / "organization `A`")同样是 graph 证据,即便角色关键词被匿名化。
    seedish = bool(_SEED_ACCOUNT_RE.search(
        "\n".join(g for info in manifest["nodes"].values()
                  for g in info["given"])))
    if cellish >= 4 and (roles or seedish):
        return "mixed"
    if cellish >= 4:
        return "sheet"
    if roles or seedish:
        return "graph"
    return "none"


def _parse_node_scenarios(manifest: dict) -> list[dict]:
    """每个可编译场景 → {nid, sname, cells, workbook}(新旧句式合并解析)。

    legacy parse_given_cells 负责旧 sheet 句式("A1 is X"/"A1:B2 are ...");
    parse_seed_sentence 负责 hackathon 新句式;两者按 sheet 名互补合并。
    """
    out: list[dict] = []
    for nid, info in manifest["nodes"].items():
        groups = info.get("scenarios") or (
            [{"name": "default", "given": info["given"]}]
            if info["given"] else [])
        for sc in groups:
            text = "\n".join(sc["given"])
            cells = parse_given_cells(text)
            seed = parse_seed_sentence(text)
            for sname_, scells in seed["sheets"].items():
                merged_sheet = dict(cells.get(sname_, {}))
                for coord, cell in scells.items():
                    merged_sheet.setdefault(coord, cell)
                cells = {**cells, sname_: merged_sheet}
            if not cells:
                continue
            out.append({"nid": nid, "sname": sc["name"], "cells": cells,
                        "workbook": seed["workbook"]})
    return out


def _merge_canonical_workbook(entries: list[dict], name: str) -> dict:
    """同名工作簿场景 → 单一规范工作簿(证据合并,冲突回报告)。

    返回 {"workbook": row, "covered": {nid: [sname]}, "arrange":
    {nid: [sname]}}。合并策略:每个 sheet 取单元格最多的一版作底,
    其余场景只补空位;任何坐标上的值冲突 = 该场景的预置态不进种子
    (测试经 UI 自行布置),记入 arrange。
    """
    covered: dict[str, list[str]] = {}
    arrange: dict[str, list[str]] = {}
    # 逐 sheet 分桶
    by_sheet: dict[str, list[dict]] = {}
    for e in entries:
        for sname_, cells_ in e["cells"].items():
            by_sheet.setdefault(sname_, []).append(
                {"nid": e["nid"], "sname": e["sname"], "cells": cells_})
    sheets: list[dict] = []
    canonical_cells: dict[str, dict] = {}
    for sname_ in sorted(by_sheet):
        bucket = by_sheet[sname_]
        base = max(bucket, key=lambda b: len(b["cells"]))
        merged = dict(base["cells"])
        base_coords = set(merged)
        conflicts: list[dict] = []
        for b in bucket:
            if b is base:
                continue
            clash = any(
                c in merged and merged[c] != cell
                for c, cell in b["cells"].items())
            if clash:
                conflicts.append(b)
                continue
            fresh = {c: v for c, v in b["cells"].items() if c not in merged}
            if fresh or not (set(b["cells"]) - base_coords):
                merged.update(fresh)
        sheets.append({"name": sname_, "cells": merged, "selection": "A1"})
        canonical_cells[sname_] = merged
        for b in bucket:
            if b in conflicts:
                arrange.setdefault(b["nid"], []).append(b["sname"])
            else:
                covered.setdefault(b["nid"], []).append(b["sname"])
    active = next((s["name"] for s in sheets if s["name"] == "Sheet1"),
                  sheets[0]["name"])
    row = {
        "id": "wb-" + hashlib.sha1(name.encode()).hexdigest()[:10],
        "name": name,
        "lastUpdated": "2026-01-01T00:00:00.000Z",
        "state": {"activeSheet": active, "sheets": sheets,
                  "focus": {s["name"]: "A1" for s in sheets}},
    }
    return {"workbook": row, "covered": covered, "arrange": arrange}


def _legacy_scenario_workbook(entry: dict, db: dict, covered: dict,
                              used_names: set) -> None:
    """旧 sheet 行为:一个场景 = 一个独立命名工作簿(隔离契约)。"""
    sname = entry["sname"]
    name = sname if sname not in used_names else f"{sname} ({entry['nid']})"
    used_names.add(sname)
    key = f"{entry['nid']}::{sname}"
    sheet_rows = [
        {"name": sname_, "cells": cells_, "selection": "A1"}
        for sname_, cells_ in entry["cells"].items()]
    db.setdefault("workbooks", []).append({
        "id": "wb-" + hashlib.sha1(key.encode()).hexdigest()[:10],
        "name": name,
        "lastUpdated": "2026-01-01T00:00:00.000Z",
        "state": {"activeSheet": sheet_rows[0]["name"],
                  "sheets": sheet_rows,
                  "focus": {s["name"]: "A1" for s in sheet_rows}},
    })
    covered.setdefault(entry["nid"], []).append(sname)


def compile_seed(manifest: dict) -> dict:
    """夹具清单 → 宿主可落盘产物(确定性,无 IO)。

    返回 {"kind", "db": {collection: [rows]}, "schema": starter|None,
          "covered": {nid: [scenario 名]}, "uncovered": [...],
          "arrange": {nid: [scenario 名]}, "logins": [...]}
    covered 的场景种子已就位;arrange 的场景预置态与规范种子冲突
    (测试经 UI 自行布置,宿主不播种);uncovered 一律报告、不自编。
    sheet 型同名工作簿 ≥2 次被引用时合并为单一规范工作簿(评分断言
    的 `Q3 Sales` 必须唯一可见),否则保留按场景独立实例的 legacy 行为。
    """
    kind = _kind(manifest)
    db: dict[str, list] = {}
    covered: dict[str, list[str]] = {}
    arrange: dict[str, list[str]] = {}
    uncovered: list[dict] = []
    logins: list[dict] = []
    canonical_mode = False

    if kind in ("sheet", "mixed"):
        entries = _parse_node_scenarios(manifest)
        name_counts: dict[str, int] = {}
        for e in entries:
            if e["workbook"]:
                name_counts[e["workbook"]] = \
                    name_counts.get(e["workbook"], 0) + 1
        canonical_name = (max(name_counts.items(), key=lambda kv: kv[1])[0]
                          if name_counts else None)
        if canonical_name and name_counts[canonical_name] >= 2:
            canonical_mode = True
            # 规范合并集 = 点名规范工作簿的场景 + 未点名工作簿的场景
            # (如 REQ-5 只说 "seeded worksheet range");点名其他名字的
            # 才走 legacy 独立实例。
            named = [e for e in entries
                     if e["workbook"] in (canonical_name, None)]
            merged = _merge_canonical_workbook(named, canonical_name)
            db.setdefault("workbooks", []).append(merged["workbook"])
            for nid, snames in merged["covered"].items():
                covered.setdefault(nid, []).extend(snames)
            for nid, snames in merged["arrange"].items():
                arrange.setdefault(nid, []).extend(snames)
            used_names: set[str] = {canonical_name}
            for e in entries:
                if e["workbook"] in (canonical_name, None):
                    continue
                _legacy_scenario_workbook(e, db, covered, used_names)
        else:
            used_names = set()
            for e in entries:
                _legacy_scenario_workbook(e, db, covered, used_names)
        # 未解析出任何单元格但确有坐标语境的场景 → 报告(不自编)。
        parsed_keys = {(e["nid"], e["sname"]) for e in entries}
        for nid, info in manifest["nodes"].items():
            for sc in (info.get("scenarios") or []):
                sname = sc.get("name", "")
                if (nid, sname) in parsed_keys:
                    continue
                blob = _QUARTER_TOKEN_RE.sub("",
                                             "\n".join(sc.get("given") or []))
                if COORD_RE.findall(blob):
                    uncovered.append({"node": nid, "scenario": sname,
                                      "reason": "given cells unparseable"})

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
        # hackathon 新句式种子实体:逐字账号/组织/仓库/成员/团队
        # ("The seeded data is account `alice-dev`, email `a@x.test`,
        # password `P!`" / "seeded data includes the existing organization
        # `Acme Demo`, repository `acme-docs`, member `bob-reviewer`,
        # team `frontend-team`")。密码经 scrypt 落库,登录按明文约定。
        seeded = parse_seeded_entities(
            global_blob + [s for info in manifest["nodes"].values()
                           for s in info.get("seed_sentences", [])]
            + [g for info in manifest["nodes"].values()
               for g in info["given"]])
        for acct in seeded["accounts"]:
            if any(u["username"] == acct["username"]
                   for u in db.get("users", [])):
                continue
            salt = hashlib.md5(acct["username"].encode()).hexdigest()[:16]
            db.setdefault("users", []).append({
                "id": f"u-seed-{acct['username']}",
                "username": acct["username"],
                "email": acct["email"],
                "password": _scrypt_hex(acct["password"], salt),
                "verifiedEmail": True, "terms": True,
                "createdAt": "2026-01-01T00:00:00.000Z"})
            logins.append({"username": acct["username"],
                           "password": acct["password"], "role": "seed"})
        org_rows = db.setdefault("organizations", [])
        for org in seeded["organizations"]:
            if any(o["name"] == org for o in org_rows):
                continue
            org_rows.append({
                "id": "o-seed-" + hashlib.sha1(org.encode()).hexdigest()[:8],
                "name": org, "displayName": org, "public": True,
                "createdAt": "2026-01-01T00:00:00.000Z"})
        primary_org = (org_rows[0]["name"] if org_rows else "fixture-org")
        repo_rows = db.setdefault("repositories", [])
        for repo in seeded["repositories"]:
            if any(r["name"] == repo for r in repo_rows):
                continue
            repo_rows.append({
                "id": "r-seed-" + hashlib.sha1(repo.encode()).hexdigest()[:8],
                "orgName": primary_org, "name": repo,
                "description": f"Seeded repository {repo}",
                "visibility": "public", "defaultBranch": "main",
                "updatedAt": "2026-01-01T00:00:00.000Z"})
        team_rows = db.setdefault("teams", [])
        for team in seeded["teams"]:
            if any(t["name"] == team for t in team_rows):
                continue
            team_rows.append({
                "id": "t-seed-" + hashlib.sha1(team.encode()).hexdigest()[:8],
                "orgName": primary_org, "name": team,
                "description": f"Seeded team {team}",
                "createdAt": "2026-01-01T00:00:00.000Z"})
        member_rows = db.setdefault("members", [])
        for member in seeded["members"]:
            if any(m["username"] == member for m in member_rows):
                continue
            member_rows.append({
                "id": "m-seed-" + hashlib.sha1(member.encode()).hexdigest()[:8],
                "orgName": primary_org, "username": member, "role": "member"})
        # C:仓库附属种子(分支/issue/PR/标签/里程碑/提交)挂到首个仓库;
        # README.md 与 main 分支是需求原文点名的最小文件集。
        primary_repo = (repo_rows[0]["name"] if repo_rows else PUBLIC_REPO_DEFAULT)
        branch_rows = db.setdefault("branches", [])
        def _branch(name: str, protected: bool = False) -> None:
            if any(b["name"] == name and b["repoName"] == primary_repo
                   for b in branch_rows):
                return
            branch_rows.append({
                "id": "br-seed-" + hashlib.sha1(
                    f"{primary_repo}:{name}".encode()).hexdigest()[:8],
                "repoOrg": primary_org, "repoName": primary_repo,
                "name": name, "head": "seed-head",
                "protected": protected})
        _branch("main", protected=True)
        for name in seeded["branches"]:
            _branch(name)
        if "main" not in seeded["branches"]:
            pass  # main 已由默认分支补齐
        issue_rows = db.setdefault("issues", [])
        for i, title in enumerate(seeded["issues"], 1):
            if any(x["title"] == title for x in issue_rows):
                continue
            issue_rows.append({
                "id": "i-seed-" + hashlib.sha1(
                    f"{primary_repo}:{title}".encode()).hexdigest()[:8],
                "repoOrg": primary_org, "repoName": primary_repo,
                "number": i, "title": title, "description": "",
                "author": (logins[0]["username"] if logins else "seed"),
                "status": "open", "labels": ""})
        pull_rows = db.setdefault("pulls", [])
        for i, title in enumerate(seeded["pulls"], 1):
            if any(x["title"] == title for x in pull_rows):
                continue
            pull_rows.append({
                "id": "p-seed-" + hashlib.sha1(
                    f"{primary_repo}:{title}".encode()).hexdigest()[:8],
                "repoOrg": primary_org, "repoName": primary_repo,
                "number": i, "title": title,
                "author": (logins[0]["username"] if logins else "seed"),
                "sourceBranch": "feature-search", "targetBranch": "main",
                "status": "open", "reviewers": ""})
        label_rows = db.setdefault("labels", [])
        for name in seeded["labels"]:
            if any(x["name"] == name for x in label_rows):
                continue
            label_rows.append({
                "id": "l-seed-" + hashlib.sha1(
                    f"{primary_repo}:{name}".encode()).hexdigest()[:8],
                "repoOrg": primary_org, "repoName": primary_repo,
                "name": name, "color": "#84b6eb"})
        ms_rows = db.setdefault("milestones", [])
        for title in seeded["milestones"]:
            if any(x["title"] == title for x in ms_rows):
                continue
            ms_rows.append({
                "id": "ms-seed-" + hashlib.sha1(
                    f"{primary_repo}:{title}".encode()).hexdigest()[:8],
                "repoOrg": primary_org, "repoName": primary_repo,
                "title": title, "dueOn": "2026-09-30"})
        # 种子账号/实体已被宿主播种的节点 → covered(FOCUS 头变
        # "verify addressable",避免模型重复播种)。
        seeded_names = ({u["username"] for u in db.get("users", [])}
                        | {m["username"] for m in db.get("members", [])}
                        | {o["name"] for o in db.get("organizations", [])}
                        | {r["name"] for r in db.get("repositories", [])}
                        | {t["name"] for t in db.get("teams", [])})
        if seeded_names:
            for nid, info in manifest["nodes"].items():
                blob = "\n".join(info["given"])
                if any(name in blob for name in seeded_names):
                    covered.setdefault(nid, []).append("seed-entities")

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
            # C(hackathon--github 47 节点):issue/PR/分支/标签/里程碑/
            # 评论是 REQ-4/5/6 全族的挂靠实体,starter 不给,模型就得
            # 每批自造数据模型——共享文件重写与回归震荡的又一根源。
            {"name": "branches", "label": "Branch", "fields": [
                {"name": "repoOrg", "label": "Repository owner",
                 "type": "string"},
                {"name": "repoName", "label": "Repository", "type": "string",
                 "required": True},
                {"name": "name", "label": "Name", "type": "string",
                 "required": True},
                {"name": "head", "label": "Head commit", "type": "string"},
                {"name": "protected", "label": "Protected", "type": "boolean"}]},
            {"name": "issues", "label": "Issue", "fields": [
                {"name": "repoOrg", "label": "Repository owner",
                 "type": "string"},
                {"name": "repoName", "label": "Repository", "type": "string",
                 "required": True},
                {"name": "number", "label": "Number", "type": "number",
                 "required": True},
                {"name": "title", "label": "Title", "type": "string",
                 "required": True},
                {"name": "description", "label": "Description",
                 "type": "string"},
                {"name": "author", "label": "Author", "type": "string"},
                {"name": "status", "label": "Status", "type": "string",
                 "enum": ["open", "closed"]},
                {"name": "labels", "label": "Labels", "type": "string"}]},
            {"name": "pulls", "label": "Pull request", "fields": [
                {"name": "repoOrg", "label": "Repository owner",
                 "type": "string"},
                {"name": "repoName", "label": "Repository", "type": "string",
                 "required": True},
                {"name": "number", "label": "Number", "type": "number",
                 "required": True},
                {"name": "title", "label": "Title", "type": "string",
                 "required": True},
                {"name": "author", "label": "Author", "type": "string"},
                {"name": "sourceBranch", "label": "Source branch",
                 "type": "string"},
                {"name": "targetBranch", "label": "Target branch",
                 "type": "string"},
                {"name": "status", "label": "Status", "type": "string",
                 "enum": ["open", "draft", "merged", "closed"]},
                {"name": "reviewers", "label": "Reviewers", "type": "string"}]},
            {"name": "labels", "label": "Label", "fields": [
                {"name": "repoOrg", "label": "Repository owner",
                 "type": "string"},
                {"name": "repoName", "label": "Repository", "type": "string"},
                {"name": "name", "label": "Name", "type": "string",
                 "required": True},
                {"name": "color", "label": "Color", "type": "string"}]},
            {"name": "milestones", "label": "Milestone", "fields": [
                {"name": "repoOrg", "label": "Repository owner",
                 "type": "string"},
                {"name": "repoName", "label": "Repository", "type": "string"},
                {"name": "title", "label": "Title", "type": "string",
                 "required": True},
                {"name": "dueOn", "label": "Due date", "type": "string"}]},
            {"name": "comments", "label": "Comment", "fields": [
                {"name": "parentType", "label": "Parent type",
                 "type": "string", "enum": ["issue", "pull"]},
                {"name": "repoName", "label": "Repository", "type": "string"},
                {"name": "parentNumber", "label": "Parent number",
                 "type": "number"},
                {"name": "author", "label": "Author", "type": "string"},
                {"name": "body", "label": "Body", "type": "string"},
                {"name": "createdAt", "label": "Created at",
                 "type": "string"}]},
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
            "arrange": arrange, "canonical": canonical_mode,
            "uncovered": uncovered, "logins": logins}


# ----------------------------------------- 图谱种子实体(hackathon 新句式)

_SEED_ACCOUNT_RE = re.compile(
    r"account\s+`([^`]+)`\s*,\s*email\s+`([^`]+)`\s*,\s*"
    r"password\s+`([^`]+)`", re.IGNORECASE)
_SEED_ENTITY_RES = [
    (re.compile(r"\borganization\s+`([^`]+)`", re.IGNORECASE),
     "organizations"),
    (re.compile(r"\brepository\s+`([^`]+)`", re.IGNORECASE),
     "repositories"),
    (re.compile(r"\bmember\s+`([^`]+)`", re.IGNORECASE), "members"),
    (re.compile(r"\bteams?\s+`([^`]+)`", re.IGNORECASE), "teams"),
    (re.compile(r"\bbranches\s+`([^`]+)`\s+and\s+`([^`]+)`", re.IGNORECASE),
     "branches"),
    (re.compile(r"\bbranch\s+`([^`]+)`", re.IGNORECASE), "branches"),
    (re.compile(r"\bissues?\s+`([^`]+)`", re.IGNORECASE), "issues"),
    (re.compile(r"\bpull requests?\s+`([^`]+)`(?:\s+and\s+`([^`]+)`)?",
                re.IGNORECASE), "pulls"),
    (re.compile(r"\blabels\s+`([^`]+)`\s+and\s+`([^`]+)`", re.IGNORECASE),
     "labels"),
    (re.compile(r"\blabel\s+`([^`]+)`", re.IGNORECASE), "labels"),
    (re.compile(r"\bmilestones?\s+`([^`]+)`", re.IGNORECASE), "milestones"),
    (re.compile(r"\bcommits?\s+`([^`]+)`", re.IGNORECASE), "commits"),
]


def parse_seeded_entities(texts: list[str]) -> dict:
    """hackathon 图谱种子实体(逐字提取,顺序稳定去重)。

    多捕获组(branch A and B)按序展开;同名单实体去重。
    """
    blob = "\n".join(texts)
    accounts: list[dict] = []
    seen_users: set[str] = set()
    for m in _SEED_ACCOUNT_RE.finditer(blob):
        username = m.group(1).strip()
        if username in seen_users:
            continue
        seen_users.add(username)
        accounts.append({"username": username, "email": m.group(2).strip(),
                         "password": m.group(3).strip()})
    keys = ("organizations", "repositories", "members", "teams", "branches",
            "issues", "pulls", "labels", "milestones", "commits")
    entities: dict[str, list[str]] = {k: [] for k in keys}
    for regex, key in _SEED_ENTITY_RES:
        for m in regex.finditer(blob):
            for g in m.groups():
                if not g:
                    continue
                name = g.strip()
                if name and name not in entities[key]:
                    entities[key].append(name)
    return {"accounts": accounts, **entities}


# ------------------------------------------------- extra_rules(稳定段)

def render_extra_rules(manifest: dict, stats: dict | None = None) -> str:
    """全局夹具契约文本:确定性渲染(同输入 → 逐字节相同,缓存律),≤2400。

    stats 键:kind / uncovered / arrange / seeded / canonical。
    按任务形态条件化——无种子账号不提密码,规范种子模式宣布
    "种子即终态",不留会误导 sheet 题命名/隔离策略的 graph 语汇。
    """
    stats = stats or {}
    e2e = manifest["e2e_envs"]
    e2e_str = ", ".join(e2e[:12]) + (f" (+{len(e2e) - 12} more)"
                                     if len(e2e) > 12 else "") if e2e \
        else "none named"
    uncovered_n = int(stats.get("uncovered", 0))
    arrange_n = int(stats.get("arrange", 0))
    seeded_n = int(stats.get("seeded", 0))
    is_canonical = bool(stats.get("canonical"))
    is_sheetish = stats.get("kind") in ("sheet", "mixed")
    parts = [
        "FIXTURE CONTRACT (graded, not demo data): the requirement's data: "
        "section and every scenario GIVEN line are the graded seed "
        "contract — names, roles, ownership, visibility, and relationships "
        "must match VERBATIM. The host "
        "seeded the machine-compilable part into backend/data/db.json: "
        "NEVER delete or rename seeded rows/collections; extend "
        "backend/data/schema.json so every seeded field is exposed.",
    ]
    if seeded_n:
        parts.append(
            f"{seeded_n} seeded account(s) from the requirement text are in "
            "the seed with their exact emails and scrypt-hashed passwords — "
            "auth must verify them from the seed; tests sign in with the "
            "plaintext passwords quoted in the GIVEN lines.")
    elif stats.get("kind") in ("graph", "mixed"):
        parts.append(
            f'Seeded fixture accounts share the password "'
            f'{FIXTURE_PASSWORD}" (username fixture-<role>, email '
            "fixture-<role>@example.test) — auth must verify them from the "
            "seed and sign-in pages must accept these credentials.")
    if is_canonical and is_sheetish:
        parts.append(
            "The seeded workbook is CANONICAL and FINAL: never rewrite it, "
            "never create another workbook with the same name, and never "
            "suffix seeded names (graded lookups match names exactly and "
            "uniquely). Scenario-specific cell states that conflict with "
            "the seed are arranged through the UI by the tests themselves — "
            "do not pre-seed them.")
    else:
        parts.append(
            "Scenarios that MODIFY data each need their OWN fresh "
            "preconfigured instance (never share one mutable fixture across "
            "scenarios; the graders restore state between runs).")
    parts.append(
        f"E2E_* harness fixtures named by the requirement: {e2e_str} — "
        "every one must map to a concrete seeded object you create.")
    if stats.get("kind") in ("graph", "mixed"):
        parts.append(
            "Files/commits are the single source of truth: branches store "
            "head references only, NEVER duplicate whole file trees per "
            "branch or fork.")
    if arrange_n:
        parts.append(
            f"{arrange_n} scenario pre-state(s) conflict with the canonical "
            "seed and are NOT seeded — the node's GIVEN lists them; the "
            "grader arranges them through the UI, your code must accept "
            "them like any other edit.")
    if uncovered_n:
        parts.append(
            f"{uncovered_n} scenario fixture(s) could NOT be auto-compiled — "
            "they appear per-node under GIVEN; hand-seed those exact "
            "pre-states before implementing the node.")
    parts.append(
        "Runtime state (backend/data/db.run.json) never ships; it resets "
        "to the seed on every server restart.")
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
