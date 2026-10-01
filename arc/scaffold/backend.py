"""
backend 模板常量(纯标准库):完整可启动后端的全部文件内容。
"""

from __future__ import annotations

NPMRC = "registry=https://registry.npmmirror.com\n"

BACKEND_PACKAGE_JSON = {
    "name": "backend",
    "version": "1.0.0",
    "private": True,
    "main": "src/index.js",
    "scripts": {
        "start": "node src/index.js",
        "dev": "node --watch src/index.js",
        "test": "node --test test/",
    },
    "dependencies": {"express": "^4.19.2"},
}

UTIL_JS = """\
// util.js — HTTP 响应/cookie/随机 id 工具。
const crypto = require('crypto');
exports.json = (res, code, data) => res.status(code).json(data);
exports.bad = (res, errors) => res.status(400).json({ errors });
exports.err = (res, msg) => res.status(400).json({ errors: [{ msg }] });
exports.cookies = (req) => Object.fromEntries((req.headers.cookie || '').split(';').filter((p) => p.includes('=')).map((p) => { const i = p.indexOf('='); return [p.slice(0, i).trim(), decodeURIComponent(p.slice(i + 1))]; }));
exports.setCookie = (res, name, value) => { const c = `${name}=${encodeURIComponent(value)}; Path=/; HttpOnly; SameSite=Lax`; const old = res.getHeader('Set-Cookie'); res.setHeader('Set-Cookie', old ? [].concat(old, c) : c); };
exports.token = () => crypto.randomBytes(24).toString('base64url');
exports.id = () => Date.now().toString(36) + crypto.randomBytes(5).toString('hex');
exports.now = () => new Date().toISOString();
"""

SESSION_JS = """\
// session.js — 会话机制助手(纯库,不挂路由):登录/登出/me 路由由模型自行
// 接线;会话行存 store 的 'sessions' 集合——server 重启保留 db.run.json,
// 会话随之中存续(graded contract: "sessions survive server restart");
// 仅冷启动(无 run 文件)才从种子重建=干净会话面(交付前宿主删 run 文件,
// 评测从种子起;种子由模型按需求实现)。hash/verify 转出口令哈希——模型
// 种子里的口令一律经 store.hash 生成,直接 verify 兼容。
const store = require('../database/store');
const U = require('./util');
const COOKIE = 'arc_session';
const readToken = (req) => (req.cookies || {})[COOKIE];
exports.createSession = (res, userId) => { const token = U.token(); store.insert('sessions', { token, userId, createdAt: U.now() }); U.setCookie(res, COOKIE, token); return token; };
exports.readSession = (req) => { const t = readToken(req); return (t && store.one('sessions', (s) => s.token === t)) || null; };
exports.currentUser = (req, entity) => { const s = exports.readSession(req); if (!s) return null; return store.one(entity || 'users', (r) => r.id === s.userId) || null; };
exports.destroySession = (req, res) => { const t = readToken(req); if (t) store.remove('sessions', (s) => s.token === t); U.setCookie(res, COOKIE, ''); };
exports.hash = store.hash;
exports.verify = store.verify;
"""

CRUD_JS = """\
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
"""

STORE_JS = """\
// store.js — 种子/运行时双文件 db:db.json 是模型维护的种子(交付干净、git 跟踪),
// db.run.json 承载运行时状态:无 run 文件时从种子初始化,有则原样加载——
// 写入与会话跨 server 重启存活(需求侧持久化事实)。「回到种子起点」
// 由宿主在彩排/Tier-2/评测每次 boot 前删 run 文件达成,交付前同样删它
// (zip 只带干净种子);不是本 store 的重启行为。
// 集合 CRUD、通用查询(query:where/sort/分页)、原子多写事务(tx)、每作用域
// 自增序号(nextSeq)、唯一索引(小写比较)、scrypt;进程内单例。
const fs = require('fs'); const path = require('path'); const crypto = require('crypto');
const DATA = path.join(__dirname, '..', '..', 'data');
const SEED_FILE = path.join(DATA, 'db.json');
const FILE = path.join(DATA, 'db.run.json');
let db = null;
let txDepth = 0; // tx 期间 save 延迟到收尾——中途崩溃磁盘零半写
const doSave = () => { fs.mkdirSync(DATA, { recursive: true }); const tmp = `${FILE}.tmp`; fs.writeFileSync(tmp, JSON.stringify(db)); fs.renameSync(tmp, FILE); };
exports.load = () => {
  if (db) return db;
  fs.mkdirSync(DATA, { recursive: true });
  if (!fs.existsSync(FILE)) { try { fs.copyFileSync(SEED_FILE, FILE); } catch { /* no seed yet */ } }
  try { db = JSON.parse(fs.readFileSync(FILE, 'utf8')); } catch { db = {}; }
  if (!db.collections) db.collections = {};
  return db;
};
exports.save = () => { if (txDepth) return; doSave(); };
exports.coll = (name) => { const d = exports.load(); if (!d.collections[name]) d.collections[name] = []; return d.collections[name]; };
exports.all = (name) => exports.coll(name).slice();
exports.one = (name, fn) => exports.coll(name).find(fn);
exports.get = (name, id) => exports.coll(name).find((r) => r.id === id) || null;
exports.dup = (name, field, value, skipId) => { const s = String(value).toLowerCase(); return exports.coll(name).find((r) => r[field] !== undefined && String(r[field]).toLowerCase() === s && r.id !== skipId) || null; };
exports.insert = (name, row) => { exports.coll(name).push(row); exports.save(); return row; };
exports.update = (name, id, patch) => { const r = exports.get(name, id); if (!r) return null; if (patch && typeof patch === 'object' && !Array.isArray(patch)) Object.assign(r, patch); exports.save(); return r; };
exports.remove = (name, fn) => { const d = exports.load(); const keep = d.collections[name].filter((r) => !fn(r)); const n = d.collections[name].length - keep.length; d.collections[name] = keep; if (n) exports.save(); return n; };
exports.query = (name, o) => { o = o || {}; let rows = exports.all(name); if (o.where) rows = rows.filter(o.where); if (o.sort) { const k = o.sort; rows = rows.slice().sort((a, b) => { const x = a[k]; const y = b[k]; const c = x === y ? 0 : (x > y ? 1 : -1); return o.desc ? -c : c; }); } const off = Math.max(0, o.offset | 0); return o.limit === undefined ? rows.slice(off) : rows.slice(off, off + Math.max(0, o.limit | 0)); };
exports.tx = (fn) => { const d = exports.load(); const snap = JSON.stringify(d); txDepth++; try { const out = fn(d); txDepth--; doSave(); return out; } catch (e) { txDepth--; const fresh = JSON.parse(snap); for (const k of Object.keys(d)) delete d[k]; Object.assign(d, fresh); throw e; } };
exports.nextSeq = (name, scope) => { const d = exports.load(); if (!d.seq) d.seq = {}; const key = `${name}:${scope === undefined ? '_' : scope}`; const n = (d.seq[key] || 0) + 1; d.seq[key] = n; exports.save(); return n; };
exports.hash = (pw) => { const salt = crypto.randomBytes(16).toString('hex'); return `${salt}:${crypto.scryptSync(String(pw), salt, 32).toString('hex')}`; };
exports.verify = (pw, stored) => { try { const [salt, h] = String(stored).split(':'); return crypto.scryptSync(String(pw), salt, 32).toString('hex') === h; } catch { return false; } };
"""

APP_JS = """\
// app.js — express 装配:健康检查、业务 routes、静态 frontend/dist。
const path = require('path'); const fs = require('fs'); const express = require('express'); const U = require('./lib/util');
const app = express();
try { require('./lib/eval-telemetry').mount(app); } catch { /* telemetry best-effort, never blocks the app */ }
// 2mb: workbook/document states travel whole in the entity JSON (an
// imported CSV becomes part of the row) — a small body cap turns a large
// import into a 413 the frontend reports as a content failure.
app.use(express.json({ limit: '2mb' }));
app.use((req, res, next) => { req.cookies = U.cookies(req); next(); });
app.get('/api/health', (req, res) => U.json(res, 200, { ok: true }));
require('./routes')(app);
const dist = path.join(__dirname, '..', '..', 'frontend', 'dist');
if (fs.existsSync(dist)) { app.use(express.static(dist)); app.get(/^(?!\\/api(\\/|$)).*/, (req, res) => res.sendFile(path.join(dist, 'index.html'))); }
module.exports = app;
"""

# backend/src/lib/eval-telemetry.js — 评测期遥测(2026-09-27)。
# 平台 func/e2e 评测在 agent 退出后于同一 workspace 跑分,失败明细平台不下发;
# 应用自身把全量 /api 流量 + 前端异常逐行追加到 workspace 的
# octos/logs/eval-traffic.jsonl,随工作区 zip 带回做赛后取证
# (2026-09-26 github:8 会话后零写入的死因、func 失败的响应体,都靠这层还原)。
# 约束:零依赖 CommonJS;所有写入 try/catch best-effort——遥测永远不能
# 影响应用行为;>4MB 轮转保留一代;agent 阶段流量与评测流量同文件,
# 由 postflight 写入的 marker 行切分。
# 09-29:路径从 octos-logs/ 收敛进 octos/logs/(全部日志统一 octos/ 一处)。
EVAL_TELEMETRY_JS = """\
// eval-telemetry.js — host-owned runtime telemetry (best-effort, never throws).
// Appends one JSON line per /api request (and frontend error reports) to
// <workspace>/octos/logs/eval-traffic.jsonl. Pure fs, no dependencies.
const fs = require('fs');
const path = require('path');
const LOG = path.join(__dirname, '..', '..', '..', 'octos', 'logs', 'eval-traffic.jsonl');
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
    fs.appendFileSync(LOG, JSON.stringify(obj) + '\\n');
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
"""

INDEX_JS = """\
// index.js — 启动:PORT + ARC_EXTRA_PORTS 全部监听。
const app = require('./app');
const ports = [process.env.PORT || 3000].concat((process.env.ARC_EXTRA_PORTS || '').split(',').filter(Boolean));
for (const p of ports) app.listen(Number(p), () => console.log('[backend] listening on', p));
"""

ROUTES_INDEX_JS = """\
// routes/index.js — ALL task API endpoints live here (auth included):
// implement the requirement's data model and flows directly with Express,
// on top of ../database/store (seed/run dual-file JSON store; db.json is
// the graded seed — load its rows, never delete or rename them) and
// ../lib/util (json/bad/err responses, cookies, ids, scrypt helpers are
// in store.hash/verify). Auth, CRUD, search, flows — everything the
// requirements name is yours to write in this directory.
module.exports = () => {};
"""

DB_JSON = '{"collections":{}}\n'
