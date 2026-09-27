"""frontend 模板常量(纯标准库):React+Vite 模块化八件套。

从 plan_envelope.py 迁入——plan_envelope 只保留 envelope 契约文本
(HOST_STACK_RULES 等),模板内容统一归 scaffold 包。

结构(run8 教训:单文件 main.jsx 1002 行叠改,后批重构破坏前批功能,
18 个失败里 12 个是这种"做完又弄坏"):

    src/main.jsx        宿主独占,仅 createRoot
    src/app/App.jsx     组装层:import + 挂载 features,禁业务逻辑
    src/app/store.jsx   宿主提供的共享数据层(useResource)
    src/app/api.js      宿主提供的 REST 客户端
    src/app/styles.css  全局基础样式
    src/features/       模型扩展点:每需求域一个自包含文件

批与批物理隔离——REQ-A 的重构只发生在 features/<a>.jsx 和 App.jsx 的
接线上,REQ-B 的代码一行不被触碰。零新依赖(react/react-dom + Vite
原生 css import),测试契约(语义选择器)与文件结构零耦合。
"""

from __future__ import annotations

FRONTEND_PACKAGE_JSON = {
    "name": "frontend",
    "version": "1.0.0",
    "private": True,
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
      // so eval-time crashes land in octos-logs/eval-traffic.jsonl.
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

export const api = {
  list: (resource) => request(`/api/${resource}`),
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

## Grid editor kernel pattern (spreadsheet-like tasks)

Build every grid feature on ONE shared selection+editing state model,
created in the FIRST grid feature file and imported by later features
(never re-implemented, never rewritten):

```jsx
// useGrid: single source of truth for selection + editing
// anchor + focus cells define the rectangular selection
const [grid, setGrid] = useState({
  anchor: 'A1', focus: 'A1', editing: null, // editing: {coord, draft}
});
// aria contract (graded): each cell renders
//   role="gridcell" aria-label={coord}
//   aria-selected={inRectangle(coord, anchor, focus) ? 'true' : 'false'}
// commit:  Enter or click on another cell applies editing.draft
// cancel:  Escape restores the original value, formula bar shows it
// formula bar: value = editing ? draft : (formulaOf(selected) ?? valueOf(selected))
```

Rules the graders assert verbatim (see your INTERACTION SEMANTICS section):
- Escape cancels an uncommitted edit; Enter commits.
- aria-selected must be maintained on EVERY gridcell, true inside the
  current rectangle, false outside.
- One visible error element per failure, exact message strings from the
  requirement.
- DIALOG CONTRACT: prefer the NATIVE <dialog> element — keep a ref,
  call ref.current.showModal() to open and ref.current.close() to
  dismiss; Escape, backdrop click handling, and focus trapping come
  with the platform for free (never hand-roll an overlay). Every
  dialog closes on Escape AND on cancel/overlay click, and its confirm
  button carries the requirement's EXACT name ("Delete", not "Delete
  worksheet"). An undismissable overlay blocks every later grader
  step; a misnamed confirm is never found.
- SELECTION CONTRACT: Shift-click EXTENDS the selection — the plain
  click handler must not collapse it (guard with e.shiftKey); sort /
  filter / validation / pivot ranges all read the selection anchor, so
  one collapsing handler breaks five requirement families.
- GRID COMPLETENESS: the grid renders its FULL structure — visible
  columnheader cells (column letters) and rowheader cells (row
  numbers) around role="gridcell" cells named by coordinate. A grid
  that renders only the data region fails every structural step.
- DROPDOWN CONTRACT: dropdowns are native <select> with real <option>
  labels (graders use getByRole('combobox') + selectOption). Never a
  custom div dropdown — without full ARIA combobox semantics it is
  invisible to every selector.
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
FRONTEND_UI_JSX = """\
import React, { useEffect, useRef } from 'react';

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

// Signed-in identity: the username must be its OWN exact-text element
// inside the account trigger (graders assert getByText(username,
// {exact:true}) and click it to open the menu — a "Welcome, {user}"
// heading fails every exact-text locator).
export function AccountTrigger({ username, onOpen }) {
  return (
    <button aria-label="Account menu" onClick={onOpen}>
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

const COL_LETTERS = 'ABCDEFGHIJ';

// Spreadsheet-shaped grid: FIXED-SIZE (empty cells beyond the data region
// stay selectable — a data-sized grid killed ~7 requirements in one run),
// role=grid + named columnheaders (letters) + rowheaders (numbers) +
// gridcells whose accessible name is the coordinate (A1). Owns NO state:
// pass textAt(coord) and the selection Set; wire click/mousedown/context
// menu handlers to your grid state.
export function FixedGrid({ name = 'Worksheet grid', rows = 26, cols = 10,
                            textAt, selected, onCellClick, onCellDown,
                            onContextMenu }) {
  const letters = COL_LETTERS.slice(0, Math.max(1, Math.min(cols, 10)));
  return (
    <table role="grid" aria-label={name} aria-multiselectable="true">
      <thead>
        <tr role="row">
          <td></td>
          {letters.split('').map((c) => (
            <th key={c} role="columnheader">{c}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {Array.from({ length: rows }, (_, i) => i + 1).map((r) => (
          <tr key={r} role="row">
            <th role="rowheader">{String(r)}</th>
            {letters.split('').map((c) => {
              const coord = c + r;
              const sel = selected && selected.has && selected.has(coord);
              return (
                <td key={coord} role="gridcell" aria-label={coord}
                    aria-selected={sel ? 'true' : 'false'}
                    onClick={() => onCellClick && onCellClick(coord)}
                    onMouseDown={(e) => onCellDown && onCellDown(coord, e)}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      onContextMenu && onContextMenu(coord);
                    }}>
                  {textAt ? (textAt(coord) || '') : ''}
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
