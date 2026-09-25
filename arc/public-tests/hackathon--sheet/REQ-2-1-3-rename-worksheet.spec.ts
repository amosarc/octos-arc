import { expect, test } from '@playwright/test';
import { openSheetMenu, openWorkbook } from './support/e2e';

// REQ-2-1-3 重命名工作表:标签菜单 "Rename" → 对话框 "Rename worksheet"
// 含预填 "Worksheet name" 文本框与 "Save";空名报
// "Worksheet name cannot be empty",重名报 "Worksheet name already
// exists";成功后标签显示新名。结束还原为 Sheet2。

test('REQ-2-1-3: rename Sheet2 with validation errors and restore', async ({ page }) => {
  await openWorkbook(page);
  await openSheetMenu(page, 'Sheet2');
  await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Rename worksheet' });
  await expect(dialog).toBeVisible();
  const nameBox = dialog.getByLabel('Worksheet name');
  await expect(nameBox).toHaveValue('Sheet2');

  await nameBox.fill('');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(
    page.getByText('Worksheet name cannot be empty')).toBeVisible();

  await nameBox.fill('Sheet1');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(
    page.getByText('Worksheet name already exists')).toBeVisible();

  await nameBox.fill('Data Sheet');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(
    page.getByRole('tab', { name: 'Data Sheet', exact: true })).toBeVisible();

  // 还原
  await openSheetMenu(page, 'Data Sheet');
  await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
  await page.getByLabel('Worksheet name').fill('Sheet2');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true }))
    .toBeVisible();
});
