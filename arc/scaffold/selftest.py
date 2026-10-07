"""模型自测骨架:e2e/ 的 playwright config + MY-CHECK 示例 + 宿主
可达性行走 spec 落盘。

宿主只提供最小骨架(非规则、非用例)——config 是最顺手的标准跑法
(双绿自动停机),Playwright 用法本身不设限。_host-walk 是宿主唯一的
自造 spec:零任务知识地行走首页+内链,断言零 pageerror 且种子实体名
可见——与 final 链的 page-health walk 同款,把"评分器能不能看到东西"
变成批内就能跑绿的门。幂等。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


# 与 main.py 的 _PAGE_HEALTH_SPEC 同款行走逻辑;实体名从同目录
# _host-walk.json 读(宿主在开工时由 fixture 编译产出)。
_HOST_WALK_SPEC = """\
// _host-walk.spec.ts — HOST-OWNED reachability walk (you do not edit it;
// your own specs live beside it). It loads the app, walks the home page
// plus every internal hash link, and asserts: zero uncaught page errors
// AND every seeded top-level entity name from _host-walk.json visible in
// some visited page's text — the names the eval grader looks for.
// Run it with:  sh scripts/run-e2e.sh _host-walk.spec.ts
import { writeFileSync } from 'fs';
import { test, expect } from '@playwright/test';
import entities from './_host-walk.json';

const errors: string[] = [];

test('host walk - no page errors, seed entities reachable', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__phErrors = [];
    window.addEventListener('unhandledrejection', (e) => {
      (window as any).__phErrors.push(
        'unhandledrejection: ' + String((e as any)?.reason ?? e));
    });
  });
  page.on('pageerror', (e) => errors.push('pageerror: ' + String(e?.message ?? e)));
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
  const injected = await page.evaluate(() => (window as any).__phErrors || []);
  const missing = (entities as string[]).filter(
    (n) => !seen.some((t) => t.toLowerCase().includes(n.toLowerCase())));
  try {
    writeFileSync('walk-report.json',
      JSON.stringify({ checked: entities, missing }));
  } catch { /* best effort */ }
  const all = errors.concat(
    injected,
    missing.map((m) => 'seed entity not visible anywhere on the walk: ' + m));
  expect(all, 'page walk: ' + all.slice(0, 6).join(' | ')).toEqual([]);
});
"""


def stage_selftest_dir(output_dir: Path, smoke_port: int,
                       subdir: str = "e2e",
                       entity_names: list[str] | None = None) -> Path:
    """落盘模型自测所需的最小骨架(非规则、非用例):playwright config +
    MY-CHECK 示例。该 config 是最顺手的标准跑法(双绿自动停机);
    Playwright 用法本身不再受限。幂等。"""
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
    # 宿主可达性行走:spec + 实体名 fixture(来自需求 fixture 编译;
    # 空清单时 walk 退化为纯 page-error 检查,仍然有效)。
    names = [str(n) for n in (entity_names or []) if str(n).strip()]
    (stage / "_host-walk.json").write_text(
        json.dumps(names, ensure_ascii=False) + "\n", encoding="utf-8")
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
