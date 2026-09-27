// engine.js — schema 驱动 CRUD(非 auth 实体):每实体 POST(建)/GET(列)/GET /:id;坏载荷一律 4xx。
// 消息三级与 validate.js 同律:messages.duplicate > message > "X already exists"。
const store = require('./store'); const { check } = require('../lib/validate'); const U = require('../lib/util');
const SECRET = (f) => f.type === 'password' || f.format === 'password';
const DUP = (f) => (f.messages && f.messages.duplicate) || f.message || `${f.label || f.name} already exists`;
const pub = (e, r) => { const o = { id: r.id, createdAt: r.createdAt }; for (const f of e.fields || []) if (!SECRET(f) && r[f.name] !== undefined) o[f.name] = r[f.name]; return o; };
const plain = (req) => req.body && typeof req.body === 'object' && !Array.isArray(req.body) ? req.body : {};
const uniq = (e, body) => { const errors = check(e.fields, body); for (const f of e.fields || []) if (f.unique && body[f.name] !== undefined && store.dup(e.name, f.name, body[f.name])) errors.push({ field: f.name, msg: DUP(f) }); return errors; };
exports.mount = (app, schema) => {
  for (const e of (schema.entities || []).filter((x) => !x.auth)) {
    const base = `/api/${e.name}`;
    app.post(base, (req, res) => { try {
      const body = plain(req);
      const errors = uniq(e, body);
      if (errors.length) return U.bad(res, errors);
      const row = { id: U.id(), createdAt: U.now() };
      for (const f of e.fields || []) if (!f.match && body[f.name] !== undefined) row[f.name] = SECRET(f) ? store.hash(body[f.name]) : body[f.name];
      U.json(res, 201, pub(e, store.insert(e.name, row)));
    } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
    app.get(base, (req, res) => { try { U.json(res, 200, store.all(e.name).map((r) => pub(e, r))); } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
    app.get(`${base}/:id`, (req, res) => { try { const r = store.one(e.name, (x) => x.id === req.params.id); return r ? U.json(res, 200, pub(e, r)) : U.json(res, 404, { errors: [{ msg: 'Not found' }] }); } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
  }
};
