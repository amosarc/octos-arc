// template_smoke.js — 宿主基座端到端冒烟:cwd 必须是拷贝出的 backend/。
// 09-28 CRUD 生成器退役:业务 API(auth/CRUD)归模型(backend/src/routes/),
// 基座承诺 装配可启动 + /api/health + store 数据层(种子/运行双文件、
// query/tx/nextSeq)+ session 机制库 + 可选 crud 工厂 + 重启回种子。
// 用法: node template_smoke.js
const path = require('path');
const fs = require('fs');
const app = require(path.join(process.cwd(), 'src', 'app'));
const ok = (name, cond) => { if (!cond) { console.error('FAIL ' + name); process.exit(1); } console.log('PASS ' + name); };
(async () => {
  // 1) store:seed→run 双文件语义 + 数据层新原语(模块级,无 HTTP 形状假设)。
  const store = require(path.join(process.cwd(), 'src', 'database', 'store'));
  const seedPath = path.join(process.cwd(), 'data', 'db.json');
  const runPath = path.join(process.cwd(), 'data', 'db.run.json');
  fs.rmSync(runPath, { force: true });
  fs.writeFileSync(seedPath, JSON.stringify({ collections: { users: [
    { id: 'u1', username: 'smoke-user', password: store.hash('pw-plain') },
  ] } }));
  const db = store.load();
  ok('store-loads-seed', db.collections.users.length === 1);
  ok('store-run-file-created', fs.existsSync(runPath));
  ok('store-scrypt-roundtrip', store.verify('pw-plain', db.collections.users[0].password)
    && !store.verify('wrong', db.collections.users[0].password));
  store.insert('users', { id: 'u2', username: 'smoke-2' });
  ok('store-writes-run-not-seed',
    JSON.parse(fs.readFileSync(runPath, 'utf8')).collections.users.length === 2
    && JSON.parse(fs.readFileSync(seedPath, 'utf8')).collections.users.length === 1);
  // 数据层:get/update/query/tx/nextSeq。
  ok('store-get', store.get('users', 'u1').username === 'smoke-user' && store.get('users', 'nope') === null);
  store.update('users', 'u1', { username: 'smoke-user-2' });
  ok('store-update', store.get('users', 'u1').username === 'smoke-user-2');
  store.insert('smokethings', { id: 't1', name: 'beta', n: 2 });
  store.insert('smokethings', { id: 't2', name: 'alpha', n: 1 });
  store.insert('smokethings', { id: 't3', name: 'gamma', n: 3 });
  ok('store-query-filter-sort-page', JSON.stringify(
    store.query('smokethings', { where: (r) => r.n > 1, sort: 'name', offset: 0, limit: 1 })
      .map((r) => r.name)) === JSON.stringify(['beta']));
  let rolledBack = false;
  try { store.tx(() => { store.insert('smokethings', { id: 't4', name: 'doomed' }); throw new Error('boom'); }); }
  catch { rolledBack = store.get('smokethings', 't4') === null; }
  ok('store-tx-rolls-back', rolledBack);
  // tx 磁盘原子性:事务中途(nextSeq/insert 已改内存)崩溃/抛错,run 文件
  // 字节不变——txDepth 使 tx 内 save 变 no-op,收尾统一落盘。
  const beforeDisk = fs.readFileSync(runPath, 'utf8');
  try { store.tx(() => { store.nextSeq('issues', 'repo/x'); store.insert('smokethings', { id: 't5', name: 'doomed' }); throw new Error('boom'); }); } catch {}
  ok('store-tx-no-midwrite',
    fs.readFileSync(runPath, 'utf8') === beforeDisk
    && store.nextSeq('issues', 'repo/x') === 1);
  store.tx(() => { store.insert('smokethings', { id: 't6', name: 'kept' }); });
  ok('store-tx-commits',
    store.get('smokethings', 't6') !== null
    && JSON.parse(fs.readFileSync(runPath, 'utf8')).collections.smokethings.some((r) => r.id === 't6'));
  const seqA = store.nextSeq('issues', 'repo/one');
  ok('store-nextSeq-scoped', seqA === 1 && store.nextSeq('issues', 'repo/one') === 2
    && store.nextSeq('issues', 'repo/two') === 1);
  // 2) session 机制库:建会话→读当前用户→销毁(mock res cookie 头)。
  const session = require(path.join(process.cwd(), 'src', 'lib', 'session'));
  const mockRes = { h: null, setHeader(_k, v) { this.h = v; }, getHeader() { return this.h; } };
  const token = session.createSession(mockRes, 'u1');
  ok('session-sets-cookie', String(mockRes.h).includes('arc_session='));
  const req = { cookies: { arc_session: token } };
  ok('session-currentUser', session.currentUser(req, 'users').username === 'smoke-user-2');
  ok('session-currentUser-entity-miss', session.currentUser(req, 'ghosts') === null);
  session.destroySession(req, mockRes);
  ok('session-destroy', session.readSession(req) === null && session.currentUser(req, 'users') === null);
  // 3) crud 可选工厂:显式挂载 + 钩子 + 错误形状 + 自定义路由优先。
  // (express 从被测 backend 的 node_modules 解析——本文件物理上在 tests/。)
  const express = require(path.join(process.cwd(), 'node_modules', 'express'));
  const crud = require(path.join(process.cwd(), 'src', 'lib', 'crud'));
  const a2 = express();
  a2.use(express.json());
  a2.get('/api/smokethings', (_q, res) => res.json({ custom: true })); // 先注册→优先
  crud.mount(a2, 'smokethings', {
    publicRow: (r) => ({ id: r.id, name: r.name }),
    validate: (body) => (!body.name ? [{ field: 'name', msg: 'Name is required' }] : []),
    prepare: (body) => ({ name: String(body.name).trim() }),
  });
  await new Promise((res) => { a2.server = a2.listen(0, res); });
  const B2 = 'http://127.0.0.1:' + a2.server.address().port;
  const call = async (m, p, body) => {
    const r = await fetch(B2 + p, { method: m, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    let j = null; try { j = await r.json(); } catch {}
    return { status: r.status, json: j };
  };
  ok('crud-custom-route-precedence', (await call('GET', '/api/smokethings')).json.custom === true);
  ok('crud-post-201-public-row', (await call('POST', '/api/smokethings', { name: '  thing-1 ' })).status === 201);
  const bad = await call('POST', '/api/smokethings', {});
  ok('crud-validate-400-shape', bad.status === 400 && bad.json.errors[0].field === 'name'
    && bad.json.errors[0].msg === 'Name is required');
  ok('crud-bad-payload-never-5xx', (await call('POST', '/api/smokethings', 'not-json-object')).status === 400);
  const created = store.all('smokethings').find((r) => r.name === 'thing-1');
  ok('crud-prepare-applied', !!created);
  ok('crud-get-one', (await call('GET', '/api/smokethings/' + created.id)).json.name === 'thing-1');
  ok('crud-get-404', (await call('GET', '/api/smokethings/nope')).status === 404);
  ok('crud-patch', (await call('PATCH', '/api/smokethings/' + created.id, { name: 'thing-1b' })).json.name === 'thing-1b');
  ok('crud-delete', (await call('DELETE', '/api/smokethings/' + created.id)).status === 200
    && store.get('smokethings', created.id) === null);
  a2.server.close();
  // 4) HTTP 面:健康检查 + 未知 API 走 express 404(装配可启动)。
  const server = app.listen(0);
  await new Promise((res) => server.once('listening', res));
  const B = 'http://127.0.0.1:' + server.address().port;
  ok('health', (await fetch(B + '/api/health')).status === 200);
  ok('unknown-api-is-404', (await fetch(B + '/api/nope')).status === 404);
  server.close();
  // 5) 重启回种子:store 是进程内单例,"重启"=新进程——子进程删 run
  //    文件后 load() 必须从种子重建(单进程内删档不重读,属预期)。
  const { spawnSync } = require('child_process');
  fs.rmSync(runPath, { force: true });
  const storePath = JSON.stringify(path.join(process.cwd(), 'src', 'database', 'store'));
  const chk = spawnSync(process.execPath, ['-e',
    `const s=require(${storePath});` +
    `process.exit(s.load().collections.users.length === 1 ? 0 : 1);`],
    { cwd: process.cwd() });
  ok('restart-resets-to-seed', chk.status === 0);
  fs.rmSync(runPath, { force: true }); // 留干净交付面
  process.exit(0);
})().catch((e) => { console.error('FAIL exception', e); process.exit(1); });
