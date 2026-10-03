// router.js — hash routing with pathname deep-link fallback:
// parseRoute (pure) + useHashRoute hook; route table and path shapes are
// yours (e.g. #/wb/<id> deep links). A URL like /repos/acme-docs (no
// hash) resolves through the pathname — the backend serves index.html
// for every non-/api GET, so deep links render the target page directly
// instead of blanking.
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

// Hash wins; empty hash falls back to the URL pathname.
export function currentRoute() {
  if (String(window.location.hash || '').replace(/^#/, '')) {
    return parseRoute(window.location.hash);
  }
  return parseRoute(window.location.pathname);
}

export function useHashRoute() {
  const [route, setRoute] = useState(() => currentRoute());
  useEffect(() => {
    const on = () => setRoute(currentRoute());
    window.addEventListener('hashchange', on);
    window.addEventListener('popstate', on);
    return () => {
      window.removeEventListener('hashchange', on);
      window.removeEventListener('popstate', on);
    };
  }, []);
  const navigate = (to) => { window.location.hash = to; };
  return { route, navigate };
}
