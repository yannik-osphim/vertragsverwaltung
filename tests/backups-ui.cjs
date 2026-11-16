const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const base = process.env.API_TEST_BASE_URL;
  assert(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('[name="username"]').fill('admin');
    await page.locator('[name="password"]').fill('preview-testing-only');
    await Promise.all([page.waitForURL(base + '/'), page.locator('button[type="submit"]').click()]);
    await page.goto(base + '/backups');
    assert(await page.locator('.administration-navigation a[href="/backups"]').getAttribute('aria-current') === 'page');
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; localStorage.setItem('osphim-theme', value); }, theme);
      for (const width of [1440, 1024, 768, 390]) {
        await page.setViewportSize({ width, height: 1100 });
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page overflow');
        const fields = await page.locator('.backup-settings input:not([type="hidden"])').evaluateAll(nodes => nodes.map(node => {
          const r = node.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth + 1;
        }));
        assert(fields.every(Boolean));
        if (width === 1440 || width === 390) await page.screenshot({ path: path.join(os.tmpdir(), `osphim-backups-${width}-${theme}.png`), fullPage: true });
      }
    }
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.locator('.row-action-trigger').click();
    const [download] = await Promise.all([
      page.waitForEvent('download'), page.getByRole('link', { name: 'Datenbank', exact: true }).click(),
    ]);
    assert.equal(download.suggestedFilename(), 'database.dump');
    await page.keyboard.press('Escape');
    await page.locator('[name="retention_days"]').fill('30');
    await Promise.all([page.waitForURL(base + '/backups?saved=1'), page.getByRole('button', { name: 'Einstellungen speichern' }).click()]);
    assert.equal(await page.locator('[name="retention_days"]').inputValue(), '30');
    await page.locator('[name="retention_days"]').fill('14');
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByRole('button', { name: 'Einstellungen speichern' }).click();
    assert.equal(await page.locator('.backup-settings').getAttribute('data-current-retention'), '30');
    page.once('dialog', dialog => dialog.accept());
    await Promise.all([page.waitForResponse(response => response.url().endsWith('/backups/settings') && response.request().method() === 'POST'), page.getByRole('button', { name: 'Einstellungen speichern' }).click()]);
    await page.waitForLoadState('domcontentloaded');
    await page.goto(base + '/backups');
    assert.equal(await page.locator('[name="retention_days"]').inputValue(), '14');
    await Promise.all([page.waitForResponse(response => response.url().endsWith('/backups/run')), page.getByRole('button', { name: 'Jetzt sichern' }).click()]);
    await page.waitForLoadState('domcontentloaded');
    await page.goto(base + '/backups');
    assert(await page.getByRole('button', { name: 'Jetzt sichern' }).isDisabled());
    assert.match(await page.locator('.backup-request').textContent(), /Angefordert/);
    assert.deepEqual(errors, []);
    console.log('Backup UI: responsive light/dark layouts, sidebar, download, settings, confirmation and manual request OK');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
