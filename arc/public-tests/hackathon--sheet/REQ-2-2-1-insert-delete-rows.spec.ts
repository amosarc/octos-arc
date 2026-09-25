import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, openRowMenu, openWorkbook,
} from './support/e2e';

// REQ-2-2-1 行结构:行号菜单提供 "Insert 1 row above"/"Insert 1 row
// below"/"Delete row";插入时目标行及其后整行记录整体下移,删除时上移。

test('REQ-2-2-1: insert 1 row above shifts records down, delete shifts back', async ({ page }) => {
  await openWorkbook(page);
  await expect(cell(page, 'A2')).toHaveText('East');
  await expect(cell(page, 'B2')).toHaveText('1200');

  await openRowMenu(page, 2);
  await page.getByRole('menuitem', { name: 'Insert 1 row above', exact: true })
    .click();
  // 原第 2 行(East/1200/Open)整体下移到第 3 行,新第 2 行为空
  await expect(cell(page, 'A2')).toHaveText('');
  await expect(cell(page, 'A3')).toHaveText('East');
  await expect(cell(page, 'B3')).toHaveText('1200');
  await expect(cell(page, 'C3')).toHaveText('Open');
  await expect(cell(page, 'A4')).toHaveText('North');

  await deleteRow(page, 2);
  await expect(cell(page, 'A2')).toHaveText('East');
  await expect(cell(page, 'B2')).toHaveText('1200');
  await expect(cell(page, 'A3')).toHaveText('North');
});
