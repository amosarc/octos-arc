// Minimal same-origin REST client for the backend API.
async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${options.method || 'GET'} ${path} -> ${res.status} ${body.slice(0, 200)}`);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  list: (resource) => request(`/api/${resource}`),
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
