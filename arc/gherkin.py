"""requirements yaml 场景 → Gherkin 渲染(确定性文本清洗,无语义发明)。

09-30 定则:模型读到的验收信号从"缩进 GIVEN/WHEN/THEN 原文"升级为
Gherkin(Feature/Scenario/Given/When/Then)。清洗规则全部机械可审计:

  1. 空白折叠 → 按句切分(句末标点 + 后随大写/引号);
  2. 丢弃 "Seed values: …" 句(种子数据的字面重复);
  3. 剔除占位短语子句:"the requested workflow" 连同其尾随噪声 token
     (与占位检测器同一形态)——sheet 类模板垃圾的确定性清洗;以空格
     替换保词边界,残渣不与真词黏连;
  4. 清洗后为空的步骤 → 回退原文并标注 # uncleanable(不发明)。

场景名同规则清洗,坍缩到只剩 id 时只留 id。`_steps_templated` 场景只
渲染清洗后 GIVEN + 注释行(WHEN/THEN 是平台占位样板,REQ CONTENT 是
流程契约——与 data-only 注记同 philosophy)。

纯标准库;同输入 → 逐字节相同(缓存律)。
"""

from __future__ import annotations

import re

_KEYWORDS = {"GIVEN": "Given", "WHEN": "When", "THEN": "Then"}

# 占位短语(含尾随噪声 token,如 "the requested workflow sheet1")及其
# 前导连接词——循环匹配以吞掉连续串。
_PLACEHOLDER_RE = re.compile(
    r"(?:,\s*|\b(?:and|with)\s+)?(?:the\s+requested\s+workflow"
    r"(?:\s+[a-z0-9]+)?\s*)+",
    re.IGNORECASE)
_SEED_VALUES_RE = re.compile(r"^seed values\s*:", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"‘`])")
_EMPTY_QUOTE_RE = re.compile(r"(\"\"|'')")
# 引用名:“…”(github)与 "…"(sheet)——反引号是种子值不在此列;
# 单引号 '…' 有撇号陷阱("the user's own 'X'" 跨词配对)也不取。
_QUOTED_NAME_RE = re.compile(r"“([^”]+)”|\"([^\"]+)\"")
# TURN 打标支的占位注记(与 render_scenarios 的注记同哲学;TURN 里
# 没有 REQ CONTENT 段名,指向上方 Flow contract 注释行)。
_BOILER_NOTE = ("# WHEN/THEN are platform placeholder boilerplate "
                "— the flow contract above governs.")


def _fold(text: str) -> str:
    return " ".join(str(text or "").split())


def clean_text(text: str) -> str:
    """确定性清洗:折叠空白 → 剔占位短语串(以空格替换保词边界)
    → 去空引号对 → 标点坍缩。"""
    t = _fold(text)
    t = _PLACEHOLDER_RE.sub(" ", t)
    t = _EMPTY_QUOTE_RE.sub("", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;!?])", r"\1", t)
    t = re.sub(r",\s*,", ", ", t)
    t = re.sub(r"\(\s*\)", "", t)
    return t.strip(" ,;-")


def clean_name(name: str) -> str:
    """场景名清洗:同规则,坍缩到只剩 id 时只留 id。"""
    t = clean_text(name)
    t = re.sub(r"\s*-\s*$", "", t).strip()
    return t or _fold(name)


def quoted_names(text: str, limit: int = 12) -> list[str]:
    """需求原文引用名提取(依赖块 REUSED UI CONTRACT 的数据源)。

    从节点 name+description 提取 “…”/"…" 引用的控件/文案名——引用
    名即精确 accessible name(总纲 LAW 1),宿主只做确定性摘录,零
    语义发明。顺序保序去重,每名空白折叠,limit 封顶。
    """
    out: list[str] = []
    for m in _QUOTED_NAME_RE.finditer(str(text or "")):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        name = _fold(raw)
        if name and name not in out:
            out.append(name)
            if len(out) >= limit:
                break
    return out


def seed_sentences(nodes: list[dict], limit: int = 16
                   ) -> tuple[list[str], int]:
    """批+依赖节点的种子契约句(逐字,词汇无关)。

    从 GIVEN 步骤与节点描述原文按句提取含 "seed" 的整句——播种类句式
    ("The seeded data is …"/"The evaluation seed contains …"/"Seed data
    comprises …")自动覆盖,新任务词汇零正则(09-30 用户拍板:逐实体
    正则不具泛化能力,整句照抄不丢任何词汇)。"Seed values:" 字面重复
    句由 clean_sentences 的既有清洗律丢弃,占位短语照常剥离。顺序保序
    (调用方给序:批节点先、依赖按闭包序),同句去重;limit 封顶,返回
    (句列表, 截断数)。
    """
    out: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for n in nodes or []:
        texts = [str(n.get("description") or "")]
        for sc in n.get("scenarios") or []:
            for step in (sc.get("steps") or []):
                if isinstance(step, dict) and \
                        str(step.get("keyword", "")).upper() == "GIVEN":
                    texts.append(str(step.get("content", "")))
        for text in texts:
            sents, _ok = clean_sentences(text)
            for s in sents:
                key = s.lower()
                if "seed" not in key or key in seen:
                    continue
                if len(out) >= limit:
                    dropped += 1
                    continue
                seen.add(key)
                out.append(s)
    return out, dropped


def clean_sentences(content: str) -> tuple[list[str], bool]:
    """步骤文本 → 清洗后句子列表;(_, False) = 不可清洗,须回退原文。"""
    t = clean_text(content)
    if not t:
        return [_fold(content)], False
    sents = [s.strip() for s in _SENTENCE_SPLIT_RE.split(t) if s.strip()]
    if not sents:
        return [_fold(content)], False
    kept = [s for s in sents if not _SEED_VALUES_RE.match(s)]
    if not kept:
        return [_fold(content)], False
    return kept, True


def render_steps(steps: list[dict], only_given: bool = False,
                 indent: str = "      ") -> tuple[list[str], int, int]:
    """yaml 步骤 → Gherkin 行。

    返回 (lines, n_clean, n_total):n_clean/n_total 供清洗率统计。
    连续同类关键字 → And;清洗后为空的步骤回退原文 + # uncleanable。
    """
    lines: list[str] = []
    prev_kw: str | None = None
    n_clean = 0
    n_total = 0
    for step in steps:
        kw = str(step.get("keyword", "")).upper()
        gkw = _KEYWORDS.get(kw, kw.capitalize() or "And")
        if only_given and kw != "GIVEN":
            continue
        sents, ok = clean_sentences(str(step.get("content", "")))
        n_total += 1
        n_clean += 1 if ok else 0
        for i, s in enumerate(sents):
            cur = gkw if i == 0 else "And"
            if cur == prev_kw:
                cur = "And"
            lines.append(f"{indent}{cur} {s}")
            prev_kw = cur
    return lines, n_clean, n_total


def render_scenarios(node: dict, verification: str = "full",
                     indent: str = "  ") -> str:
    """一个原子节点的全部场景 → Gherkin 块。

    full:非模板场景渲染全部步骤;模板场景只渲染 GIVEN + 注记行。
    data-only:只渲染 GIVEN(骨架数据契约)。
    """
    lines: list[str] = []
    node_templated = bool(node.get("_scenarios_templated"))
    for sc in node.get("scenarios") or []:
        name = clean_name(str(sc.get("name") or "scenario"))
        lines.append(f"{indent}Scenario: {name}")
        steps_templated = node_templated or bool(sc.get("_steps_templated"))
        steps = [s for s in (sc.get("steps") or [])
                 if isinstance(s, dict)
                 and str(s.get("keyword", "")).upper() == "GIVEN"]
        if verification == "full" and not steps_templated:
            steps = [s for s in (sc.get("steps") or []) if isinstance(s, dict)]
        glines, _, _ = render_steps(steps)
        lines.extend(glines)
        if (verification == "full" and steps_templated
                and not any(l.strip().startswith(("When", "Then")) for l in glines)):
            lines.append(
                f"{indent}  # WHEN/THEN are platform placeholder boilerplate "
                "— the REQ CONTENT description above is the flow contract.")
    return "\n".join(lines)


def _direct_atomic_groups(tree: dict) -> list[dict]:
    """每个"直接包含原子节点的 FOLDER"一组:folder + 其原子子节点。"""
    groups: list[dict] = []

    def walk(node: dict) -> None:
        children = node.get("children") or []
        atoms = [c for c in children
                 if str(c.get("type", "")).upper() == "ATOMIC"]
        if atoms:
            groups.append({"folder": node, "atoms": atoms})
        for c in children:
            if isinstance(c, dict) and str(c.get("type", "")).upper() != "ATOMIC":
                walk(c)

    walk(tree)
    return groups


def feature_text(folder: dict, atoms: list[dict]) -> str:
    """家族文件夹 → .feature 文本。

    Feature 名 = 文件夹名;描述块 = 文件夹描述首句;Background = 其余句
    (恰为用户样例的结构);原子节点 → Scenario(全步骤 Gherkin)。
    打标节点(检测器判定 WHEN/THEN 为平台占位样板)改走 GIVEN-only 支:
    Flow contract 注释 + GIVEN 原文 + 注记行——占位 WHEN/THEN 不承载
    节点自身操作语义(sheet 审计 24/24,22 个连名称动词都零出现),
    保留只会注入装错场景的操作指令。
    """
    name = _fold(folder.get("name") or folder.get("id") or "feature")
    desc = clean_text(str(folder.get("description") or ""))
    sents = [s for s in _SENTENCE_SPLIT_RE.split(desc) if s.strip()]
    head = sents[0] if sents else ""
    background = " ".join(sents[1:]).strip()
    lines = [f"Feature: {name}"]
    if head:
        lines.append(f"  {head}")
    if background:
        lines += ["", "  Background:", f"    {background}"]
    for atom in atoms:
        lines.append("")
        lines.append(f"  Scenario: {clean_name(str(atom.get('name') or ''))}")
        sc0 = (atom.get("scenarios") or [{}])[0]
        steps = [s for s in (sc0.get("steps") or [])
                 if isinstance(s, dict)]
        if atom.get("_scenarios_templated") or sc0.get("_steps_templated"):
            flow = clean_text(str(atom.get("description") or ""))
            if flow:
                lines.append(f"    # Flow contract: {flow}")
            given_lines, _, _ = render_steps(
                steps, only_given=True, indent="    ")
            lines.extend(given_lines)
            lines.append(f"    {_BOILER_NOTE}")
        else:
            node_lines, _, _ = render_steps(steps, indent="    ")
            lines.extend(node_lines)
    return "\n".join(lines) + "\n"


def batch_turn_features(batch: list[dict],
                        parent_of: dict[str, dict]) -> str:
    """一个批次的原子节点 → TURN 正文(按直接父文件夹分组的 Feature,
    文档序)。parent_of = 原子 id → 直接父 FOLDER(main._index_parents)。"""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for atom in batch:
        folder = parent_of.get(str(atom.get("id")))
        if folder is None:
            continue
        fid = str(folder.get("id") or len(order))
        if fid not in groups:
            groups[fid] = {"folder": folder, "atoms": []}
            order.append(fid)
        groups[fid]["atoms"].append(atom)
    return "\n\n".join(
        feature_text(groups[fid]["folder"], groups[fid]["atoms"])
        for fid in order)


def feature_files(tree: dict) -> list[dict]:
    """树 → [{id, name, text}] 每直接包含原子节点的 FOLDER 一项。"""
    out = []
    for g in _direct_atomic_groups(tree):
        folder = g["folder"]
        fid = str(folder.get("id") or "feature")
        out.append({"id": fid, "name": _fold(folder.get("name") or fid),
                    "text": feature_text(folder, g["atoms"])})
    return out
