"""frontend 模板常量(纯标准库):React+Vite 五件套。

从 plan_envelope.py 迁入——plan_envelope 只保留 envelope 契约文本
(HOST_STACK_RULES 等),模板内容统一归 scaffold 包。
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

function App() {
  return <div>App scaffold</div>;
}

createRoot(document.getElementById('root')).render(<App />);
"""
