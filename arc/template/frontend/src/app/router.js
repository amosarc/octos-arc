// router.js — hash routing: parseRoute (pure) + useHashRoute hook;
// route table and path shapes are yours (e.g. #/wb/<id> deep links).
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
