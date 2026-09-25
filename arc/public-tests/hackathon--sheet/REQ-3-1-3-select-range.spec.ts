import { expect, test } from '@playwright/test';
import {
  GRID_NAME, cell, cellAriaSelected, dragSelect, openWorkbook,
} from './support/e2e';

// REQ-3-1-3 矩形选区:从一角拖到对角选中连续矩形;网格
// aria-multiselectable="true";矩形内每个 gridcell aria-selected=
// "true",矩形外 "false";新选择替换旧选择。

test('REQ-3-1-3: drag select marks exactly the rectangle via aria-selected', async ({ page }) => {
  await openWorkbook(page);
  await expect(page.getByRole('grid', { name: GRID_NAME })).toHaveAttribute(
    'aria-multiselectable', 'true');

  await dragSelect(page, 'A1', 'B2');
  for (const coord of ['A1', 'B1', 'A2', 'B2']) {
    await expect(cell(page, coord)).toHaveAttribute('aria-selected', 'true');
  }
  for (const coord of ['C1', 'C2', 'A3', 'C4']) {
    expect(await cellAriaSelected(page, coord)).toBe('false');
  }

  // 新选择替换旧选择
  await dragSelect(page, 'C3', 'C4');
  expect(await cellAriaSelected(page, 'A1')).toBe('false');
  expect(await cellAriaSelected(page, 'C3')).toBe('true');
});
