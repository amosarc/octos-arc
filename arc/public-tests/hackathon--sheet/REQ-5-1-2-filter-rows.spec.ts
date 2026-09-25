import { expect, test } from '@playwright/test';
import {
  cell, dataMenuItem, openWorkbook,
} from './support/e2e';

// REQ-5-1-2 筛选:"Data" 菜单 → "Create filter";每个表头提供
// "Filter <header>" 按钮;值筛选对话框提供 "Clear selection"、以源值
// 为可访问名的复选框与 "Apply";条件筛选提供 "Condition" 组合框
// ("Text contains"/"Greater than"/... )与 "Value" 文本框;筛选只改
// 可见性。测试结束恢复全部行可见。

test('REQ-5-1-2: value filter hides rows without deleting them', async ({ page }) => {
  await openWorkbook(page);
  await dataMenuItem(page, 'Create filter');

  await page.getByRole('button', { name: 'Filter Status' }).click();
  const dialog = page.getByRole('dialog', { name: 'Filter Status' });
  await dialog.getByRole('checkbox', { name: 'Closed' }).uncheck();
  await dialog.getByRole('button', { name: 'Apply', exact: true }).click();

  // North(Closed) 行隐藏但仍在;East/South(Open) 行可见
  await expect(cell(page, 'A3')).not.toBeVisible();
  await expect(cell(page, 'A2')).toBeVisible();
  await expect(cell(page, 'A4')).toBeVisible();

  // 还原:重新勾选 Closed
  await page.getByRole('button', { name: 'Filter Status' }).click();
  await page.getByRole('dialog', { name: 'Filter Status' })
    .getByRole('checkbox', { name: 'Closed' }).check();
  await page.getByRole('dialog', { name: 'Filter Status' })
    .getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(cell(page, 'A3')).toBeVisible();
});

test('REQ-5-1-2: condition filter Greater than 750 on Sales', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('button', { name: 'Filter Sales' }).click();
  const dialog = page.getByRole('dialog', { name: 'Filter Sales' });
  await dialog.getByLabel('Condition').selectOption({ label: 'Greater than' });
  await dialog.getByLabel('Value').fill('750');
  await dialog.getByRole('button', { name: 'Apply', exact: true }).click();

  // South(700) 隐藏,East(1200)/North(800) 可见
  await expect(cell(page, 'A4')).not.toBeVisible();
  await expect(cell(page, 'A2')).toBeVisible();
  await expect(cell(page, 'A3')).toBeVisible();

  // 还原:Clear selection 后全选应用(或直接全选)
  await page.getByRole('button', { name: 'Filter Sales' }).click();
  const restore = page.getByRole('dialog', { name: 'Filter Sales' });
  await restore.getByRole('button', { name: 'Clear selection' }).click();
  await restore.getByRole('checkbox', { name: '700' }).check();
  await restore.getByRole('checkbox', { name: '800' }).check();
  await restore.getByRole('checkbox', { name: '1200' }).check();
  await restore.getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(cell(page, 'A4')).toBeVisible();
});
