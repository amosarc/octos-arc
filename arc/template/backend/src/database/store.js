// store.js — 种子/运行时双文件 db:db.json 是模型维护的种子(交付干净、git 跟踪),
// db.run.json 承载运行时状态;进程首次 load 时从种子初始化,此后读写只走 run 文件
// ——每次 server 重启(rehearsal/Tier-2/eval)都自动回到种子起点,跨轮测试污染不累积。
// 集合 CRUD、唯一索引(小写比较)、scrypt;进程内单例。
const fs = require('fs'); const path = require('path'); const crypto = require('crypto');
const DATA = path.join(__dirname, '..', '..', 'data');
const SEED_FILE = path.join(DATA, 'db.json');
const FILE = path.join(DATA, 'db.run.json');
let db = null;
exports.load = () => {
  if (db) return db;
  fs.mkdirSync(DATA, { recursive: true });
  if (!fs.existsSync(FILE)) { try { fs.copyFileSync(SEED_FILE, FILE); } catch { /* no seed yet */ } }
  try { db = JSON.parse(fs.readFileSync(FILE, 'utf8')); } catch { db = {}; }
  if (!db.collections) db.collections = {};
  return db;
};
exports.save = () => { fs.mkdirSync(DATA, { recursive: true }); const tmp = `${FILE}.tmp`; fs.writeFileSync(tmp, JSON.stringify(db)); fs.renameSync(tmp, FILE); };
exports.coll = (name) => { const d = exports.load(); if (!d.collections[name]) d.collections[name] = []; return d.collections[name]; };
exports.all = (name) => exports.coll(name).slice();
exports.one = (name, fn) => exports.coll(name).find(fn);
exports.dup = (name, field, value, skipId) => { const s = String(value).toLowerCase(); return exports.coll(name).find((r) => r[field] !== undefined && String(r[field]).toLowerCase() === s && r.id !== skipId) || null; };
exports.insert = (name, row) => { exports.coll(name).push(row); exports.save(); return row; };
exports.remove = (name, fn) => { const d = exports.load(); const keep = d.collections[name].filter((r) => !fn(r)); const n = d.collections[name].length - keep.length; d.collections[name] = keep; if (n) exports.save(); return n; };
exports.hash = (pw) => { const salt = crypto.randomBytes(16).toString('hex'); return `${salt}:${crypto.scryptSync(String(pw), salt, 32).toString('hex')}`; };
exports.verify = (pw, stored) => { try { const [salt, h] = String(stored).split(':'); return crypto.scryptSync(String(pw), salt, 32).toString('hex') === h; } catch { return false; } };
