// Cell formula engine — HOST-SHIPPED, task-independent. Do NOT hand-roll
// an evaluator: hand-rolled ones have shipped #ERROR! on =SUM(A1)*2, on
// spaces (=A1 + B1), on unary minus and on nested calls. This module
// supports all of those, plus the graded error vocabulary:
//   #DIV/0! divide by zero, #REF! invalid/circular reference,
//   #NAME? unsupported function, #ERROR! malformed expression,
//   #VALUE! arithmetic on non-numeric cell text.
// Cell shape: { "A1": { v: 1200 | "East" | "=B2*2" }, ... } (absent = blank).
// Case-insensitive SUM/AVERAGE/COUNT/MIN/MAX; aggregates ignore blanks and
// text (COUNT counts numbers only; blanks are never zero); $ absolute refs;
// relative-reference adjustment for copy and for row/column shifts.
// Import from here: displayValue(cells, coord) renders a cell for the grid;
// evalFormula / adjustFormula / shiftRefsOnRowChange / shiftRefsOnColChange
// power the formula bar, copy-paste and structure operations.

const NUMERIC_TEXT_RE = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
const REF_RE = /^\$?([A-Za-z]+)\$?(\d+)$/;
const AGGREGATES = { SUM: 1, AVERAGE: 1, COUNT: 1, MIN: 1, MAX: 1 };

export const colNum = (s) => String(s).toUpperCase().split('').reduce(
  (n, ch) => n * 26 + (ch.charCodeAt(0) - 64), 0);

export const colLetter = (n) => {
  let out = '';
  let j = n - 1;
  do { out = String.fromCharCode(65 + (j % 26)) + out; j = Math.floor(j / 26) - 1; } while (j >= 0);
  return out;
};

const isNumeric = (v) => typeof v === 'number'
  ? Number.isFinite(v)
  : (typeof v === 'string' && NUMERIC_TEXT_RE.test(v.trim()));

// Value of a non-formula cell for arithmetic: number, or null (blank/text).
const plainValue = (v) => {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  const s = String(v).trim();
  return NUMERIC_TEXT_RE.test(s) ? Number(s) : null;
};

// Evaluate one cell reference to a number | {error} | null (blank/text).
function refValue(cells, coord, visited) {
  const cell = cells[coord];
  if (!cell || cell.v === undefined || cell.v === null || cell.v === '') return null;
  if (typeof cell.v === 'string' && cell.v.trim().startsWith('=')) {
    if (visited && visited.has(coord)) return { error: '#REF!' };
    const nested = new Set(visited || []);
    nested.add(coord);
    const res = evalFormula(cell.v, cells, coord, nested);
    if (res.error !== undefined) return { error: res.error };
    return Number.isFinite(res.value) ? res.value : null;
  }
  return plainValue(cell.v);
}

// ── tokenizer ────────────────────────────────────────────────────────────
// Whitespace is insignificant between tokens (=A1 + B1 evaluates like
// =A1+B1). Token kinds: num, ref, name (function call), op, punct.
function tokenize(src) {
  const tokens = [];
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (/\s/.test(ch)) { i += 1; continue; }
    if (/[0-9]/.test(ch)
        || (ch === '.' && /[0-9]/.test(src[i + 1] || ''))) {
      let j = i + 1;
      while (j < src.length && /[0-9.]/.test(src[j])) j += 1;
      if (/[eE]/.test(src[j] || '')
          && /[0-9+-]/.test(src[j + 1] || '')) {
        j += 1;
        if (/[+-]/.test(src[j])) j += 1;
        while (j < src.length && /[0-9]/.test(src[j])) j += 1;
      }
      tokens.push({ kind: 'num', text: src.slice(i, j) });
      i = j;
      continue;
    }
    if (/[A-Za-z_$]/.test(ch)) {
      let j = i + 1;
      while (j < src.length && /[A-Za-z0-9_$.]/.test(src[j])) j += 1;
      const text = src.slice(i, j);
      let k = j;
      while (k < src.length && /\s/.test(src[k])) k += 1;
      if (src[k] === '(') {
        tokens.push({ kind: 'name', text: text.replace(/[$]/g, '').toUpperCase() });
        i = k; // the '(' itself is consumed by the parser
      } else if (REF_RE.test(text)) {
        tokens.push({ kind: 'ref', text: text.replace(/[$]/g, '').toUpperCase() });
        i = j;
      } else {
        return { error: '#ERROR!' };
      }
      continue;
    }
    if ('+-*/(),:'.includes(ch)) {
      tokens.push({ kind: ch === ',' || ch === ':' ? 'punct' : 'op', text: ch });
      i += 1;
      continue;
    }
    return { error: '#ERROR!' };
  }
  return { tokens };
}

// ── parser / evaluator ───────────────────────────────────────────────────
// AST nodes are evaluated inline: {value} | {error} | {range: [coords]}.
function parse(tokens, cells, visited) {
  let pos = 0;
  const peek = () => tokens[pos];
  const take = () => tokens[pos++];

  const rangeCoords = (a, b) => {
    const am = REF_RE.exec(a);
    const bm = REF_RE.exec(b);
    if (!am || !bm) return null;
    const c1 = colNum(am[1]);
    const c2 = colNum(bm[1]);
    const r1 = Number(am[2]);
    const r2 = Number(bm[2]);
    if (Math.min(c1, c2) < 1 || Math.max(r1, r2) > 100000
        || Math.max(c1, c2) > 5000) return null; // sanity cap
    const out = [];
    for (let r = Math.min(r1, r2); r <= Math.max(r1, r2); r++) {
      for (let c = Math.min(c1, c2); c <= Math.max(c1, c2); c++) {
        out.push(colLetter(c) + r);
      }
    }
    return out;
  };

  function parsePrimary() {
    const t = take();
    if (t === undefined) return { error: '#ERROR!' };
    if (t.kind === 'num') return { value: Number(t.text) };
    if (t.kind === 'ref') {
      // A range literal (A1:B3) is only meaningful inside an aggregate.
      if (peek() && peek().kind === 'punct' && peek().text === ':') {
        take();
        const b = take();
        if (!b || b.kind !== 'ref') return { error: '#ERROR!' };
        const coords = rangeCoords(t.text, b.text);
        return coords ? { range: coords } : { error: '#REF!' };
      }
      return evalOne(t.text);
    }
    if (t.kind === 'op' && t.text === '(') {
      const inner = parseExpression();
      const close = take();
      if (inner.error !== undefined || !close || close.text !== ')') {
        return { error: inner.error !== undefined ? inner.error : '#ERROR!' };
      }
      return inner;
    }
    if (t.kind === 'name') {
      if (!peek() || peek().text !== '(') return { error: '#ERROR!' };
      take(); // '('
      const args = [];
      if (peek() && peek().text !== ')') {
        for (;;) {
          args.push(parseExpression());
          const sep = peek();
          if (sep && sep.kind === 'punct' && sep.text === ',') { take(); continue; }
          break;
        }
      }
      const close = take();
      if (!close || close.text !== ')') return { error: '#ERROR!' };
      return callAggregate(t.text, args);
    }
    return { error: '#ERROR!' };
  }

  function parseUnary() {
    const t = peek();
    if (t && t.kind === 'op' && (t.text === '-' || t.text === '+')) {
      take();
      const operand = parseUnary();
      if (operand.error !== undefined) return operand;
      if (operand.range) return { error: '#ERROR!' };
      return { value: t.text === '-' ? -operand.value : operand.value };
    }
    return parsePrimary();
  }

  function parseTerm() {
    let left = parseUnary();
    if (left.error !== undefined) return left;
    for (;;) {
      const op = peek();
      if (!op || op.kind !== 'op' || (op.text !== '*' && op.text !== '/')) break;
      take();
      const right = parseUnary();
      if (right.error !== undefined) return right;
      if (left.range || right.range) return { error: '#ERROR!' };
      if (op.text === '/') {
        if (right.value === 0) return { error: '#DIV/0!' };
        left = { value: left.value / right.value };
      } else {
        left = { value: left.value * right.value };
      }
    }
    return left;
  }

  function parseExpression() {
    let left = parseTerm();
    if (left.error !== undefined) return left;
    for (;;) {
      const op = peek();
      if (!op || op.kind !== 'op' || (op.text !== '+' && op.text !== '-')) break;
      take();
      const right = parseTerm();
      if (right.error !== undefined) return right;
      if (left.range || right.range) return { error: '#ERROR!' };
      left = { value: op.text === '+' ? left.value + right.value
                                : left.value - right.value };
    }
    return left;
  }

  function evalOne(coord) {
    const v = refValue(cells, coord, visited);
    if (v && typeof v === 'object' && v.error !== undefined) return v;
    if (typeof v === 'number') return { value: v };
    // null = blank or text. A BLANK cell coerces to 0 in scalar arithmetic
    // (Excel), TEXT is #VALUE! (="East"+1). Aggregates skip both silently.
    const cell = (cells || {})[coord];
    const blank = !cell || cell.v === undefined || cell.v === null
      || cell.v === '';
    return blank ? { value: 0 } : { error: '#VALUE!' };
  }

  function callAggregate(name, args) {
    if (!AGGREGATES[name]) return { error: '#NAME?' };
    const nums = [];
    const pushArg = (arg) => {
      if (arg.error !== undefined) return arg;
      if (arg.range) {
        for (const coord of arg.range) {
          const v = refValue(cells, coord, visited);
          if (v && typeof v === 'object' && v.error !== undefined) return v;
          if (typeof v === 'number') nums.push(v);
        }
        return { value: 0 };
      }
      if (arg.value !== null && arg.value !== undefined
          && Number.isFinite(arg.value)) nums.push(arg.value);
      return { value: 0 };
    };
    for (const arg of args) {
      const r = pushArg(arg);
      if (r.error !== undefined) return r;
    }
    if (name === 'COUNT') return { value: nums.length };
    if (name === 'SUM') return { value: nums.reduce((a, b) => a + b, 0) };
    if (name === 'MIN') return { value: nums.length ? Math.min(...nums) : 0 };
    if (name === 'MAX') return { value: nums.length ? Math.max(...nums) : 0 };
    // AVERAGE: Excel semantics — no numeric value at all is #DIV/0!.
    return nums.length
      ? { value: nums.reduce((a, b) => a + b, 0) / nums.length }
      : { error: '#DIV/0!' };
  }

  const result = parseExpression();
  if (result.error !== undefined) return result;
  if (result.range) return { error: '#ERROR!' };
  if (pos !== tokens.length) return { error: '#ERROR!' };
  return result;
}

export function evalFormula(expr, cells, atCoord, visited) {
  const src = String(expr == null ? '' : expr).trim().replace(/^=/, '');
  if (!src) return { value: 0 };
  if (src.includes('#REF!')) return { error: '#REF!' };
  const tok = tokenize(src);
  if (tok.error !== undefined) return { error: tok.error };
  const seen = new Set(visited || []);
  if (atCoord) seen.add(atCoord);
  return parse(tok.tokens, cells || {}, seen);
}

// Rendered value for the grid: '' (blank), the computed result, or the
// error string. Non-formula cells render verbatim (numbers are numbers).
export function displayValue(cells, coord) {
  const cell = (cells || {})[coord];
  if (!cell || cell.v === undefined || cell.v === null || cell.v === '') return '';
  if (typeof cell.v === 'string' && cell.v.trim().startsWith('=')) {
    const res = evalFormula(cell.v, cells, coord);
    return res.error !== undefined ? res.error : String(res.value);
  }
  return String(cell.v);
}

// Copy a formula to a target offset: relative refs move, $-absolute stay;
// a reference pushed off-sheet collapses the whole formula to =#REF!
// (the contract's formula-bar form). Returns the raw formula string
// (with the leading '='), ready to store.
export function adjustFormula(formula, dCol, dRow) {
  const body = String(formula == null ? '' : formula).trim();
  const adjusted = body.replace(
    /\$?[A-Za-z]+\$?[0-9]+/g,
    (ref) => {
      const m = /^(\$?)([A-Za-z]+)(\$?)([0-9]+)$/.exec(ref);
      if (!m) return ref;
      const nc = m[1] === '$' ? colNum(m[2]) : colNum(m[2]) + dCol;
      const nr = m[3] === '$' ? Number(m[4]) : Number(m[4]) + dRow;
      if (nc < 1 || nr < 1) return '#REF!';
      return m[1] + colLetter(nc) + m[3] + nr;
    });
  if (adjusted.includes('#REF!')) return '=#REF!';
  return adjusted.startsWith('=') ? adjusted : '=' + adjusted;
}

// Row/column structure changes: rewrite relative references inside every
// formula cell (rows >= startRow shift by delta; a reference on a deleted
// row/col becomes #REF! inline — the grid renders #REF! from there).
// Returns a NEW cells map; non-formula entries are carried as-is.
export function shiftRefsOnRowChange(cells, startRow, delta) {
  const next = {};
  Object.keys(cells || {}).forEach((key) => {
    const cell = cells[key];
    if (!cell || typeof cell.v !== 'string'
        || !cell.v.trim().startsWith('=')) { next[key] = cell; return; }
    next[key] = { v: String(cell.v).replace(
      /\$?[A-Za-z]+\$?[0-9]+/g,
      (ref) => {
        const m = /^(\$?)([A-Za-z]+)(\$?)([0-9]+)$/.exec(ref);
        if (!m) return ref;
        const row = Number(m[4]);
        if (m[3] === '$') return ref;
        if (delta < 0 && row === startRow) return '#REF!';
        if (row < startRow) return ref;
        const nr = row + delta;
        if (nr < 1) return '#REF!';
        return m[1] + m[2] + m[3] + nr;
      }) };
  });
  return next;
}

export function shiftRefsOnColChange(cells, startCol, delta) {
  const start = typeof startCol === 'number' ? startCol : colNum(startCol);
  const next = {};
  Object.keys(cells || {}).forEach((key) => {
    const cell = cells[key];
    if (!cell || typeof cell.v !== 'string'
        || !cell.v.trim().startsWith('=')) { next[key] = cell; return; }
    next[key] = { v: String(cell.v).replace(
      /\$?[A-Za-z]+\$?[0-9]+/g,
      (ref) => {
        const m = /^(\$?)([A-Za-z]+)(\$?)([0-9]+)$/.exec(ref);
        if (!m) return ref;
        const col = colNum(m[2]);
        if (m[1] === '$') return ref;
        if (delta < 0 && col === start) return '#REF!';
        if (col < start) return ref;
        const nc = col + delta;
        if (nc < 1) return '#REF!';
        return m[1] + colLetter(nc) + m[3] + m[4];
      }) };
  });
  return next;
}
