// crud.js — 可选 REST 工厂(纯库,绝不自动挂载):模型对需要的每个集合
// 显式 mount(app, '<collection>', opts);注册在本库之前的同名自定义路由
// 优先(express 注册序),挂完可继续加端点。行为规则全走钩子——校验规则、
// 错误文案(需求逐字串)、行可见性、列表过滤、落库前变换都由模型提供;
// 本库只供统一管道:默认 CRUD 端点、{errors:[{field,msg}]} 错误形状、
// 坏载荷一律 4xx 永不 5xx。
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
