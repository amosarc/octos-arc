import { expect, test } from '@playwright/test';
import { addWorksheet, deleteWorksheet, openWorkbook } from './support/e2e';

// REQ-2-1-1 添加工作表:"Add worksheet" 按钮创建新工作簿页签并激活
// (名称按 SheetN 惯例)。spec 结束删除新表,保持种子不变。

test('REQ-2-1-1: Add worksheet creates and activates a new SheetN tab', async ({ page }) => {
  await openWorkbook(page);
  const created = await addWorksheet(page);
  expect(created).toMatch(/^Sheet\d+$/);
  await expect(page.getByRole('tab', { name: created, exact: true }))
    .toHaveAttribute('aria-selected', 'true');

  await deleteWorksheet(page, created);
});
