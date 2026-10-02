"""requirements yaml 场景 → Gherkin 渲染(确定性文本清洗,无语义发明)。

09-30 定则:模型读到的验收信号从"缩进 GIVEN/WHEN/THEN 原文"升级为
Gherkin(Feature/Scenario/Given/When/Then)。清洗规则全部机械可审计:

  1. 空白折叠 → 按句切分(句末标点 + 后随大写/引号);
  2. 丢弃 "Seed values: …" 句(种子数据的字面重复);
  3. 清洗后为空的步骤 → 回退原文并标注 # uncleanable(不发明)。

10-02 全量恢复(用户澄清):步骤里的 "the requested workflow" 是
"所请求的工作流"——LLM 须按顺序走完的测试本体,节点 description 以
Flow contract 注释同场给出该工作流的定义;二者都不是可裁剪对象。
09-30~10-01 的占位短语剥离与场景打标检测据此整链退役:渲染层一律
全场景全步骤(GIVEN/WHEN/THEN),data-only 模式除外。

10-02 同文即残渣(用户拍板):渲染"全"不含克隆——同节点步骤序列
逐字节相同的模板克隆场景(题目 yaml "- Scenario N" 变体,sheet 100
场景含 42 克隆/github 13 个)与同场景内逐字节相同的句子只渲染首见;
判分器读 yaml 全量,渲染层去重不影响判分面。Flow contract 同律:
每节点恰一行,不逐场景继承。

场景名同规则清洗,坍缩到只剩 id 时只留 id。

纯标准库;同输入 → 逐字节相同(缓存律)。
"""

from __future__ import annotations

import re

_KEYWORDS = {"GIVEN": "Given", "WHEN": "When", "THEN": "Then"}

# 占位短语剥离已退役(10-02):"the requested workflow" 是步骤的语义
# 成分,不是模板垃圾——见模块 docstring。
_SEED_VALUES_RE = re.compile(r"^seed values\s*:", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"‘`])")
_EMPTY_QUOTE_RE = re.compile(r"(\"\"|'')")
# 引用名:“…”(github)与 "…"(sheet)——反引号是种子值不在此列;
# 单引号 '…' 有撇号陷阱("the user's own 'X'" 跨词配对)也不取。
_QUOTED_NAME_RE = re.compile(r"“([^”]+)”|\"([^\"]+)\"")


def _fold(text: str) -> str:
    return " ".join(str(text or "").split())


def clean_text(text: str) -> str:
    """确定性清洗:折叠空白 → 去空引号对 → 标点坍缩。"""
    t = _fold(text)
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


def _unique_scenarios(node: dict) -> list[dict]:
    """同节点内步骤序列逐字节相同的场景只留首见。

    题目 yaml 每节点按 "- Scenario N" 模板造变体,克隆场景步骤与
    首见场景逐字节相同(sheet 100 场景含 42 克隆/github 13 个,10-02
    实测)——零新信息,只烧 token;判分器读 yaml 全量,渲染层去重
    不影响判分面(10-02 拍板:同文即残渣,不继承)。
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for sc in node.get("scenarios") or []:
        if not isinstance(sc, dict):
            continue
        sig = tuple(
            (str(s.get("keyword", "")), str(s.get("content", "")))
            for s in (sc.get("steps") or []) if isinstance(s, dict))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(sc)
    return out


def render_steps(steps: list[dict],
                 indent: str = "      ") -> tuple[list[str], int, int]:
    """yaml 步骤 → Gherkin 行。

    返回 (lines, n_clean, n_total):n_clean/n_total 供清洗率统计。
    连续同类关键字 → And;清洗后为空的步骤回退原文 + # uncleanable。
    同场景内逐字节相同的句子只渲染首见(题目模板把标准 GIVEN 串进
    变体步骤时会一式两份,如 REQ-1-1-2 克隆的 "The visitor starts …"
    句;10-02 拍板:同文即残渣)。
    """
    lines: list[str] = []
    prev_kw: str | None = None
    seen: set[str] = set()
    n_clean = 0
    n_total = 0
    for step in steps:
        kw = str(step.get("keyword", "")).upper()
        gkw = _KEYWORDS.get(kw, kw.capitalize() or "And")
        sents, ok = clean_sentences(str(step.get("content", "")))
        n_total += 1
        n_clean += 1 if ok else 0
        fresh = []
        for s in sents:
            if s not in seen:
                seen.add(s)
                fresh.append(s)
        for i, s in enumerate(fresh):
            cur = gkw if i == 0 else "And"
            if cur == prev_kw:
                cur = "And"
            lines.append(f"{indent}{cur} {s}")
            prev_kw = cur
    return lines, n_clean, n_total


def render_scenarios(node: dict, verification: str = "full",
                     indent: str = "  ") -> str:
    """一个原子节点的全部场景 → Gherkin 块。

    full:节点级一行 Flow contract(与 feature_text 同口径:每节点恰
    一行,不逐场景重复)+ 全部步骤逐字(10-02 全量恢复:GIVEN/WHEN/
    THEN 都是判分器要走的测试)。
    data-only:只渲染 GIVEN(骨架数据契约),不带 description。
    """
    lines: list[str] = []
    flow = (clean_text(str(node.get("description") or ""))
            if verification == "full" else "")
    if flow:
        lines.append(f"{indent}# Flow contract: {flow}")
    for sc in _unique_scenarios(node):
        name = clean_name(str(sc.get("name") or "scenario"))
        lines.append(f"{indent}Scenario: {name}")
        steps = [s for s in (sc.get("steps") or [])
                 if isinstance(s, dict)
                 and (verification == "full"
                      or str(s.get("keyword", "")).upper() == "GIVEN")]
        glines, _, _ = render_steps(steps)
        lines.extend(glines)
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

    Feature 名 = 文件夹名;描述块 = 文件夹描述首句;Background = 其余句。
    每个原子节点:`# <节点 id>` 锚 + **节点级一行**
    `# Flow contract: <description 清洗文>`(10-02 三次拍板:有就展示、
    无就不展示、不逐场景继承——节点下全部场景共用同一条契约,变体
    场景经 id 锚一步对上;逐场景重复同一大段文本只烧 token 不添信息)
    ——其下逐字渲染全部唯一场景的全部 GIVEN/WHEN/THEN(克隆场景只留
    首见;判分器读 yaml 全量,不再 sc0-only、不再按占位标记裁剪)。首场景标题沿用节点名(与既有 turn
    形态字节兼容),其余场景用场景自身名区分变体。场景正文里的交叉
    引用("compliant with REQ-1-1-1")经 id 注释一步可定位。无
    scenarios 的原子只渲染锚 + 单行 Flow contract。
    有则渲染、无则不渲染(10-02 拍板"有就展示无就不展示"):无 id 不发
    悬空 "# " 锚,无 description 不发 Flow contract 行,三者皆无的原子
    整体省略——与 SCENARIOS 空省略同律,不留任何空标签。
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
        atom_id = str(atom.get("id") or "").strip()
        flow = clean_text(str(atom.get("description") or ""))
        scs = _unique_scenarios(atom)
        if not atom_id and not flow and not scs:
            continue
        lines.append("")
        if atom_id:
            lines.append(f"  # {atom_id}")
        if flow:
            lines.append(f"  # Flow contract: {flow}")
        if not scs:
            continue
        node_title = (clean_name(str(atom.get("name") or ""))
                      or atom_id or "scenario")
        for si, sc in enumerate(scs):
            if si == 0:
                lines.append(f"  Scenario: {node_title}")
            else:
                lines.append(f"  Scenario: "
                             f"{clean_name(str(sc.get('name') or '')) or 'scenario'}")
            steps = [s for s in (sc.get("steps") or [])
                     if isinstance(s, dict)]
            glines, _, _ = render_steps(steps, indent="    ")
            lines.extend(glines)
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
