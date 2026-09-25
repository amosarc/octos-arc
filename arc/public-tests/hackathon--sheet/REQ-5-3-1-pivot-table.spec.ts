import { expect, test } from '@playwright/test';
import {
  cell, dataMenuItem, dragSelect, openWorkbook, reload,
} from './support/e2e';

// REQ-5-3-1 数据透视:选中含表头的源区 → "Data" 菜单 → "Create pivot
// table" 对话框显示 "Source range: <区域>"、"New worksheet" 单选项与
// "Create" 按钮;首个未用名 Pivot1;"Pivot table editor" 区提供
// "Rows"/"Columns"/"Values"/"Summarize by" 组合框与 "Apply";选项以源
// 表头为名,汇总方式 SUM/COUNT/AVERAGE。源数据只读不改。

test('REQ-5-3-1: create Pivot1 on a new worksheet and sum Sales by Region', async ({ page }) => {
  await openWorkbook(page);
  await dragSelect(page, 'A1', 'C4');

  await dataMenuItem(page, 'Create pivot table');
  const dialog = page.getByRole('dialog', { name: 'Create pivot table' });
  await expect(dialog).toContainText('Source range: A1:C4');
  await dialog.getByRole('radio', { name: 'New worksheet' }).check();
  await dialog.getByRole('button', { name: 'Create', exact: true }).click();

  // 透视结果落在 Pivot1 工作表
  const pivotTab = page.getByRole('tab', { name: 'Pivot1', exact: true });
  await expect(pivotTab).toBeVisible();
  await pivotTab.click();

  await expect(page.getByText('Pivot table editor')).toBeVisible();
  await page.getByLabel('Rows').selectOption({ label: 'Region' });
  await page.getByLabel('Values').selectOption({ label: 'Sales' });
  await page.getByLabel('Summarize by').selectOption({ label: 'SUM' });
  await page.getByRole('button', { name: 'Apply', exact: true }).click();

  // SUM 结果:East=1200、North=800、South=700
  await expect(page.getByText('1200').first()).toBeVisible();
  await expect(page.getByText('800').first()).toBeVisible();
  await expect(page.getByText('700').first()).toBeVisible();

  await reload(page);
  await expect(page.getByRole('tab', { name: 'Pivot1', exact: true }))
    .toBeVisible();

  // 源表数据未被修改
  await page.getByRole('tab', { name: 'Sheet1' }).click();
  await expect(cell(page, 'A2')).toHaveText('East');
  await expect(cell(page, 'B2')).toHaveText('1200');
});
