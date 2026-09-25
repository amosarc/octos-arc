import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, editViaFormulaBar, formulaBar, openWorkbook, reload,
  selectCell, typeIntoCell,
} from './support/e2e';

// REQ-4-2-1 依赖重算:源值编辑后直接与间接依赖公式按依赖序更新;
// 公式栏保持原式,网格显示新结果;刷新后与当前源值一致。

test('REQ-4-2-1: direct and indirect dependents recalculate in order', async ({ page }) => {
  await openWorkbook(page);
  await typeIntoCell(page, 'B9', '3');
  await typeIntoCell(page, 'C9', '=B9*2');
  await typeIntoCell(page, 'D9', '=C9+1');
  await expect(cell(page, 'C9')).toHaveText('6');
  await expect(cell(page, 'D9')).toHaveText('7');

  // 源编辑 → 直接依赖 C9 与间接依赖 D9 同时更新
  await editViaFormulaBar(page, 'B9', '5');
  await expect(cell(page, 'C9')).toHaveText('10');
  await expect(cell(page, 'D9')).toHaveText('11');
  // 公式栏保持原式
  await selectCell(page, 'C9');
  await expect(formulaBar(page)).toHaveValue('=B9*2');

  await reload(page);
  await expect(cell(page, 'C9')).toHaveText('10');
  await expect(cell(page, 'D9')).toHaveText('11');
});

test('REQ-4-2-1: cleanup removes the scratch row', async ({ page }) => {
  await openWorkbook(page);
  await deleteRow(page, 9);
  await reload(page);
  await expect(cell(page, 'C9')).toHaveText('');
});
