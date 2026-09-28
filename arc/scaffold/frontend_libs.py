"""frontend 通用库模板常量(纯标准库):csv/undo/router 三件可选工具箱。

与 grid_template.py/formula_template.py 同形态:宿主代码化的技术方案,
零任务语义,模型显式 import 接线。csv.js 零依赖 CommonJS+ESM 双兼容面
(export 语法;node 测试走 --experimental 或转译?——不,直接 ESM export,
node 冒烟用动态 import 断言)。
"""

from __future__ import annotations

CSV_JS = """\
// csv.js — 表格文本与文件 I/O(纯函数,零依赖):RFC4180 CSV 解析、
// 字节精确序列化(无 BOM、必要才加引号、引号成对转义)、TSV 剪贴板
// 解析、浏览器下载触发。导入容忍并剥除 BOM;导出绝不写 BOM(评测按
// 字节断言)。解析失败(引号不闭合)返回 {error},错误文案由调用方按
// 需求原文给。
const stripBom = (s) => (s.charCodeAt(0) === 0xfeff ? s.slice(1) : s);
function splitRows(text, delim) {
  const rows = [[]];
  let row = rows[0];
  let field = '';
  let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQ = false;
      } else field += c;
    } else if (c === '"' && field === '') inQ = true;
    else if (c === delim) { row.push(field); field = ''; }
    else if (c === '\\n' || c === '\\r') {
      if (c === '\\r' && text[i + 1] === '\\n') i++;
      row.push(field); field = '';
      row = []; rows.push(row);
    } else field += c;
  }
  if (inQ) return null;
  row.push(field);
  if (rows.length > 1 && rows[rows.length - 1].length === 1 &&
      rows[rows.length - 1][0] === '') rows.pop();
  if (rows.length === 1 && rows[0].length === 1 && rows[0][0] === '') return [];
  return rows;
}
const quoted = (v) => {
  const s = v === undefined || v === null ? '' : String(v);
  return /[",\\n\\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};
export function parseCSV(text) {
  const rows = splitRows(stripBom(String(text)), ',');
  return rows === null ? { error: 'unterminated quote' } : { rows };
}
export function parseTSV(text) {
  const rows = splitRows(stripBom(String(text)), '\\t');
  return rows === null ? { error: 'unterminated quote' } : { rows };
}
export function stringifyCSV(rows) {
  const out = (rows || []).map((r) => (r || []).map(quoted).join(',')).join('\\r\\n');
  return out ? out + '\\r\\n' : '';
}
export function downloadFile(name, text, mime = 'text/csv;charset=utf-8') {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: mime }));
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
// File → text (UTF-8) for the import loop: <input type="file"> +
// readFileText(file) + parseCSV(...) is the whole CSV-import chain; BOM
// stripping happens inside parseCSV.
export function readFileText(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsText(file, 'utf-8');
  });
}
// Clipboard write with fallback (the "Copy <value>" → "Copied" feedback
// contract): navigator.clipboard on secure contexts (localhost qualifies),
// execCommand fallback otherwise. Returns whether the write succeeded —
// show the requirement's feedback word only on success.
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy'); ta.remove(); return ok;
  } catch { return false; }
}
"""

UNDO_JS = """\
// undo.js — 快照式撤销栈 hook(纯机制):调用方在"每次修改前"push()
// (快照函数由你给——把整个状态 blob 序列化,值/结构/规则/配置一类
// 操作全覆盖);undo()/redo() 返回旧/新快照,由你写回状态并走正常持久
// 化(撤销后状态随常规保存跨刷新存活)。新修改清空 redo 栈;canUndo/
// canRedo 驱动按钮禁用态(Disabled 是暂态,永不禁死提交)。
import { useCallback, useRef, useState } from 'react';

export function useUndoRedo(getSnapshot, cap = 50) {
  const past = useRef([]);
  const future = useRef([]);
  const [, bump] = useState(0);
  const push = useCallback(() => {
    past.current.push(getSnapshot());
    if (past.current.length > cap) past.current.shift();
    future.current = [];
    bump((n) => n + 1);
  }, [getSnapshot, cap]);
  const undo = useCallback(() => {
    if (!past.current.length) return null;
    const prev = past.current.pop();
    future.current.push(getSnapshot());
    bump((n) => n + 1);
    return prev;
  }, [getSnapshot]);
  const redo = useCallback(() => {
    if (!future.current.length) return null;
    const next = future.current.pop();
    past.current.push(getSnapshot());
    bump((n) => n + 1);
    return next;
  }, [getSnapshot]);
  return {
    push, undo, redo,
    canUndo: past.current.length > 0,
    canRedo: future.current.length > 0,
  };
}
"""

ROUTER_JS = """\
// router.js — hash 路由纯机制:parseRoute 纯函数(可单测)+ useHashRoute
// hook(hashchange 监听;navigate 触发;刷新/直达/后退一致)。路由表、
// 路径形状与页面归属全由调用方定(如 #/wb/<id> 深链)。
import { useEffect, useState } from 'react';

export function parseRoute(hash) {
  const h = String(hash || '').replace(/^#/, '');
  const [path, q] = h.split('?');
  const query = {};
  if (q) {
    for (const kv of q.split('&')) {
      const i = kv.indexOf('=');
      if (i > 0) {
        query[decodeURIComponent(kv.slice(0, i))] =
          decodeURIComponent(kv.slice(i + 1));
      }
    }
  }
  return { path, parts: path.split('/').filter(Boolean), query };
}

export function useHashRoute() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  const navigate = (to) => { window.location.hash = to; };
  return { route, navigate };
}
"""
