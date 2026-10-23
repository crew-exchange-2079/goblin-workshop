// Smoke tests: both static pages load cleanly, and every guided lesson can be
// finished from the browser by typing its solution into the console.
import { test, expect } from '@playwright/test';
import { LESSONS, SOLUTIONS } from '../lessons.js';

// fail any test whose page logs an error or throws
test.beforeEach(async ({ page }, info) => {
  const problems = [];
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') problems.push(`console.error: ${m.text()}`);
  });
  info.problems = problems;
});
test.afterEach(async ({}, info) => {
  expect(info.problems, 'page errors').toEqual([]);
});

async function noHorizontalScroll(page) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(sw, 'page scrolls sideways').toBeLessThanOrEqual(cw);
}

async function run(page, cmd) {
  await page.locator('#cmd').fill(cmd);
  await page.locator('#cmd').press('Enter');
}

test('playground: loads, draws the library and the clock moves', async ({ page }) => {
  await page.goto('/index.html?scenario=quiet-morning&autostart=1&speed=60');
  await expect(page.locator('#grid .slot')).toHaveCount(20);
  await expect(page.locator('#drives .drive')).toHaveCount(3);
  await expect(page.locator('#clock')).not.toHaveText('00:00', { timeout: 10_000 });
  await noHorizontalScroll(page);
});

test('playground: links to the lessons', async ({ page }) => {
  await page.goto('/index.html');
  await page.getByRole('link', { name: 'guided lessons' }).click();
  await expect(page).toHaveURL(/lessons\.html/);
  await expect(page.locator('#lessonlist .lessonbtn')).toHaveCount(5);
});

test('lessons: the picker lists all five and opens the first by default', async ({ page }) => {
  await page.goto('/lessons.html');
  const buttons = page.locator('#lessonlist .lessonbtn');
  await expect(buttons).toHaveCount(LESSONS.length);
  for (const [i, l] of LESSONS.entries()) await expect(buttons.nth(i)).toContainText(l.title);
  await expect(page.locator('#lessontitle')).toHaveText(LESSONS[0].title);
  await expect(page.locator('#steplist li.active')).toHaveCount(1);
  await noHorizontalScroll(page);
});

test('lessons: picking a lesson switches the library and the URL', async ({ page }) => {
  await page.goto('/lessons.html');
  await page.locator('[data-lesson="robot-jam"]').click();
  await expect(page.locator('#lessontitle')).toHaveText('Inventory after a robot jam');
  await expect(page).toHaveURL(/lesson=robot-jam/);
  await expect(page.locator('#robotbox')).toContainText('JAMMED');
  await expect(page.locator('#libstate')).toHaveText('inventory verified');
});

for (const lesson of LESSONS) {
  test(`lessons: "${lesson.title}" can be completed from the console`, async ({ page }) => {
    await page.goto(`/lessons.html?lesson=${lesson.id}`);
    await expect(page.locator('#lessontitle')).toHaveText(lesson.title);
    for (const cmd of SOLUTIONS[lesson.id]) await run(page, cmd);
    await expect(page.locator('#debrief')).toBeVisible();
    await expect(page.locator('#steplist li.passed')).toHaveCount(lesson.steps.length);
    await expect(page.locator(`[data-lesson="${lesson.id}"] .tick`)).toHaveText('✓ done');
  });
}

test('lessons: the hint button puts a working command in the console', async ({ page }) => {
  await page.goto('/lessons.html?lesson=mount');
  await page.getByRole('button', { name: 'Hint' }).click();
  await expect(page.locator('#cmd')).toHaveValue('status');
  await page.locator('#cmd').press('Enter');
  await expect(page.locator('#feedback')).toHaveText('✓ step 1 done');
  await expect(page.locator('#steplist li.passed')).toHaveCount(1);
});

test('lessons: the library refuses to move tapes on an unverified inventory', async ({ page }) => {
  await page.goto('/lessons.html?lesson=robot-jam');
  await run(page, 'robot reset');
  await expect(page.locator('#libstate')).toHaveText('inventory UNVERIFIED');
  await run(page, 'transfer 10 15');
  await expect(page.locator('#output .err').last()).toContainText('run `inventory`');
  await expect(page.locator('#feedback')).toHaveClass(/warn/);
  await run(page, 'inventory');
  await expect(page.locator('#output .ok').last()).toContainText('4 differences');
  await expect(page.locator('#libstate')).toHaveText('inventory verified');
});

test('lessons: finished lessons are remembered after a reload', async ({ page }) => {
  await page.goto('/lessons.html?lesson=mount');
  for (const cmd of SOLUTIONS.mount) await run(page, cmd);
  await expect(page.locator('#progress')).toHaveText('1/5 done');
  await page.reload();
  await expect(page.locator('#progress')).toHaveText('1/5 done');
  await expect(page.locator('[data-lesson="mount"] .tick')).toHaveText('✓ done');
  // restarting a lesson resets its steps, not the record that you finished it
  await page.getByRole('button', { name: 'Restart lesson' }).click();
  await expect(page.locator('#steplist li.passed')).toHaveCount(0);
  await expect(page.locator('#progress')).toHaveText('1/5 done');
});
