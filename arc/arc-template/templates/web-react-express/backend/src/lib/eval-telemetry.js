// eval-telemetry.js — host-owned runtime telemetry (best-effort, never throws).
// Appends one JSON line per /api request (and frontend error reports) to
// <workspace>/octos-logs/eval-traffic.jsonl. Pure fs, no dependencies.
const fs = require('fs');
const path = require('path');
const LOG = path.join(__dirname, '..', '..', '..', 'octos-logs', 'eval-traffic.jsonl');
const MAX_BYTES = 4 * 1024 * 1024;
function line(obj) {
  try {
    if (!fs.existsSync(path.dirname(LOG))) fs.mkdirSync(path.dirname(LOG), { recursive: true });
    try {
      if (fs.statSync(LOG).size > MAX_BYTES) {
        fs.copyFileSync(LOG, LOG + '.1');
        fs.writeFileSync(LOG, '');
      }
    } catch { /* fresh file */ }
    fs.appendFileSync(LOG, JSON.stringify(obj) + '\n');
  } catch { /* telemetry must never break the app */ }
}
const clip = (v, n) => {
  try {
    const s = typeof v === 'string' ? v : JSON.stringify(v);
    return s.length > n ? s.slice(0, n) + '…' : s;
  } catch { return undefined; }
};
exports.mount = (app) => {
  line({ t: new Date().toISOString(), kind: 'boot', port: process.env.PORT || 3000 });
  // Frontend error reports (sendBeacon with an application/json Blob).
  app.post('/api/__telemetry',
    require('express').json({ type: ['application/json', 'text/plain'] }),
    (req, res) => {
    const body = req.body;
    const events = Array.isArray(body) ? body : [body];
    for (const e of events) {
      if (e && typeof e === 'object') {
        line({ t: new Date().toISOString(), kind: String(e.kind || 'console'),
               msg: clip(e.msg, 300), href: clip(e.href, 120) });
      }
    }
    res.status(204).end();
  });
  // Traffic log: mounted FIRST so it sees every /api exchange. Mutation
  // request bodies (<=200 chars) and 4xx/5xx response bodies (<=300 chars,
  // via a res.json wrapper) carry the "why" of func failures.
  app.use((req, res, next) => {
    const started = Date.now();
    const isApi = String(req.originalUrl || req.url || '').startsWith('/api');
    let resClip;
    if (isApi) {
      const origJson = res.json.bind(res);
      res.json = (payload) => {
        try { if (res.statusCode >= 400) resClip = clip(payload, 300); } catch { /* best effort */ }
        return origJson(payload);
      };
    }
    res.on('finish', () => {
      if (!isApi) return;
      const rec = { t: new Date().toISOString(), kind: 'http', method: req.method,
                    path: clip(req.originalUrl || req.url, 160), status: res.statusCode,
                    ms: Date.now() - started };
      try {
        if (!/^GET$/i.test(req.method) && req.body !== undefined) rec.reqBody = clip(req.body, 200);
      } catch { /* best effort */ }
      if (res.statusCode >= 400 && resClip !== undefined) rec.resBody = resClip;
      line(rec);
    });
    next();
  });
};
