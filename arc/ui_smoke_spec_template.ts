// contract-smoke.spec.ts — HOST-OWNED (read only, generated).
// Generated from the requirement tree by arc/ui_smoke.py. Six probe classes
// catching the defect families that shipped blind in the hackathon--sheet
// 3-point run. Never edit; the host regenerates it.
import { test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const CONTRACT = __CONTRACT_JSON__ as { exact: string[]; anchors: { kind: string; value: string }[]; auth: { username: string; passwords: string[] } | null };
const OUT = path.join(__dirname, 'last-run.jsonl');
// red-shots: the spec screenshots the page the moment a red fires (capped);
// the host pipes these through its visual lane so the fix turn can SEE the
// failing page instead of guessing from an abstract message.
const SHOT_DIR = path.join(__dirname, 'red-shots');
try { fs.mkdirSync(SHOT_DIR, { recursive: true }); } catch { /* best effort */ }
let page0: any = null;
let shots = 0;
// jsonl append per red: module state may reload between tests, so reds are
// never buffered in memory — every red lands on disk the moment it fires.
function red(probe: string, s: string, detail: string) {
  try { fs.appendFileSync(OUT, JSON.stringify({ probe, string: s, detail }) + '\n'); } catch { /* best effort */ }
  if (page0 && shots < 6) {
    shots += 1;
    const safe = `${String(shots).padStart(2, '0')}-${probe}-${s}`
      .replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 70);
    try { page0.screenshot({ path: path.join(SHOT_DIR, safe + '.png') }); } catch { /* best effort */ }
  }
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

const CLICK_ROLES = ['button', 'menuitem', 'link', 'tab', 'option', 'combobox'] as const;
// Carriers split (2026-09-27, sheet/github 二跑教训):
//  - INTERACTIVE: controls graders operate (click/selectOption). Native
//    <option> included — a closed <select>'s options have no client rects,
//    so they get a parent-visibility exemption. The sheet run's "hidden
//    option" reds pushed the model into a custom div combo that broke every
//    selectOption grader — options ARE satisfiable carriers.
//  - STRUCTURAL: headings/labels/table cells/dialog names/aria-labels on any
//    element. aria-label counts as a name (the github run's text-only scan
//    pushed "Account menu" from aria-label+username to literal text).
//  - Deliberately EXCLUDED: bare spans/divs/p. A contract string surfaced as
//    static text (the github run's 35-chip "tour panel") clears NOTHING.
const INTERACTIVE_SELECTOR = 'button, a, select, input, textarea, option, ' +
  '[role="menuitem"], [role="tab"], [role="option"], [role="combobox"]';
const STRUCTURAL_SELECTOR = 'h1, h2, h3, label, legend, th, td, ' +
  '[role="columnheader"], [role="rowheader"], [role="gridcell"], ' +
  '[role="dialog"], [aria-label], [aria-labelledby]';

// Counts per carrier selector. Options ride their parent <select>'s
// visibility (a closed select hides its options off-DOM-wise; they are still
// selectable via selectOption — the only legitimate reading of an option).
async function carrierCounts(page: any, sel: string): Promise<Record<string, number>> {
  return page.evaluate((s: string) => {
    const counts: Record<string, number> = {};
    for (const el of document.querySelectorAll(s)) {
      const doc = el.ownerDocument;
      let visible = true;
      if (el.tagName === 'OPTION') {
        const sel = el.closest('select');
        visible = !!sel && !!sel.getClientRects().length;
      } else {
        const cs = getComputedStyle(el);
        visible = cs.display !== 'none' && cs.visibility !== 'hidden'
          && !!el.getClientRects().length;
      }
      if (!visible) continue;
      const name = nameOf(el, doc);
      if (!name || name.length > 120) continue;
      counts[name] = (counts[name] ?? 0) + 1;
    }
    function nameOf(el: any, doc: Document): string {
      let name = el.getAttribute('aria-label') ?? '';
      if (!name) {
        const labelledby = el.getAttribute('aria-labelledby');
        if (labelledby) {
          name = labelledby.split(/\s+/).map((id: string) =>
            doc.getElementById(id)?.textContent ?? '').join(' ');
        }
      }
      if (!name) name = el.getAttribute('title') ?? '';
      if (!name) {
        const own = [...el.childNodes].filter((n) => n.nodeType === 3)
          .map((n) => n.textContent).join(' ');
        name = own.trim() || (el.textContent ?? '');
      }
      return name.replace(/\s+/g, ' ').trim();
    }
    return counts;
  }, sel);
}

// Contract strings are cleared only by carriers: interactive controls or
// structural name elements. Static text (spans/divs) proves nothing — the
// github run rendered 35 contract strings as chips on the signed-out home
// page just to silence this crawler; that path must stay dead.
async function visibleNames(page: any): Promise<Record<string, number>> {
  const a = await carrierCounts(page, INTERACTIVE_SELECTOR);
  const b = await carrierCounts(page, STRUCTURAL_SELECTOR);
  return { ...a, ...b };
}

// Clickable-only name counts: strict-mode violations are about controls the
// graders address by role (button/link/tab/option/menuitem/combobox/select).
async function clickableCounts(page: any): Promise<Record<string, number>> {
  return carrierCounts(page, INTERACTIVE_SELECTOR);
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

// Generic sign-in for the seeded fixture account. Returns true when a
// password box is gone after submit (= authenticated, best effort).
async function trySignIn(page: any, auth: { username: string; passwords: string[] }): Promise<boolean> {
  await page.keyboard.press('Escape').catch(() => {});
  await page.mouse.click(5, 5).catch(() => {});
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);
  try {
    // The sign-in form often sits behind a header "Sign in" button or link.
    let pw = page.locator('input[type="password"]').filter({ visible: true }).first();
    if (!(await pw.count())) {
      const openerBtn = page.getByRole('button', { name: /^sign in$/i }).filter({ visible: true }).first();
      const openerLink = page.getByRole('link', { name: /^sign in$/i }).filter({ visible: true }).first();
      const opener = (await openerBtn.count()) ? openerBtn : openerLink;
      if (await opener.count()) {
        await opener.click({ timeout: 2000 });
        await page.waitForTimeout(500);
      }
      pw = page.locator('input[type="password"]').filter({ visible: true }).first();
    }
    if (!(await pw.count())) return false;
    const userBox = page.getByRole('textbox', { name: /user|email/i }).filter({ visible: true }).first();
    const submit = page.locator('button[type="submit"], input[type="submit"]')
      .filter({ visible: true }).last();
    const named = page.getByRole('button', { name: /sign in|log ?in/i }).filter({ visible: true }).last();
    const go = (await named.count()) ? named : submit;
    for (const candidate of auth.passwords) {
      if (await userBox.count()) await userBox.fill(auth.username, { timeout: 2000 }).catch(() => {});
      await pw.fill(candidate, { timeout: 2000 }).catch(() => {});
      if (!(await go.count())) break;
      await go.click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(900);
      const stillPw = page.locator('input[type="password"]').filter({ visible: true }).first();
      if (!(await stillPw.count())) return true;
    }
  } catch { /* best effort */ }
  return false;
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

// Auth-gate (2026-09-26 github 6-point run): the platform e2e signed in fine
// (residue: 3 users + 8 sessions) then EVERY test died at its first
// signed-in assertion and the runner bailed — ~39 tests never executed. The
// literal contract ("the resulting signed-in username remains visible after
// reload") requires the username as its OWN visible element. This probe is
// the single highest-leverage check for any auth-gated app.
test('auth-gate - signed-in username exact-visible across reload', async ({ page }) => {
  test.setTimeout(60_000);
  page0 = page;
  if (!(CONTRACT.auth && CONTRACT.auth.username)) return; // no auth gate
  const u = CONTRACT.auth.username;
  const signedIn = await trySignIn(page, CONTRACT.auth);
  if (!signedIn) {
    red('auth-gate', 'sign-in', `fixture account ${u} could not sign in (tried ${CONTRACT.auth.passwords.length} password candidates) — every gated contract string is unreachable; fix the sign-in flow first`);
    failIfReds('auth-gate');
    return;
  }
  const exactVisible = async (): Promise<number> =>
    (await page.getByText(u, { exact: true }).filter({ visible: true }).count());
  if (!(await exactVisible())) {
    red('auth-gate', u, `signed in as ${u} but NO element carries the username as its own visible exact text — graders assert the signed-in username (and often click it to open the account menu); render it as its own element, e.g. <button aria-label="Account menu"><span class="...">{username}</span></button>; a heading like "Welcome, {username}" does NOT satisfy exact-text locators`);
  } else {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);
    if (!(await exactVisible())) {
      red('auth-gate', u, `username visible after sign-in but GONE after reload — the session must restore AND keep the username exactly visible (literal contract: "the signed-in username remains visible after reload")`);
    }
  }
  failIfReds('auth-gate');
});

test('contract crawler - openable, exact-named, closable, page stays alive', async ({ page }) => {
  test.setTimeout(240_000);
  page0 = page;
  const found = new Set<string>();
  const visited = new Set<string>();
  let interactions = 0;
  const crawlStart0 = Date.now();

  // strict-mode: a contract string may match exactly ONE visible clickable;
  // two simultaneous matches break every strict-mode locator (round 31/32
  // scoring family). Flagged once per string per crawl.
  const strictFlagged = new Set<string>();
  const strictCheck = async () => {
    const counts = await clickableCounts(page);
    for (const s of CONTRACT.exact) {
      if (strictFlagged.has(s) || !found.has(s)) continue;
      if ((counts[s] ?? 0) > 1) {
        strictFlagged.add(s);
        red('strict-mode', s, `string matches ${counts[s]} visible clickable controls — Playwright strict mode fails with ${counts[s]} matches; keep exactly ONE visible element per value`);
      }
    }
  };

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
    await trySignIn(page, CONTRACT.auth);
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

  // Grid completeness (sheet run 2: a grid that renders ONLY the data region
  // destroyed ~7 requirements): a role="grid" must expose visible column and
  // row headers, and its cells must carry coordinate accessible names.
  const gridProbe = async () => {
    const grid = page.locator('[role="grid"]').first();
    if (!(await grid.count())) return;
    const colHeads = grid.getByRole('columnheader').filter({ visible: true });
    const rowHeads = grid.getByRole('rowheader').filter({ visible: true });
    if (!(await colHeads.count()) || !(await rowHeads.count())) {
      red('grid-structure', 'grid', 'a role="grid" is present but lacks visible columnheader and/or rowheader cells — render the complete grid (column letters, row numbers), a data-only region fails every structural grader step');
      return;
    }
    const cells = grid.getByRole('gridcell').filter({ visible: true });
    const n = Math.min(await cells.count(), 8);
    for (let i = 0; i < n; i++) {
      const label = await cells.nth(i).getAttribute('aria-label').catch(() => null);
      if (label && /^[A-Z]{1,3}\d+$/.test(label.trim())) return; // one is enough
    }
    if (n > 0) {
      red('grid-structure', 'gridcell', 'gridcells carry no coordinate accessible name (aria-label like "A1") — graders locate cells by coordinate');
    }
  };
  // Spreadsheet grids (column headers are coordinate letters A/B/C…) must
  // render a FIXED-SIZE empty grid, not just the data extent (sheet run 2:
  // a 4×3 data-only grid made A5/D5/A9 unselectable and killed ~8
  // requirements at their first click).
  const gridExtentProbe = async () => {
    const grid = page.locator('[role="grid"]').first();
    if (!(await grid.count())) return;
    const heads = grid.getByRole('columnheader').filter({ visible: true });
    const cnt = Math.min(await heads.count(), 3);
    let letters = 0;
    for (let i = 0; i < cnt; i++) {
      const t = ((await heads.nth(i).textContent()) ?? '').trim();
      if (/^[A-Z]{1,3}$/.test(t)) letters += 1;
    }
    if (letters < Math.min(cnt, 2)) return; // not a spreadsheet grid
    const rows = grid.getByRole('rowheader').filter({ visible: true });
    if ((await heads.count()) < 8 || (await rows.count()) < 15) {
      red('grid-structure', 'grid-extent',
        `spreadsheet grid renders only ${await heads.count()} columns × ${await rows.count()} rows — render a FIXED-SIZE empty grid (≥8 columns × ≥15 rows) so graders can select and type into EMPTY cells beyond the seeded data`);
    }
  };
  // Custom dropdown widgets are invisible to graders (sheet run 2 REQ-5
  // family): a non-native combobox without aria-expanded cannot be opened by
  // getByRole('combobox') flows; the fix is a native <select>.
  const comboboxProbe = async () => {
    const boxes = page.locator('[role="combobox"]').filter({ visible: true });
    const n = Math.min(await boxes.count(), 4);
    for (let i = 0; i < n; i++) {
      const b = boxes.nth(i);
      const tag = (await b.evaluate((el: any) => el.tagName)).toUpperCase();
      if (tag === 'SELECT') continue; // native select = the correct pattern
      const expanded = await b.getAttribute('aria-expanded').catch(() => null);
      if (expanded === null) {
        red('combobox', 'dropdown', 'a custom dropdown widget lacks aria-expanded — without full ARIA combobox semantics graders cannot open or select it; use a native <select> with real <option> labels');
      }
    }
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
  await gridProbe();
  await gridExtentProbe();
  await comboboxProbe();

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
  // sweep. Signed-out-only strings are re-seeked after the sweeps. The
  // queue stays document-order: found strings are still CLICKED (their
  // dialogs reveal nested strings — deferring them breaks the opener chain,
  // tried 2026-09-27 and reverted).
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
        'no interactive/structural carrier exposes this exact name and no control opens it — the chain is missing. Fix by making the REAL control reachable; static text (a span/chip listing the string) does NOT clear reds');
      continue;
    }
    if (!(await loc.isEnabled().catch(() => true))) {
      found.add(s); // presence proven either way …
      // … but a PERMANENTLY disabled control can never be clicked by a test
      // (sheet run 2: Save stayed disabled forever). Genuinely conditional
      // controls (Undo/Redo with empty stack, destructive confirms) are the
      // exempt classes.
      if (!DESTRUCTIVE.test(s) && !/^(undo|redo)/i.test(s)) {
        red('disabled-only', s, 'control exists but is DISABLED at rest — if it is a submit that should enable once required inputs are filled, fix the enablement logic (conditionally-disabled controls like Undo/Delete-when-unselected are exempt)');
      }
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
  await strictCheck();
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

  // Signed-out re-sweep (2026-09-27): registration / sign-in / password-reset
  // contract strings live ONLY in the signed-out state; the crawl above ran
  // signed-in, so without this pass every one of them false-reds and the fix
  // loop gets pushed at phantom defects. Drop the session, walk the classic
  // signed-out entry pages, then re-scan and re-seek what is still missing.
  if (CONTRACT.auth && CONTRACT.auth.username) {
    try { await page.context().clearCookies(); } catch { /* best effort */ }
    await page.evaluate(() => {
      try { localStorage.clear(); sessionStorage.clear(); } catch { /* best effort */ }
    }).catch(() => {});
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    await scan();
    await trackAnchors();
    // Mini-walk over the classic signed-out entry chain (home → sign-in →
    // "Create an account" → registration form; → "Forgot password" → reset
    // form → code form): each page reveals further entries, so each hop
    // clicks one not-yet-clicked entry-named control, returning home when
    // the current page is exhausted (bounded). One email-form submit is
    // included: reset/code forms sit behind a submitted email.
    const ENTRY_RE = /create an account|sign in|log ?in|sign up|forgot|reset/i;
    const soClicked = new Set<string>();
    let emailSubmitted = false;
    for (let hop = 0; hop < 10; hop++) {
      const pick = async (): Promise<any> => {
        const cands = page.locator('a, button').filter({ visible: true, hasText: ENTRY_RE });
        const n = await cands.count();
        for (let i = 0; i < n; i++) {
          const c = cands.nth(i);
          const t = (((await c.textContent().catch(() => '')) ?? '').trim());
          if (t && !soClicked.has(t)) { soClicked.add(t); return c; }
        }
        return null;
      };
      let target = await pick();
      if (!target) {
        // On the CURRENT page first: a visible email form (reset step 1)
        // submits once — the code/verification form sits behind it.
        if (!emailSubmitted) {
          const email = page.getByRole('textbox', { name: /email/i }).filter({ visible: true }).first();
          const go = page.locator('button[type="submit"], button').filter({ visible: true }).last();
          if ((await email.count()) && (await go.count())) {
            emailSubmitted = true;
            await email.fill('smoke@example.test', { timeout: 1500 }).catch(() => {});
            await go.click({ timeout: 1500 }).catch(() => {});
            await page.waitForTimeout(600);
            await scan();
            await trackAnchors();
            continue;
          }
        }
        await page.goto('/', { waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(400);
        target = await pick();
        if (!target) break;
      }
      await target.click({ timeout: 2000 }).catch(() => {});
      await page.waitForTimeout(400);
      await scan();
      await trackAnchors();
      if (await dialogOpen()) await closeAttempt('signed-out re-sweep');
    }
    for (const s of CONTRACT.exact) {
      if (!found.has(s)) await deepSeek(s);
    }
    // A dialog opened during the re-seek must not linger.
    if (await dialogOpen()) await closeAttempt('signed-out re-sweep');
  }
  // Reconcile: an affordance red fired at dequeue time, before a later pass
  // (signed-out re-sweep, edit-mode anchors) satisfied its string. Strings
  // now found must not stay red — the fix loop must only see REAL gaps.
  {
    const all = redsOnDisk();
    const stale = all.filter((r: any) =>
      r.probe === 'affordance' && found.has(String(r.string)));
    if (stale.length) {
      const keep = all.filter((r: any) => !stale.includes(r));
      try {
        fs.writeFileSync(OUT, keep.map((r: any) => JSON.stringify(r)).join('\n') + '\n');
      } catch { /* best effort */ }
    }
  }
  failIfReds('affordance');
  failIfReds('click');
  failIfReds('exact-name');
  failIfReds('escape-close');
  failIfReds('page-alive');
  failIfReds('shift-select');
  failIfReds('strict-mode');
  failIfReds('grid-structure');
  failIfReds('combobox');
  failIfReds('disabled-only');
});

test('downloads - non-empty, no BOM', async ({ page }) => {
  page0 = page;
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
