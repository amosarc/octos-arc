const express = require('express');

const app = express();
app.use(express.json());

app.get('/api/health', (req, res) => {
  res.json({ ok: true });
});

app.post('/api/register', (req, res) => {
  res.status(201).json({ id: 1 });
});

module.exports = app;
