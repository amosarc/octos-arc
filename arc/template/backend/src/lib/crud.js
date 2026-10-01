// crud.js — optional REST factory (pure lib, never auto-mounts):
// mount(app, '<collection>', opts) for default CRUD endpoints; custom
// routes registered before mount take precedence. Bad payloads are 4xx,
// never 5xx; validation and messages come from your hooks.
const store = require('../database/store');
const U = require('./util');
const plain = (b) => (b && typeof b === 'object' && !Array.isArray(b) ? b : {});
const intOr = (v, d) => { const n = parseInt(v, 10); return Number.isFinite(n) && n >= 0 ? n : d; };
const NOT_FOUND = { errors: [{ msg: 'Not found' }] };
exports.mount = (app, name, opts) => {
  opts = opts || {};
  const pub = opts.publicRow || ((r) => r);
  const validate = opts.validate || (() => []);
  const prepare = opts.prepare || ((body) => body);
  const base = `/api/${name}`;
  app.get(base, (req, res) => { try {
    const q = opts.listQuery ? (opts.listQuery(req.query) || {}) : {};
    if (q.sort === undefined) q.sort = req.query.sort || undefined;
    if (q.desc === undefined) q.desc = req.query.dir === 'desc';
    if (q.offset === undefined) q.offset = intOr(req.query.offset, 0);
    if (q.limit === undefined && req.query.limit !== undefined) q.limit = intOr(req.query.limit, 0);
    return U.json(res, 200, store.query(name, q).map(pub));
  } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
  app.get(`${base}/:id`, (req, res) => { try {
    const r = store.get(name, req.params.id);
    return r ? U.json(res, 200, pub(r)) : U.json(res, 404, NOT_FOUND);
  } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
  app.post(base, (req, res) => { try {
    const body = plain(req.body);
    const errors = validate(body, { existing: null });
    if (errors.length) return U.bad(res, errors);
    const row = Object.assign({ id: U.id(), createdAt: U.now() }, body, prepare(body, { existing: null }));
    return U.json(res, 201, pub(store.insert(name, row)));
  } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
  const change = (req, res) => { try {
    const existing = store.get(name, req.params.id);
    if (!existing) return U.json(res, 404, NOT_FOUND);
    const body = plain(req.body);
    const errors = validate(body, { existing });
    if (errors.length) return U.bad(res, errors);
    const patch = Object.assign({}, body, prepare(body, { existing }));
    return U.json(res, 200, pub(store.update(name, existing.id, patch)));
  } catch { U.bad(res, [{ msg: 'Invalid request' }]); } };
  app.patch(`${base}/:id`, change);
  app.put(`${base}/:id`, change);
  app.delete(`${base}/:id`, (req, res) => { try {
    const n = store.remove(name, (r) => r.id === req.params.id);
    return n ? U.json(res, 200, { ok: true }) : U.json(res, 404, NOT_FOUND);
  } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
};
