import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, formulaBar, openWorkbook, reload, selectCell,
  typeIntoCell,
} from './support/e2e';

// REQ-4-1-1 公式与聚合:=开头公式;常量/括号/四则/A1 引用/SUM、
// AVERAGE、COUNT、MIN、MAX;网格显结果、选中时公式栏显原式;函数名
// 大小写不敏感;聚合忽略空单元格;刷新后都持久。预置态(REQ-3/4 冲突
// 态)由本 spec 经 UI 自行布置,与规范种子约定一致。

test('REQ-4-1-1: arithmetic, references and aggregates with formula-bar echo', async ({ page }) => {
  await openWorkbook(page);
  await typeIntoCell(page, 'A9', '2');
  await typeIntoCell(page, 'B9', '3');

  await typeIntoCell(page, 'C9', '=A9+B9');
  await expect(cell(page, 'C9')).toHaveText('5');
  await selectCell(page, 'C9');
  await expect(formulaBar(page)).toHaveValue('=A9+B9');

  await typeIntoCell(page, 'D9', '=(A9+B9)*2');
  await expect(cell(page, 'D9')).toHaveText('10');
  await typeIntoCell(page, 'E9', '=SUM(A9:B9)');
  await expect(cell(page, 'E9')).toHaveText('5');
  await typeIntoCell(page, 'F9', '=sum(a9:b9)');
  await expect(cell(page, 'F9')).toHaveText('5');
  await typeIntoCell(page, 'G9', '=AVERAGE(A9:B9)');
  await expect(cell(page, 'G9')).toHaveText('2.5');
  await typeIntoCell(page, 'H9', '=COUNT(A9:B9)');
  await expect(cell(page, 'H9')).toHaveText('2');
  await typeIntoCell(page, 'I9', '=MIN(A9:B9)');
  await expect(cell(page, 'I9')).toHaveText('2');
  await typeIntoCell(page, 'J9', '=MAX(A9:B9)');
  await expect(cell(page, 'J9')).toHaveText('3');

  await reload(page);
  await expect(cell(page, 'C9')).toHaveText('5');
  await selectCell(page, 'C9');
  await expect(formulaBar(page)).toHaveValue('=A9+B9');
});

test('REQ-4-1-1: cleanup removes the scratch row', async ({ page }) => {
  await openWorkbook(page);
  await deleteRow(page, 9);
  await reload(page);
  await expect(cell(page, 'A9')).toHaveText('');
});
