// Run only against the isolated preview fixture, never a production instance.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const baseUrl = process.env.API_TEST_BASE_URL;
  assert(baseUrl && /^http:\/\/127\.0\.0\.1:\d+$/.test(baseUrl), 'Provide a local API_TEST_BASE_URL');
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const keyName = 'Zeiterfassung Integration ' + Date.now();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.goto(baseUrl + '/login');
    await page.locator('[name="username"]').fill('admin');
    await page.locator('[name="password"]').fill('preview-testing-only');
    await Promise.all([
      page.waitForURL(baseUrl + '/'),
      page.locator('button[type="submit"]').click(),
    ]);
    await page.goto(baseUrl + '/api-keys/new');
    await page.locator('[name="name"]').fill(keyName);
    await page.locator('[name="user_id"]').selectOption('2');
    await page.locator('[name="contract_ids"][value="1"]').check();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-api-key-desktop.png'), fullPage: true });
    const assertContained = async () => {
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Page overflows');
    };
    await assertContained();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-api-key-mobile.png'), fullPage: true });
    await assertContained();
    await page.locator('[data-theme-toggle]').click();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-api-key-mobile-dark.png'), fullPage: true });
    await Promise.all([
      page.waitForURL(baseUrl + '/api-keys'),
      page.getByRole('button', { name: 'Schluessel erstellen', exact: true }).click(),
    ]);
    assert(await page.locator('.secret-value').isVisible());
    await assertContained();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-api-keys-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-api-keys-desktop.png'), fullPage: true });
    await assertContained();
    // A fresh GET must never display the full key again.
    await page.goto(baseUrl + '/api-keys');
    assert.equal(await page.locator('.secret-value').count(), 0);
    const row = page.locator('tbody tr').filter({ hasText: keyName });
    await row.locator('summary').click();
    page.once('dialog', dialog => dialog.accept());
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/revoke') && response.status() === 303),
      page.getByRole('button', { name: 'Widerrufen', exact: true }).click(),
    ]);
    await page.waitForLoadState('networkidle');
    assert.match(await row.textContent(), /Widerrufen/);
    const externalName = 'Alex Extern ' + Date.now();
    await page.goto(baseUrl + '/api-keys/new');
    await page.locator('[name="owner_type"][value="external"]').check();
    assert(await page.locator('[name="user_id"]').isDisabled());
    assert(await page.locator('[name="booking_name"]').isVisible());
    assert(await page.locator('[name="booking_name"]').evaluate(input => input.required));
    await page.locator('[name="name"]').fill('Externe Zeiterfassung');
    await page.locator('[name="booking_name"]').fill(externalName);
    await page.locator('[name="contract_ids"][value="1"]').check();
    await page.locator('[name="owner_type"][value="account"]').check();
    assert(await page.locator('[name="booking_name"]').isDisabled());
    assert(await page.locator('[name="user_id"]').isVisible());
    await page.locator('[name="owner_type"][value="external"]').check();
    assert.equal(await page.locator('[name="booking_name"]').inputValue(), externalName);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      await assertContained();
      await page.screenshot({ path: path.join(os.tmpdir(), `osphim-api-external-${width}.png`), fullPage: true });
    }
    await page.getByRole('button', { name: 'Schluessel erstellen', exact: true }).click();
    await page.waitForURL(baseUrl + '/api-keys');
    const secret = await page.locator('.secret-value').textContent();
    const booking = await page.request.post(baseUrl + '/create-booking', {
      headers: { 'X-API-Key': secret },
      data: { contract_number: 'C-100', service_id: 1, start_date: '2026-11-15', end_date: '2026-11-15', amount: '0,75', unit: 'day' },
    });
    assert.equal(booking.status(), 201);
    await page.goto(baseUrl + '/time-entries');
    assert.equal(await page.locator('tbody tr').filter({ hasText: externalName }).count(), 1);
    await assertContained();
    await page.goto(baseUrl + '/api-keys');
    const externalRow = page.locator('tbody tr').filter({ hasText: externalName });
    await externalRow.locator('summary').click();
    await page.getByRole('link', { name: 'Bearbeiten', exact: true }).click();
    assert.equal(await page.locator('[name="owner_type"]').count(), 0);
    assert.equal(await page.locator('[name="booking_name"]').count(), 0);
    assert(await page.locator('[name="contract_ids"][value="1"]').isChecked());
    await page.locator('.table-filter').fill('C-SECRET');
    await page.locator('[name="contract_ids"][value="2"]').check();
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      await assertContained();
      await page.screenshot({ path: path.join(os.tmpdir(), `osphim-api-edit-${width}.png`), fullPage: true });
    }
    await page.getByRole('button', { name: 'Aenderungen speichern', exact: true }).click();
    await page.waitForURL(baseUrl + '/api-keys?saved=true');
    const getServices = number => page.request.post(baseUrl + '/get-services', {
      headers: { 'X-API-Key': secret }, data: { contract_number: number },
    });
    assert.equal((await getServices('C-100')).status(), 200, 'Hidden selections must be retained');
    assert.equal((await getServices('C-SECRET')).status(), 200, 'Existing key must gain access');
    await externalRow.locator('summary').click();
    await page.getByRole('link', { name: 'Bearbeiten', exact: true }).click();
    await page.locator('[name="contract_ids"][value="1"]').uncheck();
    await page.locator('[name="contract_ids"][value="2"]').uncheck();
    await page.getByRole('button', { name: 'Aenderungen speichern', exact: true }).click();
    await page.waitForURL(baseUrl + '/api-keys?saved=true');
    assert.equal((await getServices('C-100')).status(), 404);
    assert.equal((await getServices('C-SECRET')).status(), 404);
    assert.match(await externalRow.textContent(), /Keine Vertraege/);
    assert.equal(await page.locator('.secret-value').count(), 0);
    console.log('Desktop/mobile layout, account/external keys, booking, editing filtered grants, immediate access changes and revocation: OK');
    console.log('Screenshots: ' + os.tmpdir());
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
