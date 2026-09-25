import { expect, type Page } from '@playwright/test';

// 本套件按 requirements.yaml 逐字编写:可访问名("Create an account"、
// "Username or email"、"Sign in"、"Find a repository"、"Search issues"、
// "New issue" ...)与种子数据(alice-dev / Valid-password-123! /
// Acme Demo / acme-docs / secret-research / bob-reviewer / frontend-team /
// Improve onboarding / bug / documentation / Q3 launch)均为需求原文
// 给出的评分契约。

export const SEED_USER = 'alice-dev';
export const SEED_EMAIL = 'alice.dev@example.test';
export const SEED_PASSWORD = 'Valid-password-123!';
export const ORG_NAME = 'Acme Demo';
export const PUBLIC_REPO = 'acme-docs';
export const PRIVATE_REPO = 'secret-research';
export const SEED_ISSUE = 'Improve onboarding';

export function baseUrl(): string {
  return process.env.E2E_BASE_URL ?? 'http://127.0.0.1:3100';
}

export async function openHome(page: Page): Promise<void> {
  await page.goto(baseUrl());
}

export async function openSignIn(page: Page): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in' }).first().click();
}

export async function signIn(
  page: Page,
  identifier: string = SEED_USER,
  password: string = SEED_PASSWORD,
): Promise<void> {
  await openSignIn(page);
  await page.getByLabel('Username or email').fill(identifier);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  // 登录成功:右上角账户菜单可见
  await expect(page.getByText(identifier, { exact: true })).toBeVisible();
}

export async function signOut(page: Page): Promise<void> {
  await page.getByText(SEED_USER, { exact: true }).first().click();
  await page.getByRole('menuitem', { name: /sign out/i }).click();
  await expect(page.getByRole('link', { name: 'Sign in' }).first())
    .toBeVisible();
}

export async function openOrg(page: Page, name = ORG_NAME): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name }).first().click();
  await expect(page.getByText(name).first()).toBeVisible();
}

export async function openRepo(
  page: Page, owner: string, repo: string,
): Promise<void> {
  await page.getByRole('link', { name: `${owner}/${repo}` }).first().click();
  await expect(page.getByRole('heading', { name: `${owner}/${repo}` }))
    .toBeVisible();
}
