// 360 px wide (the narrowest common phone): no sideways scroll, controls usable.
import { test, expect } from '@playwright/test';
import { STATES } from './states.mjs';

test.use({ viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true });

const overflow = (page) =>
  page.evaluate(() => {
    const w = document.documentElement.clientWidth;
    const wide = [...document.querySelectorAll('body *')]
      .filter((el) => !el.closest('.tablewrap') && el.getClientRects().length)
      .filter((el) => el.getBoundingClientRect().right > w + 0.5)
      .map((el) => el.id || el.className || el.tagName);
    return { scroll: document.documentElement.scrollWidth, width: w, wide: wide.slice(0, 5) };
  });

for (const state of STATES) {
  test(`no horizontal overflow at 360 px: ${state.name}`, async ({ page }) => {
    await page.goto('index.html' + state.query);
    const o = await overflow(page);
    expect(o.wide, 'elements past the right edge').toEqual([]);
    expect(o.scroll).toBeLessThanOrEqual(o.width);
  });
}

test('no overflow while the robot works across a whole run at 360 px', async ({ page }) => {
  await page.goto('index.html?scenario=big-restore&speed=60');
  for (let i = 0; i < 12; i++) {
    await page.locator('#step').tap();
    const o = await overflow(page);
    expect(o.wide, `minute ${i + 1}`).toEqual([]);
  }
});

test('console controls fit and are big enough to tap at 360 px', async ({ page }) => {
  await page.goto('index.html');
  for (const id of ['scenario', 'policy', 'playpause', 'reset', 'step', 'compare', 'speed']) {
    const box = await page.locator('#' + id).boundingBox();
    expect(box.x, id).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width, id).toBeLessThanOrEqual(360);
    expect(box.height, id).toBeGreaterThanOrEqual(24);
  }
  await page.locator('#playpause').tap();
  await expect(page.locator('#runstate')).toHaveText('running');
});

test('comparison table scrolls inside its own box at 360 px', async ({ page }) => {
  await page.goto('index.html?compare=1');
  const wrap = page.locator('.tablewrap');
  await expect(wrap).toBeVisible();
  const { client, scroll } = await wrap.evaluate((el) => ({ client: el.clientWidth, scroll: el.scrollWidth }));
  expect(client).toBeLessThanOrEqual(360);
  expect(scroll).toBeGreaterThanOrEqual(client);
  await page.screenshot({ path: test.info().outputPath('360-compare.png'), fullPage: true });
});
