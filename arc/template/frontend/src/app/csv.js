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
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
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
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};
export function parseCSV(text) {
  const rows = splitRows(stripBom(String(text)), ',');
  return rows === null ? { error: 'unterminated quote' } : { rows };
}
export function parseTSV(text) {
  const rows = splitRows(stripBom(String(text)), '\t');
  return rows === null ? { error: 'unterminated quote' } : { rows };
}
export function stringifyCSV(rows) {
  const out = (rows || []).map((r) => (r || []).map(quoted).join(',')).join('\r\n');
  return out ? out + '\r\n' : '';
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
