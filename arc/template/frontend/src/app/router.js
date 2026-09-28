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
