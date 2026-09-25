import { expect, test } from '@playwright/test';
import {
  cell, editViaFormulaBar, openWorkbook, reload,
} from './support/e2e';

// REQ-3-2-2 撤销/重做:工具栏按钮可访问名 "Undo"/"Redo"(Ctrl+Z /
// Ctrl+Y 同义);undo 逆序还原,redo 重做;每次 undo/redo 后的状态刷新
// 后持久。测试以还原种子值收尾。

test('REQ-3-2-2: Undo/Redo buttons and persistence', async ({ page }) => {
  await openWorkbook(page);
  await editViaFormulaBar(page, 'B2', '1300');
  await expect(cell(page, 'B2')).toHaveText('1300');

  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(cell(page, 'B2')).toHaveText('1200');
  await reload(page);
  await expect(cell(page, 'B2')).toHaveText('1200');

  await page.getByRole('button', { name: 'Redo', exact: true }).click();
  await expect(cell(page, 'B2')).toHaveText('1300');
  await reload(page);
  await expect(cell(page, 'B2')).toHaveText('1300');

  // 还原种子值
  await editViaFormulaBar(page, 'B2', '1200');
  await expect(cell(page, 'B2')).toHaveText('1200');
});
