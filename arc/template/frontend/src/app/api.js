// Minimal same-origin REST client for the backend API.
// Non-2xx responses throw a structured error: `err.status` is the HTTP
// code and `err.body` is the parsed JSON response (raw text when not
// JSON). Read field errors from `err.body` — never parse `err.message`:
//   catch (err) { setErrors(err.body?.errors ?? []); }
async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  });
  if (!res.ok) {
    const raw = await res.text().catch(() => '');
    let body;
    try { body = JSON.parse(raw); } catch { body = raw; }
    const err = new Error(`${options.method || 'GET'} ${path} -> ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

const qs = (params) => {
  if (!params) return '';
  const pairs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  return pairs.length ? `?${pairs.join('&')}` : '';
};

export const api = {
  list: (resource, params) => request(`/api/${resource}${qs(params)}`),
  get: (resource, id) => request(`/api/${resource}/${id}`),
  create: (resource, data) =>
    request(`/api/${resource}`, { method: 'POST', body: JSON.stringify(data) }),
  update: (resource, id, data) =>
    request(`/api/${resource}/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  patch: (resource, id, data) =>
    request(`/api/${resource}/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (resource, id) =>
    request(`/api/${resource}/${id}`, { method: 'DELETE' }),
};
