// validate.js — schema 词表驱动校验;check(fields, body) → [{field,msg}],消息中英双关键字。
const FMT = {
  email: (v) => (v.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) || '邮箱无效 (invalid email)',
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
