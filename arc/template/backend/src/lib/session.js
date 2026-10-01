// session.js — session mechanics helper (pure lib): login/logout/me
// routes are yours to wire on the store; hash/verify re-export password
// hashing for the accounts you seed.
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
