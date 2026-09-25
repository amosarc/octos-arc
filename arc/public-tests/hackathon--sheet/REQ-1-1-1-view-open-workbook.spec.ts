import { expect, test, type Page } from '@playwright/test';
import {
  GRID_NAME, WORKBOOK, cell, formulaBar, openHome, openWorkbook, reload,
} from './support/e2e';

// REQ-1-1-1 查看与打开工作簿:首页记录显示 "Last updated: <值>"、
// 可访问名为工作簿名的链接;编辑器显示同一 Last updated 值、工作簿名、
// 页签与顺序、网格值;编辑器 URL 刷新后仍恢复同一工作簿。

const norm = (s: string | null) => (s ?? '').replace(/\s+/g, ' ').trim();

async function checkFormulaBarEchoes(page: Page): Promise<void> {
  await cell(page, 'A1').click();
  await expect(formulaBar(page)).toHaveValue('Region');
  await cell(page, 'B2').click();
  await expect(formulaBar(page)).toHaveValue('1200');
}

test('REQ-1-1-1: home lists Q3 Sales with Last updated and opens the editor', async ({ page }) => {
  await openHome(page);
  const link = page.getByRole('link', { name: WORKBOOK, exact: true });
  await expect(link).toBeVisible();
  const homeUpdated = norm(
    await page.getByText(/Last updated:/).first().textContent());
  expect(homeUpdated).toMatch(/Last updated:/i);

  await link.click();
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toBeVisible();
  const editorUpdated = norm(
    await page.getByText(/Last updated:/).first().textContent());
  expect(editorUpdated).toBe(homeUpdated);
});

test('REQ-1-1-1: editor exposes ARIA tab/grid contract and seeded values', async ({ page }) => {
  await openWorkbook(page);
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toHaveAttribute(
    'aria-selected', 'true');
  const grid = page.getByRole('grid', { name: GRID_NAME });
  await expect(grid).toHaveAttribute('aria-multiselectable', 'true');
  await expect(cell(page, 'A1')).toHaveText('Region');
  await checkFormulaBarEchoes(page);
});

test('REQ-1-1-1: direct editor URL restores the same workbook after refresh', async ({ page }) => {
  await openWorkbook(page);
  const url = page.url();
  await reload(page);
  expect(page.url()).toBe(url);
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toHaveAttribute(
    'aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveText('Region');
  await openHome(page);
  await expect(page.getByRole('link', { name: WORKBOOK, exact: true })).toBeVisible();
});
