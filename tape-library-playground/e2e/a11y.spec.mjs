// axe-core scan of every page state at desktop and 360 px width. Serious and
// critical findings fail the test; moderate/minor ones are printed, not gated.
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { STATES, VIEWPORTS } from './states.mjs';

const BLOCKING = new Set(['serious', 'critical']);

for (const vp of VIEWPORTS) {
  for (const state of STATES) {
    test(`axe: ${state.name} @ ${vp.name}`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto('index.html' + state.query);
      await expect(page.locator('#clock')).not.toHaveText('00:00', { timeout: 5000 }).catch(() => {});
      const { violations } = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'])
        .analyze();
      const describe = (v) =>
        `${v.impact} ${v.id}: ${v.help}\n` + v.nodes.slice(0, 5).map((n) => `    ${n.target.join(' ')}`).join('\n');
      const minor = violations.filter((v) => !BLOCKING.has(v.impact));
      if (minor.length) console.log(`[${state.name} @ ${vp.name}] non-blocking:\n` + minor.map(describe).join('\n'));
      const blocking = violations.filter((v) => BLOCKING.has(v.impact));
      expect(blocking.map(describe), 'serious/critical axe findings').toEqual([]);
    });
  }
}
