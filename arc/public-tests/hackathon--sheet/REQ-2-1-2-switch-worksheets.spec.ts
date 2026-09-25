import { expect, test } from '@playwright/test';
import { cell, formulaBar, openWorkbook, reload } from './support/e2e';

// REQ-2-1-2 切换工作表:点击另一个 ARIA tab 后网格/公式栏切到目标表;
// 首次打开无选择历史的表选 A1;切回源表恢复最近状态;直接重开显示
// 最后激活的页签。

test('REQ-2-1-2: switching tabs swaps grid state and selects A1 on first open', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('tab', { name: 'Sheet2' }).click();
  await expect(page.getByRole('tab', { name: 'Sheet2' })).toHaveAttribute(
    'aria-selected', 'true');
  // Sheet2 种子:行 East/1200、North/800;首次打开选 A1
  await expect(cell(page, 'A1')).toHaveText('East');
  await expect(cell(page, 'B1')).toHaveText('1200');
  await expect(cell(page, 'A2')).toHaveText('North');
  await expect(cell(page, 'B2')).toHaveText('800');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  await cell(page, 'A1').click();
  await expect(formulaBar(page)).toHaveValue('East');

  // 切回 Sheet1:恢复原状态(A1=Region)
  await page.getByRole('tab', { name: 'Sheet1' }).click();
  await expect(cell(page, 'A1')).toHaveText('Region');

  // 重开工作簿:显示最后激活的页签并恢复
  await reload(page);
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toHaveAttribute(
    'aria-selected', 'true');
});
