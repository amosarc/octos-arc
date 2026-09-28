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
