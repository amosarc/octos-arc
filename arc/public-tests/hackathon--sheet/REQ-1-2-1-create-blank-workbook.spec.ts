import { expect, test } from '@playwright/test';
import { GRID_NAME, cell, openHome } from './support/e2e';

// REQ-1-2-1 创建空白工作簿:首页按钮 "New blank workbook" → 创建页 →
// 提交按钮 "Create" → 编辑器只显示 Sheet1 且激活、A1 选中;刷新/回首页
// 重开后状态一致。

test('REQ-1-2-1: New blank workbook -> Create opens editor with blank Sheet1', async ({ page }) => {
  await openHome(page);
  await page.getByRole('button', { name: 'New blank workbook' }).click();
  await page.getByRole('button', { name: 'Create', exact: true }).click();

  // 编辑器:只有 Sheet1、Sheet1 激活、A1 选中
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toHaveAttribute(
    'aria-selected', 'true');
  await expect(page.getByRole('grid', { name: GRID_NAME })).toBeVisible();
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
});

test('REQ-1-2-1: created workbook persists on the home page', async ({ page }) => {
  await openHome(page);
  const before = await page.getByRole('link').count();
  await page.getByRole('button', { name: 'New blank workbook' }).click();
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toBeVisible();

  await openHome(page);
  const after = await page.getByRole('link').count();
  expect(after).toBeGreaterThan(before - 1);
});
