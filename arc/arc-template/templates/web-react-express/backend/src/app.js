// app.js — express 装配:健康检查、auth、schema CRUD、业务 routes、静态 frontend/dist。
const path = require('path'); const fs = require('fs'); const express = require('express'); const U = require('./lib/util');
const app = express();
app.use(express.json({ limit: '64kb' }));
app.use((req, res, next) => { req.cookies = U.cookies(req); next(); });
app.get('/api/health', (req, res) => U.json(res, 200, { ok: true }));
const schema = require('./database/schema');
require('./lib/auth').mount(app, schema);
// 业务 routes 先于引擎 CRUD 注册：同名路径以任务定制实现为准（后注册不覆盖先注册）。
require('./routes')(app);
require('./database/engine').mount(app, schema);
const dist = path.join(__dirname, '..', '..', 'frontend', 'dist');
if (fs.existsSync(dist)) { app.use(express.static(dist)); app.get(/^(?!\/api(\/|$)).*/, (req, res) => res.sendFile(path.join(dist, 'index.html'))); }
module.exports = app;
