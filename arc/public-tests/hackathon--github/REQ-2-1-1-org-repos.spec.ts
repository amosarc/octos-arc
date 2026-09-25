import { expect, test } from '@playwright/test';
import {
  ORG_NAME, PRIVATE_REPO, PUBLIC_REPO, openOrg,
} from './support/e2e';

// REQ-2-1-1 浏览组织仓库:组织页 "Repositories" 导航链接 + "Find a
// repository" 文本框输入即过滤(无独立提交);结果链接以仓库名命名,
// 打开后标题为 "org/repo";访客不得看到私有仓库链接。

test('REQ-2-1-1: repositories tab filters as the user types', async ({ page }) => {
  await openOrg(page);
  await page.getByRole('link', { name: 'Repositories' }).click();

  const filter = page.getByLabel('Find a repository');
  await filter.fill(PUBLIC_REPO);
  await expect(
    page.getByRole('link', { name: PUBLIC_REPO, exact: true })).toBeVisible();

  await filter.fill('no-such-repo-xyz');
  await expect(
    page.getByRole('link', { name: PUBLIC_REPO, exact: true })).toHaveCount(0);
});

test('REQ-2-1-1: public repo opens with org/repo heading; private hidden from visitors', async ({ page }) => {
  await openOrg(page);
  await page.getByRole('link', { name: 'Repositories' }).click();
  await page.getByRole('link', { name: PUBLIC_REPO, exact: true }).click();
  await expect(
    page.getByRole('heading', { name: `${ORG_NAME}/${PUBLIC_REPO}` }))
    .toBeVisible();

  // 私有仓库对访客不可见
  await page.goBack();
  await expect(
    page.getByRole('link', { name: PRIVATE_REPO, exact: true }))
    .toHaveCount(0);
});
