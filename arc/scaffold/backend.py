"""backend 模板常量(纯标准库):完整可启动后端的全部文件内容。

设计律:凡宿主能算的不交模型——HTTP/cookie/存储/校验/auth/CRUD 全部预置;
模型只声明 schema(经 scaffold.codegen 编译)与写业务端点/前端。
风格硬约束:JS 无空行、扁平早返回、2 空格缩进、变量名短而清晰
(文件会被模型经 repomap/直读反复消费,字节即 token)。
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

VALIDATE_JS = """\
// validate.js — schema 词表驱动校验;check(fields, body) → [{field,msg}],消息中英双关键字。
const FMT = {
  email: (v) => (v.length <= 254 && /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(v)) || '邮箱无效 (invalid email)',
  username: (v) => /^[A-Za-z0-9_-]{3,32}$/.test(v) || '用户名需 3-32 位字母数字连字符下划线 (invalid username)',
  password: (v, f) => { const min = f.min || 12; const max = f.max || 128; const ok = v.length >= min && v.length <= max && /[a-z]/.test(v) && /[A-Z]/.test(v) && /[0-9]/.test(v) && /[^A-Za-z0-9]/.test(v); return ok || `密码需 ${min}-${max} 位且含大小写字母数字特殊字符 (invalid password)`; },
};
const LABEL = (f) => f.label || f.name;
exports.check = (fields, body) => {
  const errors = [];
  for (const f of fields || []) {
    if (f.match) { if (String(body[f.name] || '') !== String(body[f.match] || '')) errors.push({ field: f.name, msg: f.message || `${LABEL(f)}与${f.match}不一致 (match)` }); continue; }
    const v = body[f.name];
    if (v === undefined || v === null || v === '') { if (f.required) errors.push({ field: f.name, msg: f.message || `${LABEL(f)}必填 (required)` }); continue; }
    if (f.type === 'number' && typeof v !== 'number') { errors.push({ field: f.name, msg: f.message || `${LABEL(f)}需为数字 (invalid)` }); continue; }
    if (f.type === 'boolean' && typeof v !== 'boolean') { errors.push({ field: f.name, msg: f.message || `${LABEL(f)}需为布尔 (invalid)` }); continue; }
    if (f.enum && !f.enum.includes(v)) { errors.push({ field: f.name, msg: f.message || `${LABEL(f)}无效 (invalid)` }); continue; }
    const s = String(v);
    if ((f.min && s.length < f.min) || (f.max && s.length > f.max)) { errors.push({ field: f.name, msg: f.message || `${LABEL(f)}长度需 ${f.min || 0}-${f.max || 128} (invalid)` }); continue; }
    if (f.pattern && !new RegExp(f.pattern).test(s)) { errors.push({ field: f.name, msg: f.message || `${LABEL(f)}格式无效 (invalid)` }); continue; }
    const fmt = f.format || (f.type === 'password' ? 'password' : null);
    if (fmt && FMT[fmt]) { const r = FMT[fmt](s, f); if (r !== true) errors.push({ field: f.name, msg: f.message || r }); }
  }
  return errors;
};
"""

STORE_JS = """\
// store.js — db.json 原子读改写、集合 CRUD、唯一索引(小写比较)、scrypt;进程内单例。
const fs = require('fs'); const path = require('path'); const crypto = require('crypto');
const FILE = path.join(__dirname, '..', '..', 'data', 'db.json');
let db = null;
exports.load = () => { if (db) return db; try { db = JSON.parse(fs.readFileSync(FILE, 'utf8')); } catch { db = {}; } if (!db.collections) db.collections = {}; return db; };
exports.save = () => { fs.mkdirSync(path.dirname(FILE), { recursive: true }); const tmp = `${FILE}.tmp`; fs.writeFileSync(tmp, JSON.stringify(db)); fs.renameSync(tmp, FILE); };
exports.coll = (name) => { const d = exports.load(); if (!d.collections[name]) d.collections[name] = []; return d.collections[name]; };
exports.all = (name) => exports.coll(name).slice();
exports.one = (name, fn) => exports.coll(name).find(fn);
exports.dup = (name, field, value, skipId) => { const s = String(value).toLowerCase(); return exports.coll(name).find((r) => r[field] !== undefined && String(r[field]).toLowerCase() === s && r.id !== skipId) || null; };
exports.insert = (name, row) => { exports.coll(name).push(row); exports.save(); return row; };
exports.remove = (name, fn) => { const d = exports.load(); const keep = d.collections[name].filter((r) => !fn(r)); const n = d.collections[name].length - keep.length; d.collections[name] = keep; if (n) exports.save(); return n; };
exports.hash = (pw) => { const salt = crypto.randomBytes(16).toString('hex'); return `${salt}:${crypto.scryptSync(String(pw), salt, 32).toString('hex')}`; };
exports.verify = (pw, stored) => { try { const [salt, h] = String(stored).split(':'); return crypto.scryptSync(String(pw), salt, 32).toString('hex') === h; } catch { return false; } };
"""

ENGINE_JS = """\
// engine.js — schema 驱动 CRUD(非 auth 实体):每实体 POST(建)/GET(列)/GET /:id;坏载荷一律 4xx。
const store = require('./store'); const { check } = require('../lib/validate'); const U = require('../lib/util');
const SECRET = (f) => f.type === 'password' || f.format === 'password';
const pub = (e, r) => { const o = { id: r.id, createdAt: r.createdAt }; for (const f of e.fields || []) if (!SECRET(f) && r[f.name] !== undefined) o[f.name] = r[f.name]; return o; };
const plain = (req) => req.body && typeof req.body === 'object' && !Array.isArray(req.body) ? req.body : {};
const uniq = (e, body) => { const errors = check(e.fields, body); for (const f of e.fields || []) if (f.unique && body[f.name] !== undefined && store.dup(e.name, f.name, body[f.name])) errors.push({ field: f.name, msg: f.message || `${f.label || f.name}已存在 (duplicate)` }); return errors; };
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
    } catch { U.bad(res, [{ msg: '请求无效 (invalid)' }]); } });
    app.get(base, (req, res) => { try { U.json(res, 200, store.all(e.name).map((r) => pub(e, r))); } catch { U.bad(res, [{ msg: '请求无效 (invalid)' }]); } });
    app.get(`${base}/:id`, (req, res) => { try { const r = store.one(e.name, (x) => x.id === req.params.id); return r ? U.json(res, 200, pub(e, r)) : U.json(res, 404, { errors: [{ msg: '资源不存在 (not found)' }] }); } catch { U.bad(res, [{ msg: '请求无效 (invalid)' }]); } });
  }
};
"""

AUTH_JS = """\
// auth.js — 通用认证:schema 中 auth:true 的实体激活;登录标识 = 该实体 unique 非密码字段(用户名或邮箱)。
const store = require('../database/store'); const { check } = require('./validate'); const U = require('./util');
const COOKIE = 'arc_session';
const SECRET = (f) => f.type === 'password' || f.format === 'password';
const pub = (e, r) => { const o = { id: r.id }; for (const f of e.fields || []) if (!SECRET(f) && !f.match && r[f.name] !== undefined) o[f.name] = r[f.name]; return o; };
const uniq = (e, body) => { const errors = check(e.fields, body); for (const f of e.fields || []) if (f.unique && body[f.name] !== undefined && store.dup(e.name, f.name, body[f.name])) errors.push({ field: f.name, msg: f.message || `${f.label || f.name}已存在 (duplicate)` }); return errors; };
const matchIdent = (f, stored, ident) => stored !== undefined && (String(stored) === ident || (f.format === 'email' && String(stored).toLowerCase() === ident.toLowerCase()));
const startSession = (res, userId) => { const token = U.token(); store.insert('sessions', { token, userId, createdAt: U.now() }); U.setCookie(res, COOKIE, token); };
const readToken = (req) => (req.cookies || {})[COOKIE];
exports.currentUser = (e, req) => { const t = readToken(req); const s = t && store.one('sessions', (x) => x.token === t); return (s && store.one(e.name, (r) => r.id === s.userId)) || null; };
exports.mount = (app, schema) => {
  const e = (schema.entities || []).find((x) => x.auth);
  if (!e) return;
  const pwField = (e.fields || []).find(SECRET);
  const ids = (e.fields || []).filter((f) => f.unique && !SECRET(f) && !f.match);
  app.post('/api/auth/register', (req, res) => { try {
    const body = req.body && typeof req.body === 'object' ? req.body : {};
    const errors = uniq(e, body);
    if (errors.length) return U.bad(res, errors);
    const row = { id: U.id(), createdAt: U.now() };
    for (const f of e.fields || []) if (!f.match && body[f.name] !== undefined) row[f.name] = SECRET(f) ? store.hash(body[f.name]) : body[f.name];
    store.insert(e.name, row);
    startSession(res, row.id);
    U.json(res, 201, { user: pub(e, row) });
  } catch { U.bad(res, [{ msg: '请求无效 (invalid)' }]); } });
  app.post('/api/auth/login', (req, res) => { try {
    const b = req.body || {};
    const ident = String(b.username !== undefined ? b.username : b.identifier !== undefined ? b.identifier : b.email || '').trim();
    const row = ident && store.one(e.name, (r) => ids.some((f) => matchIdent(f, r[f.name], ident)));
    if (!row || !pwField || !store.verify(String(b.password || ''), row[pwField.name])) return U.err(res, '账号或密码错误 (invalid credentials)');
    startSession(res, row.id);
    U.json(res, 200, { user: pub(e, row) });
  } catch { U.err(res, '账号或密码错误 (invalid credentials)'); } });
  app.post('/api/auth/logout', (req, res) => { const t = readToken(req); if (t) store.remove('sessions', (s) => s.token === t); U.setCookie(res, COOKIE, ''); U.json(res, 200, { ok: true }); });
  app.get('/api/me', (req, res) => { const u = exports.currentUser(e, req); U.json(res, 200, { user: u ? pub(e, u) : null }); });
};
"""

APP_JS = """\
// app.js — express 装配:健康检查、auth、schema CRUD、业务 routes、静态 frontend/dist。
const path = require('path'); const fs = require('fs'); const express = require('express'); const U = require('./lib/util');
const app = express();
app.use(express.json({ limit: '64kb' }));
app.use((req, res, next) => { req.cookies = U.cookies(req); next(); });
app.get('/api/health', (req, res) => U.json(res, 200, { ok: true }));
const schema = require('./database/schema');
require('./lib/auth').mount(app, schema);
// 业务 routes 先于引擎 CRUD 注册：同名路径以任务定制实现为准（后注册不覆盖先注册）。
require('./routes')(app);
require('./database/engine').mount(app, schema);
const dist = path.join(__dirname, '..', '..', 'frontend', 'dist');
if (fs.existsSync(dist)) { app.use(express.static(dist)); app.get(/^(?!\\/api(\\/|$)).*/, (req, res) => res.sendFile(path.join(dist, 'index.html'))); }
module.exports = app;
"""

INDEX_JS = """\
// index.js — 启动:PORT + ARC_EXTRA_PORTS 全部监听。
const app = require('./app');
const ports = [process.env.PORT || 3000].concat((process.env.ARC_EXTRA_PORTS || '').split(',').filter(Boolean));
for (const p of ports) app.listen(Number(p), () => console.log('[backend] listening on', p));
"""

ROUTES_INDEX_JS = """\
// routes/index.js — 业务端点扩展点:本任务的特有 API 路由挂在这里(用 ../database/store 与 ../lib/*)。
module.exports = () => {};
"""

SCHEMA_JS = """\
// schema.js — 宿主由 backend/data/schema.json 编译生成,勿手改;空 entities = 仅通用层。
module.exports = { "entities": [] };
"""

DB_JSON = '{"collections":{}}\n'
