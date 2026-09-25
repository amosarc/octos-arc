import { expect, test } from '@playwright/test';
import { WORKBOOK, openHome, openWorkbook } from './support/e2e';

// REQ-1-2-2 重命名工作簿:编辑器标题旁 "Rename workbook" 按钮 →
// 文本框 labeled "Workbook name" 预填当前名 + "Save" 按钮;空名报
// "Workbook name cannot be empty";成功后编辑器标题与首页链接同显新名。
// 本 spec 结束时把名字改回种子名,保持规范种子不变。

test('REQ-1-2-2: rename Q3 Sales -> Q4 Sales and back', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('button', { name: 'Rename workbook' }).click();
  const nameBox = page.getByLabel('Workbook name');
  await expect(nameBox).toHaveValue(WORKBOOK);

  // 空名被拒绝,原名字保留
  await nameBox.fill('');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(
    page.getByText('Workbook name cannot be empty')).toBeVisible();

  // 成功改名:编辑器标题与首页链接同显新名
  await nameBox.fill('Q4 Sales');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Q4 Sales').first()).toBeVisible();
  await openHome(page);
  await expect(
    page.getByRole('link', { name: 'Q4 Sales', exact: true })).toBeVisible();

  // 还原种子名
  await page.getByRole('link', { name: 'Q4 Sales', exact: true }).click();
  await page.getByRole('button', { name: 'Rename workbook' }).click();
  await page.getByLabel('Workbook name').fill(WORKBOOK);
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await openHome(page);
  await expect(
    page.getByRole('link', { name: WORKBOOK, exact: true })).toBeVisible();
});
