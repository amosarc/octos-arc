"""Rejection-literal membership audit — static, task-agnostic backstop.

09-30 sheet 法医:模型给「创建工作簿」发明了空名 400(需求无此句;该句
只属于 rename 节点 REQ-1-2-2),评分器逐字执行 description(没有填名
步骤),POST /api/workbooks {"name":""} → 400 连续 ~95 次杀穿几乎所有
场景。本模块是与模型行为无关的静态保险网:交付后端里每条拒绝文案,
归一化后必须逐字出现在 requirements.yaml——查无此文 = 编造嫌疑。

09-30 深夜改版(用户拍板 code style 无注释,注释锚退役):接地判定从
「检查点 8 行内的引用注释」改为「文案 ⊂ 需求原文」——模型实际写的
消息本身就是证据,不再需要注释式记账。代价(如实记档):跨操作泄漏
(文案在 yaml 里但属于别的操作的规则)静态上不可见——它对完全合规
写引用的旧模型同样不可见;防线=总纲法则("only where the text states
it, quoted message verbatim")+ final 轮对照原文重查。

v1 范围:backend/src/**/*.js。评分器面对的否决闸门是 API;前端错误
标签本身不构成流级拒绝(disabled submit 属 FLOW FIDELITY,静态不可
判定),不进 v1。

红/info 分级(不设任务特设豁免表,词数即分级):
- red  : ≥4 词的文案在需求原文中查无(完整句级校验规则,如
         "Some invented validation message")
- info : <4 词的查无文案("Invalid state" / "Not found" 类防御性
         短语)——只入账,永不触发修复。需求要求拒绝但没给文案时
         模型自选的短消息可能落 info——final 轮指令让模型对照原文
         自查,不强制改码。

纯标准库;audit() 绝不抛出以外的副作用,调用方(main.py final 链)
负责 try/except 兜底。
"""

from __future__ import annotations

import re
from pathlib import Path

MIN_RED_WORDS = 4
_SCAN_ROOTS = ("backend/src",)
_SCAN_SUFFIXES = (".js", ".mjs", ".cjs")

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


def _norm(s: str) -> str:
    s = (s.replace("\u201c", "\"").replace("\u201d", "\"")
          .replace("\u2018", "'").replace("\u2019", "'"))
    return re.sub(r"\s+", " ", s).strip().lower()


def _word_count(s: str) -> int:
    return len([w for w in re.split(r"\s+", s.strip()) if w])


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


def audit(workspace: Path, requirements_text: str) -> dict:
    """Scan backend rejection literals; every one must appear verbatim
    (normalized) in the requirement text — membership IS the grounding,
    no citation comments (code style forbids them). Returns a json-ready
    dict; never raises on file-level problems (unreadable files are
    skipped)."""
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
            consts = {m[0]: m[2] for m in _P_CONST_DEF.findall(text)}
            rel = path.relative_to(workspace).as_posix()
            for lineno, literal in _rejection_literals(text, consts):
                if _norm(literal) in req_norm:
                    continue
                words = _word_count(literal)
                findings.append({
                    "file": rel,
                    "line": lineno,
                    "literal": literal,
                    "words": words,
                    "kind": "red" if words >= MIN_RED_WORDS else "info",
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

    只在 red_count > 0 时返回非空串。findings=需求原文查无的文案;
    修复语义是对照原文自查(保留有据的,拆掉无据的),不再有注释
    载体。
    """
    reds = [f for f in result.get("findings", []) if f["kind"] == "red"]
    if not reds:
        return ""
    lines = [
        "\nREJECTION AUDIT (ground truth, static): these backend "
        "rejection messages appear nowhere in the requirement text. For "
        "each: if the text mandates THIS operation's rejection, keep it "
        "and use the text's quoted message verbatim when it quotes one "
        "(that finding needs no code change); if the text does not "
        "mandate it, the operation must SUCCEED with a neutral default "
        "(an auto-generated name, a blank field) — an invented "
        "rejection breaks every scenario that walks the flow the text "
        "describes."
    ]
    for f in reds[:limit]:
        lines.append(f"  {f['file']}:{f['line']} \"{f['literal']}\"")
    if len(reds) > limit:
        lines.append(f"  ... and {len(reds) - limit} more")
    return "\n".join(lines) + "\n"
