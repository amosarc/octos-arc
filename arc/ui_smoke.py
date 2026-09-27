#!/usr/bin/env python3
"""ui_smoke — 宿主 UI 契约冒烟(requirements-only 任务的浏览器级验证通道).

背景(hackathon--sheet 3 分根因):无 staged specs 的任务,宿主验证止步于
HTTP 层(build/boot/路由探针),模型层又被禁止跑浏览器——六类 UI 契约缺陷
(整链缺失/按钮精确名不符/契约词藏在隐藏 option/对话框关不掉遮罩卡死/
shift 选区塌缩/导出 BOM)全部盲区,24 条需求自评全 [x] 拿 3 分。

本模块补上这条通道,三层:

  collect_contract_strings(tree)  需求树 → 契约串清单
      (双引号逐字串;含 <占位符> 的转前缀锚点;错误消息单独归类;
       每串记来源节点,红项可映射回 REQ)
  scan_built_assets(dist_dir, …)  构建产物逐串存在性检查(必要条件,
      抓整条链缺失类;不 boot 服务器,读 frontend/dist 文件即可)
  stage_ui_smoke / run_smoke      生成并执行通用 Playwright 契约爬虫
      (六类探针,断言宿主写、模型改不了;staged 只读于 .arc/ui-smoke/)

agent 侧同权调用:stage 之后,提示词下发被批准的 UI SMOKE RUN 命令
(`node <cli> test --config .arc/ui-smoke/playwright.config.ts`)——与
SPEC RUN 同构;该命令形状不是内联 node 脚本、不指向 .arc/acceptance、
不在 trace 拦截清单里,内核三道 guard 全部结构性放行(见
crates/octos-agent/src/tools/shell.rs:1508-1717),无需内核改动。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

UI_SMOKE_SUBDIR = ".arc/ui-smoke"
# 静态扫描与爬虫共同的上限:防御性,超长需求树不该把冒烟拖成分钟级。
# 控件类(爬虫输入)与消息类(仅静态层)分开配额——控件占满时消息被挤光
# 会让错误文案类缺陷(sheet 实测:60 条控件串)失去静态覆盖。
MAX_CONTRACT_STRINGS = 60
MAX_MESSAGE_STRINGS = 12

# ---------------------------------------------------------------------------
# 契约串收集
# ---------------------------------------------------------------------------

# 双引号逐字串:首字符大写字母或数字(过滤 "true"/"false"/".csv" 这类非
# 控件文本,保留 "123456" 这类逐字值),最短 3 字符(plan_envelope 的 6
# 字符下限会漏掉 "Save"/"Data"/"Sort"/"Undo"/"Redo"/"Apply" 高频契约词)。
# 引号兼容直引号与弯引号:github 任务需求文本用 “ ”(U+201C/201D),
# sheet 任务用 " —— 两族都要抽(hackathon--github 首测:弯引号 0 命中)。
_QUOTED_RE = re.compile(
    '["\u201c]([A-Z0-9][^"\u201c\u201d\n]{2,120})["\u201d]')
_PLACEHOLDER_RE = re.compile(r"<[^<>]{1,60}>")
# 错误消息启发:标点结尾,或祈使/拒绝句式。消息类串只在静态层查(展示
# 时机在失败路径上,通用爬虫无法触发),爬虫只追控件类。
# 2026-09-27 补:①纯数字/数字为主的短串(验证码形 "123456"/"000000")是
# 展示值不是控件可访问名——按控件猎捕必然误报红(github 二跑实测);
# ②"Password updated"类成功态文案("updated/created/saved")同理。
_MESSAGE_HINT_RE = re.compile(
    r"[.!?:]$|^(Please|Invalid|Error|Could not|Cannot|Must)"
    r"|\bmust\b|\bcannot\b|already exists|failed to|is required"
    r"|is invalid|not satisfied|does not match|not found|not allowed"
    r"|\bdenied\b|already a member|is incorrect"
    r"|\bupdated\b|\bcreated\b|\bsaved\b|\bdeleted\b")
_NUMERIC_CODE_RE = re.compile(r"^[0-9][0-9A-Za-z_-]{3,15}$")


def _classify(quoted: str) -> dict | None:
    """单条引号串 → 契约条目;纯占位符模板返回 None(无静态锚点)。"""
    text = quoted.strip()
    if not text or _PLACEHOLDER_RE.search(text) is None:
        return {"value": text, "kind": "exact",
                "message": bool(_MESSAGE_HINT_RE.search(text))
                or bool(_NUMERIC_CODE_RE.fullmatch(text))}
    # 含 <占位符>:取首个占位符前的文本作前缀锚点("Filter <header text>"
    # → "Filter");前缀为空则尝试尾后缀;两头都是占位符的纯模板放弃。
    parts = _PLACEHOLDER_RE.split(text, maxsplit=1)
    prefix = parts[0].strip() if parts else ""
    if prefix:
        return {"value": prefix, "kind": "prefix", "message": bool(_MESSAGE_HINT_RE.search(text))}
    tail = _PLACEHOLDER_RE.split(text)[-1].strip()
    if tail:
        return {"value": tail, "kind": "suffix", "message": bool(_MESSAGE_HINT_RE.search(text))}
    return None


def collect_contract_strings(tree: dict,
                             max_strings: int = MAX_CONTRACT_STRINGS) -> list:
    """需求树 → 契约串清单(确定性,无 IO)。

    每条:{value, kind: exact|prefix|suffix, message: bool, nodes: [REQ id]}。
    去重合并来源节点;文档序输出;消息类不挤占控件类的上限配额。
    """
    entries: dict[tuple, dict] = {}

    def visit(node) -> None:
        nid = str(node.get("id") or "")
        desc = str(node.get("description") or "")
        for m in _QUOTED_RE.finditer(desc):
            entry = _classify(m.group(1))
            if not entry:
                continue
            key = (entry["kind"], entry["value"])
            if key not in entries:
                entries[key] = {**entry, "nodes": []}
            if nid and nid not in entries[key]["nodes"]:
                entries[key]["nodes"].append(nid)
        for child in node.get("children") or []:
            visit(child)

    visit(tree or {})
    ordered = list(entries.values())
    controls = [e for e in ordered if not e["message"]]
    messages = [e for e in ordered if e["message"]]
    return (controls[:max_strings]
            + messages[:MAX_MESSAGE_STRINGS])


# ---------------------------------------------------------------------------
# 静态扫描:构建产物逐串存在性(必要条件)
# ---------------------------------------------------------------------------

_DIST_TEXT_GLOBS = ("*.html", "assets/*.js", "*.js", "*.css")


def scan_built_assets(dist_dir: Path, entries: list) -> dict:
    """frontend/dist 产物逐串存在性检查。dist 缺失 → {"attempted": False}。

    只证明"串在包里"这一必要条件(exact 用子串、prefix/suffix 用锚点);
    可见性/可点击性由爬虫断言。误报方向是"假绿"(动态拼接的串查不到
    才会假红,而那本身就该被人工看一眼)。
    """
    dist = Path(dist_dir)
    if not dist.is_dir():
        return {"attempted": False, "reason": "no dist"}
    corpus = []
    for pattern in _DIST_TEXT_GLOBS:
        for p in sorted(dist.glob(pattern)):
            try:
                corpus.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    if not corpus:
        return {"attempted": False, "reason": "empty dist"}
    text = "\n".join(corpus)
    reds = []
    for e in entries:
        v = e["value"]
        hit = v in text
        if hit:
            continue
        reds.append({**e, "detail": "string absent from the built frontend "
                                    "bundle (chain likely unimplemented)"})
    return {"attempted": True, "checked": len(entries), "reds": reds,
            "corpus_bytes": len(text)}


# ---------------------------------------------------------------------------
# 爬虫 spec 生成(staged 只读)
# ---------------------------------------------------------------------------

_CONFIG_TS = """\
// playwright.config.ts — HOST-OWNED (read-only, generated).
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  // 与隐藏评测配置同构(10s 断言墙):爬虫自设 test.setTimeout 拉长整测
  // 时限,但每条 expect 必须在评测同款 10s 内成立。
  expect: { timeout: 10_000 },
  use: { baseURL: process.env.E2E_BASE_URL || '__BASE_URL__' },
});
"""

_GITIGNORE = "node_modules\nlast-run.jsonl\ntest-results\n.playwright-report\n"

# staged 目录自我说明:模型见到即知不可改。
_README = """\
# .arc/ui-smoke — HOST-OWNED (read-only)

Host-generated UI contract smoke for this task (arc/ui_smoke.py). The suite
asserts, in a real headless browser: quoted contract strings are openable,
carry their EXACT accessible name, and match exactly ONE visible clickable
(strict mode); a role="grid" renders visible column/row headers plus
coordinate-named gridcells, and a spreadsheet grid is FIXED-SIZE (empty
cells beyond the data region are selectable); dropdowns are native selects
or full ARIA comboboxes (aria-expanded; native <option> labels count as
carriers); named controls are not permanently disabled; dialogs close on
Escape/Cancel; the page never gets stuck behind an overlay; Shift-click
does not collapse selection; downloads are non-empty without a UTF-8 BOM;
and for auth-gated apps the signed-in username is its OWN visible exact
text element and survives reload.

Carrier rule (anti-gaming): a contract string is credited ONLY on
interactive controls or structural name carriers (headings, table cells,
dialog names, aria-labels). Rendering the string as static text (a
span/chip "tour" listing) clears NOTHING — fix the real control.

Sanctioned self-check command (runs against the dev server as-is; the host
suite always filters to contract-smoke.spec.ts, so MY-CHECK never pollutes
it):

    node __CLI__ test --config .arc/ui-smoke/playwright.config.ts contract-smoke.spec.ts

For a ONE-OFF custom check: copy MY-CHECK.spec.ts.example to MY-CHECK.spec.ts,
edit it, and run it with the explicit file argument appended to the command
above. That form is allowed, re-runnable, and green-stop-visible; inline
`node -e` browser scripts stay forbidden.

Reds are host-authored contract failures — fix the app, rerun. If you have
positive evidence a red is a crawler-state artifact (the control works but
needs a state the crawler cannot reach), state that with evidence in your
completion note instead of restructuring correct UI. Do not edit
anything in this directory except your own MY-CHECK file; the host
regenerates the rest.
"""

# 一次性自定义检查的正路模板:改名即用(带 .example 后缀,Playwright 不收集)。
_MY_CHECK_EXAMPLE = """\
// MY-CHECK.spec.ts.example — one-off custom checks belong in THIS form.
// Copy to MY-CHECK.spec.ts, edit, then run with an explicit file argument:
//   node <cli> test --config .arc/ui-smoke/playwright.config.ts MY-CHECK.spec.ts
// That form is allowed (inline `node -e` browser scripts are not),
// re-runnable, and green-stop-visible. The host contract suite never runs
// this file (it filters to contract-smoke.spec.ts).
import { test, expect } from '@playwright/test';

test('my check - name what this verifies', async ({ page }) => {
  await page.goto('/');
  // Example: walk one form chain end to end.
  // await page.getByLabel('Workbook name').fill('Q3 Sales');
  // await page.getByRole('button', { name: 'Create', exact: true }).click();
  // await expect(page.getByText('Q3 Sales')).toBeVisible();
  expect(true).toBe(true);
});
"""


def _contract_for_spec(entries: list, auth: dict | None = None,
                       crawler_cap: int = MAX_CONTRACT_STRINGS) -> dict:
    # 静态扫描吃全量(便宜);爬虫有交互预算,控件串按文档序截断——前段
    # (认证/基础链)优先,PR 深链由静态层兜底。
    exact = [e["value"] for e in entries
             if e["kind"] == "exact" and not e["message"]][:crawler_cap]
    anchors = [{"kind": e["kind"], "value": e["value"]}
               for e in entries
               if e["kind"] in ("prefix", "suffix") and not e["message"]][:crawler_cap]
    return {"exact": exact, "anchors": anchors, "auth": auth}


_SPEC_TEMPLATE_PATH = Path(__file__).resolve().parent / "ui_smoke_spec_template.ts"


def _spec_template() -> str:
    return _SPEC_TEMPLATE_PATH.read_text(encoding="utf-8")


def generate_spec(entries: list, auth: dict | None = None) -> str:
    return _spec_template().replace(
        "__CONTRACT_JSON__",
        json.dumps(_contract_for_spec(entries, auth), ensure_ascii=False))


def generate_config(smoke_port: int) -> str:
    return _CONFIG_TS.replace("__BASE_URL__", f"http://127.0.0.1:{smoke_port}")


def stage_ui_smoke(output_dir: Path, entries: list, smoke_port: int,
                   cli_hint: str, auth: dict | None = None) -> Path:
    """(重新)落盘 .arc/ui-smoke/{spec,config,README,.gitignore}。幂等。

    auth: {"username","password"} 登录前奏用(夹具账号,fixture 链约定
    的 FIXTURE_PASSWORD);无登录门的应用传 None。
    """
    stage = Path(output_dir) / UI_SMOKE_SUBDIR
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "contract-smoke.spec.ts").write_text(
        generate_spec(entries, auth), encoding="utf-8")
    (stage / "playwright.config.ts").write_text(generate_config(smoke_port), encoding="utf-8")
    (stage / "README.md").write_text(_README.replace("__CLI__", cli_hint), encoding="utf-8")
    (stage / "MY-CHECK.spec.ts.example").write_text(
        _MY_CHECK_EXAMPLE, encoding="utf-8")
    (stage / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    for junk in ("test-results", ".playwright-report", "red-shots"):
        shutil.rmtree(stage / junk, ignore_errors=True)
    return stage


def _auth_from_users(users: list, entries: list | None,
                     fixture_password: str) -> dict | None:
    """users 行列表 → {"username","passwords"};空表 → None。"""
    if not users:
        return None
    first = users[0]
    candidates = [fixture_password]
    for e in entries or []:
        v = e["value"]
        if (len(v) >= 8 and any(c.isdigit() for c in v)
                and any(c.isalpha() for c in v) and " " not in v
                and v not in candidates):
            candidates.insert(0, v)
    return {"username": str(first.get("username") or first.get("email") or ""),
            "passwords": candidates[:4]}


def db_login_users(output_dir: Path) -> list:
    """db.json 里的 users 行(登录门存在性判定;读不到返回空表)。"""
    try:
        db = json.loads((Path(output_dir) / "backend" / "data" / "db.json")
                        .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return (db.get("collections") or {}).get("users") or []


def fixture_auth(output_dir: Path, entries: list | None = None,
                 fixture_password: str = "Fixture-password-123!") -> dict | None:
    """db.json 首个种子账号 + 登录候选密码表。

    候选序:①需求文本逐字引用的密码形串(hackathon--github 实测:种子
    密码是 “Valid-password-123!”,不是 FIXTURE_PASSWORD——那次 run 的
    播种用需求引号值);②fixture 链统一 FIXTURE_PASSWORD。爬虫逐个试,
    密码形判定:长度≥8 且含字母数字。

    2026-09-27:不再 `import fixture_manifest`(staging 时机早于 sys.path
    就绪时 ImportError 直接吞成 None);密码经参数传入,users 读不到时
    由调用方用 auth_from_compiled(宿主播种结果)回退。
    """
    return _auth_from_users(db_login_users(output_dir), entries,
                            fixture_password)


def auth_from_compiled(fixture_compiled: dict | None, entries: list | None = None,
                       fixture_password: str = "Fixture-password-123!"
                       ) -> dict | None:
    """宿主播种结果(fixture_manifest.materialize 的返回值)→ 登录前奏。

    db.json 尚未落盘/读取失败时的第二来源:materialize 的 db.users 就是
    即将写入的种子行,时序上永远可用。"""
    users = ((fixture_compiled or {}).get("db") or {}).get("users") or []
    return _auth_from_users(users, entries, fixture_password)


def build_ui_smoke_cmd(cli_path: str | None) -> str:
    """节点/收尾提示词附带的被批准自检命令(与 SPEC RUN 同构)。

    显式限定 contract-smoke.spec.ts:模型自建的同目录 MY-CHECK.spec.ts
    不会混进权威自检;一次性检查走"MY-CHECK + 显式文件参数"正路。"""
    if cli_path:
        cmd = (f"node {cli_path} test --config "
               f".arc/ui-smoke/playwright.config.ts contract-smoke.spec.ts")
    else:
        cmd = ("npx --yes @playwright/test test --config "
               ".arc/ui-smoke/playwright.config.ts contract-smoke.spec.ts")
    return (
        "\nUI SMOKE RUN (sanctioned, host-authored contract suite): run this "
        "any time to self-check the UI contract in a real browser — contract "
        "strings openable, exact-named and STRICT-mode-unique (interactive/"
        "structural carriers only — static text clears nothing); grids "
        "complete AND fixed-size; dropdowns native <select>; named controls "
        "not permanently disabled; dialogs closable; page never stuck; "
        "Shift-click keeps selection; downloads BOM-free; signed-in username "
        "exact-visible across reload (auth-gated apps). Reds are host-"
        "authored failures: fix the real control, then rerun — never surface "
        "contract strings as static text. For a one-off custom check, copy "
        ".arc/ui-smoke/MY-CHECK.spec.ts.example to MY-CHECK.spec.ts and run "
        "it with an explicit file argument — writing your own browser "
        "scripts stays forbidden.\n  " + cmd + "\n")


# ---------------------------------------------------------------------------
# 宿主权威执行(仿 _run_staged_playwright)
# ---------------------------------------------------------------------------

def parse_report(json_stdout: str, last_run: Path | None) -> dict:
    """JSON reporter stdout + last-run.jsonl → {passed, failed, titles, reds}。

    fails=全部失败标题(flow_suite 据此映射回 REQ 节点;titles 仍是
    截断的展示串)。"""
    passed = failed = 0
    titles: list[str] = []
    fails: list[str] = []

    try:
        payload = json.loads(json_stdout[json_stdout.find("{"):])
    except (json.JSONDecodeError, ValueError):
        payload = None
    if payload:
        def walk(suite: dict) -> None:
            nonlocal passed, failed
            for spec in suite.get("specs") or []:
                for t in spec.get("tests") or []:
                    results = t.get("results") or []
                    ok = (t.get("outcome") == "expected"
                          or (results and all(
                              r.get("status") in ("passed", "skipped")
                              for r in results)
                              and any(r.get("status") == "passed" for r in results)))
                    title = str(t.get("title")
                                or spec.get("title") or "untitled")[:160]
                    if ok:
                        passed += 1
                    else:
                        failed += 1
                        fails.append(title)
                        if len(titles) < 8:
                            titles.append(title[:120])
            for sub in suite.get("suites") or []:
                walk(sub)
        for suite in payload.get("suites") or []:
            walk(suite)
    reds: list = []
    if last_run is not None and last_run.is_file():
        try:
            seen: set = set()
            for line in last_run.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                key = (entry.get("probe"), entry.get("string"))
                if key not in seen:
                    seen.add(key)
                    reds.append(entry)
        except (json.JSONDecodeError, OSError, ValueError):
            reds = []
    return {"passed": passed, "failed": failed,
            "titles": " | ".join(titles), "fails": fails, "reds": reds}


def _lg_cli() -> Path | None:
    """local-grader 的 @playwright/test cli.js(容器内 _ensure_host_toolchain 装好)。"""
    here = Path(__file__).resolve().parent
    for root in (here / "local-grader", here.parent / "local-grader"):
        cli = root / "node_modules" / "@playwright" / "test" / "cli.js"
        if cli.is_file():
            return cli
    return None


def _describe_red_shots(run_dir: Path, report: dict, log=print) -> None:
    """红项截图 → VISUAL_MODEL 描述,回填 report["visuals"](best-effort)。

    模型修红项时"看不见"页面;spec 模板在红项命中时截了 red-shots/*.png,
    这里用视觉通道转述成可行动文字。视觉通道未配置/失败 → 静默跳过
    (红项本身已可行动)。上限 2 张、单张 120s(实测带 max 思考的单张
    8-96s,2 张封顶避免挤压 final 修复轮的预算),复用 visual_ref 的通道
    解析与 (sha256, model) 缓存(缓存键即截图相对路径,同图重跑不付费)。
    """
    try:
        import visual_ref
    except ImportError:
        return
    lane = visual_ref.visual_lane()
    if lane is None:
        return
    shots_dir = run_dir / "red-shots"
    if not shots_dir.is_dir():
        return
    model, base_url, api_key = lane
    visuals: list[dict] = []
    for shot in sorted(shots_dir.glob("*.png"))[:2]:
        text = visual_ref._describe_image(
            model, base_url, api_key, shot, timeout=120, log=log)
        if text:
            label = shot.stem[:60]
            visuals.append({"label": label, "text": text})
    if visuals:
        report["visuals"] = visuals
        log(f"[ui-smoke] described {len(visuals)} red screenshot(s) "
            "via the visual lane")


def run_smoke(output_dir: Path, smoke_port: int, entries: list,
              extra_ports: list | None = None, timeout: int = 300,
              auth: dict | None = None,
              fixture_password: str = "Fixture-password-123!",
              grep: str | None = None) -> dict | None:
    """宿主权威执行:从种子起跑自起服务器 → 跑契约爬虫 → 红绿+红项明细。

    auth=None 时自动从 db.json/需求密码串推导(2026-09-26 github 教训:
    本函数曾硬编码重落盘 auth=None 的 spec,final 权威轮退化为未登录爬取,
    33 条登录态字符串全部误报红,把收尾修复循环带偏)。显式传 auth=None
    且 db 无 users 才是"无登录门应用"。
    返回 None = 未尝试(工具链/启动不可用),调用方不得当作失败。
    需要 import 方提供 plan_envelope.rehearsal_boot_env(惰性 import,
    避免包内循环依赖)。
    """
    import plan_envelope  # 惰性:main.py 顶层已 import,这里拿引用

    if auth is None:
        auth = fixture_auth(output_dir, entries, fixture_password)

    cli = _lg_cli()
    if cli is None:
        return None
    # resolve() 到真实路径:CLI 经 symlink 路径加载时,其内部 require 与
    # spec 经 run_dir/node_modules(亦为 symlink)解析出的 '@playwright/test'
    # 会成为两个模块实例("two versions" 立即 0 tests)。容器内是真实目录,
    # 本地联调是软链——统一 realpath 消除这类差异。
    cli = cli.resolve()
    output_dir = Path(output_dir)
    stage_ui_smoke(output_dir, entries, smoke_port, str(cli), auth=auth)
    run_dir = output_dir / UI_SMOKE_SUBDIR
    # config/spec import '@playwright/test' —— 从 run_dir 解析,把
    # local-grader 的 node_modules 链过来(Tier-2 同款手法,.gitignore 兜住)。
    nm = run_dir / "node_modules"
    if not nm.exists():
        try:
            nm.symlink_to(cli.parents[2], target_is_directory=True)
        except OSError:
            pass
    last_run = run_dir / "last-run.jsonl"
    try:
        last_run.unlink()
    except OSError:
        pass

    # 服务器必须从种子起跑:残留 db.run.json 会让状态型断言漂移。
    run_db = output_dir / "backend" / "data" / "db.run.json"
    if run_db.exists():
        try:
            run_db.unlink()
        except OSError:
            pass

    node_bin = shutil.which("node") or "node"
    env = plan_envelope.rehearsal_boot_env(os.environ, smoke_port,
                                           extra_ports or [])
    env["E2E_BASE_URL"] = f"http://127.0.0.1:{smoke_port}"
    # 先清端口再 boot(Tier-2 的 _stop_dev_server 同款语义):宿主 dev
    # server 或上一轮残留占着冒烟口时,npm start 会 EADDRINUSE 崩掉,
    # 而 socket 轮询会被旧进程误判为 boot 成功。
    try:
        subprocess.run(["sh", "-c",
                        f"lsof -ti TCP:{smoke_port} -sTCP:LISTEN"
                        " | xargs kill 2>/dev/null || true"],
                       check=False, timeout=10)
        time.sleep(0.5)
    except (OSError, subprocess.SubprocessError):
        pass
    proc = None
    try:
        boot_log = run_dir / "boot.log"
        with open(boot_log, "w") as fh:
            proc = subprocess.Popen(["npm", "start"], cwd=output_dir / "backend",
                                    env=env, stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", smoke_port), timeout=2):
                        break
                except OSError:
                    if proc.poll() is not None:
                        return None
                    time.sleep(1)
            else:
                return None
        # 权威判定跑契约爬虫 + 场景流套件(flow_suite 生成,平台同构的
        # 交互级走查);模型自建的 MY-CHECK.spec.ts 不进宿主判定。
        # grep(批后流回归):只跑已完成节点的流,未实现节点不产噪音。
        targets = ["contract-smoke.spec.ts"]
        if (run_dir / "flow-suite.spec.ts").is_file():
            targets.append("flow-suite.spec.ts")
        args = [node_bin, str(cli), "test",
                "--config", "playwright.config.ts", *targets,
                "--reporter=json"]
        if grep:
            args += ["--grep", grep]
        r = subprocess.run(
            args,
            cwd=run_dir, capture_output=True, text=True, timeout=timeout,
            errors="replace")
        report = parse_report(r.stdout, last_run)
        # 红项截图 → 视觉描述(有通道才做;失败不影响红项本体)。
        # log 用 print 兜底:run_smoke 无宿主 log 上下文(NameError 教训)。
        _describe_red_shots(run_dir, report, log=print)
        # 红项回填来源节点(修复提示词要指名 REQ)。
        by_value = {}
        for e in entries:
            by_value.setdefault(e["value"], []).extend(e.get("nodes") or [])
        for red_item in report["reds"]:
            v = str(red_item.get("string") or "")
            red_item["nodes"] = by_value.get(v) or by_value.get(
                v.rstrip("*"), [])
        return report
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
