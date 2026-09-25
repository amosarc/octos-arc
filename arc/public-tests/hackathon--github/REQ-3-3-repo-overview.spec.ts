import { expect, test } from '@playwright/test';
import {
  ORG_NAME, PUBLIC_REPO, openRepo, openOrg,
} from './support/e2e';

// REQ-3-3 公共仓库概览:标题含 "owner/仓库名",公共仓库有可见
// "Public" 标记,提供区别于克隆菜单 "Code" 按钮的 "Code" 导航链接;
// 无需登录即可读,刷新后标题不变。

test('REQ-3-3: public repo overview is readable without sign-in', async ({ page }) => {
  await openOrg(page);
  await openRepo(page, ORG_NAME, PUBLIC_REPO);
  await expect(page.getByText('Public', { exact: true }).first())
    .toBeVisible();
  await expect(
    page.getByRole('link', { name: 'Code', exact: true }).first())
    .toBeVisible();

  await page.reload();
  await expect(
    page.getByRole('heading', { name: `${ORG_NAME}/${PUBLIC_REPO}` }))
    .toBeVisible();
});
