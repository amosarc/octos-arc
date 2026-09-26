// contract-smoke.spec.ts — HOST-OWNED (read only, generated).
// Generated from the requirement tree by arc/ui_smoke.py. Six probe classes
// catching the defect families that shipped blind in the hackathon--sheet
// 3-point run. Never edit; the host regenerates it.
import { test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const CONTRACT = __CONTRACT_JSON__ as { exact: string[]; anchors: { kind: string; value: string }[]; auth: { username: string; passwords: string[] } | null };
const OUT = path.join(__dirname, 'last-run.jsonl');
// jsonl append per red: module state may reload between tests, so reds are
// never buffered in memory — every red lands on disk the moment it fires.
function red(probe: string, s: string, detail: string) {
  try { fs.appendFileSync(OUT, JSON.stringify({ probe, string: s, detail }) + '\n'); } catch { /* best effort */ }
}
function redsOnDisk(): any[] {
  try {
    return fs.readFileSync(OUT, 'utf8').split('\n').filter(Boolean)
      .map((l: string) => JSON.parse(l));
  } catch { return []; }
}
function failIfReds(scope: string) {
  const mine = redsOnDisk().filter((r) => r.probe.startsWith(scope));
  if (mine.length) {
    throw new Error(mine.map((r) => `[${r.probe}] ${r.string}: ${r.detail}`).join('\n'));
  }
}

const CLICK_ROLES = ['button', 'menuitem', 'link', 'tab', 'option'] as const;
const NAME_SELECTOR = 'button, a, select, input, span, [role="menuitem"], ' +
  '[role="menu"], [role="dialog"], [role="grid"], [role="region"], ' +
  '[role="tab"], [role="option"], [role="textbox"], [role="columnheader"], ' +
  '[role="rowheader"], [role="gridcell"], h1, h2, h3, label, legend, th, td';

// Name = aria-label, else the element's OWN text nodes, else full textContent.
// Own-text-first matters for labels wrapping a <select>: their full
// textContent concatenates every option ("Sort byAscendingDescending"), which
// would hide the exact string "Sort by" AND fake-show hidden options.
async function visibleNames(page: any): Promise<Record<string, number>> {
  return page.evaluate((sel: string) => {
    const counts: Record<string, number> = {};
    for (const el of document.querySelectorAll(sel)) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      if (!el.getClientRects().length) continue;
      let name = el.getAttribute('aria-label') ?? '';
      if (!name) {
        const own = [...el.childNodes].filter((n) => n.nodeType === 3)
          .map((n) => n.textContent).join(' ');
        name = own.trim() || (el.textContent ?? '');
      }
      name = name.replace(/\s+/g, ' ').trim();
      if (!name || name.length > 120) continue;
      counts[name] = (counts[name] ?? 0) + 1;
    }
    return counts;
  }, NAME_SELECTOR);
}

async function openerLocator(page: any, name: string) {
  for (const role of CLICK_ROLES) {
    const loc = page.getByRole(role, { name, exact: true }).filter({ visible: true });
    if (await loc.count()) return loc.first();
  }
  return null;
}

async function anchorLocator(page: any, anchor: { kind: string; value: string }) {
  const esc = anchor.value.split('').map((ch) =>
    ('.*+?^${}()|[]\\'.includes(ch) ? '\\' + ch : ch)).join('');
  for (const role of CLICK_ROLES) {
    const name = new RegExp(anchor.kind === 'prefix' ? '^' + esc : esc + '$');
    const loc = page.getByRole(role, { name }).filter({ visible: true });
    if (await loc.count()) return loc.first();
  }
  return null;
}

test('boot - no pageerror, page not blank', async ({ page }) => {
  const errs: string[] = [];
  page.on('pageerror', (e) => errs.push(String(e).slice(0, 160)));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  if (errs.length) red('boot', 'pageerror', errs.join(' ; '));
  const body = (await page.locator('body').innerText().catch(() => '')) ?? '';
  if (body.trim().length < 10) red('boot', 'blank', 'page renders no visible text');
  failIfReds('boot');
});

test('contract crawler - openable, exact-named, closable, page stays alive', async ({ page }) => {
  test.setTimeout(240_000);
  const found = new Set<string>();
  const visited = new Set<string>();
  let interactions = 0;
  const crawlStart0 = Date.now();

  const scan = async () => {
    for (const n of Object.keys(await visibleNames(page))) found.add(n);
  };
  const dialogOpen = async () => (await page.locator('[role="dialog"]').count()) > 0;

  // Anchor satisfied at ANY point counts (the crawl itself may remove the
  // thing that satisfied it — clicking "Clear filter" drops the "Filter
  // <header>" buttons that had just appeared).
  const anchorEver = new Set<string>();
  const trackAnchors = async () => {
    for (const a of CONTRACT.anchors) {
      if (anchorEver.has(a.value)) continue;
      if (await anchorLocator(page, a)) anchorEver.add(a.value);
    }
  };

  const gotoHome = async () => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(400);
    await scan();
    await trackAnchors();
  };
  const coverage = () => CONTRACT.exact.filter((s) => found.has(s)).length;

  // Most contract strings live behind an entry link (workbook list →
  // editor). Walk visible links once, remember the richest view, and keep a
  // way back to it for the whole crawl.
  let bestIdx = -1;
  const enterBest = async () => {
    await gotoHome();
    if (bestIdx >= 0) {
      await page.locator('a').filter({ visible: true }).nth(bestIdx)
        .click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(400);
      await scan();
      await trackAnchors();
    }
  };
  const enterRichestView = async () => {
    await gotoHome();
    const links = page.locator('a').filter({ visible: true });
    const n = Math.min(await links.count(), 5);
    let bestCov = -1;
    for (let i = 0; i < n; i++) {
      await gotoHome();
      await page.locator('a').filter({ visible: true }).nth(i)
        .click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(350);
      await scan();
      await trackAnchors();
      const cov = coverage();
      if (cov > bestCov) { bestCov = cov; bestIdx = i; }
    }
    await enterBest();
  };
  // Auth prelude: most contract strings of an auth-gated app live behind
  // the sign-in — and the signed-OUT strings (registration, sign-in form)
  // live only BEFORE it. Sweep 0 crawls signed-out; this prelude logs in
  // once with the seeded fixture account (best effort); sweep 1 crawls the
  // signed-in state.
  const authPrelude = async () => {
    if (!(CONTRACT.auth && CONTRACT.auth.username)) return;
    // Sweep 0 may have left a modal/overlay open — dismiss before login.
    await page.keyboard.press('Escape').catch(() => {});
    await page.mouse.click(5, 5).catch(() => {});
    await gotoHome();
    await page.waitForTimeout(300);
    try {
      // The sign-in form often sits behind a header "Sign in" button/modal.
      let pw = page.locator('input[type="password"]').filter({ visible: true }).first();
      if (!(await pw.count())) {
        const opener = page.getByRole('button', { name: /^sign in$/i }).filter({ visible: true }).first();
        if (await opener.count()) {
          await opener.click({ timeout: 2000 });
          await page.waitForTimeout(500);
        }
        pw = page.locator('input[type="password"]').filter({ visible: true }).first();
      }
      if (await pw.count()) {
        const userBox = page.getByRole('textbox', { name: /user|email/i }).filter({ visible: true }).first();
        const submit = page.locator('button[type="submit"], input[type="submit"]')
          .filter({ visible: true }).last();
        const named = page.getByRole('button', { name: /sign in|log ?in/i }).filter({ visible: true }).last();
        const go = (await named.count()) ? named : submit;
        // Try each seeded password candidate until one sticks (the failure
        // banner disappears).
        for (const candidate of CONTRACT.auth.passwords) {
          if (await userBox.count()) await userBox.fill(CONTRACT.auth.username, { timeout: 2000 }).catch(() => {});
          await pw.fill(candidate, { timeout: 2000 }).catch(() => {});
          if (!(await go.count())) break;
          await go.click({ timeout: 2000 }).catch(() => {});
          await page.waitForTimeout(900);
          const stillPw = page.locator('input[type="password"]').filter({ visible: true }).first();
          if (!(await stillPw.count())) break;
        }
      }
    } catch { /* best effort */ }
  };
  await enterRichestView();

  // Data features (filter/sort/validation/pivot) operate on a SELECTED
  // range — graders select one first; do the same once so those chains are
  // reachable (click first cell, Shift-click a mid-range cell).
  {
    const g = page.locator('[role="grid"]').first();
    if (await g.count()) {
      const cells = g.getByRole('gridcell').filter({ visible: true });
      if (await cells.count() >= 6) {
        // DRAG, not Shift-click: apps with collapsed-selection bugs (the
        // very defect shift-select flags) still honor drag ranges, and the
        // data features need a real multi-cell region.
        const a = await cells.nth(0).boundingBox();
        const b = await cells.nth(5).boundingBox();
        if (a && b) {
          await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
          await page.mouse.down();
          await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 6 });
          await page.mouse.up();
          await page.waitForTimeout(250);
        }
      }
    }
  }

  // While a dialog is open: a dialog BUTTON that only CONTAINS a contract
  // string ("Delete worksheet" vs contract "Delete") is an exact-name
  // violation — graders locate confirm buttons by their exact names.
  const dialogProbe = async (via: string) => {
    if (!(await dialogOpen())) return;
    const dlg = page.locator('[role="dialog"]').first();
    const label = (await dlg.getAttribute('aria-label').catch(() => null)) ?? via;
    const names: string[] = await dlg.evaluate((root: any) => {
      const out: string[] = [];
      for (const b of root.querySelectorAll('button')) {
        const cs = getComputedStyle(b);
        if (cs.display === 'none' || cs.visibility === 'hidden' || !b.getClientRects().length) continue;
        out.push((b.getAttribute('aria-label') ?? b.textContent ?? '').replace(/\s+/g, ' ').trim());
      }
      return out.filter((nm) => nm.length >= 3);
    });
    for (const b of names) {
      for (const s of CONTRACT.exact) {
        if (b === s || s.length < 3) continue;
        if (b.startsWith(s) && b.length !== s.length && b.length - s.length <= 24) {
          red('exact-name', s, `dialog "${label}" exposes a button named "${b}" — the contract string is only a substring; the exact accessible name "${s}" is required`);
        }
      }
    }
  };

  const closeAttempt = async (via: string) => {
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(150);
    const escClosed = !(await dialogOpen());
    if (!escClosed) {
      const cancel = page.getByRole('button', { name: 'Cancel', exact: true }).first();
      if (await cancel.count()) { await cancel.click().catch(() => {}); await page.waitForTimeout(150); }
    }
    if (await dialogOpen()) {
      red('escape-close', via, 'dialog stayed open after Escape AND Cancel — an undismissable overlay blocks every later grader step');
    } else if (!escClosed && via !== 'reset') {
      red('escape-close', via, 'Escape does not close the opened dialog (Cancel did) — grader flows that dismiss with Escape hang on the leftover overlay');
    }
  };
  const canary = async (via: string) => {
    await page.mouse.click(5, 5).catch(() => {}); // dismiss leftover menus
    await page.waitForTimeout(100);
    const target = page.locator('h1').first();
    if (!(await target.count())) return;
    try { await target.click({ timeout: 1200 }); }
    catch { red('page-alive', via, 'page stopped accepting clicks after this interaction (stuck overlay)'); }
  };

  // Context menus (row/column headers) carry their own contract menu items.
  // Skip the unlabeled corner cell — right-clicking it is a no-op.
  const contextProbe = async () => {
    const grid = page.locator('[role="grid"]').first();
    if (!(await grid.count())) return;
    for (const part of ['rowheader', 'columnheader']) {
      const heads = grid.getByRole(part as any)
        .filter({ visible: true, hasText: /\S/ });
      const n = Math.min(await heads.count(), 2);
      for (let i = 0; i < n; i++) {
        interactions += 1;
        await heads.nth(i).click({ button: 'right', timeout: 2500 }).catch(() => {});
        await page.waitForTimeout(300);
        await scan();
        await page.keyboard.press('Escape').catch(() => {});
        await page.mouse.click(5, 5).catch(() => {});
        await page.waitForTimeout(200);
      }
    }
  };
  await contextProbe();

  // Destructive dialog confirms stay unclicked; everything else (Sort,
  // Apply, Create, Save) is safe on this throwaway instance and unlocks the
  // states their strings live in (a created pivot sheet exposes "Refresh
  // pivot table").
  const DESTRUCTIVE = /^(delete|remove|clear|reset|confirm|discard|drop)/i;
  const confirmProbe = async (via: string) => {
    if (!(await dialogOpen())) return;
    for (const s of CONTRACT.exact) {
      if (DESTRUCTIVE.test(s)) continue;
      const btn = page.locator('[role="dialog"]').first()
        .getByRole('button', { name: s, exact: true }).filter({ visible: true }).first();
      if (!(await btn.count())) continue;
      if (!(await btn.isEnabled().catch(() => true))) continue;
      interactions += 1;
      await btn.click({ timeout: 2500 }).catch(() => {});
      await page.waitForTimeout(400);
      await scan();
      await trackAnchors();
      break; // one confirm per dialog is enough to unlock its state
    }
  };

  // BFS over contract-named openers. Anchors first: option menus ("Worksheet
  // options for <name>") unlock the Rename/Delete items cheaply. Clicking an
  // opener often reveals the next (toolbar → menu → menuitem → dialog).
  // A string already SEEN is still CLICKED when its control is reachable —
  // the click opens the view/dialog where further strings live.
  const locateAnywhere = async (s: string) => {
    let loc = await openerLocator(page, s);
    if (loc) return loc;
    await gotoHome(); // the create form / import dialog live on home
    loc = await openerLocator(page, s);
    if (loc) return loc;
    await enterBest(); // richest view (usually the editor)
    return await openerLocator(page, s);
  };
  const locateAnchorAnywhere = async (anchor: { kind: string; value: string }) => {
    let loc = await anchorLocator(page, anchor);
    if (loc) return loc;
    await gotoHome();
    loc = await anchorLocator(page, anchor);
    if (loc) return loc;
    await enterBest();
    return await anchorLocator(page, anchor);
  };
  type QEntry = { kind: 'exact' | 'anchor'; value: string; anchor?: { kind: string; value: string } };
  const queue: QEntry[] = [
    ...CONTRACT.anchors.map((a) => ({ kind: 'anchor' as const, value: a.value, anchor: a })),
    ...CONTRACT.exact.map((s) => ({ kind: 'exact' as const, value: s })),
  ];
  // Transient containers (menus, dialogs) close before a deferred queue
  // entry would reach them — process their items RIGHT HERE, depth-first.
  // Menus often close after EACH item click, so `reopen` re-clicks the
  // container's own opener before every subsequent item.
  const processTransient = async (depth: number, reopen: (() => Promise<void>) | null) => {
    if (depth > 3 || interactions >= 70 || Date.now() - crawlStart0 > 150_000) return;
    const pending: string[] = [];
    for (const s of CONTRACT.exact) {
      if (visited.has('exact:' + s) || !found.has(s)) continue;
      if (await openerLocator(page, s)) pending.push(s);
    }
    for (const s of pending) {
      if (interactions >= 70) return;
      visited.add('exact:' + s);
      let loc = await openerLocator(page, s);
      if (!loc && reopen) {
        // A leftover PARENT menu may be covering the container's opener —
        // dismiss stray menus first, then give the reopen two attempts
        // (menus re-open asynchronously and race React's state settle).
        for (let a = 0; a < 2 && !loc; a++) {
          await page.keyboard.press('Escape').catch(() => {});
          await page.mouse.click(5, 5).catch(() => {});
          await page.waitForTimeout(150);
          await reopen();
          await page.waitForTimeout(400);
          loc = await openerLocator(page, s);
        }
      }
      if (!loc) continue;
      if (!(await loc.isEnabled().catch(() => true))) { visited.add('exact:' + s); continue; }
      visited.add('exact:' + s);
      interactions += 1;
      await loc.click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(200);
      await scan();
      await dialogProbe(s);
      await confirmProbe(s);
      // Each container re-opens ITSELF: the child's reopen is built from the
      // item that just opened it (re-clicking the parent's opener would
      // re-open the PARENT menu, stranding sibling items again).
      const childReopen = async () => {
        const again = await locateAnywhere(s);
        if (again) await again.click({ timeout: 2000 }).catch(() => {});
      };
      await processTransient(depth + 1, childReopen);
      await closeAttempt(s);
    }
  };
  // Missing-string fallback before flagging: some strings sit behind a
  // non-contract toggle ("Filter by condition" reveals "Condition"), so try
  // up to TWO containing-named clickable controls.
  const deepSeek = async (s: string): Promise<boolean> => {
    let tries = 0;
    for (const role of ['button', 'menuitem'] as const) {
      const cand = page.getByRole(role, { name: s }).filter({ visible: true });
      const cnt = Math.min(await cand.count(), 2);
      for (let i = 0; i < cnt && tries < 2; i++) {
        tries += 1;
        interactions += 1;
        await cand.nth(i).click({ timeout: 1500 }).catch(() => {});
        await page.waitForTimeout(200);
        await scan();
        if (found.has(s)) return true;
        await closeAttempt(s);
      }
    }
    return found.has(s);
  };

  const crawlStart = Date.now();
  // Auth-gated apps: log in BEFORE the crawl — the signed-in state carries
  // the overwhelming majority of contract strings and gets the clean first
  // sweep. Signed-out-only strings (registration/reset forms) become known
  // noise-reds; the static layer still covers them verbatim.
  await authPrelude();
  for (let sweep = 0; sweep < 2; sweep++) {
  if (sweep > 0) { await enterBest(); }
  while (queue.length && interactions < (sweep === 0 ? 45 : 75)
         && Date.now() - crawlStart < 150_000) {
    const item = queue.shift() as QEntry;
    const key = item.kind + ':' + item.value;
    if (visited.has(key)) continue;
    visited.add(key);
    if (await dialogOpen()) { await closeAttempt(item.value); await canary(item.value); }

    if (item.kind === 'anchor' && item.anchor) {
      const loc = await locateAnchorAnywhere(item.anchor);
      if (loc) {
        interactions += 1;
        await loc.click({ timeout: 2500 }).catch(() => {});
        await page.waitForTimeout(200);
        await scan();
        const reopen = async () => {
          const again = await locateAnchorAnywhere(item.anchor!);
          if (again) await again.click({ timeout: 2500 }).catch(() => {});
        };
        await processTransient(1, reopen);
        await closeAttempt(item.value);
        await canary(item.value);
      }
      continue;
    }

    const s = item.value;
    const loc = await locateAnywhere(s);
    if (!loc) {
      if (!found.has(s) && !(await deepSeek(s))) red('affordance', s,
        'no visible element carries this exact name and no control opens it — the chain is missing (or it only exists as a hidden <option>/inner tab)');
      continue;
    }
    if (!(await loc.isEnabled().catch(() => true))) {
      found.add(s); // disabled (e.g. Undo with empty stack) still proves presence
      continue;
    }
    interactions += 1;
    try { await loc.click({ timeout: 2500 }); }
    catch { red('click', s, 'named control present but not clickable (covered or hidden)'); continue; }
    await page.waitForTimeout(200);
    await scan();
    const reopen = async () => {
      const again = await locateAnywhere(s);
      if (again) await again.click({ timeout: 2500 }).catch(() => {});
    };
    await dialogProbe(s);
    await confirmProbe(s);
    await processTransient(1, reopen);
    await closeAttempt(s);
    await canary(s);
  }

  // Edit-mode anchors ("Edit <cell coordinate>") exist only while a cell
  // edit is active — dblclick the first grid cell once to surface them.
  const anyGrid = page.locator('[role="grid"]').first();
  if (await anyGrid.count()) {
    const c0 = anyGrid.getByRole('gridcell').filter({ visible: true }).first();
    if (await c0.count()) {
      await c0.dblclick({ timeout: 2500 }).catch(() => {});
      await page.waitForTimeout(300);
      await scan();
      await trackAnchors();
      await page.keyboard.press('Escape').catch(() => {});
    }
  }
  await trackAnchors();
  for (const anchor of CONTRACT.anchors) {
    if (anchorEver.has(anchor.value)) continue;
    if (await anchorLocator(page, anchor)) continue;
    const text = page.getByText(anchor.value, { exact: false }).first();
    if ((await text.count()) && (await text.isVisible().catch(() => false))) continue;
    let seen = false;
    for (const n of found) {
      if (anchor.kind === 'prefix' ? n.startsWith(anchor.value) : n.endsWith(anchor.value)) seen = true;
    }
    if (!seen) red('anchor-warn', anchor.value + '*',
      `no reachable element name ${anchor.kind === 'prefix' ? 'starts' : 'ends'} with this anchor (placeholder anchor — verify by hand)`);
  }

  // Shift-click must not collapse selection (one bug family that killed 5
  // requirements in the sheet run).
  if (await anyGrid.count()) {
    const cells = anyGrid.getByRole('gridcell').filter({ visible: true });
    if (await cells.count() >= 2) {
      await cells.nth(0).click({ timeout: 2500 }).catch(() => {});
      await cells.nth(1).click({ timeout: 2500, modifiers: ['Shift'] }).catch(() => {});
      const a0 = await cells.nth(0).getAttribute('aria-selected').catch(() => null);
      const a1 = await cells.nth(1).getAttribute('aria-selected').catch(() => null);
      if (a0 !== 'true' || a1 !== 'true') {
        red('shift-select', `${a0 ?? '?'}/${a1 ?? '?'}`,
          'after click + Shift-click both cells must be aria-selected="true" — a collapsed selection breaks every range feature (sort/filter/validation/pivot ranges)');
      }
    }
  }
  }
  failIfReds('affordance');
  failIfReds('click');
  failIfReds('exact-name');
  failIfReds('escape-close');
  failIfReds('page-alive');
  failIfReds('shift-select');
});

test('downloads - non-empty, no BOM', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(600);
  const exportish = CONTRACT.exact.filter((s) => /export|download|csv/i.test(s));
  if (!exportish.length) return;
  // Export controls usually live behind the entry link (editor view).
  const tryClick = async (s: string) => {
    const loc = await openerLocator(page, s);
    if (!loc) return;
    const dl = page.waitForEvent('download', { timeout: 4000 }).catch(() => null);
    await loc.click({ timeout: 2500 }).catch(() => {});
    const d = await dl;
    if (!d) return; // opens a dialog instead of downloading directly — fine
    const p = await d.path();
    if (!p) { red('download', s, 'download event fired but no file landed'); return; }
    const buf = fs.readFileSync(p);
    if (!buf.length) red('download', s, 'downloaded file is EMPTY');
    else if (buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) {
      red('download', s, 'downloaded file starts with a UTF-8 BOM — byte-exact comparisons fail');
    }
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(200);
  };
  for (const s of exportish) await tryClick(s);
  const links = page.locator('a').filter({ visible: true });
  const n = Math.min(await links.count(), 3);
  for (let i = 0; i < n; i++) {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    await page.locator('a').filter({ visible: true }).nth(i)
      .click({ timeout: 2500 }).catch(() => {});
    await page.waitForTimeout(500);
    for (const s of exportish) await tryClick(s);
  }
  failIfReds('download');
});
