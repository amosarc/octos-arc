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
