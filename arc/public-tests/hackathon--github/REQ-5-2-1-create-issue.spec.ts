import { expect, test } from '@playwright/test';
import {
  ORG_NAME, PUBLIC_REPO, openOrg, openRepo, signIn,
} from './support/e2e';

// REQ-5-2-1 创建 Issue:有写权限的登录用户在 Issues 列表页点
// "New issue" → 非空标题 + 可选描述 → 提交后系统分配递增唯一编号、
// Open 状态,并跳转到 issue 详情页;失败不分配编号不留半成品。

test('REQ-5-2-1: New issue creates a numbered Open issue', async ({ page }) => {
  await signIn(page);
  await openOrg(page);
  await openRepo(page, ORG_NAME, PUBLIC_REPO);
  await page.getByRole('link', { name: /Issues/i }).first().click();

  const before = await page.locator('body').textContent() ?? '';
  await page.getByRole('link', { name: 'New issue' })
    .or(page.getByRole('button', { name: 'New issue' })).first().click();

  const title = `Spec issue ${Date.now()}`;
  await page.getByLabel('Title').fill(title);
  await page.getByLabel('Description').fill('Created by the acceptance suite.');
  await page.getByRole('button', { name: /Submit new issue/i }).click();

  // 详情页:标题 + Open 状态 + 编号(#N)
  await expect(page.getByText(title).first()).toBeVisible();
  const body = await page.locator('body').textContent() ?? '';
  expect(body).toMatch(/#\d+/);
  expect(body).toMatch(/Open/i);
  void before;
});
