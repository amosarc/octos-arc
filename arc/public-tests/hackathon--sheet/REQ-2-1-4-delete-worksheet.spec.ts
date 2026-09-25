import { expect, test } from '@playwright/test';
import { addWorksheet, deleteWorksheet, openWorkbook, reload } from './support/e2e';

// REQ-2-1-4 删除工作表:标签菜单 "Delete" → 对话框 "Delete worksheet"
// 描述含目标名 + "Delete worksheet" 确认按钮;删除后页签消失且刷新后
// 仍不在。用"先加后删"的临时表,种子表(Sheet1/Sheet2)不动。

test('REQ-2-1-4: delete a worksheet via the tab menu confirm dialog', async ({ page }) => {
  await openWorkbook(page);
  const temp = await addWorksheet(page);

  await deleteWorksheet(page, temp);
  await reload(page);
  await expect(page.getByRole('tab', { name: temp, exact: true }))
    .toHaveCount(0);
  // 相邻工作表仍完好
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Sheet2' })).toBeVisible();
});
