// validate.js — schema 词表驱动校验;check(fields, body) → [{field,msg}]。
// 消息三级:field.messages[required|invalid|duplicate|match](三态) >
// field.message(单一覆盖,兼容) > 英文契约族默认("X is required"/
// "X is invalid"/"X does not match Y")。需求原文逐字引号串由
// scaffold.codegen 桥自动灌进 messages(09-27 C1:中文默认与需求英文
// 逐字契约冲突,注册/登录族是第一测试族+止损敏感区)。
const LABEL = (f) => f.label || f.name;
const MSG = (f, kind, dflt) => (f.messages && f.messages[kind]) || f.message || dflt;
const FMT = {
  email: (v, f) => (v.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) || MSG(f, 'invalid', `${LABEL(f)} is invalid`),
  username: (v, f) => /^[A-Za-z0-9_-]{3,32}$/.test(v) || MSG(f, 'invalid', `Invalid ${LABEL(f).toLowerCase()}`),
  password: (v, f) => { const min = f.min || 12; const max = f.max || 128; const ok = v.length >= min && v.length <= max && /[a-z]/.test(v) && /[A-Z]/.test(v) && /[0-9]/.test(v) && /[^A-Za-z0-9]/.test(v); return ok || MSG(f, 'invalid', `Invalid ${LABEL(f).toLowerCase()}: must be ${min}-${max} characters with upper, lower, digit and symbol`); },
};
exports.check = (fields, body) => {
  const errors = [];
  for (const f of fields || []) {
    if (f.match) { if (String(body[f.name] || '') !== String(body[f.match] || '')) errors.push({ field: f.name, msg: MSG(f, 'match', `${LABEL(f)} does not match ${f.match}`) }); continue; }
    const v = body[f.name];
    if (v === undefined || v === null || v === '') { if (f.required) errors.push({ field: f.name, msg: MSG(f, 'required', `${LABEL(f)} is required`) }); continue; }
    if (f.type === 'number' && typeof v !== 'number') { errors.push({ field: f.name, msg: MSG(f, 'invalid', `${LABEL(f)} is invalid`) }); continue; }
    if (f.type === 'boolean' && typeof v !== 'boolean') { errors.push({ field: f.name, msg: MSG(f, 'invalid', `${LABEL(f)} is invalid`) }); continue; }
    if (f.enum && !f.enum.includes(v)) { errors.push({ field: f.name, msg: MSG(f, 'invalid', `${LABEL(f)} is invalid`) }); continue; }
    const s = String(v);
    if ((f.min && s.length < f.min) || (f.max && s.length > f.max)) { errors.push({ field: f.name, msg: MSG(f, 'invalid', `${LABEL(f)} is invalid`) }); continue; }
    if (f.pattern && !new RegExp(f.pattern).test(s)) { errors.push({ field: f.name, msg: MSG(f, 'invalid', `${LABEL(f)} is invalid`) }); continue; }
    const fmt = f.format || (f.type === 'password' ? 'password' : null);
    if (fmt && FMT[fmt]) { const r = FMT[fmt](s, f); if (r !== true) errors.push({ field: f.name, msg: r }); }
  }
  return errors;
};
