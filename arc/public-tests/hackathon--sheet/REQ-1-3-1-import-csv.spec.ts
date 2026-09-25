import { expect, test } from '@playwright/test';
import { openHome } from './support/e2e';

// REQ-1-3-1 导入 CSV 建簿:首页 "Import CSV" 按钮 → 对话框 "Import CSV"
// 含 labeled "CSV file" 文件控件与 "Confirm import" 按钮;保留空字段/
// 引号包裹逗号/换行/中文;未闭合引号 = 无效 CSV,报
// "Invalid CSV file format. Import failed.";成功后新工作簿名 = 文件名
// 去扩展名。

test('REQ-1-3-1: import valid CSV (quoted commas, newlines, CJK) as a new workbook', async ({ page }) => {
  await openHome(page);
  await page.getByRole('button', { name: 'Import CSV' }).click();
  const dialog = page.getByRole('dialog', { name: 'Import CSV' });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('CSV file').setInputFiles({
    name: 'city-report.csv', mimeType: 'text/csv',
    buffer: Buffer.from(
      '城市,销售额\n"Shanghai, CN",1200\n"Line\nBreak",800\n上海,700',
      'utf-8'),
  });
  await dialog.getByRole('button', { name: 'Confirm import' }).click();

  await expect(
    page.getByRole('link', { name: 'city-report', exact: true }))
    .toBeVisible();
});

test('REQ-1-3-1: unclosed quote is rejected with the exact error', async ({ page }) => {
  await openHome(page);
  await page.getByRole('button', { name: 'Import CSV' }).click();
  const dialog = page.getByRole('dialog', { name: 'Import CSV' });
  await dialog.getByLabel('CSV file').setInputFiles({
    name: 'broken.csv', mimeType: 'text/csv',
    buffer: Buffer.from('Name,Note\n"Unclosed,oops', 'utf-8'),
  });
  await dialog.getByRole('button', { name: 'Confirm import' }).click();
  await expect(
    page.getByText('Invalid CSV file format. Import failed.')).toBeVisible();
  // 半成品记录不得出现在首页
  await expect(
    page.getByRole('link', { name: 'broken', exact: true })).toHaveCount(0);
});
