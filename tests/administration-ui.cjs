// Run against preview_booking_api.py --all-tables, never the business database.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const sections = [
  ['Einstellungen', '/settings'], ['Katalog', '/catalog'], ['Merkmale', '/characteristics'],
  ['Benutzer', '/users'], ['API-Schluessel', '/api-keys'], ['Benachrichtigungen', '/notifications'],
];
const edits = [
  ['/users/2/edit', '/users'], ['/catalog/types/license/1/edit', '/catalog'],
  ['/characteristics/1/edit', '/characteristics'], ['/api-keys/new', '/api-keys'], ['/api-keys/1/edit', '/api-keys'],
];

(async () => {
  const base = process.env.API_TEST_BASE_URL;
  assert(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('[name="username"]').fill('admin');
    await page.locator('[name="password"]').fill('preview-testing-only');
    await page.getByRole('button', { name: 'Anmelden', exact: true }).click();
    await page.waitForURL(base + '/');
    const mainNav = page.getByRole('navigation', { name: 'Hauptnavigation' });
    assert.equal(await mainNav.locator('details').count(), 0);
    await mainNav.getByRole('link', { name: 'Verwaltung', exact: true }).click();
    await page.waitForURL(base + '/settings');
    const sidebar = page.getByRole('navigation', { name: 'Verwaltung', exact: true });

    async function verifyLayout(expected, width) {
      assert.equal(await sidebar.getByRole('link').count(), 6);
      assert.equal(await sidebar.locator('[aria-current="page"]').getAttribute('href'), expected);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Overflow at ${page.url()} / ${width}`);
      const side = await page.locator('.administration-sidebar').boundingBox();
      const content = await page.locator('.administration-content').boundingBox();
      assert(side && content);
      if (width > 860) assert(side.x + side.width <= content.x, 'Sidebar overlaps content');
      else assert(side.y + side.height <= content.y, 'Mobile navigation overlaps content');
      for (const link of await sidebar.getByRole('link').all()) {
        assert(await link.isVisible());
        assert(await link.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'Navigation text clipped');
      }
    }

    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => localStorage.setItem('osphim-theme', value), theme);
      for (const width of [1440, 1024, 768, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const [label, href] of sections) {
          await sidebar.getByRole('link', { name: label, exact: true }).click();
          await page.waitForURL(base + href);
          await verifyLayout(href, width);
          if (['/settings', '/catalog'].includes(href) && [1440, 390].includes(width)) {
            await page.screenshot({ path: path.join(os.tmpdir(), `osphim-admin-${href.slice(1)}-${theme}-${width}.png`), fullPage: true });
          }
        }
        for (const [href, section] of edits) {
          assert.equal((await page.goto(base + href)).status(), 200);
          await verifyLayout(section, width);
        }
      }
    }
    await mainNav.getByRole('link', { name: 'Dashboard', exact: true }).click();
    await page.waitForURL(base + '/');
    assert.equal(await page.locator('.administration-sidebar').count(), 0);
    assert.deepEqual(errors, []);
    console.log('Administration navigation: all sections and edit pages, direct settings link, four widths, light/dark: OK');
    console.log('Screenshots: ' + os.tmpdir());
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
