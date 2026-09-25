import { expect, type Locator, type Page } from '@playwright/test';

// 本套件按 requirements.yaml 逐字编写:所有可访问名(button/dialog/
// label/menuitem)、ARIA 角色(tab/grid/gridcell/option)与种子值
// (Q3 Sales / Sheet1 / Region / East/1200 / North/800 / South/700)
// 都是需求原文给出的评分契约,不得更改。
export const WORKBOOK = 'Q3 Sales';
export const GRID_NAME = 'Worksheet grid';

export function baseUrl(): string {
  return process.env.E2E_BASE_URL ?? 'http://127.0.0.1:3100';
}

export async function openHome(page: Page): Promise<void> {
  await page.goto(baseUrl());
}

export async function openWorkbook(page: Page, name = WORKBOOK): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name, exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet1' })).toBeVisible();
}

// ARIA 契约(REQ-1 文件夹描述):grid 可访问名 "Worksheet grid",
// aria-multiselectable="true",gridcell 以坐标为可访问名。
export function grid(page: Page): Locator {
  return page.getByRole('grid', { name: GRID_NAME });
}

export function cell(page: Page, coord: string): Locator {
  return grid(page).getByRole('gridcell', { name: coord, exact: true });
}

export async function expectCellValue(
  page: Page, coord: string, value: string | number,
): Promise<void> {
  await expect(cell(page, coord)).toHaveText(String(value));
}

export async function selectCell(page: Page, coord: string): Promise<void> {
  await cell(page, coord).click();
}

// 文本框 labeled "Formula bar"(REQ-3-1)。
export function formulaBar(page: Page): Locator {
  return page.getByRole('textbox', { name: 'Formula bar' });
}

export async function editViaFormulaBar(
  page: Page, coord: string, value: string,
): Promise<void> {
  await selectCell(page, coord);
  await formulaBar(page).fill(value);
  await page.keyboard.press('Enter');
}

// 点击选中后直接键入(REQ-3-1:选中即可在网格中修改),commit 提交。
export async function typeIntoCell(
  page: Page, coord: string, text: string, commit = 'Enter',
): Promise<void> {
  await selectCell(page, coord);
  await page.keyboard.type(text);
  await page.keyboard.press(commit);
}

export async function dragSelect(
  page: Page, from: string, to: string,
): Promise<void> {
  const a = await cell(page, from).boundingBox();
  const b = await cell(page, to).boundingBox();
  if (!a || !b) throw new Error(`boundingBox failed for ${from}:${to}`);
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.up();
}

export async function cellAriaSelected(
  page: Page, coord: string,
): Promise<string | null> {
  return cell(page, coord).getAttribute('aria-selected');
}

// 行号菜单(REQ-2-2-1:"Insert 1 row above"/"Insert 1 row below"/
// "Delete row")。行头优先按 ARIA rowheader 约定右键打开。
export async function openRowMenu(page: Page, row: number): Promise<void> {
  const header = page.getByRole('rowheader', { name: String(row), exact: true });
  await header.click({ button: 'right' });
}

export async function openColumnMenu(page: Page, col: string): Promise<void> {
  const header = page.getByRole('columnheader', { name: col, exact: true });
  await header.click({ button: 'right' });
}

// 工作表标签菜单(REQ-2-1 文件夹):每个标签提供
// "Worksheet options for <name>" 按钮,菜单命令为 menuitem 角色。
export async function openSheetMenu(
  page: Page, sheet: string,
): Promise<void> {
  await page.getByRole('button', { name: `Worksheet options for ${sheet}` })
    .click();
}

// "Data" 菜单(REQ-5 文件夹):编辑器工具栏按钮,命令为 menuitem。
export async function openDataMenu(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Data' }).click();
}

export async function dataMenuItem(page: Page, name: string): Promise<void> {
  await openDataMenu(page);
  await page.getByRole('menuitem', { name, exact: true }).click();
}

// 整理辅助:删除一行(经行号菜单),用于破坏性 spec 的现场还原。
export async function deleteRow(page: Page, row: number): Promise<void> {
  await openRowMenu(page, row);
  await page.getByRole('menuitem', { name: 'Delete row', exact: true })
    .click();
}

export async function addWorksheet(page: Page): Promise<string> {
  await page.getByRole('button', { name: 'Add worksheet' }).click();
  const tab = page.getByRole('tab').filter({ hasText: /Sheet\d+/ }).last();
  await expect(tab).toBeVisible();
  const name = (await tab.textContent())?.trim() ?? 'Sheet9';
  return name;
}

// 删除工作表(REQ-2-1-4:确认对话框含目标名 + "Delete worksheet" 按钮)。
export async function deleteWorksheet(page: Page, sheet: string): Promise<void> {
  await openSheetMenu(page, sheet);
  await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Delete worksheet' });
  await expect(dialog).toContainText(sheet);
  await dialog.getByRole('button', { name: 'Delete worksheet' }).click();
  await expect(page.getByRole('tab', { name: sheet, exact: true }))
    .toHaveCount(0);
}

export async function reload(page: Page): Promise<void> {
  await page.reload();
  await expect(grid(page)).toBeVisible();
}
