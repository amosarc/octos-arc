"""
frontend 模板常量(纯标准库):React+Vite 模块化八件套。
"""

from __future__ import annotations

FRONTEND_PACKAGE_JSON = {
    "name": "frontend",
    "version": "1.0.0",
    "private": True,
    # ESM 包:src 全部 import/export 语法(jsx/app 库);node 侧单测
    # (csv.js)与 vite 消费同一语义,无双模块系统漂移。
    "type": "module",
    "scripts": {"build": "vite build"},
    "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
    "devDependencies": {"@vitejs/plugin-react": "^4.3.1", "vite": "^5.2.0"},
}

FRONTEND_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>App</title>
    <script data-host="__arcTelemetryHook">
      // Host-owned frontend error hook (best-effort): page errors and
      // unhandled rejections are beaconed to the backend telemetry endpoint
      // so eval-time crashes land in octos/logs/eval-traffic.jsonl.
      (function () {
        var send = function (kind, msg) {
          try {
            var payload = JSON.stringify({ kind: kind, msg: String(msg).slice(0, 300), href: location.href });
            navigator.sendBeacon('/api/__telemetry', new Blob([payload], { type: 'application/json' }));
          } catch (e) { /* best effort */ }
        };
        window.addEventListener('error', function (e) {
          send('pageerror', (e && e.message ? e.message : 'error') + ' @' + ((e && e.filename) || '') + ':' + ((e && e.lineno) || ''));
        });
        window.addEventListener('unhandledrejection', function (e) {
          send('unhandledrejection', e && e.reason !== undefined ? e.reason : 'rejection');
        });
      })();
    </script>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
"""

FRONTEND_MAIN_JSX = """\
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './app/App.jsx';
import './app/styles.css';

createRoot(document.getElementById('root')).render(<App />);
"""

FRONTEND_APP_JSX = """\
import React from 'react';
import { DataProvider } from './store.jsx';

// ASSEMBLY LAYER — the only file that wires features together.
// Per requirement: add ONE import of its feature component and mount it.
// The FIRST requirement usually lays out the page structure here; after
// that, keep edits to imports + mounts. Business logic NEVER lives here —
// it goes in frontend/src/features/<name>.jsx (self-contained).
export default function App() {
  return (
    <DataProvider>
      <div className="app">
        {/* mount feature components here */}
      </div>
    </DataProvider>
  );
}
"""

FRONTEND_STORE_JSX = """\
import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';
import { api } from './api.js';

// Shared-data layer: every backend resource is loaded through
// useResource('<resource>') so shared entities live in ONE place instead
// of being duplicated (and drifting) inside individual features.
// Features read `items` and call create/update/remove; the host backend
// (schema-driven engine) serves the REST endpoints.
const DataContext = createContext(null);

export function DataProvider({ children }) {
  const [collections, setCollections] = useState({});

  const ensure = useCallback((resource) => {
    setCollections((prev) => {
      if (prev[resource]) return prev;
      return { ...prev, [resource]: { items: [], ready: false } };
    });
  }, []);

  const refresh = useCallback(async (resource) => {
    ensure(resource);
    try {
      const items = await api.list(resource);
      setCollections((prev) => ({
        ...prev,
        [resource]: { items, ready: true },
      }));
    } catch {
      setCollections((prev) => ({
        ...prev,
        [resource]: { items: prev[resource]?.items || [], ready: true },
      }));
    }
  }, [ensure]);

  const create = useCallback(async (resource, data) => {
    const item = await api.create(resource, data);
    setCollections((prev) => ({
      ...prev,
      [resource]: { items: [...(prev[resource]?.items || []), item], ready: true },
    }));
    return item;
  }, []);

  const update = useCallback(async (resource, id, data) => {
    const item = await api.update(resource, id, data);
    setCollections((prev) => ({
      ...prev,
      [resource]: {
        items: (prev[resource]?.items || []).map((x) => (x.id === id ? item : x)),
        ready: true,
      },
    }));
    return item;
  }, []);

  const remove = useCallback(async (resource, id) => {
    await api.remove(resource, id);
    setCollections((prev) => ({
      ...prev,
      [resource]: {
        items: (prev[resource]?.items || []).filter((x) => x.id !== id),
        ready: true,
      },
    }));
  }, []);

  const value = useMemo(
    () => ({ collections, refresh, create, update, remove }),
    [collections, refresh, create, update, remove],
  );

  return <DataContext.Provider value={value}>{children}</DataContext.Provider>;
}

export function useResource(resource) {
  const ctx = useContext(DataContext);
  if (!ctx) throw new Error('useResource must be used inside <DataProvider>');
  const collection = ctx.collections[resource];
  useEffect(() => {
    if (!collection || !collection.ready) ctx.refresh(resource);
  }, [resource]); // eslint-disable-line react-hooks/exhaustive-deps
  return {
    items: collection?.items || [],
    ready: Boolean(collection?.ready),
    refresh: () => ctx.refresh(resource),
    create: (data) => ctx.create(resource, data),
    update: (id, data) => ctx.update(resource, id, data),
    remove: (id) => ctx.remove(resource, id),
  };
}
"""

FRONTEND_API_JS = """\
// Minimal same-origin REST client for the backend API.
async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${options.method || 'GET'} ${path} -> ${res.status} ${body.slice(0, 200)}`);
  }
  return res.status === 204 ? null : res.json();
}

const qs = (params) => {
  if (!params) return '';
  const pairs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  return pairs.length ? `?${pairs.join('&')}` : '';
};

export const api = {
  list: (resource, params) => request(`/api/${resource}${qs(params)}`),
  get: (resource, id) => request(`/api/${resource}/${id}`),
  create: (resource, data) =>
    request(`/api/${resource}`, { method: 'POST', body: JSON.stringify(data) }),
  update: (resource, id, data) =>
    request(`/api/${resource}/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  patch: (resource, id, data) =>
    request(`/api/${resource}/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (resource, id) =>
    request(`/api/${resource}/${id}`, { method: 'DELETE' }),
};
"""

FRONTEND_STYLES_CSS = """\
/* Global base styles. Add feature-scoped styles inside each feature file
   (a <style> block or a colocated .css import), not here. */
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  color: #1f2933;
  background: #f8f9fa;
}
.app { max-width: 960px; margin: 0 auto; padding: 16px; }
.row { display: flex; gap: 8px; align-items: center; }
.column { display: flex; flex-direction: column; gap: 8px; }
.card { background: #fff; border: 1px solid #e0e6eb; border-radius: 8px; padding: 12px; }
.button {
  border: 1px solid #cbd2d9; background: #fff; border-radius: 6px;
  padding: 6px 12px; cursor: pointer; font-size: 14px;
}
.button:hover { background: #f0f4f8; }
.input, .textarea {
  border: 1px solid #cbd2d9; border-radius: 6px;
  padding: 6px 10px; font-size: 14px; font-family: inherit;
}
.muted { color: #616e7c; }
.error-text { color: #c0392b; }
"""

# B2(run-002 复盘):各批次自造网格状态模型、反复重写同一共享文件是
# 回归震荡的根源。宿主以只读 README 给出规范内核模式——选中模型/
# aria 契约/Escape/Enter 语义/公式栏回显——feature 实现一律建立在该
# 模式之上。README 属宿主契约(角色 host),模型禁改。
FEATURES_README = """\
# frontend/src/features — feature implementation notes

Each requirement domain lives in its own self-contained file here
(<name>.jsx, default export). This README is HOST-OWNED (read-only).
The host PRE-CREATES one stub per requirement family at initialization
(headers list the family's REQ ids as a work order); fill them in place
and add sibling files freely — the division is a starting grid, not a cage.

## Grid editor kernel pattern (spreadsheet-like tasks)

The kernel is HOST-SHIPPED CODE, not a pattern to re-derive. Import it and
the graded interaction semantics hold by construction:

```jsx
// frontend/src/app/grid.js (host-owned): useGrid + geometry helpers
import { useGrid, rectOf, inRectangle, rangeCoords } from '../app/grid.js';

const grid = useGrid({ initial: 'A1', onSelect: persistSelection });
// FixedGrid wires click/SHIFT-click-extend/drag/aria-selected for you:
<FixedGrid name="Worksheet grid" grid={grid} rows={30} cols={12}
           textAt={(coord) => displayValue(cells, coord)} />
```

- aria-selected is derived from anchor/focus for the WHOLE rendered grid —
  never maintain a per-cell Set (a Set capped at the render extent ships
  stale aria state the moment the grid grows).
- Task layer stays yours: in your own onCellClick (runs BEFORE the kernel
  handler) commit a pending edit; persist via onSelect(anchor, focus);
  drive the editing input with beginEdit/setDraft/commitEdit/cancelEdit
  (Enter commits, Escape cancels).
- FORMULAS: import the engine from ../app/formula.js — displayValue(cells,
  coord) for the grid, evalFormula for checks, adjustFormula for
  copy/paste, shiftRefsOnRowChange/shiftRefsOnColChange for row/column
  operations. Do NOT hand-roll an evaluator: hand-rolled ones have shipped
  #ERROR! on =SUM(A1:A3)*2, on spaces (=A1 + B1), on unary minus and on
  nested calls; this engine handles all of them plus the graded error
  vocabulary (#DIV/0!, #REF!, #NAME?, #ERROR!).

Rules the graders assert verbatim (see your INTERACTION SEMANTICS section):
- Escape cancels an uncommitted edit; Enter commits.
- aria-selected must be true on EVERY gridcell inside the current
  rectangle and false outside (the kernel derives this; keep it true when
  you hand-roll a grid instead).
- One visible error element per failure, exact message strings from the
  requirement.
- REGION CONTRACT: a requirement phrase "A region named X" is the ARIA
  region role — render <Region label="X"> (frontend/src/app/ui.jsx) so
  getByRole('region', { name: X }) resolves. A <div aria-label> has NO
  region role and the grader's locator times out (an entire pivot family
  died exactly this way).
- DIALOG CONTRACT: prefer the NATIVE <dialog> element — keep a ref,
  call ref.current.showModal() to open and ref.current.close() to
  dismiss; Escape, backdrop click handling, and focus trapping come
  with the platform for free (never hand-roll an overlay). Every
  dialog closes on Escape AND on cancel/overlay click, and its confirm
  button carries the requirement's EXACT name ("Delete", not "Delete
  worksheet"). An undismissable overlay blocks every later grader
  step; a misnamed confirm is never found.
- SELECTION CONTRACT (built into the useGrid kernel): Shift-click
  EXTENDS the selection and a plain click replaces it — never write a
  click handler that collapses a shift-extend (sort / filter / validation
  / pivot ranges all read the selection anchor, so one collapsing handler
  breaks five requirement families; this exact bug shipped twice).
- GRID SIZING: size FixedGrid from the data you must SHOW plus margin —
  derive rows/cols from the seeded/imported extent (e.g.
  max(26, maxRow + 6) × max(10, maxCol + 4)); a grid smaller than the
  imported data hides rows/columns no test can reach.
- GRID COMPLETENESS: the grid renders its FULL structure — visible
  columnheader cells (column letters) and rowheader cells (row
  numbers) around role="gridcell" cells named by coordinate. A grid
  that renders only the data region fails every structural step.
- DROPDOWN CONTRACT: dropdowns are native <select> with real <option>
  labels (graders use getByRole('combobox') + selectOption). Never a
  custom div dropdown — without full ARIA combobox semantics it is
  invisible to every selector.
- MENU CONTRACT: right-click/header menus use the host's useContextMenu +
  Menu (frontend/src/app/ui.jsx): role=menu with menuitem buttons whose
  labels are the requirement's EXACT strings ("Insert 1 row above", not
  "Insert row"), closing on Escape AND outside click. FixedGrid already
  routes onContextMenu/onColumnHeaderMenu/onRowHeaderMenu into this —
  hand-rolled positioned divs drift on every one of those axes.
- SUBMIT ENABLEMENT: a submit/save button becomes ENABLED once the
  required fields are filled. Disabled is a transient state (Undo with
  an empty stack), never permanent — a permanently disabled submit can
  never be clicked by a test.
- Downloaded/exported files are byte-exact: no UTF-8 BOM, exact CSV
  quoting.

Keep feature files additive: a later requirement EXTENDS the state model
(import it) instead of forking it — the post-batch regression runs every
completed feature's specs against your changes.
"""

# UI kit(09-27 契合度定论 S3):满分形态从提示词规则下沉为代码——
# 满分版与平台版两代独立代码、同一坑两跑重现,证明"规则文本靠模型
# 抽样遵守"方差过大;这些原语让正确形态成为默认路径(零新依赖)。
# 09-28 增量(sheet 56 分法医):Region 原语("A region named X" 契约
# 短语的 region role 由 <section> 隐式提供——div aria-label 评测器
# getByRole('region') 0 命中,pivot 全族 10/10 卡死);FixedGrid 集成
# grid.js 内核(Shift-click 扩选/拖选/aria 全网格判定内建,塌缩 onClick
# 规则文本两跑失效后改由代码兜底)+ 表头 aria-label + 编辑态输入 +
# 筛选/验证扩展点(对齐满分版模板演进形态)。
FRONTEND_UI_JSX = """\
import React, { useEffect, useRef } from 'react';
import { inRectangle } from './grid.js';

// UI KIT — the graded shapes, by construction. The hidden specs drive the
// app with Playwright role/label locators: getByRole with EXACT accessible
// names, getByLabel, selectOption on native selects, strict-mode single
// matches. Use these primitives wherever the requirement matches; hand-
// rolled substitutes have shipped single-digit scores twice.

// Dropdowns: native <select> — graders call selectOption; a custom div
// dropdown is invisible to it (a hand-rolled combo lost a whole REQ family).
export function NativeSelect({ label, value, onChange, options, id }) {
  const fid = id || 'sel-' + label;
  return (
    <span className="control">
      <label htmlFor={fid}>{label}</label>
      <select id={fid} value={value}
              onChange={(e) => onChange && onChange(e.target.value)}>
        {(options || []).map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </span>
  );
}

// Modals: native <dialog> + showModal() — Escape, backdrop dismissal and
// focus trapping come free; an undismissable overlay blocks every later
// grader step.
export function AppDialog({ name, open, onClose, children }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog ref={ref} aria-label={name} onClose={onClose}>{children}</dialog>
  );
}

// REGION: requirement phrase "A region named X" => <Region label="X">.
// <section> carries the implicit ARIA region role, so the grader's
// getByRole('region', { name: X }) resolves. A plain <div aria-label>
// has NO role — that exact miss stalled an entire pivot family (every
// scenario timed out at the editor the step after creating it).
export function Region({ label, children, ...rest }) {
  return (
    <section aria-label={label} {...rest}>{children}</section>
  );
}

// Signed-in identity: the username must be its OWN exact-text element
// inside the account trigger (graders assert getByText(username,
// {exact:true}) and click it to open the menu — a "Welcome, {user}"
// heading fails every exact-text locator). Pass the trigger's accessible
// name from the REQUIREMENT via label; without it the button is named by
// the username span itself (still exact-text clickable).
export function AccountTrigger({ username, onOpen, label }) {
  const props = label ? { 'aria-label': label } : {};
  return (
    <button {...props} onClick={onOpen}>
      <span className="acc-username">{username}</span>
    </button>
  );
}

// Exactly ONE error element at a time — two matching elements is a
// strict-mode violation. Render this per form, never per field next to a
// form-level summary.
export function SingleError({ message }) {
  if (!message) return null;
  return <div role="alert" className="form-error">{message}</div>;
}

// PER-FIELD parallel errors (registration-style forms): several invalid
// fields submitted together must show ALL their messages at once ("must
// show ... together, rather than revealing only one"), each field owning
// exactly ONE element beside it. errors is the backend {errors:[{field,
// msg}]} shape (src/lib/crud.js emits it verbatim).
export const byField = (errors, field) =>
  (errors || []).filter((e) => e && e.field === field).slice(0, 1);
export function FieldError({ errors, field }) {
  const first = byField(errors, field)[0];
  if (!first) return null;
  return <div role="alert" className="error-text">{first.msg}</div>;
}

// MENUS (row/column-header menus, worksheet options, right-click grids):
// role=menu with menuitem buttons carrying EXACT labels from the
// requirement, closing on Escape AND outside click. FixedGrid already
// routes onContextMenu / onColumnHeaderMenu / onRowHeaderMenu here —
// useContextMenu captures the event point, Menu renders the popup.
// items: [{label, onSelect}] — label IS the accessible name (verbatim).
export function useContextMenu() {
  const [menu, setMenu] = React.useState(null);
  const open = (e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }); };
  const close = () => setMenu(null);
  return { menu, open, close };
}

export function Menu({ menu, onClose, items }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!menu) return;
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onClose();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [menu, onClose]);
  if (!menu) return null;
  return (
    <div ref={ref} role="menu" className="ctx-menu"
         style={{ position: 'fixed', left: menu.x, top: menu.y, zIndex: 1000 }}>
      {(items || []).map((it) => (
        <button key={it.label} role="menuitem"
                onClick={() => { onClose(); it.onSelect && it.onSelect(); }}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

// Column letters beyond Z wrap to AA, AB, ... — no artificial column cap
// (a capped grid dies the first time a grader clicks past it).
const colLetters = (n) => {
  const out = [];
  for (let i = 0; i < Math.max(1, n); i++) {
    let s = '';
    let j = i;
    do { s = String.fromCharCode(65 + (j % 26)) + s; j = Math.floor(j / 26) - 1; } while (j >= 0);
    out.push(s);
  }
  return out;
};

// Spreadsheet-shaped grid: FIXED-SIZE (empty cells beyond the data region
// stay selectable — a data-sized grid killed ~7 requirements in one run;
// size it from the seeded/imported data extent plus margin, never smaller
// than 40 rows × 20 cols — the reference apps show a LARGE empty grid
// around the data, and graders address cells well beyond the seeded
// extent).
// role=grid + columnheaders named by LETTER + rowheaders by ROW NUMBER +
// gridcells whose accessible name is the coordinate (A1). name and
// multiselectable come FROM THE REQUIREMENT (its quoted grid name and
// selection contract).
//
// Selection: pass the useGrid kernel as `grid` (from ./grid.js) and the
// built-in handlers give plain-click select, SHIFT-click extend, drag
// rectangle and whole-grid aria-selected with ZERO wiring on your side;
// your own on* callbacks still fire first (commit a pending edit there).
// Without `grid`, the legacy `selected` Set keeps working. `editing` is
// the kernel's editing object {coord, draft} rendered as a cell input.
// headerExtra/cellExtra decorate headers/cells (filter buttons, dropdown
// triggers); rowHidden(r) hides filtered rows without deleting them.
export function FixedGrid({ name, rows = 40, cols = 20, multiselectable = true,
                            textAt, selected, grid,
                            onCellClick, onCellDown, onCellEnter,
                            onCellDoubleClick, onContextMenu,
                            onColumnHeaderMenu, onRowHeaderMenu,
                            editing, onEditChange, onEditKeyDown,
                            headerExtra, cellExtra, rowHidden }) {
  const letters = colLetters(cols);
  const isSelected = (coord) => (grid
    ? inRectangle(coord, grid.anchor, grid.focus)
    : Boolean(selected && selected.has && selected.has(coord)));
  const editingCell = editing || (grid && grid.editing) || null;
  return (
    <table role="grid" aria-label={name}
           aria-multiselectable={multiselectable ? 'true' : 'false'}>
      <thead>
        <tr role="row">
          <td></td>
          {letters.map((c) => (
            <th key={c} role="columnheader" aria-label={c}
                onContextMenu={(e) => {
                  e.preventDefault();
                  onColumnHeaderMenu && onColumnHeaderMenu(c, e);
                }}>
              {c}
              {headerExtra ? headerExtra(c) : null}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {Array.from({ length: rows }, (_, i) => i + 1).map((r) => (
          <tr key={r} role="row" hidden={rowHidden ? rowHidden(r) : false}>
            <th role="rowheader" aria-label={String(r)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  onRowHeaderMenu && onRowHeaderMenu(r, e);
                }}>
              {String(r)}
            </th>
            {letters.map((c) => {
              const coord = c + r;
              const sel = isSelected(coord);
              const isEditing = editingCell && editingCell.coord === coord;
              return (
                <td key={coord} role="gridcell" aria-label={coord}
                    aria-selected={sel ? 'true' : 'false'}
                    onClick={(e) => {
                      onCellClick && onCellClick(coord, e);
                      grid && grid.handleCellClick(coord, e);
                    }}
                    onMouseDown={(e) => {
                      onCellDown && onCellDown(coord, e);
                      grid && grid.handleCellMouseDown(coord, e);
                    }}
                    onMouseEnter={() => {
                      onCellEnter && onCellEnter(coord);
                      grid && grid.handleCellMouseEnter(coord);
                    }}
                    onDoubleClick={() => onCellDoubleClick && onCellDoubleClick(coord)}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      onContextMenu && onContextMenu(coord, e);
                      grid && grid.handleCellClick(coord, e);
                    }}>
                  {isEditing ? (
                    <input className="cell-editor" aria-label={'Edit ' + coord}
                           value={editingCell.draft} autoFocus
                           onChange={(e) => onEditChange && onEditChange(e.target.value)}
                           onKeyDown={(e) => onEditKeyDown && onEditKeyDown(e)} />
                  ) : (
                    <React.Fragment>
                      {textAt ? (textAt(coord) || '') : ''}
                      {cellExtra ? cellExtra(coord) : null}
                    </React.Fragment>
                  )}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
"""
