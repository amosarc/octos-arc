// auth.js — 通用认证:schema 中 auth:true 的实体激活;登录标识 = 该实体 unique 非密码字段(用户名或邮箱)。
// 登录失败文案:schema.messages.loginFailure(需求引号串,codegen 桥灌入)>
// 默认 "Invalid username or password"——统一文案不泄露账号存在性,
// 且文本可被需求逐字契约覆盖(09-27 C1)。
const store = require('../database/store'); const { check } = require('./validate'); const U = require('./util');
const COOKIE = 'arc_session';
const SECRET = (f) => f.type === 'password' || f.format === 'password';
const DUP = (f) => (f.messages && f.messages.duplicate) || f.message || `${f.label || f.name} already exists`;
const pub = (e, r) => { const o = { id: r.id }; for (const f of e.fields || []) if (!SECRET(f) && !f.match && r[f.name] !== undefined) o[f.name] = r[f.name]; return o; };
const uniq = (e, body) => { const errors = check(e.fields, body); for (const f of e.fields || []) if (f.unique && body[f.name] !== undefined && store.dup(e.name, f.name, body[f.name])) errors.push({ field: f.name, msg: DUP(f) }); return errors; };
const matchIdent = (f, stored, ident) => stored !== undefined && (String(stored) === ident || (f.format === 'email' && String(stored).toLowerCase() === ident.toLowerCase()));
const startSession = (res, userId) => { const token = U.token(); store.insert('sessions', { token, userId, createdAt: U.now() }); U.setCookie(res, COOKIE, token); };
const readToken = (req) => (req.cookies || {})[COOKIE];
exports.currentUser = (e, req) => { const t = readToken(req); const s = t && store.one('sessions', (x) => x.token === t); return (s && store.one(e.name, (r) => r.id === s.userId)) || null; };
exports.mount = (app, schema) => {
  const e = (schema.entities || []).find((x) => x.auth);
  if (!e) return;
  const LOGIN_FAIL = (schema.messages && schema.messages.loginFailure) || 'Invalid username or password';
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
  } catch { U.bad(res, [{ msg: 'Invalid request' }]); } });
  app.post('/api/auth/login', (req, res) => { try {
    const b = req.body || {};
    const ident = String(b.username !== undefined ? b.username : b.identifier !== undefined ? b.identifier : b.email || '').trim();
    const row = ident && store.one(e.name, (r) => ids.some((f) => matchIdent(f, r[f.name], ident)));
    if (!row || !pwField || !store.verify(String(b.password || ''), row[pwField.name])) return U.err(res, LOGIN_FAIL);
    startSession(res, row.id);
    U.json(res, 200, { user: pub(e, row) });
  } catch { U.err(res, LOGIN_FAIL); } });
  app.post('/api/auth/logout', (req, res) => { const t = readToken(req); if (t) store.remove('sessions', (s) => s.token === t); U.setCookie(res, COOKIE, ''); U.json(res, 200, { ok: true }); });
  app.get('/api/me', (req, res) => { const u = exports.currentUser(e, req); U.json(res, 200, { user: u ? pub(e, u) : null }); });
};
