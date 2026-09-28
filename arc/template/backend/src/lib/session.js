// session.js — 会话机制助手(纯库,不挂路由):登录/登出/me 路由由模型自行
// 接线;会话行存 store 的 'sessions' 集合——server 重启保留 db.run.json,
// 会话随之中存续(graded contract: "sessions survive server restart");
// 仅冷启动(无 run 文件)才从种子重建=干净会话面(交付前宿主删 run 文件,
// 评测从种子起)。hash/verify 转出口令哈希(宿主播种账号的 scrypt 已逐位
// 兼容,直接 verify)。
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
