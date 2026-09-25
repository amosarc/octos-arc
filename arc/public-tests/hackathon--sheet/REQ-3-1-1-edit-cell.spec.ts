import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, editViaFormulaBar, formulaBar, openWorkbook, reload,
  selectCell,
} from './support/e2e';

// REQ-3-1-1 单元格编辑:网格直接键入与公式栏皆可提交;Enter/点击他格
// 提交,Escape 取消;普通单元格网格与公式栏同显;刷新持久。

test('REQ-3-1-1: type into grid, commit with Enter, persists after reload', async ({ page }) => {
  await openWorkbook(page);
  await selectCell(page, 'A5');
  await page.keyboard.type('West');
  await page.keyboard.press('Enter');
  await expect(cell(page, 'A5')).toHaveText('West');
  await reload(page);
  await expect(cell(page, 'A5')).toHaveText('West');
});

test('REQ-3-1-1: formula bar edits the selected cell; Escape cancels', async ({ page }) => {
  await openWorkbook(page);
  // 选中单元格时公式栏显示原值
  await selectCell(page, 'A1');
  await expect(formulaBar(page)).toHaveValue('Region');

  // 公式栏编辑提交
  await editViaFormulaBar(page, 'B4', '750');
  await expect(cell(page, 'B4')).toHaveText('750');

  // Escape 取消未提交修改
  await selectCell(page, 'A4');
  await page.keyboard.type('XXXX');
  await page.keyboard.press('Escape');
  await expect(cell(page, 'A4')).toHaveText('Open');
  await expect(formulaBar(page)).toHaveValue('Open');
});

test('REQ-3-1-1: cleanup restores seeded values', async ({ page }) => {
  await openWorkbook(page);
  await editViaFormulaBar(page, 'B4', '700');
  await expect(cell(page, 'B4')).toHaveText('700');
  // A5(本套件第一次测试写入)通过删除整行还原
  await deleteRow(page, 5);
  await reload(page);
  await expect(cell(page, 'A5')).toHaveText('');
});
