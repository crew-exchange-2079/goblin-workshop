// Keyboard-only operation of the control console and the scenario (lesson) picker:
// everything reachable by Tab, operable without a pointer, focus never lost.
import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('index.html');
  await expect(page.locator('#runstate')).toHaveText('paused');
});

const focusedId = (page) => page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName);

test('Tab reaches every console control in visual order', async ({ page }) => {
  const order = [];
  for (let i = 0; i < 9; i++) {
    await page.keyboard.press('Tab');
    order.push(await focusedId(page));
  }
  // the event log is focusable so keyboard users can scroll it; then focus leaves the page
  expect(order).toEqual(['scenario', 'policy', 'playpause', 'reset', 'step', 'compare', 'speed', 'events', 'BODY']);
});

test('focused controls show a visible focus ring', async ({ page }) => {
  for (const id of ['scenario', 'playpause', 'speed']) {
    await page.keyboard.press('Tab');
    while ((await focusedId(page)) !== id) await page.keyboard.press('Tab');
    const outline = await page.locator('#' + id).evaluate((el) => getComputedStyle(el).outlineStyle);
    expect(outline, id).not.toBe('none');
  }
});

test('scenario picker: arrow keys pick a lesson and restart the run', async ({ page }) => {
  await page.locator('#scenario').focus();
  await page.keyboard.press('ArrowDown');
  await expect(page.locator('#scenario')).toHaveValue('big-restore');
  await expect(page.locator('#scenehint')).toContainText('40 tapes');
  await expect(page.locator('#clock')).toHaveText('00:00');
  // the digit shortcuts must not fire while the picker has focus
  await page.keyboard.press('4');
  await expect(page.locator('#scenehint')).toContainText('40 tapes');
});

test('policy picker is operable with arrow keys', async ({ page }) => {
  await page.locator('#policy').focus();
  await page.keyboard.press('ArrowUp');
  await expect(page.locator('#policy')).toHaveValue('fifo');
  await expect(page.locator('#policyblurb')).toContainText('Oldest queued request first');
});

test('Space and Enter on a focused button activate that button, not the global shortcut', async ({ page }) => {
  await page.locator('#step').focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#clock')).toHaveText('01:00');
  await expect(page.locator('#runstate')).toHaveText('paused');
  await page.keyboard.press('Enter');
  await expect(page.locator('#clock')).toHaveText('02:00');
  await page.locator('#reset').focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#clock')).toHaveText('00:00');
  await expect(page.locator('#runstate')).toHaveText('paused');
  await page.locator('#playpause').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#runstate')).toHaveText('running');
  await expect(page.locator('#playpause')).toBeFocused();
});

test('speed slider moves with arrow keys', async ({ page }) => {
  await page.locator('#speed').focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#speedout')).toHaveText('21×');
  await page.keyboard.press('Home');
  await expect(page.locator('#speedout')).toHaveText('1×');
});

test('global shortcuts work from the page and are announced', async ({ page }) => {
  await expect(page.locator('body')).toBeFocused();
  await page.keyboard.press('2');
  await expect(page.locator('#scenario')).toHaveValue('big-restore');
  await expect(page.locator('#announce')).toHaveText('scenario: Big restore');
  await page.keyboard.press('f');
  await expect(page.locator('#policy')).toHaveValue('fifo');
  await expect(page.locator('#announce')).toContainText('policy:');
  await page.keyboard.press(']');
  await expect(page.locator('#speedout')).toHaveText('25×');
  await page.keyboard.press('Space');
  await expect(page.locator('#runstate')).toHaveText('running');
  await expect(page.locator('#announce')).toHaveText('running');
});

test('shortcuts ignore ctrl/cmd/alt combinations', async ({ page }) => {
  await page.keyboard.press('Control+f');
  await page.keyboard.press('Alt+2');
  await expect(page.locator('#policy')).toHaveValue('batch');
  await expect(page.locator('#scenario')).toHaveValue('quiet-morning');
});

test('comparison panel: opens with focus inside, Esc closes and returns focus', async ({ page }) => {
  await page.locator('#compare').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#comparepanel')).toBeVisible();
  await expect(page.locator('#comparetitle')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.locator('#compareclose')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.locator('.tablewrap')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#comparepanel')).toBeHidden();
  await expect(page.locator('#compare')).toBeFocused();
  // and via the close button
  await page.keyboard.press('c');
  await page.locator('#compareclose').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#comparepanel')).toBeHidden();
  await expect(page.locator('#compare')).toBeFocused();
});

test('restarting while focus is in the comparison panel does not drop focus to body', async ({ page }) => {
  await page.keyboard.press('c');
  await expect(page.locator('#comparetitle')).toBeFocused();
  await page.keyboard.press('3');
  await expect(page.locator('#comparepanel')).toBeHidden();
  await expect(page.locator('#compare')).toBeFocused();
});
