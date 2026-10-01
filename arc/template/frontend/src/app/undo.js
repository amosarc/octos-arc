// undo.js — snapshot undo/redo stack hook: push() before each
// mutation (you supply the snapshot fn), undo()/redo() return snapshots
// for you to write back; canUndo/canRedo drive button disabled states.
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
