import { expect, test } from '@playwright/test';
import { openSignIn } from './support/e2e';

// REQ-1-1-1 注册:登录页唯一链接 "Create an account";表单恰含
// Username/Email/Password/Confirm password 文本框、"Agree to the terms"
// 复选框(初始未勾)与 "Create account" 按钮;注册成功即视为邮箱已验证,
// 无后续验证页;密码不得回显。

test('REQ-1-1-1: registration form contract and successful signup', async ({ page }) => {
  await openSignIn(page);
  await page.getByRole('link', { name: 'Create an account' }).click();

  const username = `pw-user-${Date.now()}`;
  const usernameBox = page.getByLabel('Username');
  const emailBox = page.getByLabel('Email');
  const passwordBox = page.getByLabel('Password', { exact: true });
  const confirmBox = page.getByLabel('Confirm password');
  const terms = page.getByRole('checkbox', { name: 'Agree to the terms' });
  await expect(terms).not.toBeChecked();
  await expect(page.getByRole('button', { name: 'Create account' }))
    .toBeEnabled();
  // 密码输入不得回显
  await expect(passwordBox).toHaveAttribute('type', 'password');
  await expect(confirmBox).toHaveAttribute('type', 'password');

  await usernameBox.fill(username);
  await emailBox.fill(`${username}@example.test`);
  await passwordBox.fill('Valid-password-123!');
  await confirmBox.fill('Valid-password-123!');
  await terms.check();
  await page.getByRole('button', { name: 'Create account' }).click();

  // 直接注册成功(邮箱自动已验证,无验证页)
  await expect(page.getByText(username, { exact: true })).toBeVisible();
});

test('REQ-1-1-1: mismatched confirmation is rejected inline', async ({ page }) => {
  await openSignIn(page);
  await page.getByRole('link', { name: 'Create an account' }).click();
  await page.getByLabel('Username').fill(`pw-mismatch-${Date.now()}`);
  await page.getByLabel('Email').fill(`pw-mismatch-${Date.now()}@example.test`);
  await page.getByLabel('Password', { exact: true }).fill('Valid-password-123!');
  await page.getByLabel('Confirm password').fill('Different-password-123!');
  await page.getByRole('checkbox', { name: 'Agree to the terms' }).check();
  await page.getByRole('button', { name: 'Create account' }).click();
  // 留在注册页且出现内联错误(不创建半成品账号)
  await expect(page.getByLabel('Username')).toBeVisible();
  await expect(page.locator('body')).toContainText(
    /match|incorrect|invalid|must/i);
});
