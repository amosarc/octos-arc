"""模型自测骨架:e2e/ 的 playwright config + MY-CHECK 示例 + 宿主
可达性行走 spec 落盘。

宿主只提供最小骨架(非规则、非用例)——config 是最顺手的标准跑法
(双绿自动停机),Playwright 用法本身不设限。_host-walk 是宿主唯一的
自造 spec:零任务知识地行走首页+内链(有种子凭据时加登录腿),断言
零 pageerror(任何需求下都是 bug 的物理事实),实体可见性/登录态丢失
一律 report-only 落 walk-report.json——判定权在模型(需求原文在它
手里),平台 e2e 唯一终裁。幂等。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


# 与 main.py 的 _PAGE_HEALTH_SPEC 同款行走逻辑;实体名从同目录
# _host-walk.json 读(宿主在开工时由 fixture 编译产出,携带种子登录
# 凭据时附加登录腿)。登录腿=传感非门:发现只落 walk-report.json
# 与控制台,判定权在模型(需求原文在它手里)。
_HOST_WALK_SPEC = """\
// _host-walk.spec.ts — HOST-OWNED reachability walk (you do not edit it;
// your own specs live beside it). Two legs, one asserted fact:
//   asserted (both legs contribute): zero uncaught page errors on any
//     visited page — a page that throws is a bug under every requirement,
//     so this alone stays a gate.
//   reported (never asserted — the requirement text, not this walk,
//     decides): staged seed entity names not seen on the signed-out walk,
//     and — when _host-walk.json carries seed login credentials — which
//     names the signed-in pages no longer show. Findings land in
//     walk-report.json and the console; you judge and act.
// The report ledger unions the staged names with a runtime scan of
// backend/data/db.run.json (fallback db.json), so entities seeded later
// in the round are covered too.
// Run it with:  sh scripts/run-e2e.sh _host-walk.spec.ts
import { readFileSync, writeFileSync } from 'fs';
import { test, expect } from '@playwright/test';
import cfgRaw from './_host-walk.json';

const cfg: any = cfgRaw;
const ENTITIES: string[] = Array.isArray(cfg) ? cfg : (cfg.names || []);
const LOGIN = (!Array.isArray(cfg) && cfg.login) ? cfg.login : null;

const ledgerScan = (): string[] => {
  const names: string[] = [];
  let data: any = null;
  for (const p of ['../backend/data/db.run.json', '../backend/data/db.json']) {
    try { data = JSON.parse(readFileSync(p, 'utf8')); break; } catch { /* next */ }
  }
  const cols = (data && data.collections) || {};
  for (const rows of Object.values(cols) as any[][]) {
    if (!Array.isArray(rows)) continue;
    let taken = 0;
    for (const r of rows) {
      if (!r || typeof r !== 'object') continue;
      const v = String(r.displayName || r.name || r.title || '').trim();
      if (v.length >= 5 && !names.includes(v)) { names.push(v); taken += 1; }
      if (taken >= 4) break;
    }
  }
  return names.slice(0, 40);
};
const LEDGER: string[] = Array.from(new Set([...ENTITIES, ...ledgerScan()]));
const has = (seen: string[], n: string) =>
  seen.some((t) => t.toLowerCase().includes(n.toLowerCase()));

const legErrors: Record<string, string[]> = { out: [], inn: [] };
let leg = 'out';

const walkPages = async (page: any): Promise<string[]> => {
  const seen: string[] = [];
  const grab = async () => seen.push(String(await page.evaluate(
    () => (document.body ? document.body.innerText : ''))).slice(0, 6000));
  await page.goto('/');
  await page.waitForTimeout(1200);
  await grab();
  const links = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href^="#/"]'))
      .map((a) => (a as HTMLAnchorElement).getAttribute('href') || '')
      .filter((h, i, arr) => h && arr.indexOf(h) === i)
      .slice(0, 12));
  for (const href of links) {
    await page.evaluate((h) => { location.hash = String(h).slice(1); }, href);
    await page.waitForTimeout(600);
    await grab();
  }
  return seen;
};

// UI-generic sign-in: home -> Sign in entry -> fill the form that owns
// the password field (identifier = its non-password input; submit = its
// submit button). Scoping to the password's form keeps header search
// boxes from stealing the identifier slot. Returns '' on success.
const trySignIn = async (page: any, identArg: string, pwArg: string): Promise<string> => {
  try {
    await page.goto('/');
    await page.waitForTimeout(800);
    const link = page.getByRole('link', { name: 'Sign in' });
    const btn = page.getByRole('button', { name: 'Sign in' });
    if (await link.count()) await link.first().click({ timeout: 4000 });
    else if (await btn.count()) await btn.first().click({ timeout: 4000 });
    else return 'no sign-in entry on the home page';
    await page.waitForTimeout(600);
    const form = page.locator('form',
      { has: page.locator('input[type=password]') });
    const pwBox = page.locator('input[type=password]').first();
    await pwBox.fill(pwArg, { timeout: 4000 });
    let ident = form.locator('input:not([type=password])').first();
    if (!(await ident.count()))
      ident = page.locator('input:not([type=password]):visible').first();
    await ident.fill(identArg, { timeout: 4000 });
    const submitBtn = form.locator('button[type=submit]').first();
    const anyBtn = form.getByRole('button').first();
    if (await submitBtn.count()) await submitBtn.click({ timeout: 4000 });
    else if (await anyBtn.count()) await anyBtn.click({ timeout: 4000 });
    else await page.keyboard.press('Enter');
    await page.waitForTimeout(1800);
    if (await page.locator('input[type=password]:visible').count())
      return 'still on the sign-in page after submit';
    return '';
  } catch (e: any) {
    return String((e && e.message) || e).slice(0, 120);
  }
};

test('host walk - no page errors, seed entities reachable', async ({ page }) => {
  // 双腿(登出行走+登录传感)天然长于单场景:本测试单独放宽到 30s,
  // config 的 10s 仍是模型自写 spec 的评分器语义,不动。
  test.setTimeout(30_000);
  await page.addInitScript(() => {
    (window as any).__phErrors = [];
    window.addEventListener('unhandledrejection', (e) => {
      (window as any).__phErrors.push(
        'unhandledrejection: ' + String((e as any)?.reason ?? e));
    });
  });
  page.on('pageerror', (e) => legErrors[leg].push(
    'pageerror: ' + String(e?.message ?? e)));
  const seenOut = await walkPages(page);
  const injectedOut = await page.evaluate(() => (window as any).__phErrors || []);
  const missing = ENTITIES.filter((n) => !has(seenOut, n));

  // leg 2 — signed-in sensor (REPORT-ONLY: findings never fail this test)
  let seenIn: string[] = [];
  let loginStatus = 'skipped (no seed credentials staged)';
  if (LOGIN && LOGIN.username) {
    leg = 'inn';
    let reason = await trySignIn(page, LOGIN.username, LOGIN.password || '');
    if (reason && LOGIN.email && LOGIN.email !== LOGIN.username)
      reason = await trySignIn(page, LOGIN.email, LOGIN.password || '');
    if (!reason) {
      loginStatus = 'ok';
      seenIn = await walkPages(page);
    } else {
      loginStatus = 'failed: ' + reason;
    }
    leg = 'out';
  }
  const signedInLost = (seenIn.length || loginStatus === 'ok')
    ? LEDGER.filter((n) => has(seenOut, n) && !has(seenIn, n)) : [];
  const nowhere = LEDGER.filter(
    (n) => !has(seenOut, n) && !has(seenIn, n));
  try {
    writeFileSync('walk-report.json', JSON.stringify({
      checked: LEDGER, missing, signed_in_lost: signedInLost,
      nowhere, login: loginStatus,
      leg2_page_errors: legErrors.inn }));
  } catch { /* best effort */ }
  if (signedInLost.length)
    console.log('[host-walk] signed-in pages no longer show (report-only, '
      + 'judge against the requirement text): ' + signedInLost.join(', '));
  if (missing.length)
    console.log('[host-walk] staged seed names not visible anywhere on '
      + 'the signed-out walk (report-only, judge against the requirement '
      + 'text): ' + missing.join(', '));
  if (loginStatus.startsWith('failed'))
    console.log('[host-walk] sign-in attempt ' + loginStatus);
  if (legErrors.inn.length)
    console.log('[host-walk] signed-in leg page errors (report-only): '
      + legErrors.inn.slice(0, 3).join(' | '));

  // 唯一的门:行走页面零未捕获异常(任何需求下都是 bug 的物理事实)。
  // 实体可见性不再断言——名单失明时旧门给过假绿(github-42 案),
  // 事实降报告,判定权归模型与平台 e2e。
  const pageErrs = legErrors.out.concat(injectedOut);
  expect(pageErrs, 'page walk: ' + pageErrs.slice(0, 6).join(' | '))
    .toEqual([]);
});
"""


def stage_selftest_dir(output_dir: Path, smoke_port: int,
                       subdir: str = "e2e",
                       entity_names: list[str] | None = None,
                       login: dict | None = None) -> Path:
    """落盘模型自测所需的最小骨架(非规则、非用例):playwright config +
    MY-CHECK 示例。该 config 是最顺手的标准跑法(双绿自动停机);
    Playwright 用法本身不再受限。幂等。

    login={username, email, password} 时写入 _host-walk.json,行走 spec
    附加登录腿(传感非门);无凭据时 login=null,腿自动跳过。"""
    stage = Path(output_dir) / subdir
    stage.mkdir(parents=True, exist_ok=True)
    cfg = ("import { defineConfig } from '@playwright/test';\n"
           "export default defineConfig({ testDir: '.',\n"
           "  fullyParallel: false, workers: 1,\n"
           "  retries: 0,\n"
           "  timeout: 10_000, expect: { timeout: 10_000 },\n"
           f"  use: {{ baseURL: 'http://127.0.0.1:{smoke_port}', "
           "headless: true, screenshot: 'off', trace: 'off' } });\n")
    (stage / "playwright.config.ts").write_text(cfg, encoding="utf-8")
    example = """\
// MY-CHECK.spec.ts.example — write YOUR OWN spec from the requirement
// text (the node's scenarios are the oracle), then run it with an
// explicit file argument:
//   node <cli> test --config e2e/playwright.config.ts MY-CHECK.spec.ts
// That form is the convenient standard (re-runnable and green-stop-visible);
// any other Playwright invocation style is yours to choose as well.
//
// LOCATOR RULE — requirement phrasing maps to ROLES, exactly:
//   "A region named X"      => getByRole('region', { name: X })
//   "A dialog named X"      => getByRole('dialog', { name: X })
//   "a combo box labeled X" => getByRole('combobox', { name: X })
// getByText/getByLabel are NOT role equivalents: a <div aria-label> has
// no region role, so a getByLabel assertion on it is green while the
// grader's getByRole('region') times out (an entire family was lost
// exactly that way). Assert with the ROLE the requirement names.
//
// DIALOG-NAME RULE — a surface the text opens from a named control and
// then calls "its dialog" / "its panel" carries that control's quoted
// name as its own accessible name: the grader scopes every control
// inside it to getByRole('dialog', { name: X }), so a differently-named
// dialog is unreachable and everything in it times out (a whole family
// died to one wrong name). In your spec, open the surface and address
// its fields exactly that way.
//
// OPEN-PRESENT RULE — every control the text names as part of a dialog
// or panel is present the moment that surface opens — never behind a
// mode switch, expander, or second button the text does not name: the
// grader performs only the steps the text describes, and an unnamed
// intermediate click is a wall (a hidden combo box timed out scenarios
// while the feature itself worked). If your spec needs a click the
// text never mentions, fix the APP, never the spec.
//
// CELL-TEXT RULE — UI embedded in a value-bearing container (grid cell,
// table row, list item) must not change that container's displayed
// text: carry the marker's accessible name with aria-label, keep its
// visible label out of the container's text content, AND give the
// marker a CSS-sized visible box — an aria-label-only empty button is
// zero-size and reads as invisible (two runs died on opposite horns
// of exactly this contract). A "still displays X" assertion must read
// exactly X. After embedding any control inside a container, re-assert
// the container's full text.
//
// MAIN-LANDMARK RULE — when the requirement says a link or control is
// "on the home page" / "on the <X> page", the grader looks for it INSIDE
// the page's <main> landmark: getByRole('main').getByRole('link', ...).
// Entry links the text names (e.g. "Sign in", "Sign up", "Forgot
// password" for signed-out visitors) must render inside <main>'s
// content — header chrome does not count (24 scenarios were lost to
// exactly this: links lived in the header, main-scoped lookups timed
// out). One control, one accessible name: never duplicate the link in
// both header and main.
//
// FIELD CHECKLIST — every “quoted name” in the node's text names a
// control you must render and assert (getByRole/getByLabel with the
// exact name, scoped to main for entry links); every `backticked
// value` is data to enter or seed verbatim; every "the exact message"
// is an exact-text assertion; every "the page address identifies X" is
// a toHaveURL(/X/) assertion; every "after reload" clause re-asserts
// post page.reload(). Enumerate them from the text BEFORE writing the
// spec — a name you cannot assert is a missing field: fix the APP.
//
// GESTURE RULE — drive every selection-driven flow with BOTH drag AND
// click+Shift-click, every dialog flow with mouse AND keyboard (Enter
// commits, Escape cancels), and every combo box BOTH ways: selectOption()
// AND click-open + click on a role=option (the kit's NativeSelect
// supports both) — whichever gesture a flow never exercises is the one
// the grader may use.
//
// SOURCE RULE — the spec is derived from the requirement text, never from
// the implementation: quote dialog names, labels, and error strings
// verbatim from the scenarios. If the app and the text disagree, the text
// wins — fix the app, never edit the spec to match what you built.
//
// PAGE-ERROR RULE — a CLI Playwright run does NOT fail when the page
// itself throws: uncaught pageerror / unhandledrejection land only in
// the browser console, so a crashed submit handler looks like
// "element not found" and an entire error-path family can be lost that
// way. The collectors + final expect below turn them red — keep them in
// every spec you write, register at the top, assert at the end.
//
// WALK RULE — reach every page the way a first-time user does: from the
// home page, in the session state the scenario starts in (sign in through
// the UI when the GIVEN says signed in), by clicking visible links and
// tabs; page.goto is only for the initial load. Each family keeps one
// signed-in spec that walks from the home page to every kind of entity
// it serves — a page only a typed URL reaches is a page the grader
// never sees. Derive walk coverage from the requirement text itself:
// every entity kind, session state, and entry the scenarios mention
// gets a walk in your own specs — the host walk checks only seed names.
//
// SCREENSHOT RULE — after a flow goes green, capture the pages it
// touches (decide yourself which pages are worth evidence):
//   await page.screenshot({ path: 'octos/screenshots/<flow>-<page>.png',
//                           fullPage: true });
// (create the folder if needed; fullPage so below-the-fold navigation
// is in the picture). The host attaches your latest captures back to
// you at the batch tail for a visual nav check — what you capture is
// what gets checked; no captures, no check.
import { test, expect } from '@playwright/test';

test('my check - name what this verifies', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (e) => pageErrors.push(String(e?.message ?? e)));
  await page.addInitScript(() => {
    (window as any).__rejections = [];
    window.addEventListener('unhandledrejection', (e) => {
      (window as any).__rejections.push(String((e as any)?.reason ?? e));
    });
  });
  await page.goto('/');
  // Example: walk one form chain end to end (use the requirement's OWN
  // quoted labels and values — these placeholders are shapes, not content).
  // await page.getByLabel('Name').fill('Demo entry');
  // await page.getByRole('button', { name: 'Create', exact: true }).click();
  // await expect(page.getByText('Demo entry')).toBeVisible();
  //
  // Example: a "region named" contract asserts with the ROLE:
  // const editor = page.getByRole('region', { name: 'Editor' });
  // await expect(editor.getByRole('combobox', { name: 'Rows' })).toBeVisible();
  //
  // Example: the second selection gesture (click anchor, Shift-click end)
  // on whatever multi-select surface the requirement names:
  // await page.getByRole('option', { name: 'First item', exact: true }).click();
  // await page.getByRole('option', { name: 'Third item', exact: true })
  //           .click({ modifiers: ['Shift'] });
  // await expect(page.getByRole('option', { name: 'Second item', exact: true }))
  //           .toHaveAttribute('aria-selected', 'true');
  //
  // Example: one combo box, BOTH gestures (neutral names here — use the
  // requirement's own labels in your real spec):
  // const box = page.getByRole('combobox', { name: 'Field' });
  // await box.selectOption('Alpha');
  // await box.click();
  // await page.getByRole('option', { name: 'Alpha', exact: true }).click();
  const rejections: string[] =
    await page.evaluate(() => (window as any).__rejections || []);
  expect(pageErrors.concat(rejections),
         'uncaught page errors — fix the app, not this assertion')
    .toEqual([]);
});
"""
    (stage / "MY-CHECK.spec.ts.example").write_text(example, encoding="utf-8")
    # 宿主可达性行走:spec + 实体名/登录凭据 fixture(来自需求 fixture
    # 编译;空清单时 walk 退化为纯 page-error 检查,仍然有效)。
    names = [str(n) for n in (entity_names or []) if str(n).strip()]
    (stage / "_host-walk.json").write_text(
        json.dumps({"names": names, "login": login},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (stage / "_host-walk.spec.ts").write_text(
        _HOST_WALK_SPEC, encoding="utf-8")
    # REQ-6 doom 根因的暂存期闭环:e2e/ 向上解析不到 backend/node_modules
    # 里的 @playwright/test(评测器不踩——它用镜像自带引擎;踩的是模型
    # 自测与任何从 e2e/ 发起的 playwright 调用)。相对软链在 backend
    # 依赖安装完成前悬空无害,装好即通;脚本与手搓调用都被覆盖。
    try:
        link = stage / "node_modules"
        if not link.is_symlink() and not link.exists():
            link.symlink_to(Path("../backend/node_modules"),
                            target_is_directory=True)
    except OSError:
        pass  # 桥接失败不挡 staging;run-e2e.sh 运行时会重试
    (stage / ".gitignore").write_text(
        "test-results/\n.playwright-report/\nwalk-report.json\n"
        "node_modules/\n",
        encoding="utf-8")
    for junk in ("test-results", ".playwright-report"):
        shutil.rmtree(stage / junk, ignore_errors=True)
    return stage
