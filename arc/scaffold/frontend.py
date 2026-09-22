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
