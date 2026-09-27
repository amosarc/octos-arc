#!/usr/bin/env python3
"""flow_suite — 宿主场景流套件(FLOW RUN)。

背景(2026-09-27 设计契合度定论):自测满分与比赛个位数并存,根因是
验证语义层级错位——ui-smoke 十类探针全部是"契约串挂在载体上"的
存在性检查,而平台隐藏 spec 是场景流(点具名控件 → 断言紧跟 →
reload 断言持久),且 runner 连续失败止损。控件"存在但流走不通"
(save 永久禁用/创建页强制填名/错误消息不出场/用户名非独立精确
文本)在 presence 级原理上是盲区。

本模块从 ATOMIC description 确定性生成流级 Playwright spec(零
LLM):两道 hackathon 题的描述高度公式化——`button named “X”`/
`button with the accessible name "X"`/`dialog named “D”`/
`textbox labeled “F”`/`displays "E"`——按文本出现顺序提取控件,
生成与公开 spec 同构的走查:

  入口导航(种子工作簿/登录前奏) → 按序点击具名按钮/链接/菜单项 →
  对话框可见 + 字段标签可见(+预填非空) → 空值/重名触发错误消息 →
  成功提交对话框关闭 →(文本提及刷新时)reload 复验 → 改名流还原。

安全边界(顺序共享实例,平台同构):
  破坏性提交(delete/remove/sign out/reset/merge/update password)
  只断言可见,绝不点击;菜单命令分门别类——tab 菜单(Worksheet
  options 开门按钮)、行/列菜单(rowheader/columnheader 右键)只
  在开门控件可推导时生成;登录前奏不可推导时跳过整流而非产必红。

产物落 .arc/ui-smoke/flow-suite.spec.ts——与契约爬虫同目录同
config,内核 guard/UI SMOKE RUN 命令形状/green-stop 全部免改直接
生效;宿主权威轮与批后流回归经 ui_smoke.run_smoke 的文件参数/
grep 执行。测试标题以 `<REQ id> flow:` 开头,parse 侧映射回节点。
"""

from __future__ import annotations

import json
import re

FLOW_SPEC_NAME = "flow-suite.spec.ts"

# 引号族:sheet 用直引号,github 用弯引号(U+201C/201D)。首字符限
# 字母数字:直引号的闭合引号会被当成开启引号,跨句吞进 "; clicking
# it displays ..." 类垃圾(github/ui_smoke 的 [A-Z0-9] 同款教训)。
_Q = r'["\u201c]([A-Za-z0-9][^"\u201d\u2019\n]{1,119})["\u201d\u2019]'

# 破坏性提交:顺序共享实例下点了会毁掉后续流的现场。只断言可见。
_DESTRUCTIVE_RE = re.compile(
    r"delete|remove|sign out|reset|merge|destruct|drop|clear all"
    r"|update password|change password|confirm visibility", re.I)

# 控件提取:(角色, 正则)。
_CTRL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("dialog", re.compile(r"dialog\s+named\s+" + _Q, re.I)),
    ("button", re.compile(r"buttons?\s+named\s+" + _Q, re.I)),
    ("button", re.compile(r"\bbuttons?\s+" + _Q, re.I)),
    ("button", re.compile(
        r"button[^,.;]{0,60}?accessible name\s+" + _Q, re.I)),
    ("button", re.compile(r"submit button is named\s+" + _Q, re.I)),
    ("button", re.compile(r"\b(?:a|an|the)\s+" + _Q + r"\s+button\b", re.I)),
    ("menuitem", re.compile(
        r"\b(?:a|an|the)\s+" + _Q + r"\s+menu item\b", re.I)),
    # 菜单命令句式:choose "X" from the "Data" menu / the "Delete"
    # command / The row-number menu provides "Insert 1 row above", ...
    ("menuitem", re.compile(r"\bchoose\s+" + _Q + r"\s+from\s+the\s+" + _Q
                            + r"\s+menu", re.I)),
    ("menuitem", re.compile(r"\bclick\s+" + _Q + r"\s+in\s+the\s+" + _Q
                            + r"\s+menu", re.I)),
    ("menuitem", re.compile(r"\bthe\s+" + _Q + r"\s+command\b", re.I)),
    ("menuitem", re.compile(r"menu\s+provides\s+" + _Q, re.I)),
    ("link", re.compile(r"links?\s+named\s+" + _Q, re.I)),
    ("link", re.compile(r"\blinks?\s+" + _Q, re.I)),
    ("link", re.compile(r"\bthe\s+" + _Q + r"\s+link\b", re.I)),
    ("textbox", re.compile(
        r"(?:text\s*box|textbox|password input|file control"
        r"|field|control)\s+labeled\s+" + _Q, re.I)),
    # 'uniquely labeled password inputs “Current password”, “New
    # password”, and “Confirm password”'(形容词前置,引号在名词后)
    ("textbox", re.compile(r"password\s+inputs?\s+" + _Q, re.I)),
    # combo box 是 <select>:selectOption 驱动,fill() 会抛错——单列
    # 角色,只断言可见不参与填值腿
    ("combo", re.compile(r"combo\s*box(?:es)?\s+labeled\s+" + _Q, re.I)),
    ("checkbox", re.compile(r"checkbox\s+named\s+" + _Q, re.I)),
    ("checkbox", re.compile(
        r"\b(?:a|an|the)\s+" + _Q + r"\s+checkbox\b", re.I)),
]

# “buttons named “A” and “B”” 的追加捕获(主匹配吃 A,连词尾巴补 B)。
# 只扩展 button/textbox 列表——dialog/menuitem 的尾巴会把后续任意
# 引号串误吞成同类(实测 "Order" 被追加成 dialog)。
_LIST_TAIL_RE = re.compile(r"\b(?:and|,)\s*" + _Q)
_TAIL_ROLES = {"button", "textbox"}

# 裸引号列表句(句级扫描,见 _bare_list_controls):只认分类谓语句式
# ("... “A”, “B” are buttons" / "are ... field labels")——混合句
# (含 textbox labeled/password input labeled 的枚举句)整句收编会把
# checkbox/button 名误吞成字段,实测 REQ-1-1-1 全句翻车。
_BARE_SENTENCE_ROLES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bare\s+buttons?\b", re.I), "button"),
    (re.compile(r"\bare\s+(?:associated\s+)?field\s+labels?\b", re.I),
     "textbox"),
]

# 错误/展示消息:断言 getByText 可见。占位符模板(<X>)不是可断言文本。
_MESSAGE_RE = re.compile(
    r"(?:displays?|displayed|shown?|reject(?:ed)? with|must be rejected with"
    r"|must display|message(?:\s+is)?)\s*:?\s*" + _Q, re.I)

# 登录表单句式:"form ... containing “A”, “B”, and “C”"(裸引号
# 列表,主模式抓不到;前 n-1 是字段,末一个是提交)。
_CONTAINING_RE = re.compile(
    r"containing\s+" + _Q + r"\s*,\s*" + _Q + r"\s*,?\s*(?:and\s+)?" + _Q,
    re.I)

_PRELUDE_RE = re.compile(
    r"authenticated-session context provided by|after the user signs? ?in",
    re.I)
_PERSIST_RE = re.compile(
    r"\brefresh|reloading|reopen|persist|remains?\b", re.I)
_PREFILL_RE = re.compile(r"prefilled", re.I)
_EMPTY_MSG_RE = re.compile(r"empty|blank|required", re.I)
_WORKSHEET_NAMED_RE = re.compile(
    r"worksheet named\s+([A-Za-z0-9][\w ]{0,30})")
_GRID_NAME_RE = re.compile(r"grid[^.]{0,80}?accessible name\s+\"([^\"]+)\"")
_SEED_WORKBOOK_RE = re.compile(r"seeded workbook\s+`([^`\n]+)`")
_SEED_WORKSHEET_RE = re.compile(r"worksheet\s+`([^`\n]+)`")

_TAB_MENU_RE = re.compile(r"tab menu|worksheet tab|Worksheet options", re.I)
_ROW_MENU_RE = re.compile(r"row[- ]number menu|row menu", re.I)
_COL_MENU_RE = re.compile(r"column menu|column header menu", re.I)

_MAX_OPENER_CLICKS = 4


def _ts(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+", text) if s.strip()]


def _bare_list_controls(desc: str) -> list[tuple[int, str, str]]:
    """句级扫描:含 “are buttons/are field labels” 分类谓词的句子,
    句中全部引号串按谓词角色收编(offset 必须换算回全文坐标——
    句内局部坐标会让开门链/字段排序全部错乱)。"""
    out: list[tuple[int, str, str]] = []
    for pat, role in _BARE_SENTENCE_ROLES:
        for sent, base in _sentence_spans(desc):
            if not pat.search(sent):
                continue
            for m in re.finditer(_Q, sent):
                name = m.group(1).strip()
                if name:
                    out.append((base + m.start(1), role, name))
    return out


def _sentence_spans(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in
            re.finditer(r"[^.!?;]+[.!?;]?", text)]


def _placeholder(name: str) -> bool:
    return ("<" in name and ">" in name) or "{" in name


def _finditer_role(text: str) -> list[tuple[int, str, str]]:
    """全部控件提及,按文档 offset 序产出 (offset, role, name)。"""
    out: list[tuple[int, str, str]] = []
    for role, pat in _CTRL_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1).strip()
            if name:
                out.append((m.start(1), role, name))
            if role in _TAIL_ROLES:
                for t in _LIST_TAIL_RE.finditer(text[m.end():m.end() + 80]):
                    tname = t.group(1).strip()
                    if tname:
                        out.append((m.end() + t.start(1), role, tname))
    out.extend(_bare_list_controls(text))
    seen: set[tuple[int, str, str]] = set()
    uniq = []
    for item in sorted(out):
        if item in seen or _placeholder(item[2]):
            continue
        seen.add(item)
        uniq.append(item)
    return uniq


_CTRL_MENUBUTTON_RE = re.compile(
    r"\b(?:from|in|of)\s+the\s+" + _Q + r"\s+menu\b", re.I)
_THE_MENU_RE = re.compile(r"\bthe\s+" + _Q + r"\s+menu\b", re.I)


def _menu_button(desc: str) -> str | None:
    """节点级菜单开门按钮名(the "Data" menu → button "Data")。"""
    m = _CTRL_MENUBUTTON_RE.search(desc) or _THE_MENU_RE.search(desc)
    return m.group(1).strip() if m else None


def _messages(text: str) -> list[tuple[int, str]]:
    msgs = []
    for m in _MESSAGE_RE.finditer(text):
        name = m.group(1).strip()
        if name and not ("<" in name and ">" in name) and "{" not in name:
            msgs.append((m.start(1), name))
    return msgs


def _is_file_field(desc: str, offset: int) -> bool:
    return bool(re.search(r"file control", desc[max(0, offset - 30):offset],
                          re.I))


def _seed_info(tree: dict) -> dict:
    """入口态:种子工作簿/工作表名(场景 GIVEN 反引号)、网格可访问名
    ——全部来自需求原文,确定性。"""
    info = {"workbook": None, "worksheet": None, "worksheet2": None,
            "grid": None, "menu_opener": None}
    texts: list[str] = []

    def visit(node) -> None:
        texts.append(str(node.get("description") or ""))
        for sc in node.get("scenarios") or []:
            for step in sc.get("steps") or []:
                if isinstance(step, dict) and \
                        str(step.get("keyword", "")).upper() == "GIVEN":
                    texts.append(str(step.get("content") or ""))
        for child in node.get("children") or []:
            visit(child)

    visit(tree or {})
    joined = "\n".join(texts)
    m = _SEED_WORKBOOK_RE.search(joined)
    if m:
        info["workbook"] = m.group(1).strip()
    uniq: list[str] = []
    for s in _SEED_WORKSHEET_RE.findall(joined):
        s = s.strip()
        if s and s not in uniq:
            uniq.append(s)
    if uniq:
        info["worksheet"] = uniq[0]
        if len(uniq) > 1:
            info["worksheet2"] = uniq[1]
    m = _GRID_NAME_RE.search(joined)
    if m:
        info["grid"] = m.group(1).strip()
    m = re.search(r"accessible name\s+\"([A-Za-z ]+)<", joined)
    if m:
        info["menu_opener"] = m.group(1).strip()
    return info


def _atomics(tree: dict) -> list[dict]:
    out: list[dict] = []

    def visit(node) -> None:
        if str(node.get("type", "")).upper() == "ATOMIC":
            out.append(node)
        for child in node.get("children") or []:
            visit(child)

    visit(tree or {})
    return out


def _signin_controls(tree: dict):
    """登录链 (labels, button, entry_link) 或 None。

    github 句式 'The sign-in page is the form ... containing
    “Username or email”, “Password”, and “Sign in”';入口链接取
    'link named “Sign in”'(头部契约常见)。"""
    for node in _atomics(tree):
        desc = str(node.get("description") or "")
        if not re.search(r"sign[- ]in", desc, re.I):
            continue
        labels: list[str] = []
        button = None
        m = _CONTAINING_RE.search(desc)
        if m:
            names = [g.strip() for g in m.groups() if g and g.strip()]
            if len(names) >= 3 and re.search(
                    r"sign ?in|log ?in|submit", names[-1], re.I):
                labels, button = names[:-1], names[-1]
        if not labels:
            ctrls = _finditer_role(desc)
            labels = [n for _, r, n in ctrls if r == "textbox"]
            button = next((n for _, r, n in ctrls
                           if r == "button" and
                           re.search(r"sign ?in|log ?in", n, re.I)), None)
        if labels and button:
            entry = next((n for _, r, n in _finditer_role(desc)
                          if r == "link" and
                          re.search(r"sign ?in|log ?in", n, re.I)), None)
            return labels, button, entry
    return None


def _signin_is_node(node: dict) -> bool:
    return bool(_CONTAINING_RE.search(str(node.get("description") or ""))and
                re.search(r"sign[- ]in", str(node.get("description") or ""),
                          re.I))


def collect_messages(tree: dict) -> list[str]:
    """树内全部引号消息(去重,文档序)。

    宿主侧 .arc/host/req-messages.json sidecar 的数据源——codegen 编译
    schema 时做确定性桥接("<X> is required"/"<X> already exists" →
    字段 messages,credential 族 → loginFailure)。非消息类引号串
    (按钮名/标签)对桥是无害噪音:桥只按后缀/正则挑消息。"""
    out: list[str] = []
    seen: set[str] = set()

    def visit(node) -> None:
        for _, m in _messages(str(node.get("description") or "")):
            if m not in seen:
                seen.add(m)
                out.append(m)
        for child in node.get("children") or []:
            visit(child)

    visit(tree or {})
    return out


# ---------------------------------------------------------------------------
# 流 spec 生成
# ---------------------------------------------------------------------------

_HEADER = """\
// flow-suite.spec.ts — HOST-OWNED (read-only, generated by arc/flow_suite.py).
// Scenario FLOW checks derived from the requirement descriptions: walk the
// named controls in text order, assert the named dialogs/labels/messages,
// reload-persist where the text says so. The platform suite is flow-shaped —
// presence alone (contract-smoke) has shipped single-digit scores while
// self-checks were green. Destructive submits are asserted visible, never
// clicked (one shared app instance, platform-isomorphic).
import { expect, test } from '@playwright/test';

const AUTH = __AUTH_JSON__ as { username: string; passwords: string[] } | null;
const SEED = __SEED_JSON__ as { workbook: string | null; worksheet: string | null; worksheet2: string | null; grid: string | null };

async function openHome(page: any) {
  await page.goto('/');
}

async function openWorkbook(page: any) {
  await page.goto('/');
  await page.getByRole('link', { name: SEED.workbook!, exact: true }).click();
}
"""

_SIGNIN_HELPER = """\

async function signIn(page: any) {
  await openHome(page);
  const entry = page.getByRole('link', { name: __SIGNIN_ENTRY__, exact: true });
  if (await entry.count()) { await entry.first().click(); }
  if (!AUTH) throw new Error('no seeded account for the sign-in prelude');
  const form = page.getByRole('button', { name: __SIGNIN_BUTTON__, exact: true });
  for (const pw of AUTH.passwords) {
    await page.getByLabel(__SIGNIN_USER__).fill(AUTH.username);
    await page.getByLabel(__SIGNIN_PASS__, { exact: true }).fill(pw);
    await form.click();
    await page.waitForTimeout(400);
    if (await page.getByText(AUTH.username, { exact: true })
      .isVisible().catch(() => false)) return;
    if (!(await form.isVisible().catch(() => false))) return;
  }
  throw new Error('sign-in prelude failed: no seeded password worked (or the form never leaves)');
}
"""


def _role_expr(page_expr: str, role: str, name: str,
               exact: bool = True) -> str:
    opts = f"{{ name: {_ts(name)}" + (", exact: true }" if exact else " }")
    return f"{page_expr}.getByRole('{role}', {opts})"


def _menu_opener_lines(desc: str, seed: dict,
                       sheet_name: str | None = None) -> list[str] | None:
    """菜单命令的开门行:tab 菜单(Worksheet options 按钮)/行菜单
    (rowheader 右键)/列菜单(columnheader 右键)/具名菜单按钮(the
    "Data" menu)。不可推导 → None。"""
    name = sheet_name or seed.get("worksheet")
    if _TAB_MENU_RE.search(desc):
        if not (seed.get("menu_opener") and name):
            return None
        opener = seed["menu_opener"].rstrip() + " " + name
        return [f"await {_role_expr('page', 'button', opener, exact=False)}.click();"]
    if _ROW_MENU_RE.search(desc):
        return ["await page.getByRole('rowheader', { name: '3', exact: true })"
                ".click({ button: 'right' });"]
    if _COL_MENU_RE.search(desc):
        return ["await page.getByRole('columnheader', { name: 'C', exact: true })"
                ".click({ button: 'right' });"]
    mb = _menu_button(desc)
    if mb:
        return [f"await {_role_expr('page', 'button', mb)}.click();"]
    return None


def _gen_walk(node: dict, seed: dict, signin: tuple | None) -> list[str]:
    """通用走查流。返回 spec 行(含 test 块);不可生成 → 空表。"""
    nid = str(node.get("id") or "")
    name = str(node.get("name") or nid)
    desc = str(node.get("description") or "")
    needs_signin = bool(_PRELUDE_RE.search(desc))
    if needs_signin and signin is None:
        return []

    ctrls = _finditer_role(desc)
    msgs = _messages(desc)
    dialog_off = next((o for o, r, _ in ctrls if r == "dialog"), None)
    has_dialog = dialog_off is not None
    if len(ctrls) < 2 and not has_dialog:
        return []
    field_offsets = [o for o, r, _ in ctrls
                     if r in ("textbox", "checkbox", "combo")]
    first_field_off = min(field_offsets) if field_offsets else None
    opener_list = [(o, r, n) for o, r, n in ctrls
                   if r in ("button", "link", "menuitem")]
    if not opener_list and not has_dialog:
        # 纯字段冷断言(字段所属面板的开门控件不可推导)整流跳过
        return []

    body: list[str] = []
    # ---- 入口 ----
    if needs_signin:
        body.append("await signIn(page);")
    elif seed["workbook"] and not _signin_is_node(node) and \
            not re.search(r"home page", desc, re.I):
        # 描述点名 home page 的流(创建/导入入口在首页)从首页起
        body.append("await openWorkbook(page);")
    else:
        body.append("await openHome(page);")

    # ---- 开门链:首 dialog/首字段 前的 button/link/menuitem(字段后
    # 的按钮属于提交路径,不当开门点击) ----
    openers = [(o, r, n) for o, r, n in opener_list
               if (dialog_off is None or o < dialog_off)
               and (first_field_off is None or o < first_field_off)
               ][:_MAX_OPENER_CLICKS]
    menu_probe = _menu_opener_lines(desc, seed)
    clickable = [o for o, r, _ in openers
                 if r != "menuitem" or menu_probe is not None]
    if not clickable and not has_dialog:
        # 过滤后无有效开门控件:字段所属面板/页面不可达,冷断言必假红
        return []
    used: set[int] = set()
    menu_lines = _menu_opener_lines(desc, seed)
    menu_opened = False

    def emit_menu_open() -> None:
        nonlocal menu_opened
        if not menu_opened and menu_lines:
            body.extend(menu_lines)
            menu_opened = True

    dialog_after = lambda off: (  # noqa: E731
        dialog_off is not None and dialog_off > off)
    for i, (off, role, cname) in enumerate(openers):
        if role == "menuitem":
            if menu_lines is None:
                continue  # 开门控件不可推导 → 跳过该控件
            if not dialog_after(off) and _DESTRUCTIVE_RE.search(cname):
                # 立即执行的破坏性菜单命令(Delete row 类):只断言在场
                body.append(f"await expect({_role_expr('page', 'menuitem', cname)}).toBeVisible();")
                used.add(off)
                continue
            emit_menu_open()
            body.append(f"await {_role_expr('page', 'menuitem', cname)}.click();")
            used.add(off)
            continue
        if i > 0:
            body.append(f"await expect({_role_expr('page', role, cname)}).toBeVisible();")
            body.append(f"await expect({_role_expr('page', role, cname)}).toBeEnabled();")
        if _DESTRUCTIVE_RE.search(cname) and not dialog_after(off):
            # 立即执行的破坏性控件只断言在场;其后有确认对话框的
            # (点它只是开框)照常点击,框内确认按钮另行只断言。
            body.append(f"await expect({_role_expr('page', role, cname)}).toBeVisible();")
            used.add(off)
            continue
        body.append(f"await {_role_expr('page', role, cname)}.click();")
        used.add(off)

    # ---- 对话框块 ----
    dialog_name = next((n for o, r, n in ctrls if r == "dialog"), None)
    dlg = "page"
    if has_dialog:
        body.append(f"const dlg = {_role_expr('page', 'dialog', dialog_name)};")
        body.append("await expect(dlg).toBeVisible();")
        dlg = "dlg"

    # ---- 字段:textbox 可填;combo(<select>)与 checkbox 只断言在场 ----
    fields = [(o, r, n) for o, r, n in ctrls
              if r in ("textbox", "checkbox", "combo") and o not in used
              and (dialog_off is None or o > dialog_off)]
    fill_exprs: list[str] = []
    for off, role, fname in fields:
        if role == "textbox":
            expr = f"{dlg}.getByLabel({_ts(fname)})"
            body.append(f"await expect({expr}).toBeVisible();")
            if _PREFILL_RE.search(desc[max(0, off - 80):off + 80]):
                body.append(
                    f"await expect(({expr}).inputValue()).not.toBe('');")
            if not _is_file_field(desc, off):
                fill_exprs.append(expr)
        else:
            prole = "combobox" if role == "combo" else role
            body.append(
                f"await expect({_role_expr(dlg, prole, fname)}).toBeVisible();")

    # ---- 提交按钮:最后一个字段之后的第一个按钮(主提交动作);
    # 无字段时取 dialog 后的第一个。这是"applies the rule"类主动词
    # 所在,取末尾按钮会把 Delete rule 类收尾按钮误当提交。
    submit_name = None
    last_field_off = max(field_offsets) if field_offsets else None
    for off, role, cname in ctrls:
        if role == "button" and off not in used:
            after = (off > last_field_off if last_field_off is not None
                     else (dialog_off is None or off > dialog_off))
            if after:
                submit_name = cname
                break
    if submit_name is None:
        for off, role, cname in ctrls:
            if role == "button" and off not in used:
                submit_name = cname
    destructive = bool(submit_name and _DESTRUCTIVE_RE.search(submit_name))
    if submit_name:
        sub = _role_expr(dlg, "button", submit_name)
        body.append(f"await expect({sub}).toBeEnabled();")
        if destructive:
            body.append("// destructive submit: asserted, never clicked")

    # ---- 验证腿:空值触发首条错误消息。空值提交按需求定义就是失败
    # 路径,不改变服务器状态——破坏性提交也允许;但消息必须是
    # empty/blank/required 族(与空值语义配对),其余消息的触发条件
    # 不可推导,跳过。
    if fill_exprs and msgs and submit_name and \
            _EMPTY_MSG_RE.search(msgs[0][1]):
        body.append(f"await {fill_exprs[0]}.fill('');")
        body.append(
            f"await {_role_expr(dlg, 'button', submit_name)}.click();")
        body.append(
            f"await expect(page.getByText({_ts(msgs[0][1])})).toBeVisible();")
        if len(msgs) > 1 and seed.get("worksheet2") and \
                re.search(r"already exist", msgs[1][1], re.I):
            body.append(f"await {fill_exprs[0]}.fill(SEED.worksheet2!);")
            body.append(
                f"await {_role_expr(dlg, 'button', submit_name)}.click();")
            body.append(
                "await expect(page.getByText("
                f"{_ts(msgs[1][1])})).toBeVisible();")

    # ---- 成功腿:填新值提交;有对话框断言其关闭,无对话框断言页面
    # 存活。破坏性提交永不点击。填充值按字段语义选(邮箱/密码/用户
    # 名给合法值,否则后端校验失败把"提交成功"腿变成错误页断言)。 ----
    def _fill_value(label: str) -> str:
        low = label.lower()
        if re.search(r"e-?mail", low):
            return "flow-probe@example.test"
        if "password" in low:
            return "Flow-probe-123!"
        if "user" in low or "name" in low:
            return "flow-probe-user"
        return "Flow Value 123"

    success_value = None
    if fill_exprs and submit_name and not destructive:
        first_label = fields[0][2]
        success_value = ("Flow Renamed"
                         if re.search(r"\brename\b", name, re.I)
                         else _fill_value(first_label))
        body.append(f"await {fill_exprs[0]}.fill('{success_value}');")
        body.append(
            f"await {_role_expr(dlg, 'button', submit_name)}.click();")
        if has_dialog:
            body.append("await expect(dlg).toBeHidden();")
        else:
            body.append("await expect(page.getByRole('button').first())"
                        ".toBeVisible();")

    # ---- 还原段:改名成功腿会破坏后续流引用的种子名(工作表名/
    # 工作簿名)。tab 菜单的还原开门用新名。 ----
    if success_value and re.search(r"rename", name + " " + desc, re.I):
        original = (seed.get("worksheet") if
                    re.search(r"worksheet", dialog_name or "", re.I)
                    else seed.get("workbook")) or "Sheet1"
        first_opener = next(((o, r, n) for o, r, n in openers
                             if o in used), None)
        if first_opener and first_opener[1] == "menuitem":
            restore_menu = _menu_opener_lines(desc, seed,
                                              sheet_name=success_value)
            if restore_menu:
                body.extend(restore_menu)
                body.append(
                    f"await {_role_expr('page', 'menuitem', first_opener[2])}.click();")
        elif first_opener and first_opener[1] in ("button", "link"):
            body.append(
                f"await {_role_expr('page', first_opener[1], first_opener[2])}.click();")
        if has_dialog:
            body.append(f"const dlg2 = {_role_expr('page', 'dialog', dialog_name)};")
            body.append("await expect(dlg2).toBeVisible();")
            dlg2 = "dlg2"
        else:
            dlg2 = "page"
        if fields:
            body.append(f"await {dlg2}.getByLabel({_ts(fields[0][2])})"
                        f".fill({_ts(original)});")
        if submit_name:
            body.append(
                f"await {_role_expr(dlg2, 'button', submit_name)}.click();")
        if has_dialog:
            body.append("await expect(dlg2).toBeHidden();")

    # ---- 结果断言:worksheet named X → tab 可见 ----
    outcome_tab = _WORKSHEET_NAMED_RE.search(desc)
    if outcome_tab:
        body.append(
            f"await expect({_role_expr('page', 'tab', outcome_tab.group(1).strip())}).toBeVisible();")

    # ---- reload 复验 ----
    if _PERSIST_RE.search(desc):
        # 锚点:结果 tab > 任意可交互件。reload 后未提交的对话框必然
        # 消失,拿"打开的对话框"当锚必假红。
        if outcome_tab:
            anchor = _role_expr("page", "tab",
                                outcome_tab.group(1).strip())
        else:
            anchor = "page.getByRole('button').first()"
        body.append("await page.reload();")
        if has_dialog and success_value:
            body.append(
                f"await expect({_role_expr('page', 'dialog', dialog_name)}).toBeHidden();")
        body.append(f"await expect({anchor}).toBeVisible();")

    lines = [f"test('{nid} flow: {name.lower()}', async ({{ page }}) => {{"]
    lines.extend("  " + b for b in body)
    lines.append("});")
    return lines


def _gen_register(node: dict) -> list[str]:
    """注册流:开门链接 → 表单逐字段可见可填 → 填值 → 勾选 → 提交 →
    用户名独立精确文本可见(+reload 仍可见)。

    触发:节点名/正文是注册语义(Confirm password 标签只是辅证——
    密码找回同样有该字段,不能单凭它触发)。"""
    desc = str(node.get("description") or "")
    nid = str(node.get("id") or "")
    name = str(node.get("name") or nid)
    if not re.search(r"register|create an account|sign ?up",
                     name + " " + desc, re.I):
        return []
    if re.search(r"recovery|forgot|reset", name + " " + desc, re.I):
        return []
    ctrls = _finditer_role(desc)
    other_roles = {n for _, r, n in ctrls if r != "textbox"}
    labels = [n for _, r, n in ctrls
              if r == "textbox" and n not in other_roles]
    if not any(re.search(r"confirm", l, re.I) for l in labels):
        return []
    button = next((n for _, r, n in ctrls if r == "button"), None)
    if not button:
        return []
    entry = next((n for _, r, n in ctrls if r == "link"), None)
    checkbox = next((n for _, r, n in ctrls if r == "checkbox"), None)
    body = ["await openHome(page);"]
    if entry:
        body.append(f"await {_role_expr('page', 'link', entry)}.click();")
    for l in labels:
        if re.search(r"user(?!.*(email|name))", l, re.I) or \
                l.lower() == "username":
            v = "flow-probe-user"
        elif re.search(r"e-?mail", l, re.I):
            v = "flow-probe@example.test"
        elif re.search(r"password", l, re.I):
            v = "Flow-probe-123!"
        elif re.search(r"code", l, re.I):
            v = "123456"
        else:
            v = "Flow Value 123"
        body.append(f"await page.getByLabel({_ts(l)}).fill({_ts(v)});")
    if checkbox:
        body.append(
            f"await {_role_expr('page', 'checkbox', checkbox)}.check();")
    body.append(f"await {_role_expr('page', 'button', button)}.click();")
    body.append("await expect(page.getByText('flow-probe-user', "
                "{ exact: true })).toBeVisible();")
    if re.search(r"visible after reload", desc, re.I):
        body.append("await page.reload();")
        body.append("await expect(page.getByText('flow-probe-user', "
                    "{ exact: true })).toBeVisible();")
    lines = [f"test('{nid} flow: {name.lower()}', async ({{ page }}) => {{"]
    lines.extend("  " + b for b in body)
    lines.append("});")
    return lines


def _gen_signin_roundtrip(node: dict) -> list[str]:
    nid = str(node.get("id") or "")
    name = str(node.get("name") or nid)
    return [
        f"test('{nid} flow: {name.lower()} (round-trip)', "
        "async ({ page }) => {",
        "  await signIn(page);",
        "  // 平台杀手断言:登录后用户名必须是独立精确文本节点",
        "  await expect(page.getByText(AUTH!.username, { exact: true }))",
        "    .toBeVisible();",
        "  await page.reload();",
        "  await expect(page.getByText(AUTH!.username, { exact: true }))",
        "    .toBeVisible();",
        "});",
    ]


def _gen_grid(tree: dict, seed: dict) -> list[str]:
    """网格编辑流(表格式任务地基):A1 选中 → 空 A5(固定网格) →
    直接键入 → 值在场 → reload 持久。

    sheet 二跑 3 分主根因(网格只渲染数据区)与键入竞态都在这条流上。"""
    if not seed["grid"]:
        return []
    target = None
    for node in _atomics(tree):
        nname = str(node.get("name") or "")
        desc = str(node.get("description") or "")
        if re.search(r"\bcell\b", nname + " " + desc, re.I) and (
                re.search(r"\bedit", nname, re.I)
                or re.search(r"selecting[^.]{0,40}cell", desc, re.I)
                or (re.search(r"\btyp(e|ing)\b", desc, re.I)
                    and re.search(r"\bcell\b", desc, re.I))):
            target = node
            break
    if target is None:
        return []
    nid = str(target.get("id") or "")
    return [
        f"test('{nid} flow: grid cell select-type-persist', "
        "async ({ page }) => {",
        "  await openWorkbook(page);",
        "  const grid = page.getByRole('grid', { name: SEED.grid! });",
        "  const cell = (c: string) => grid.getByRole('gridcell', "
        "{ name: c, exact: true });",
        "  await cell('A1').click();",
        "  await expect(cell('A1')).toHaveAttribute('aria-selected', 'true');",
        "  await cell('A5').click();",
        "  await page.keyboard.type('Flow123');",
        "  await page.keyboard.press('Enter');",
        "  await expect(cell('A5')).toHaveText('Flow123');",
        "  await page.reload();",
        "  await expect(cell('A5')).toHaveText('Flow123');",
        "});",
    ]


def _title_of(block_first_line: str) -> str:
    return block_first_line[len("test('"):].rsplit("', async", 1)[0]


def generate_spec(tree: dict, auth: dict | None = None,
                  seed: dict | None = None) -> tuple[str, dict[str, str]]:
    """需求树 → (flow-suite.spec.ts 文本, {node_id: test_title})。

    无可生成流 → ("", {})。确定性:同树+同 auth/seed 字节相同。
    """
    if seed is None:
        seed = _seed_info(tree)
    signin = _signin_controls(tree)
    if signin is not None and auth is None:
        signin = None  # 无账号 → 登录前奏不可用,相关流在 _gen_walk 跳过
    tests: list[str] = []
    node_map: dict[str, str] = {}

    for node in _atomics(tree):
        nid = str(node.get("id") or "")
        title_name = str(node.get("name") or nid).lower()
        block = _gen_register(node)
        if not block and _signin_is_node(node):
            block = _gen_signin_roundtrip(node)
        if not block:
            block = _gen_walk(node, seed, signin)
        if block:
            tests.append("\n".join(block))
            node_map[nid] = _title_of(block[0])

    grid_block = _gen_grid(tree, seed)
    if grid_block:
        gid = grid_block[0][len("test('"):].split(" flow:", 1)[0]
        tests.append("\n".join(grid_block))
        node_map[gid] = _title_of(grid_block[0])

    if not tests:
        return "", {}
    text = _emit_common(signin)
    text = (text
            .replace("__AUTH_JSON__", json.dumps(auth, ensure_ascii=False))
            .replace("__SEED_JSON__", json.dumps(seed, ensure_ascii=False)))
    text += "\n\n" + "\n\n".join(tests) + "\n"
    return text, node_map


def _emit_common(signin: tuple | None) -> str:
    text = _HEADER
    if signin:
        labels, button, entry = signin
        user_label = next((l for l in labels
                           if re.search(r"user|email|account", l, re.I)),
                          labels[0])
        pass_label = next((l for l in labels
                           if re.search(r"password", l, re.I)), labels[-1])
        text += (_SIGNIN_HELPER
                 .replace("__SIGNIN_ENTRY__", _ts(entry or "Sign in"))
                 .replace("__SIGNIN_USER__", _ts(user_label))
                 .replace("__SIGNIN_PASS__", _ts(pass_label))
                 .replace("__SIGNIN_BUTTON__", _ts(button)))
    return text


def grep_for(nodes: list[str]) -> str | None:
    """已完成节点的流回归 grep(标题前缀锚定,避开未实现节点的噪音)。"""
    pats = [re.escape(n) + r" flow" for n in nodes if n]
    return "|".join(pats) or None


def merge_flow_fails(report: dict, node_map: dict[str, str]) -> int:
    """run_smoke 报告 → 流失败标题回填 reds(权威级,REQ-1 家族前置)。"""
    title_to_node = {t: n for n, t in node_map.items()}
    flow_reds = []
    for title in list(report.get("fails") or []):
        nid = title_to_node.get(title)
        if nid is None:
            continue
        flow_reds.append({
            "probe": "flow", "string": title, "nodes": [nid],
            "detail": "scenario walk failed — the platform suite is "
                      "flow-shaped; this is a score killer"})
    flow_reds.sort(key=lambda r: (
        0 if (r["nodes"] or [""])[0].startswith("REQ-1") else 1))
    report.setdefault("reds", [])
    report["reds"] = flow_reds + report["reds"]
    report["flow_reds"] = len(flow_reds)
    return len(flow_reds)
