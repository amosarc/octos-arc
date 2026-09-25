import { expect, test } from '@playwright/test';
import {
  SEED_EMAIL, SEED_PASSWORD, SEED_USER, openSignIn, signIn,
} from './support/e2e';

// REQ-1-1-2 登录:账户访问页含 "Username or email"、"Password"、
// "Sign in";种子账号 alice-dev / alice.dev@example.test /
// Valid-password-123! 可用用户名或邮箱登录;错误密码拒绝且不建立会话;
// 会话刷新后保持。

test('REQ-1-1-2: sign in with seeded username', async ({ page }) => {
  await signIn(page, SEED_USER);
  await page.reload();
  // 会话在刷新后保持
  await expect(page.getByText(SEED_USER, { exact: true })).toBeVisible();
});

test('REQ-1-1-2: sign in with seeded email', async ({ page }) => {
  await signIn(page, SEED_EMAIL);
});

test('REQ-1-1-2: wrong password is rejected without a session', async ({ page }) => {
  await openSignIn(page);
  await page.getByLabel('Username or email').fill(SEED_USER);
  await page.getByLabel('Password', { exact: true }).fill('totally-wrong-pass');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText(SEED_USER, { exact: true })).toHaveCount(0);
  await expect(page.locator('body')).toContainText(
    /incorrect|invalid|wrong|failed/i);
});
