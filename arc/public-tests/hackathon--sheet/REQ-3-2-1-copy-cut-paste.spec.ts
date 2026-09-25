import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, dragSelect, editViaFormulaBar, openWorkbook, reload,
  selectCell,
} from './support/e2e';

// REQ-3-2-1 复制/剪切/粘贴:同表内复制后源区不变;剪切源区在粘贴完成后
// 才清空;二维布局与值保持。键盘快捷键走应用自身处理,不依赖
// navigator.clipboard;清理一律用公式栏改写,避免权限与环境差异。

test('REQ-3-2-1: copy keeps source, paste reproduces layout', async ({ page }) => {
  await openWorkbook(page);
  await dragSelect(page, 'A1', 'B1');
  await page.keyboard.press('ControlOrMeta+c');
  await selectCell(page, 'D5');
  await page.keyboard.press('ControlOrMeta+v');

  await expect(cell(page, 'D5')).toHaveText('Region');
  await expect(cell(page, 'E5')).toHaveText('Sales');
  // 源区不变
  await expect(cell(page, 'A1')).toHaveText('Region');
  await expect(cell(page, 'B1')).toHaveText('Sales');
});

test('REQ-3-2-1: cut clears source only after paste lands', async ({ page }) => {
  await openWorkbook(page);
  await dragSelect(page, 'A4', 'C4');
  await page.keyboard.press('ControlOrMeta+x');
  await selectCell(page, 'D7');
  await page.keyboard.press('ControlOrMeta+v');
  await expect(cell(page, 'D7')).toHaveText('South');
  await expect(cell(page, 'E7')).toHaveText('700');
  await expect(cell(page, 'A4')).toHaveText('');
});

test('REQ-3-2-1: cleanup restores row 4 and removes scratch rows', async ({ page }) => {
  await openWorkbook(page);
  // 还原第 4 行(South/700/Open)——逐格公式栏改写,零剪贴板依赖
  await editViaFormulaBar(page, 'A4', 'South');
  await editViaFormulaBar(page, 'B4', '700');
  await editViaFormulaBar(page, 'C4', 'Open');
  // 删除草稿行 7、5(先删大行号,避免位移)
  await deleteRow(page, 7);
  await deleteRow(page, 5);
  await reload(page);
  await expect(cell(page, 'A4')).toHaveText('South');
  await expect(cell(page, 'B4')).toHaveText('700');
  await expect(cell(page, 'A5')).toHaveText('');
});
