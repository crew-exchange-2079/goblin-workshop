/*
 * One end-to-end smoke test: the page loads with no console errors, editing an
 * input recomputes the table and rewrites the URL, and that URL restores the
 * same plan in a fresh page.
 */
import { test, expect } from '@playwright/test';

test('loads, recalculates, and restores state from the URL', async ({ page, context }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));

  await page.goto('./');
  await expect(page.getByRole('heading', { name: 'LTO capacity planner' })).toBeVisible();
  const rows = page.locator('#results tbody tr');
  await expect(rows).toHaveCount(5);

  // 120 TB, no growth, one copy, native, 100% fill → exactly 10 LTO-8 cartridges
  for (const [id, v] of Object.entries({ size: '120', growth: '0', years: '0', copies: '1', inLibrary: '1', ratio: '1', fill: '100', spare: '0' })) {
    await page.fill(`#${id}`, v);
  }
  const lto8 = page.locator('tr[data-gen="lto8"]');
  await expect(lto8.locator('[data-k="tapes"]')).toHaveText('10');
  await expect(lto8.locator('[data-k="slots"]')).toHaveText('10');

  await page.fill('#copies', '3');
  await page.fill('#spare', '10');
  await expect(lto8.locator('[data-k="tapes"]')).toHaveText('30');
  await expect(lto8.locator('[data-k="slots"]')).toHaveText('11');
  await expect(page).toHaveURL(/[?&]c=3(&|$)/);

  const shared = page.url();
  const fresh = await context.newPage();
  await fresh.goto(shared);
  await expect(fresh.locator('#size')).toHaveValue('120');
  await expect(fresh.locator('#copies')).toHaveValue('3');
  await expect(fresh.locator('tr[data-gen="lto8"] [data-k="tapes"]')).toHaveText('30');

  expect(errors).toEqual([]);
});
