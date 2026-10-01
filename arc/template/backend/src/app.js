// app.js — express 装配:健康检查、业务 routes、静态 frontend/dist。
const path = require('path'); const fs = require('fs'); const express = require('express'); const U = require('./lib/util');
const app = express();
try { require('./lib/eval-telemetry').mount(app); } catch { /* telemetry best-effort, never blocks the app */ }
// 2mb body cap: an imported CSV travels inside the entity JSON.
app.use(express.json({ limit: '2mb' }));
app.use((req, res, next) => { req.cookies = U.cookies(req); next(); });
app.get('/api/health', (req, res) => U.json(res, 200, { ok: true }));
require('./routes')(app);
const dist = path.join(__dirname, '..', '..', 'frontend', 'dist');
if (fs.existsSync(dist)) { app.use(express.static(dist)); app.get(/^(?!\/api(\/|$)).*/, (req, res) => res.sendFile(path.join(dist, 'index.html'))); }
module.exports = app;
