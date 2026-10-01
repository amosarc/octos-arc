"""模型自测骨架:e2e/ 的 playwright config + MY-CHECK 示例落盘。

宿主只提供最小骨架(非规则、非用例)——config 是最顺手的标准跑法
(双绿自动停机),Playwright 用法本身不设限。幂等。
"""
from __future__ import annotations

import shutil
from pathlib import Path


def stage_selftest_dir(output_dir: Path, smoke_port: int,
                       subdir: str = "e2e") -> Path:
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
// GESTURE RULE — self-test each selection-driven flow with BOTH drag AND
// click+Shift-click (graders use both; a shift-collapsing handler passed
// drag-only tests and still lost the whole family), and each dialog flow
// with mouse AND keyboard (Enter commits, Escape cancels).
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
  const rejections: string[] =
    await page.evaluate(() => (window as any).__rejections || []);
  expect(pageErrors.concat(rejections),
         'uncaught page errors — fix the app, not this assertion')
    .toEqual([]);
});
"""
    (stage / "MY-CHECK.spec.ts.example").write_text(example, encoding="utf-8")
    (stage / ".gitignore").write_text(
        "test-results/\n.playwright-report/\n", encoding="utf-8")
    for junk in ("test-results", ".playwright-report"):
        shutil.rmtree(stage / junk, ignore_errors=True)
    return stage
