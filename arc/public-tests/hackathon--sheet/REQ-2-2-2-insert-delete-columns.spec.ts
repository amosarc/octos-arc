import { expect, test } from '@playwright/test';
import {
  cell, openColumnMenu, openWorkbook,
} from './support/e2e';

// REQ-2-2-2 列结构:列头菜单提供 "Insert 1 column left"/"Insert 1
// column right"/"Delete column";插入时目标列及其右整列数据右移。

test('REQ-2-2-2: insert 1 column left shifts columns right, delete shifts back', async ({ page }) => {
  await openWorkbook(page);
  await expect(cell(page, 'B1')).toHaveText('Sales');
  await expect(cell(page, 'B2')).toHaveText('1200');

  await openColumnMenu(page, 'B');
  await page.getByRole('menuitem', { name: 'Insert 1 column left', exact: true })
    .click();
  // 原 B 列(Sales/1200)整体右移到 C,新 B 列为空
  await expect(cell(page, 'C1')).toHaveText('Sales');
  await expect(cell(page, 'C2')).toHaveText('1200');
  await expect(cell(page, 'B1')).toHaveText('');

  await openColumnMenu(page, 'B');
  await page.getByRole('menuitem', { name: 'Delete column', exact: true })
    .click();
  await expect(cell(page, 'B1')).toHaveText('Sales');
  await expect(cell(page, 'B2')).toHaveText('1200');
});
