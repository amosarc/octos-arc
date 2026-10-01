"""夹具契约提取器 + 宿主种子编译器(纯标准库,单测覆盖)。

设计动机(2026-09-24 github 2分/sheet 5分复盘):需求的 ROOT `data:` 段、
每条场景 GIVEN 步骤与内联 "Seed data supplies..." 句子就是评分夹具契约,
此前全链无人消费——提示词只说 "demo data",模型自编种子导致夹具错配。
本模块宿主尽算三件事:

1. extract(tree): 契约清单结构化(data: 条目 / E2E_* 环境项 / 每节点 GIVEN
   与种子句),落盘 .arc/fixture-manifest.json 供对表;
2. compile_seed(manifest): 把需求逐字点名的部分转录成 db.json 行集
   (sheet 型: "A1/B1 are Item/Amount" → cells 字典,每场景一个独立工作簿,
   天然满足隔离契约;graph 型: 逐字账号/组织/仓库/团队/附属行,密码取
   需求引号明文,scrypt 与 backend store.verify 逐位兼容);
3. render_extra_rules(manifest, stats): ≤2900 字符、确定性(字节稳定,
   满足缓存律)的全局契约文本,经 envelope.extra_rules 渲染进稳定 CONTRACT 段。

边界定则(09-28):宿主只转录逐字契约、只验证模型声明——需求没点名的
账号/实体(尤其角色账号)一律由模型按需求文本自行推理决定,宿主不做
任何合成兜底(写死的名字/密码与平台真值必然错位,github 真跑 24 个
角色账号登录全 400 即教训)。解析不了的条目进 uncovered 报告降级为
清单注入,绝不自编替代品。
"""

from __future__ import annotations

import hashlib
import re

E2E_RE = re.compile(r"E2E_[A-Z0-9_]+")
SEED_SENTENCE_RE = re.compile(
    r"[^.]*[Ss]eed data (?:supplies|provides)[^.]*\.")
COORD_RE = re.compile(r"\b([A-Z]{1,3}[1-9][0-9]{0,3})\b")
# 季度/编号语境的 "Q3"(后随非数字,如 "Q3 Sales")与真实单元格坐标同形——
# 统计与解析前一律剔除(hackathon--github 曾因此 32 处裸 Q3 全部误计为
# 坐标,kind 判定被带偏成 sheet、种子账号零编译)。
_QUARTER_TOKEN_RE = re.compile(r"\b[Qq][1-4]\b(?!\d)")

# 上限只做失控保护,不做内容裁剪预算:github 全量 FIXTURE 段 ~2930 字符,
# 2900 曾恰好把尾句腰斩("scratch test scripts belong in /tmp, never
# inside .arc/acceptance." 断半)——系统行常驻文本必须整句收尾。
# 改文本后跑 goal_first_test 的不截断钉子确认仍在余量内。
_MAX_EXTRA_RULES = 3200


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
                          "seed_sentences": seeds, "description": desc}
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


# ------------------------------------------------ 角色词(graph 型分类用)

# 角色词仅用于 kind 分类(graph/sheet/none),不再驱动任何合成播种:
# 账号该建哪些、叫什么、归谁——由模型按需求文本自行推理决定
# (09-28 定则:宿主写死的猜测(fixture-<role>/统一密码)与平台真值
# 必然错位,合成底座整体退役)。裸 "write"/"admin"/"reviewer" 仍要求
# 权限语境短语,避免把任意任务误判成 graph。
_ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"organization owner|org owner", "org-owner"),
    (r"organization member|org member", "org-member"),
    (r"repository read|\bread\b role|role of read", "repo-read"),
    (r"\btriage\b\s*(access|role|permission)", "triage"),
    (r"\bwrite\b\s*(access|role|permission)", "write"),
    (r"\bmaintain(?:er)?\b\s*(access|role|permission)", "maintain"),
    (r"\badmin\b\s*(access|role|permission)|administrator role", "admin"),
    (r"pull-request author|pr author", "pr-author"),
    (r"non-author reviewer|reviewer role", "reviewer"),
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
    # 去重再计数:同一坐标反复提及("A1"×10)不构成 sheet 证据,≥4 个
    # 不同坐标才是。
    cellish = len(set(COORD_RE.findall(given_blob)))
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

    返回 {"kind", "db": {collection: [rows]},
          "covered": {nid: [scenario 名]}, "uncovered": [...],
          "arrange": {nid: [scenario 名]}, "logins": [...]}
    covered 的场景种子已就位;arrange 的场景预置态与规范种子冲突
    (测试经 UI 自行布置,宿主不播种);uncovered 一律报告、不自编。
    sheet 型同名工作簿 ≥2 次被引用时合并为单一规范工作簿(评分断言
    的 `Q3 Sales` 必须唯一可见),否则保留按场景独立实例的 legacy 行为。
    09-28 CRUD 生成器退役:schema starter(graph 13 实体块/sheet
    workbooks)随生成器整体删除——数据模型是宿主编造的内容决策
    (C 类),其唯一消费方 schema.json→engine 链已不存在。
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
        blob = "\n".join(global_blob + seed_blob).lower()
        # hackathon 新句式种子实体:逐字账号/组织/仓库/成员/团队
        # ("The seeded data is account `alice-dev`, email `a@x.test`,
        # password `P!`" / "seeded data includes the existing organization
        # `Acme Demo`, repository `acme-docs`, member `bob-reviewer`,
        # team `frontend-team`")。密码经 scrypt 落库,登录按明文约定。
        seeded = parse_seeded_entities(
            global_blob + [s for info in manifest["nodes"].values()
                           for s in info.get("seed_sentences", [])]
            + [g for info in manifest["nodes"].values()
               for g in info["given"]],
            attr_texts=[info.get("description", "")
                        for info in manifest["nodes"].values()])
        # 宿主只转录需求逐字点名的实体;角色账号/org/repo/team 的一切
        # 合成兜底已退役(09-28 定则:宿主写死的猜测与平台真值必然错位,
        # github 真跑 24 个角色账号全 400 即教训)。需求没点名的内容
        # 由模型按 GIVEN/角色措辞自行推理建种。
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
        # 归属保真:逐字实体只挂逐字 org(需求原文 "repository `acme-docs`
        # ... organization `Acme Demo`")。没有逐字 org 时 org 依赖的
        # 实体(repo/team/member/附属)一律不落——半接线行是宿主编造,
        # 留给模型按 GIVEN 自建。
        literal_orgs = [o["name"] for o in org_rows
                        if str(o.get("id", "")).startswith("o-seed-")]
        primary_org = literal_orgs[0] if literal_orgs else ""
        repo_rows = db.setdefault("repositories", [])
        if primary_org:
            for repo in seeded["repositories"]:
                if any(r["name"] == repo for r in repo_rows):
                    continue
                repo_rows.append({
                    "id": "r-seed-" + hashlib.sha1(repo.encode()).hexdigest()[:8],
                    "orgName": primary_org, "name": repo,
                    "description": f"Seeded repository {repo}",
                    # 可见性保真:"private repository `secret-research`" 按句内
                    # 修饰语落 private(旧实现硬编码 public,搜索/可见性测试
                    # 全靠模型自己救)。
                    "visibility": seeded["repo_visibility"].get(repo, "public"),
                    "defaultBranch": "main",
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
            # owner 保真:"public repository `acme-docs`, private repository
            # `secret-research`, owner `alice-dev`" → owner 账号是该 org 的
            # owner 角色成员(与 REQ-2-1-2 "创建组织者即 owner" 语义一致)。
            for owner in seeded["owners"]:
                if any(m["username"] == owner and m["orgName"] == primary_org
                       for m in member_rows):
                    continue
                member_rows.append({
                    "id": "m-seed-owner-" + hashlib.sha1(
                        owner.encode()).hexdigest()[:8],
                    "orgName": primary_org, "username": owner, "role": "owner"})
        # C:仓库附属种子(分支/issue/PR/标签/里程碑/提交)挂到首个仓库,
        # main 分支是需求点名的默认分支。仓库行本就全为逐字种子;
        # 没有仓库行就不落附属种子——半接线行留给模型按 GIVEN 自建。
        literal_repos = [r["name"] for r in repo_rows
                         if str(r.get("id", "")).startswith("r-seed-")]
        primary_repo = literal_repos[0] if literal_repos else ""
        if primary_repo:
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
            issue_rows = db.setdefault("issues", [])
            seed_login = next((l["username"] for l in logins
                               if l.get("role") == "seed"),
                              logins[0]["username"] if logins else "seed")
            seed_label_str = ", ".join(seeded["labels"])
            all_issue_titles = list(seeded["issues"]) + list(
                seeded["closed_issue_titles"])
            # closed 伴随 issue:REQ-5-1-1 点名 "The seeded closed issue
            # title is `Legacy welcome text`" ——列表过滤场景需要一开一闭
            # 两条种子。
            issue_status = {t: "closed" for t in seeded["closed_issue_titles"]}
            for title in all_issue_titles:
                if any(x["title"] == title for x in issue_rows):
                    continue
                issue_rows.append({
                    "id": "i-seed-" + hashlib.sha1(
                        f"{primary_repo}:{title}".encode()).hexdigest()[:8],
                    "repoOrg": primary_org, "repoName": primary_repo,
                    "number": len(issue_rows) + 1, "title": title,
                    # 描述保真:"its description is `Describe the onboarding
                    # improvement.`"(旧实现恒空串)。
                    "description": seeded["issue_descriptions"].get(title, ""),
                    "author": seed_login,
                    "status": issue_status.get(title, "open"),
                    # 标签压平(有意保留,非逐 issue 归属):解析器没有
                    # "issue X labels a and b" 的归属正则,全局标签集进
                    # 每一行。github 需求断言 "Closed issue with the same
                    # label",超集兼容;逐 issue 归属待真值需要时加正则。
                    "labels": seed_label_str})
            pull_rows = db.setdefault("pulls", [])
            # PR 状态约定:"one Open PR created by `alice` and one Closed
            # PR created by `alice`" → 列表序首开次闭(需求未逐题点名
            # 标题↔状态映射时的合成约定,非逐字契约);作者短名解析到
            # 逐字账号。
            pr_states = seeded["pr_states"]
            reviewers_str = ", ".join(seeded["reviewers"])
            for i, title in enumerate(seeded["pulls"], 1):
                if any(x["title"] == title for x in pull_rows):
                    continue
                status = "open"
                author = seed_login
                if pr_states:
                    if i == 1:
                        author = _resolve_seed_username(
                            pr_states["open_author"], seeded["accounts"])
                    elif i >= 2:
                        status = "closed"
                        author = _resolve_seed_username(
                            pr_states["closed_author"], seeded["accounts"])
                pull_rows.append({
                    "id": "p-seed-" + hashlib.sha1(
                        f"{primary_repo}:{title}".encode()).hexdigest()[:8],
                    "repoOrg": primary_org, "repoName": primary_repo,
                    "number": i, "title": title, "author": author,
                    "sourceBranch": "feature-search", "targetBranch": "main",
                    "status": status, "reviewers": reviewers_str})
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
            # 评论种子:REQ-5-1-2 "at least one comment" / REQ-6-3-1 "one
            # discussion comment" ——旧实现完全无评论,模型每题自造。作者
            # 取首个 reviewer/member,正文为合成占位(需求未给逐字文案,
            # extra_rules 已按填充行口径声明)。
            comment_author = (seeded["reviewers"] or seeded["members"]
                              or [seed_login])[0]
            comment_rows = db.setdefault("comments", [])
            if issue_rows and not comment_rows:
                comment_rows.append({
                    "id": "cm-seed-issue-" + hashlib.sha1(
                        primary_repo.encode()).hexdigest()[:8],
                    "parentType": "issue", "repoName": primary_repo,
                    "parentNumber": issue_rows[0]["number"],
                    "author": comment_author,
                    "body": "I can take a look at this.",
                    "createdAt": "2026-01-02T00:00:00.000Z"})
            open_pulls = [p for p in pull_rows if p["status"] == "open"]
            if open_pulls and len(comment_rows) < 2:
                comment_rows.append({
                    "id": "cm-seed-pull-" + hashlib.sha1(
                        primary_repo.encode()).hexdigest()[:8],
                    "parentType": "pull", "repoName": primary_repo,
                    "parentNumber": open_pulls[0]["number"],
                    "author": comment_author,
                    "body": "Seed discussion comment.",
                    "createdAt": "2026-01-02T00:00:00.000Z"})
            # 提交种子:旧实现提取了 commit 名却静默丢弃(schema 也没有
            # commits 实体)——REQ-4-2 的历史/对比页全靠模型每批自造。
            commit_rows = db.setdefault("commits", [])
            for message in seeded["commits"]:
                if any(c["message"] == message for c in commit_rows):
                    continue
                commit_rows.append({
                    "id": "c-seed-" + hashlib.sha1(
                        f"{primary_repo}:{message}".encode()).hexdigest()[:8],
                    "repoOrg": primary_org, "repoName": primary_repo,
                    "sha": hashlib.sha1(
                        f"{primary_repo}:{message}".encode()).hexdigest(),
                    "branch": "main", "message": message,
                    "author": seed_login,
                    "createdAt": "2026-01-02T00:00:00.000Z"})
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

    # 空集合不落键:setdefault 留下的空 list 会变成 db.json 空集合与
    # 对空集合的实体探针,纯噪音。
    db = {k: v for k, v in db.items() if v}
    return {"kind": kind, "db": db, "covered": covered,
            "arrange": arrange, "canonical": canonical_mode,
            "uncovered": uncovered, "logins": logins}


# ----------------------------------------- 图谱种子实体(hackathon 新句式)

_SEED_ACCOUNT_RE = re.compile(
    r"account\s+`([^`]+)`\s*,\s*email\s+`([^`]+)`\s*,\s*"
    r"password\s+`([^`]+)`", re.IGNORECASE)
# 属性级提取(hg-github-001 复盘):需求原文在同一句里给实体带属性——
# "private repository `secret-research`" / "owner `alice-dev`" /
# "one Open PR created by `alice` and one Closed PR created by `alice`"。
# 只抓名字的旧正则会把这些修饰语丢掉,模型得花自己的 turn 修种子。
_SEED_REPO_VISIBILITY_RE = re.compile(
    r"\b(public|private)\s+repository\s+`([^`]+)`", re.IGNORECASE)
_SEED_OWNER_RE = re.compile(r"\bowner\s+`([^`]+)`", re.IGNORECASE)
_SEED_REVIEWER_RE = re.compile(r"\breviewer\s+`([^`]+)`", re.IGNORECASE)
_SEED_CLOSED_ISSUE_RE = re.compile(
    r"closed issue title is `([^`]+)`", re.IGNORECASE)
_SEED_ISSUE_DESC_RE = re.compile(
    r"issue title is `([^`]+)`\s+and\s+its\s+description\s+is\s+`([^`]+)`",
    re.IGNORECASE)
_SEED_PR_STATES_RE = re.compile(
    r"one\s+Open\s+PR\s+created\s+by\s+`([^`]+)`\s+and\s+one\s+Closed\s+PR"
    r"\s+created\s+by\s+`([^`]+)`", re.IGNORECASE)
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


def parse_seeded_entities(texts: list[str],
                          attr_texts: list[str] | None = None) -> dict:
    """hackathon 图谱种子实体(逐字提取,顺序稳定去重)。

    多捕获组(branch A and B)按序展开;同名单实体去重。属性级扩展:
    repo_visibility/owners/reviewers/closed_issue_titles/
    issue_descriptions/pr_states 与名字表并列返回。

    `attr_texts`(节点 description 全文)只喂属性正则——"The seeded open
    issue title is `X` and its description is `Y`" / "The seeded closed
    issue title is `Z`" 这类句式只出现在描述字段;名字正则维持原输入,
    旧任务的实体集合不受描述文本影响。
    """
    blob = "\n".join(texts)
    attr_blob = "\n".join(list(texts) + list(attr_texts or []))
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

    def _dedup(names) -> list[str]:
        out: list[str] = []
        for n in names:
            n = n.strip()
            if n and n not in out:
                out.append(n)
        return out

    repo_visibility: dict[str, str] = {}
    for m in _SEED_REPO_VISIBILITY_RE.finditer(attr_blob):
        repo_visibility[m.group(2).strip()] = m.group(1).lower()
    m = _SEED_PR_STATES_RE.search(attr_blob)
    pr_states = ({"open_author": m.group(1).strip(),
                  "closed_author": m.group(2).strip()} if m else None)
    return {
        "accounts": accounts, **entities,
        "repo_visibility": repo_visibility,
        "owners": _dedup(
            m.group(1) for m in _SEED_OWNER_RE.finditer(attr_blob)),
        "reviewers": _dedup(
            m.group(1) for m in _SEED_REVIEWER_RE.finditer(attr_blob)),
        "closed_issue_titles": _dedup(
            m.group(1) for m in _SEED_CLOSED_ISSUE_RE.finditer(attr_blob)),
        "issue_descriptions": {
            m.group(1).strip(): m.group(2).strip()
            for m in _SEED_ISSUE_DESC_RE.finditer(attr_blob)},
        "pr_states": pr_states,
    }


def _resolve_seed_username(name: str, accounts: list[dict]) -> str:
    """需求句里的短名解析到逐字账号:精确匹配优先,前缀兜底。

    "one Open PR created by `alice`" → 账号 `alice-dev`(需求全文只有
    alice-dev 一个逐字账号);解析不到时原样返回(如 bob-reviewer 本身
    就是全名)。
    """
    for acct in accounts:
        if acct["username"] == name:
            return name
    for acct in accounts:
        if acct["username"].startswith(name):
            return acct["username"]
    return name


# ------------------------------------------------- extra_rules(稳定段)

def render_extra_rules(manifest: dict, stats: dict | None = None) -> str:
    """全局夹具契约文本:确定性渲染(同输入 → 逐字节相同,缓存律),≤2900。

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
        "FIXTURE CONTRACT (graded, not demo data): every scenario GIVEN line "
        "— plus a data: section when the requirement has one — is the "
        "graded seed contract: names, roles, ownership, visibility, and "
        "relationships must match VERBATIM.",
    ]
    if stats.get("kind") in ("graph", "mixed"):
        parts.append(
            "Files/commits are the single source of truth: branches store "
            "head references only.")
    parts.append(
        f"E2E_* variables the requirement names: {e2e_str}. The harness "
        "itself passes only base-URL variables — any other fixture the "
        "scenarios imply is yours to create.")
    parts.append(
        "Runtime state (backend/data/db.run.json) never ships — the "
        "delivered workspace carries only the seed db.json, so grading "
        "starts from the pristine seed; within a workspace, "
        "writes persist across server restarts. Your own past turns and "
        "host state live under the .arc trace area; "
        "scratch test scripts belong in /tmp, never inside "
        ".arc/acceptance.")
    text = " ".join(p for p in parts if p)
    return text[:_MAX_EXTRA_RULES]


def task_rules(manifest: dict, stats: dict | None = None) -> list[str]:
    """任务层贡献的规则行(pi buildRules 贡献源之一)。

    与 plan_envelope.CHARTER_RULES 汇入同一 build_rules 去重合并——
    任务规则与全局法则冲突时,谁后到谁坍缩是不对的:这里只放任务特有
    义务,全局法则由 CHARTER_RULES 承载。
    """
    stats = stats or {}
    e2e = []
    seeded_n = int(stats.get("seeded", 0))
    arrange_n = int(stats.get("arrange", 0))
    uncovered_n = int(stats.get("uncovered", 0))
    is_canonical = bool(stats.get("canonical"))
    is_sheetish = stats.get("kind") in ("sheet", "mixed")
    kind = stats.get("kind")
    rules: list[str] = []
    # 09-30 深夜:账号/actor/声明三条义务迁 BOOTSTRAP_PREAMBLE(首 turn
    # 载体,用户拍板"这些是 turn 应该关注的,不进 AGENTS.md")——系统行
    # 只留字节稳定的全局法则与架构事实。
    if kind in ("graph", "mixed") or seeded_n:
        rules.append(
            "Seed rows named by the requirement are verbatim contract; "
            "rows it does not name are yours to fill so flows have data "
            "to render — wherever filler can disagree with the requirement "
            "text, the requirement text wins.")
    if is_canonical and is_sheetish:
        rules.append(
            "The workbook you seed from the data:/GIVEN contract is "
            "CANONICAL and FINAL: implement it carefully in the bootstrap "
            "turn — correcting your own seed while wiring the API is fine, "
            "but once family specs depend on it, treat it as frozen: never "
            "rewrite it, never create another workbook with the same name, "
            "and never suffix seeded names; scenario-specific cell states "
            "that conflict with the seed are arranged through the UI by "
            "the tests — do not pre-seed them.")
    else:
        rules.append(
            "Scenarios that MODIFY data each need their OWN fresh "
            "preconfigured instance — never share one mutable fixture "
            "across scenarios; the graders restore state between runs.")
    if arrange_n:
        rules.append(
            f"{arrange_n} scenario pre-state(s) conflict with the seed and "
            "are NOT seeded — the node's GIVEN lists them; arrange them "
            "through the UI, your code must accept them like any other "
            "edit.")
    if uncovered_n:
        rules.append(
            f"{uncovered_n} scenario fixture(s) could NOT be auto-extracted "
            "— hand-seed those exact pre-states per the node's GIVEN.")
    if kind in ("graph", "mixed"):
        rules.append(
            "Branches store head references only — NEVER duplicate whole "
            "file trees per branch or fork.")
    return rules


# ------------------------------------------------ 落盘/读取(宿主 IO 面最小)

# 09-30 定则:materialize(宿主种子落盘)整体退役——db.json 由模型按
# 需求的 data: 段与 GIVEN 行自己实现(配合 GIVEN 原样进上下文),宿主
# 只保留 compile_seed 纯函数供探针/完整性基线/统计(机械需求推导=
# 治理,非内容决策)。
