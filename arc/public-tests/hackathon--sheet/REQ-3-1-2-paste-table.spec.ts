import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, openWorkbook, reload, selectCell,
} from './support/e2e';

// REQ-3-1-2 粘贴二维表:Tab 分列、换行分行的文本粘贴到起始单元格,
// 整个矩形生效、保留空字段;刷新后持久。
// headless Chromium 需要显式授予剪贴板权限。

test.beforeEach(async ({ context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
});

const TABLE = 'Item\tQty\nPen\t4\nBook\t7';

test('REQ-3-1-2: paste a 2D table into an empty area', async ({ page }) => {
  await openWorkbook(page);
  await selectCell(page, 'A6');
  await page.evaluate(async (text) => {
    await navigator.clipboard.writeText(text);
  }, TABLE);
  await page.keyboard.press('ControlOrMeta+V');

  await expect(cell(page, 'A6')).toHaveText('Item');
  await expect(cell(page, 'B6')).toHaveText('Qty');
  await expect(cell(page, 'A7')).toHaveText('Pen');
  await expect(cell(page, 'B7')).toHaveText('4');
  await expect(cell(page, 'A8')).toHaveText('Book');
  await expect(cell(page, 'B8')).toHaveText('7');

  await reload(page);
  await expect(cell(page, 'A7')).toHaveText('Pen');
  await expect(cell(page, 'B8')).toHaveText('7');
});

test('REQ-3-1-2: cleanup removes pasted rows', async ({ page }) => {
  await openWorkbook(page);
  await deleteRow(page, 6);
  await deleteRow(page, 6);
  await deleteRow(page, 6);
  await reload(page);
  await expect(cell(page, 'A6')).toHaveText('');
});
