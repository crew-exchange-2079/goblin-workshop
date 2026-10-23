// Browser tests for the UI: axe-core scan, keyboard-only operation, 360 px layout.
// `npx playwright test` serves this directory with python3's http.server (ES modules
// will not load over file://). PW_CHROMIUM_PATH points at a preinstalled Chromium
// when the bundled one is not downloaded.
import { defineConfig } from '@playwright/test';

const PORT = 8943;

export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}/`,
    browserName: 'chromium',
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
  webServer: {
    command: `python3 -m http.server ${PORT} --bind 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/index.html`,
    reuseExistingServer: !process.env.CI,
  },
});
