// template_smoke.js — 宿主模板端到端冒烟:cwd 必须是拷贝出的 backend/。
// 用法: node template_smoke.js        (phase 1: 全流程断言 + 派生 phase 2)
//       node template_smoke.js phase2  (phase 2: 重启后会话恢复)
const path = require('path');
const { spawn } = require('child_process');
const app = require(path.join(process.cwd(), 'src', 'app'));
const Phase2 = process.argv[2] === 'phase2';
const TOKEN_MSG = 'Invalid username or password';
let B = 'http://127.0.0.1';
let cookie = Phase2 && process.env.ARC_TEST_TOKEN ? 'arc_session=' + process.env.ARC_TEST_TOKEN : '';
async function call(method, path, body, withCookie = true) {
  const headers = { 'content-type': 'application/json' };
  if (withCookie && cookie) headers.cookie = cookie;
  const r = await fetch(B + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const sc = r.headers.get('set-cookie');
  if (sc) cookie = sc.split(';')[0];
  let j = null; try { j = await r.json(); } catch {}
  return { status: r.status, json: j };
}
const ok = (name, cond) => { if (!cond) { console.error('FAIL ' + name); process.exit(1); } console.log('PASS ' + name); };
(async () => {
  const server = app.listen(0);
  await new Promise((res) => server.once('listening', res));
  B += ':' + server.address().port;
  const p = (m, path, body, ck) => call(m, path, body, ck).then((r) => (r._path = path, r));
  try {
    if (Phase2) {
      const me = await call('GET', '/api/me');
      ok('phase2-session-survives-restart', me.json && me.json.user && me.json.user.username === process.env.ARC_TEST_USER);
      server.close();
      return process.exit(0);
    }
    ok('health', (await call('GET', '/api/health', undefined, false)).status === 200);
    const reg = await call('POST', '/api/auth/register', { username: 'tb-user-1', email: 'User@Example.com', password: 'Valid-password-123!', confirmPassword: 'Valid-password-123!', idType: '护照', idNumber: 'P123456', ptype: '成人', agree: true });
    ok('register-201', reg.status === 201 && reg.json.user && !('password' in reg.json.user) && !('confirmPassword' in reg.json.user));
    ok('register-sets-session-cookie', cookie.includes('arc_session='));
    const fs = require('fs'); const db = JSON.parse(fs.readFileSync('data/db.run.json', 'utf8'));
    const seed = JSON.parse(fs.readFileSync('data/db.json', 'utf8'));
    ok('register-persists-hash', db.collections.users[0].password.includes(':') && db.collections.users[0].password.length < 200 && !db.collections.users[0].password.includes('Valid-password'));
    ok('register-persists-session', db.collections.sessions.length === 1);
    ok('seed-file-untouched-by-runtime', !((seed.collections || {}).users || []).length, 'db.json must stay the pristine seed');
    ok('register-dup-username', (await p('POST', '/api/auth/register', { username: 'tb-user-1', email: 'other@x.com', password: 'Valid-password-123!', confirmPassword: 'Valid-password-123!', idType: '护照', idNumber: 'P2', ptype: '成人', agree: true })).json.errors.some((e) => /already exists/i.test(e.msg)));
    ok('register-bad-password', (await call('POST', '/api/auth/register', { username: 'u2', email: 'u2@x.com', password: 'short', confirmPassword: 'short', idType: '护照', idNumber: 'P3', ptype: '成人', agree: true })).json.errors.some((e) => /invalid password/i.test(e.msg)));
    ok('register-mismatch', (await call('POST', '/api/auth/register', { username: 'u3', email: 'u3@x.com', password: 'Valid-password-123!', confirmPassword: 'nope', idType: '护照', idNumber: 'P4', ptype: '成人', agree: true })).json.errors.some((e) => /match/.test(e.msg)));
    ok('register-missing', (await call('POST', '/api/auth/register', {})).json.errors.some((e) => /required/.test(e.msg)));
    const login = await call('POST', '/api/auth/login', { username: '  user@example.com ', password: 'Valid-password-123!' });
    ok('login-by-email-case-insensitive-trimmed', login.status === 200 && login.json.user.username === 'tb-user-1');
    const badPw = await call('POST', '/api/auth/login', { username: 'tb-user-1', password: 'wrong' });
    const noUser = await call('POST', '/api/auth/login', { username: 'ghost', password: 'wrong' });
    ok('login-uniform-error', badPw.status === 400 && noUser.status === 400 && badPw.json.errors[0].msg === TOKEN_MSG && noUser.json.errors[0].msg === TOKEN_MSG);
    ok('me', (await call('GET', '/api/me')).json.user.username === 'tb-user-1');
    const token = cookie;
    ok('logout', (await call('POST', '/api/auth/logout')).status === 200);
    cookie = token;
    ok('logout-invalidates-session', (await call('GET', '/api/me')).json.user === null);
    ok('crud-post-empty-rejected', (await call('POST', '/api/things', {}, false)).json.errors.some((e) => /required/.test(e.msg)));
    ok('crud-post-oversized-rejected', (await call('POST', '/api/things', { name: 'x'.repeat(10240) }, false)).status === 400);
    ok('crud-post-ok', (await call('POST', '/api/things', { name: 'thing-1' }, false)).status === 201);
    const list = await call('GET', '/api/things', undefined, false);
    ok('crud-list-and-get-one', Array.isArray(list.json) && list.json.length === 1 && (await call('GET', '/api/things/' + list.json[0].id, undefined, false)).status === 200);
    ok('crud-404', (await call('GET', '/api/things/nope', undefined, false)).status === 404);
    cookie = '';
    await call('POST', '/api/auth/login', { username: 'tb-user-1', password: 'Valid-password-123!' });
    const spawn2 = spawn(process.execPath, [__filename, 'phase2'], { cwd: process.cwd(), env: { ...process.env, ARC_TEST_USER: 'tb-user-1', ARC_TEST_TOKEN: cookie.split('=')[1] }, stdio: 'inherit' });
    spawn2.on('exit', (code) => process.exit(code));
    return;
  } catch (e) { console.error('FAIL exception', e); process.exit(1); } finally { if (!Phase2) server.close(); }
})();
