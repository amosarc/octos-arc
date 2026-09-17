import { expect, test } from '@playwright/test';

test.describe('booking api contract', () => {
  test('REQ-3: health endpoint answers', async ({ request }) => {
    const res = await request.get('http://127.0.0.1:3301/api/health');
    expect(res.ok()).toBeTruthy();
  });

  test('REQ-3: search posts to the api', async ({ request }) => {
    const res = await request.post('/api/trains/search', {
      data: { from: 'Shanghai', to: 'Beijing', date: 'Sun, May 31' },
    });
    expect(res.status()).toBe(200);
  });
});
