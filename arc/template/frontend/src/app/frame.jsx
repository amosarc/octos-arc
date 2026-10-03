import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';

// frame.jsx — the app shell every page renders inside: ONE persistent top
// header (brand + breadcrumb + actions), the shape the platform's
// reference screens share (GitHub-style app header / workbook title bar).
// Pages declare their context with useFrame — call it from the ONE
// component that owns the current page (two callers fight over chrome):
//   useFrame({ crumbs: [{ label: 'acme-docs', href: '#/repos/acme-docs' }],
//              actions: <button>New issue</button> });
const FrameContext = createContext({ setChrome: () => {} });

export function AppFrame({ brand = 'App', children }) {
  const [chrome, setChrome] = useState({ crumbs: [], actions: null });
  const value = useMemo(() => ({ setChrome }), []);
  return (
    <FrameContext.Provider value={value}>
      <header className="app-header">
        <span className="app-brand">{brand}</span>
        <nav className="app-crumbs" aria-label="Breadcrumb">
          {(chrome.crumbs || []).map((c) => (
            c.href
              ? <a key={c.label} href={c.href}>{c.label}</a>
              : <span key={c.label} className="app-crumb-here">{c.label}</span>
          ))}
        </nav>
        <span className="app-header-actions">{chrome.actions}</span>
      </header>
      <main className="app-main">{children}</main>
    </FrameContext.Provider>
  );
}

// Declare the header chrome for the current page. crumbs: [{label, href?}]
// — a crumb without href is the current page. actions: header-right JSX.
export function useFrame(chrome) {
  const { setChrome } = useContext(FrameContext);
  const crumbs = (chrome && chrome.crumbs) || [];
  const actions = (chrome && chrome.actions) || null;
  const key = JSON.stringify(crumbs);
  useEffect(() => {
    setChrome({ crumbs, actions });
    return () => setChrome({ crumbs: [], actions: null });
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
}

// Sub-view navigation for an entity page: named tabs, aria-current marks
// the active one. tabs: [{label, href, active?}].
export function TabNav({ label = 'Sections', tabs }) {
  return (
    <nav className="app-tabs" aria-label={label}>
      {(tabs || []).map((t) => (
        <a key={t.label} href={t.href}
           aria-current={t.active ? 'page' : undefined}>{t.label}</a>
      ))}
    </nav>
  );
}
