import { expect, test } from '@playwright/test';
import {
  cell, dataMenuItem, dragSelect, openWorkbook, reload,
} from './support/e2e';

// REQ-5-1-1 排序:选中矩形数据区 → "Data" 菜单 → "Sort range" 对话框
// ("Sort by"/"Order" 组合框、"Data has header row" 复选框、"Sort"
// 按钮);"Sort by" 选项以表头文本为可访问名;声明表头后首行不参与;
// 整行记录一起移动。用升序打乱后再降序还原。

test('REQ-5-1-1: sort A1:C4 by Sales ascending moves whole records', async ({ page }) => {
  await openWorkbook(page);
  await dragSelect(page, 'A1', 'C4');

  await dataMenuItem(page, 'Sort range');
  const dialog = page.getByRole('dialog', { name: 'Sort range' });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Sort by').selectOption({ label: 'Sales' });
  await dialog.getByLabel('Order').selectOption({ label: 'Ascending' });
  await dialog.getByLabel('Data has header row').check();
  await dialog.getByRole('button', { name: 'Sort', exact: true }).click();

  // 升序:South(700) → North(800) → East(1200),整行一起移动
  await expect(cell(page, 'A2')).toHaveText('South');
  await expect(cell(page, 'B2')).toHaveText('700');
  await expect(cell(page, 'C2')).toHaveText('Open');
  await expect(cell(page, 'A3')).toHaveText('North');
  await expect(cell(page, 'A4')).toHaveText('East');
  // 表头不动
  await expect(cell(page, 'A1')).toHaveText('Region');

  await reload(page);
  await expect(cell(page, 'A2')).toHaveText('South');

  // 还原种子顺序:降序 = East/North/South
  await dragSelect(page, 'A1', 'C4');
  await dataMenuItem(page, 'Sort range');
  await page.getByRole('dialog', { name: 'Sort range' })
    .getByLabel('Sort by').selectOption({ label: 'Sales' });
  await page.getByRole('dialog', { name: 'Sort range' })
    .getByLabel('Order').selectOption({ label: 'Descending' });
  await page.getByRole('dialog', { name: 'Sort range' })
    .getByLabel('Data has header row').check();
  await page.getByRole('dialog', { name: 'Sort range' })
    .getByRole('button', { name: 'Sort', exact: true }).click();
  await expect(cell(page, 'A2')).toHaveText('East');
  await expect(cell(page, 'A3')).toHaveText('North');
  await expect(cell(page, 'A4')).toHaveText('South');
});
