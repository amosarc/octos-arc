import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';
import { api } from './api.js';

// Shared-data layer: load backend resources through
// useResource('<resource>') so shared entities live in one place; features
// read `items` and call create/update/remove.
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
