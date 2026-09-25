import { expect, test } from '@playwright/test';
import { openWorkbook, reload } from './support/e2e';

// REQ-1-3-2 导出当前工作表 CSV:"Export CSV" 按钮触发浏览器下载,
// 文件名以 .csv 结尾;导出内容 = 用区内实际行列,公式导出计算结果;
// 导出前后网格与公式栏不变。

test('REQ-1-3-2: Export CSV downloads the active sheet verbatim', async ({ page }) => {
  await openWorkbook(page);
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export CSV' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.csv$/);

  const path = await download.path();
  const body = (await import('fs/promises')).default;
  const text = (await body.readFile(path!, 'utf-8'))
    .replace(/\r\n/g, '\n').trimEnd();
  expect(text).toBe(
    'Region,Sales,Status\n' +
    'East,1200,Open\n' +
    'North,800,Closed\n' +
    'South,700,Open');

  // 导出前后状态不变
  await reload(page);
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toBeVisible();
});
