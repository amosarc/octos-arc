import { expect, test } from '@playwright/test';
import {
  cell, deleteRow, formulaBar, openWorkbook, reload, selectCell,
  typeIntoCell,
} from './support/e2e';

// REQ-4-1-2 复制公式:相对引用按目标偏移调整,绝对引用不变;源公式与
// 结果不变;目标网格显示新引用结果;刷新持久。

test('REQ-4-1-2: copied formula adjusts relative refs and keeps absolute refs', async ({ page }) => {
  await openWorkbook(page);
  await typeIntoCell(page, 'A9', '2');
  await typeIntoCell(page, 'B9', '3');
  await typeIntoCell(page, 'C9', '=A9+B9');
  await typeIntoCell(page, 'E9', '=$A$9+B9');
  await expect(cell(page, 'C9')).toHaveText('5');

  // 复制 C9 → C10:相对引用下移一行,公式栏显示调整后的原公式
  await selectCell(page, 'C9');
  await page.keyboard.press('ControlOrMeta+c');
  await selectCell(page, 'C10');
  await page.keyboard.press('ControlOrMeta+v');
  await selectCell(page, 'C10');
  await expect(formulaBar(page)).toHaveValue('=A10+B10');
  await expect(cell(page, 'C10')).toHaveText('0');

  // 复制 E9 → E10:绝对引用 $A$9 不变,相对部分调整为 B10
  await selectCell(page, 'E9');
  await page.keyboard.press('ControlOrMeta+c');
  await selectCell(page, 'E10');
  await page.keyboard.press('ControlOrMeta+v');
  await selectCell(page, 'E10');
  await expect(formulaBar(page)).toHaveValue('=$A$9+B10');
  await expect(cell(page, 'E10')).toHaveText('2');

  // 源不变;刷新持久
  await expect(cell(page, 'C9')).toHaveText('5');
  await reload(page);
  await selectCell(page, 'C10');
  await expect(formulaBar(page)).toHaveValue('=A10+B10');
});

test('REQ-4-1-2: cleanup removes scratch rows', async ({ page }) => {
  await openWorkbook(page);
  await deleteRow(page, 10);
  await deleteRow(page, 9);
  await reload(page);
  await expect(cell(page, 'A9')).toHaveText('');
});
