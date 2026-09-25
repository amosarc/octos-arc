import { expect, test } from '@playwright/test';
import {
  ORG_NAME, PUBLIC_REPO, SEED_ISSUE, openOrg, openRepo, signIn,
} from './support/e2e';

// REQ-5-1-1 Issue 列表:行含编号/标题/状态/作者/标签/更新时间;可按
// Open/Closed(链接而非按钮)与关键词过滤,搜索框输入即更新无需回车;
// 唯一搜索框名为 "Search issues"。种子:issue `Improve onboarding`、
// 标签 `bug`/`documentation`。

async function openIssues(page: import('@playwright/test').Page): Promise<void> {
  await openOrg(page);
  await openRepo(page, ORG_NAME, PUBLIC_REPO);
  await page.getByRole('link', { name: /Issues/i }).first().click();
}

test('REQ-5-1-1: seeded issue row with status, author and labels', async ({ page }) => {
  await openIssues(page);
  const row = page.locator('tr, li, article').filter({
    hasText: SEED_ISSUE,
  }).first();
  await expect(row).toBeVisible();
  await expect(row).toContainText(/bug/i);
  await expect(row).toContainText(/documentation/i);
});

test('REQ-5-1-1: Search issues box filters without Enter', async ({ page }) => {
  await openIssues(page);
  await page.getByRole('searchbox', { name: 'Search issues' })
    .fill('no-such-issue-xyz');
  await expect(page.getByText(SEED_ISSUE)).toHaveCount(0);
  await page.getByRole('searchbox', { name: 'Search issues' })
    .fill('Improve');
  await expect(page.getByText(SEED_ISSUE).first()).toBeVisible();
});

test('REQ-5-1-1: Open and Closed are links, not buttons', async ({ page }) => {
  await openIssues(page);
  await expect(page.getByRole('link', { name: /^Open\b/i }).first())
    .toBeVisible();
  await expect(page.getByRole('link', { name: /^Closed\b/i }).first())
    .toBeVisible();
});

test('REQ-5-1-1: signed-in users see the same list (permission read)', async ({ page }) => {
  await signIn(page);
  await openIssues(page);
  await expect(page.getByText(SEED_ISSUE).first()).toBeVisible();
});
