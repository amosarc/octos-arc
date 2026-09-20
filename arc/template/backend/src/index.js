// index.js — 启动:PORT + ARC_EXTRA_PORTS 全部监听。
const app = require('./app');
const ports = [process.env.PORT || 3000].concat((process.env.ARC_EXTRA_PORTS || '').split(',').filter(Boolean));
for (const p of ports) app.listen(Number(p), () => console.log('[backend] listening on', p));
