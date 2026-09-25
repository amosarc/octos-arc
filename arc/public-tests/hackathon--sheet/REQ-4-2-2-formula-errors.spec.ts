import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, formulaBar, openWorkbook, reload, selectCell,
  typeIntoCell,
} from './support/e2e';

// REQ-4-2-2 公式错误:#DIV/0!、#REF!、#NAME?、#ERROR!、循环引用 #REF!;
// 选中错误单元格时公式栏显示用户原式;错误不阻塞其他单元格;修复后
// 网格显新结果;刷新后错误值与原式都持久。

test('REQ-4-2-2: stable error values with original formula echo', async ({ page }) => {
  await openWorkbook(page);
  await typeIntoCell(page, 'H9', '=1/0');
  await expect(cell(page, 'H9')).toHaveText('#DIV/0!');
  await typeIntoCell(page, 'H10', '=NOSUCHFN(1)');
  await expect(cell(page, 'H10')).toHaveText('#NAME?');
  await typeIntoCell(page, 'H11', '=1+');
  await expect(cell(page, 'H11')).toHaveText('#ERROR!');
  await typeIntoCell(page, 'J9', '=J10');
  await typeIntoCell(page, 'J10', '=J9');
  await expect(cell(page, 'J9')).toHaveText('#REF!');
  await expect(cell(page, 'J10')).toHaveText('#REF!');

  // 错误单元格选中时公式栏显示原式
  await selectCell(page, 'H9');
  await expect(formulaBar(page)).toHaveValue('=1/0');

  // 错误不阻塞他格;修复后正常
  await typeIntoCell(page, 'H12', '=2+3');
  await expect(cell(page, 'H12')).toHaveText('5');
  await typeIntoCell(page, 'J9', '=5');
  await expect(cell(page, 'J9')).toHaveText('5');

  await reload(page);
  await expect(cell(page, 'H9')).toHaveText('#DIV/0!');
  await selectCell(page, 'H9');
  await expect(formulaBar(page)).toHaveValue('=1/0');
  await expect(cell(page, 'J9')).toHaveText('5');
});

test('REQ-4-2-2: cleanup removes scratch rows', async ({ page }) => {
  await openWorkbook(page);
  for (const row of [12, 11, 10, 9]) {
    await deleteRow(page, row);
  }
  await reload(page);
  await expect(cell(page, 'H9')).toHaveText('');
});
