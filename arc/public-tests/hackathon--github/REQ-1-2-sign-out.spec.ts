import { expect, test } from '@playwright/test';
import { SEED_USER, signIn, signOut } from './support/e2e';

// REQ-1-2 退出登录:账户菜单(右上角)内退出后,受保护页面不可再访问,
// 首页回到未登录形态(出现 Sign in)。刷新后会话仍保持已退出。

test('REQ-1-2: sign out ends the session', async ({ page }) => {
  await signIn(page);
  await signOut(page);
  await page.reload();
  await expect(page.getByText(SEED_USER, { exact: true })).toHaveCount(0);
});
