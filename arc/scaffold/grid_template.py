"""
grid.js 模板(纯标准库常量)——网格选区/编辑内核,随 scaffold 发货。
"""

GRID_JS = """\
// Grid selection kernel — HOST-SHIPPED. Wire it ONCE and the graded
// interaction semantics hold by construction:
//   const grid = useGrid({ initial: 'A1', onSelect: persistSelection });
//   <FixedGrid name="Worksheet grid" grid={grid} ... />
// Built-in (do NOT re-implement, do NOT wrap with a collapsing handler):
//   plain click selects one cell; SHIFT-click EXTENDS the rectangle from
//   the anchor (a collapsing click handler breaks every range flow —
//   sort/filter/validation/pivot all read the selection); dragging builds
//   the rectangle and reports it on mouseup; aria-selected is derived from
//   anchor/focus for the WHOLE rendered grid (no per-cell Set to maintain).
// Task layer stays yours: commit a pending edit in your own onCellClick
// BEFORE the kernel selects (agent callbacks run first), persist via
// onSelect(anchor, focus), and drive the editing input through
// beginEdit/setDraft/commitEdit/cancelEdit (Enter commits, Escape cancels).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export const colNum = (s) => String(s).toUpperCase().split('').reduce(
  (n, ch) => n * 26 + (ch.charCodeAt(0) - 64), 0);

// Type-aware ordering for sort-driven features ("Numbers, parseable dates,
// and text are compared according to their respective types"): values that
// both parse as numbers compare numerically ('9' < '10'); values that both
// look like ISO dates compare chronologically; everything else compares as
// text by code units (deterministic — no locale variance). Array#sort is
// stable, so equal keys keep their original relative order by construction.
const asNumber = (v) => {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  const s = String(v === undefined || v === null ? '' : v).trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};
const asDate = (v) => {
  const s = String(v === undefined || v === null ? '' : v).trim();
  if (!/^\\d{4}-\\d{2}-\\d{2}/.test(s)) return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
};
export function compareValues(a, b) {
  const na = asNumber(a);
  const nb = asNumber(b);
  if (na !== null && nb !== null) return na - nb;
  const da = asDate(a);
  const dd = asDate(b);
  if (da !== null && dd !== null) return da - dd;
  const sa = a === undefined || a === null ? '' : String(a);
  const sb = b === undefined || b === null ? '' : String(b);
  return sa < sb ? -1 : sa > sb ? 1 : 0;
}

export const colLetter = (n) => {
  let out = '';
  let j = n - 1;
  do { out = String.fromCharCode(65 + (j % 26)) + out; j = Math.floor(j / 26) - 1; } while (j >= 0);
  return out;
};

const COORD_RE = /^([A-Za-z]+)(\\d+)$/;

export function rectOf(anchor, focus) {
  const am = COORD_RE.exec(String(anchor || ''));
  const fm = COORD_RE.exec(String(focus || ''));
  if (!am || !fm) return { c1: 1, c2: 1, r1: 1, r2: 1 };
  const c1 = Math.min(colNum(am[1]), colNum(fm[1]));
  const c2 = Math.max(colNum(am[1]), colNum(fm[1]));
  const r1 = Math.min(Number(am[2]), Number(fm[2]));
  const r2 = Math.max(Number(am[2]), Number(fm[2]));
  return { c1, c2, r1, r2 };
}

export function inRectangle(coord, anchor, focus) {
  const cm = COORD_RE.exec(String(coord || ''));
  if (!cm) return false;
  const { c1, c2, r1, r2 } = rectOf(anchor, focus);
  const c = colNum(cm[1]);
  const r = Number(cm[2]);
  return c >= c1 && c <= c2 && r >= r1 && r <= r2;
}

export function rangeCoords(anchor, focus) {
  const { c1, c2, r1, r2 } = rectOf(anchor, focus);
  const out = [];
  for (let r = r1; r <= r2; r++) {
    for (let c = c1; c <= c2; c++) out.push(colLetter(c) + r);
  }
  return out;
}

export function useGrid({ initial = 'A1', onSelect } = {}) {
  const [grid, setGrid] = useState({ anchor: initial, focus: initial, editing: null });
  const gridRef = useRef(grid);
  gridRef.current = grid;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const dragFromRef = useRef(null);
  const didDragRef = useRef(false);
  const suppressClickRef = useRef(false);

  const report = useCallback((anchor, focus) => {
    if (onSelectRef.current) onSelectRef.current(anchor, focus);
  }, []);

  const select = useCallback((coord) => {
    const g = gridRef.current;
    if (g.editing && g.editing.coord === coord) return; // clicking the cell being edited
    setGrid({ anchor: coord, focus: coord, editing: null });
    report(coord, coord);
  }, [report]);

  const selectRect = useCallback((anchor, focus) => {
    setGrid({ anchor, focus, editing: null });
    report(anchor, focus);
  }, [report]);

  const beginEdit = useCallback((coord, draft = '') => {
    setGrid({ anchor: coord, focus: coord, editing: { coord, draft } });
  }, []);

  const setDraft = useCallback((draft) => {
    setGrid((g) => (g.editing ? { ...g, editing: { ...g.editing, draft } } : g));
  }, []);

  const commitEdit = useCallback(() => {
    setGrid((g) => (g.editing ? { ...g, editing: null } : g));
  }, []);

  const cancelEdit = useCallback(() => {
    setGrid((g) => (g.editing ? { ...g, editing: null } : g));
  }, []);

  const handleCellClick = useCallback((coord, e) => {
    if (suppressClickRef.current) { suppressClickRef.current = false; return; }
    if (e && e.shiftKey) {
      const anchor = gridRef.current.anchor;
      setGrid({ anchor, focus: coord, editing: null });
      report(anchor, coord);
      return;
    }
    select(coord);
  }, [report, select]);

  // mousedown only ARMS the drag gesture — it must NOT touch the
  // selection, or a Shift-click would lose its anchor before the click
  // event reads it. The rectangle is written by mouseenter (drag) or by
  // the click (plain/shift).
  const handleCellMouseDown = useCallback((coord, e) => {
    if (e && e.button !== 0) return;
    dragFromRef.current = coord;
    didDragRef.current = false;
  }, []);

  const handleCellMouseEnter = useCallback((coord) => {
    if (!dragFromRef.current) return;
    didDragRef.current = true;
    suppressClickRef.current = true; // the trailing click must not re-select
    const from = dragFromRef.current;
    setGrid((g) => ({ ...g, anchor: from, focus: coord }));
  }, []);

  useEffect(() => {
    const up = () => {
      if (!dragFromRef.current) return;
      const from = dragFromRef.current;
      dragFromRef.current = null;
      if (!didDragRef.current) return; // plain click: the click event reports
      didDragRef.current = false;
      // The post-drag click lands on a common ancestor, not on a cell —
      // clear the suppress flag here or it would swallow the NEXT click.
      suppressClickRef.current = false;
      report(from, gridRef.current.focus);
    };
    window.addEventListener('mouseup', up);
    return () => window.removeEventListener('mouseup', up);
  }, [report]);

  return useMemo(() => ({
    anchor: grid.anchor,
    focus: grid.focus,
    editing: grid.editing,
    select,
    selectRect,
    beginEdit,
    setDraft,
    commitEdit,
    cancelEdit,
    selected: (coord) => inRectangle(coord, grid.anchor, grid.focus),
    handleCellClick,
    handleCellMouseDown,
    handleCellMouseEnter,
  }), [grid, select, selectRect, beginEdit, setDraft, commitEdit, cancelEdit,
       handleCellClick, handleCellMouseDown, handleCellMouseEnter]);
}
"""
