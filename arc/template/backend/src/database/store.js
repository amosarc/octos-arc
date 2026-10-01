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
