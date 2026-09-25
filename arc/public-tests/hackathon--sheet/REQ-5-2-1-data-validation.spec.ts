import { expect, test } from '@playwright/test';
import {
  cell, dataMenuItem, dragSelect, openWorkbook, selectCell,
} from './support/e2e';

// REQ-5-2-1 数据验证:选中目标区 → "Data" 菜单 → "Data validation" 对话
// 框("Rule type" 组合框;Number range 用 "Minimum"/"Maximum" 文本框;
// Dropdown 用 "Allowed values" 文本框,逗号分隔去首尾空格);"Save"
// 保存;违规输入(网格/公式栏/粘贴)报 "Please enter a number from 0 to
// 100";下拉单元格提供 "Open dropdown for <坐标>" 按钮,选项为 option
// 角色以去空格后的允许值为名。

test('REQ-5-2-1: number range 0-100 rejects 150 with the exact message', async ({ page }) => {
  await openWorkbook(page);
  await dragSelect(page, 'C2', 'C4');
  await dataMenuItem(page, 'Data validation');
  const dialog = page.getByRole('dialog', { name: 'Data validation' });
  await dialog.getByLabel('Rule type').selectOption({ label: 'Number range' });
  await dialog.getByLabel('Minimum').fill('0');
  await dialog.getByLabel('Maximum').fill('100');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog).not.toBeVisible();

  // 经公式栏写入违规值 → 精确报错,原值保留
  await selectCell(page, 'C2');
  await page.getByRole('textbox', { name: 'Formula bar' }).fill('150');
  await page.keyboard.press('Enter');
  await expect(
    page.getByText('Please enter a number from 0 to 100')).toBeVisible();
  await expect(cell(page, 'C2')).toHaveText('1200');
});

test('REQ-5-2-1: dropdown rule exposes Open dropdown button and options', async ({ page }) => {
  await openWorkbook(page);
  await dragSelect(page, 'D2', 'D3');
  await dataMenuItem(page, 'Data validation');
  const dialog = page.getByRole('dialog', { name: 'Data validation' });
  await dialog.getByLabel('Rule type').selectOption({ label: 'Dropdown' });
  await dialog.getByLabel('Allowed values').fill('Alpha, Beta');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();

  await page.getByRole('button', { name: 'Open dropdown for D2' }).click();
  await expect(page.getByRole('option', { name: 'Alpha', exact: true }))
    .toBeVisible();
  await expect(page.getByRole('option', { name: 'Beta', exact: true }))
    .toBeVisible();
  await page.keyboard.press('Escape');
});
