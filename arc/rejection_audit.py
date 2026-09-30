"""Rejection-literal citation audit — static, task-agnostic backstop.

09-30 sheet 法医:模型给「创建工作簿」发明了空名 400(需求无此句;该句
只属于 rename 节点 REQ-1-2-2),评分器逐字执行 description(没有填名
步骤),POST /api/workbooks {"name":""} → 400 连续 ~95 次杀穿几乎所有
场景。提示词层已有 CITE-BEFORE-REJECT 纪律(工单模板);本模块是与
模型行为无关的静态保险网:交付后端里每条拒绝文案,必须能在检查点
上方紧邻处(8 行内)找到引用注释,且被引句子真实存在于
requirements.yaml——「字符串在 yaml 某处出现过」不构成豁免(那正是
跨操作泄漏的形态),「同文件别处有引用」也不豁免别的检查点。

v1 范围:backend/src/**/*.js。评分器面对的否决闸门是 API;前端错误
标签本身不构成流级拒绝(disabled submit 属 FLOW FIDELITY,静态不可
判定),不进 v1。

红/info 分级(不设任务特设豁免表,词数即分级):
- red  : ≥4 词的文案无有效引用(完整句级校验规则,如
         "Workbook name cannot be empty")
- info : <4 词的无引用文案("Invalid state" / "Not found" 类防御性
         短语)——只入账,永不触发修复。

引用形态(与工单 CITE-BEFORE-REJECT 条款一致,单行注释):
  // REQ-1-2-2: "an empty name must be rejected with ..."
有效引用 = 被引句子(归一化后)存在于 requirements.yaml,且文案
(归一化后)是该句子的子串。

第二形态(需求要求拒绝但没引号给文案):注释尾标
  // REQ-3-2-1: "duplicate names display an error and no fork is
  // created" (message unspecified in text)
时豁免放宽为「被引句子存在于 requirements.yaml」(文案是模型自选
的合理消息,不要求是句子子串)。标注句不存在于 yaml 仍红;无标注
维持强规则——防止借标注形态给"需求根本没要求的拒绝"放行。

纯标准库;audit() 绝不抛出以外的副作用,调用方(main.py final 链)
负责 try/except 兜底。
"""

from __future__ import annotations

import re
from pathlib import Path

MIN_RED_WORDS = 4
_SCAN_ROOTS = ("backend/src",)
_SCAN_SUFFIXES = (".js", ".mjs", ".cjs")

# 引用注释: // REQ-1-2-2: "sentence" (可选尾标) (单引号同样收)
_LINE_COMMENT = re.compile(
    r"//\s*(REQ-[A-Za-z0-9._\-]+)\s*:\s*([\"'])(.+?)\2")
# 第二形态尾标:需求要求拒绝但没引号给文案 — 模型自选合理消息
_UNSPECIFIED_MARKER = "message unspecified"
# 引用与检查点的绑定窗口:引用必须出现在检查行上方 WINDOW 行内——
# 文件级生效会让 rename 处的一条引用豁免 create 路由的同文案(正是
# 要抓的跨操作泄漏)。
_CITATION_WINDOW = 8
# JS 字符串字面量('...' / "..." / `...`,容忍转义)
_STRING_LIT = re.compile(r"([\"'`])((?:\\.|(?!\1).)+?)\1")
# 拒绝路径锚点:U.err(res, '...') / U.bad(res, [...]) / errors:[{msg:...}]
# / 同行出现 4xx 状态码 + json/status/send
_P_REJECT_CALL = re.compile(r"\b(?:err|bad)\s*\(")
_P_ERRORS_KEY = re.compile(r"errors\s*:\s*\[")
_P_4XX = re.compile(r"\b4\d{2}\b")
_P_SENDISH = re.compile(r"\b(?:json|status|send)\b")
# 常量间接引用:U.err(res, MSG) → 同文件 const MSG = '...'
_P_IDENT_ARG = re.compile(
    r"\b(?:err|bad)\s*\(\s*[^,()]+\s*,\s*([A-Za-z_$][\w$]*)\s*\)")
_P_CONST_DEF = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    "([\"'])((?:\\\\.|(?!\\2).)+?)\\2")
_P_NODE_ID = re.compile(r"\s*- id:\s*(\S+)")


def _norm(s: str) -> str:
    s = (s.replace("\u201c", "\"").replace("\u201d", "\"")
          .replace("\u2018", "'").replace("\u2019", "'"))
    return re.sub(r"\s+", " ", s).strip().lower()


def _word_count(s: str) -> int:
    return len([w for w in re.split(r"\s+", s.strip()) if w])


def _citations(text: str) -> list[tuple[int, str, str, bool]]:
    """(lineno, REQ-id, sentence, message_unspecified) per citation."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _LINE_COMMENT.search(line)
        if m:
            out.append((lineno, m.group(1), m.group(3),
                        _UNSPECIFIED_MARKER in line.lower()))
    return out


def _valid_citation(literal: str, lineno: int,
                    cites: list[tuple[int, str, str, bool]],
                    req_norm: str) -> bool:
    """Exempt the check at `lineno` only if a citation sits within
    _CITATION_WINDOW lines ABOVE it and its quoted sentence exists in
    requirements.yaml. Strong form: the sentence contains the literal.
    Message-unspecified form (explicit marker): the requirement
    mandates the rejection without quoting a message, so the model's
    own clear message is accepted once the quoted sentence is real."""
    lit = _norm(literal)
    if not lit:
        return True
    for cit_line, _nid, sent, unspecified in cites:
        if not (cit_line <= lineno <= cit_line + _CITATION_WINDOW):
            continue
        s = _norm(sent)
        if not s or s not in req_norm:
            continue
        if lit in s or unspecified:
            return True
    return False


def _rejection_literals(text: str, consts: dict[str, str]
                        ) -> list[tuple[int, str]]:
    """(lineno, literal) for every string literal on a rejection path."""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        hit = (bool(_P_REJECT_CALL.search(line))
               or bool(_P_ERRORS_KEY.search(line))
               or bool(_P_4XX.search(line) and _P_SENDISH.search(line)))
        if not hit:
            continue
        lits = [m[1] for m in _STRING_LIT.findall(line)]
        if lits:
            out.extend((lineno, lit) for lit in lits)
            continue
        m = _P_IDENT_ARG.search(line)
        if m and m.group(1) in consts:
            out.append((lineno, consts[m.group(1)]))
    return out


def _yaml_nodes(requirements_text: str, literal: str,
                limit: int = 4) -> list[str]:
    """Requirement node ids whose (normalized) text contains the literal.

    归属判定 = 归一化流里「匹配位置之前最近的 - id:」——yaml 的叶子
    ATOMIC 节点 id 先于其 description/scenarios 出现,最近者即所属。
    """
    lit = _norm(literal)
    if not lit:
        return []
    ids: list[tuple[int, str]] = []
    buf: list[str] = []
    pos = 0
    for line in requirements_text.splitlines():
        m = _P_NODE_ID.match(line)
        w = _norm(line)
        if m:
            ids.append((pos, m.group(1)))
        buf.append(w)
        pos += len(w) + 1
    hay = " ".join(buf)
    out: list[str] = []
    i = 0
    while True:
        j = hay.find(lit, i)
        if j < 0:
            break
        owner = None
        for start, nid in ids:
            if start <= j:
                owner = nid
            else:
                break
        if owner and owner not in out:
            out.append(owner)
        i = j + max(1, len(lit))
    return out[:limit]


def audit(workspace: Path, requirements_text: str) -> dict:
    """Scan backend rejection literals; every one needs an in-file
    citation whose sentence exists in requirements.yaml and contains
    the literal. Returns a json-ready dict; never raises on file-level
    problems (unreadable files are skipped)."""
    req_norm = _norm(requirements_text)
    findings: list[dict] = []
    scanned = 0
    for root in _SCAN_ROOTS:
        base = workspace / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (path.suffix not in _SCAN_SUFFIXES or not path.is_file()
                    or "node_modules" in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            cites = _citations(text)
            consts = {m[0]: m[2] for m in _P_CONST_DEF.findall(text)}
            rel = path.relative_to(workspace).as_posix()
            for lineno, literal in _rejection_literals(text, consts):
                if _valid_citation(literal, lineno, cites, req_norm):
                    continue
                words = _word_count(literal)
                findings.append({
                    "file": rel,
                    "line": lineno,
                    "literal": literal,
                    "words": words,
                    "kind": "red" if words >= MIN_RED_WORDS else "info",
                    "yaml_nodes": _yaml_nodes(requirements_text, literal),
                })
    reds = [f for f in findings if f["kind"] == "red"]
    return {
        "red_count": len(reds),
        "info_count": len(findings) - len(reds),
        "scanned_files": scanned,
        "findings": findings,
    }


def audit_prompt_block(result: dict, limit: int = 8) -> str:
    """Render audit findings as a final-turn repair instruction block.

    只在 red_count > 0 时返回非空串;文案在 yaml 里找得到的一并给出
    归属节点——模型据此判断「补引用」还是「这本是别的操作的规则,
    改成默认成功」。
    """
    reds = [f for f in result.get("findings", []) if f["kind"] == "red"]
    if not reds:
        return ""
    lines = [
        "\nREJECTION AUDIT (ground truth, static): these backend "
        "rejection messages have no requirement citation at their "
        "check. For each: if the requirement text states THIS "
        "operation's rejection, copy that exact sentence into a "
        "`// REQ-<id>: \"<sentence>\"` comment beside the check; if the "
        "text mandates the rejection but quotes no exact message, cite "
        "the mandating sentence and end the comment with "
        "\"(message unspecified in text)\"; if no sentence mandates "
        "this rejection at all, the operation must SUCCEED with a "
        "neutral default (an auto-generated name, a blank field) — an "
        "uncited rejection breaks every scenario "
        "that walks the flow the text describes."
    ]
    for f in reds[:limit]:
        node_note = ""
        if f.get("yaml_nodes"):
            node_note = (f" (text found under: {', '.join(f['yaml_nodes'])}"
                         " — if that node governs a DIFFERENT operation, "
                         "this check must not reject)")
        lines.append(f"  {f['file']}:{f['line']} \"{f['literal']}\"{node_note}")
    if len(reds) > limit:
        lines.append(f"  ... and {len(reds) - limit} more")
    return "\n".join(lines) + "\n"
