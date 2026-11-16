// Run against the isolated TimeEntryFormTests preview fixture only.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const baseUrl = process.env.API_TEST_BASE_URL;
  assert(baseUrl && /^http:\/\/127\.0\.0\.1:\d+$/.test(baseUrl));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(baseUrl + '/login');
    await page.locator('[name="username"]').fill('admin');
    await page.locator('[name="password"]').fill('preview-testing-only');
    await Promise.all([page.waitForURL(baseUrl + '/'), page.locator('button[type="submit"]').click()]);
    await page.goto(baseUrl + '/time-entries/new');

    async function chooseContract(number) {
      await page.getByRole('button', { name: 'Vertrag', exact: true }).click();
      await page.locator('#booking-contract + .searchable-select .select-option').filter({ hasText: number }).click();
    }
    async function chooseService(name, search = name) {
      await page.getByRole('button', { name: 'Dienstleistung / Arbeitspaket', exact: true }).click();
      await page.locator('#booking-work-item + .searchable-select .select-search').fill(search);
      await page.locator('#booking-work-item + .searchable-select .select-option').filter({ hasText: name }).click();
    }
    async function contained() {
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page overflows horizontally');
      const boxes = await page.locator('.booking-section, .booking-reference, .booking-footer').evaluateAll(elements => elements.map(el => {
        const { left, top, right, bottom } = el.getBoundingClientRect();
        return { left, top, right, bottom };
      }));
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        assert(Math.min(a.right, b.right) - Math.max(a.left, b.left) <= 1 || Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) <= 1, 'Form sections overlap');
      }
    }
    const detail = page.locator('[data-work-item-detail]');
    await chooseContract('C-100');
    await chooseService('Prozessoptimierung');
    assert.match(await detail.textContent(), /Arbeitspaket Produktionsstart/);
    assert(await detail.locator('strong').isVisible());
    await page.locator('[name="hours"]').fill('1,50');
    assert.equal((await page.locator('[data-work-equivalent]').textContent()).trim(), '12,00 Stunden');
    assert.equal((await page.locator('[data-work-total]').textContent()).trim(), '1,50 AT');
    const editor = page.locator('.ql-editor');
    await editor.fill('Analyse der Prozessparameter und Abstimmung mit dem Produktionsteam.');
    await contained();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-entry-desktop-light.png'), fullPage: true });
    await page.locator('[data-theme-toggle]').click();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-entry-desktop-dark.png'), fullPage: true });

    // The description is searchable even when it is not part of the item's name.
    await chooseService('Ohne Limit', 'Abschlussbericht');
    assert.match(await detail.textContent(), /Ergebnisdokumentation/);
    await chooseService('Abnahmeworkshop');
    assert.match(await detail.textContent(), /Pauschales Arbeitspaket/);
    await chooseContract('C-SECRET');
    assert.match(await detail.textContent(), /Begleitung des zweiten Projekts/);
    assert.deepEqual(await page.locator('[name="work_item_key"] option').evaluateAll(options => options.map(o => o.value)), ['service:2']);
    await chooseContract('C-EMPTY');
    assert(await page.locator('[data-booking-submit]').isDisabled());
    assert(await page.locator('[data-booking-empty]').isVisible());
    assert(!/Begleitung/.test(await detail.textContent()));
    await chooseContract('C-100');
    await chooseService('Prozessoptimierung');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => scrollTo(0, 0));
    await contained();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-entry-mobile-dark.png'), fullPage: true });
    await page.locator('[data-theme-toggle]').click();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-entry-mobile-light.png'), fullPage: true });
    await page.setViewportSize({ width: 768, height: 1024 });
    await contained();

    await page.locator('[name="start_date"]').fill('2026-11-20');
    await page.locator('[name="start_date"]').press('Tab');
    assert.equal(await page.locator('[name="end_date"]').inputValue(), '2026-11-20');
    await page.locator('[name="end_date"]').fill('2026-11-19');
    await page.locator('[name="end_date"]').press('Tab');
    assert.equal(await page.locator('[name="end_date"]').evaluate(el => el.checkValidity()), false);
    await page.locator('[name="end_date"]').fill('2026-11-21');
    await page.locator('[name="hours"]').fill('falsch');
    assert.equal(await page.locator('[name="hours"]').evaluate(el => el.checkValidity()), false);
    await page.locator('[name="hours"]').fill('0,75');
    await Promise.all([
      page.waitForURL(/\/time-entries\/new\?saved=true$/),
      page.getByRole('button', { name: 'Aufwand speichern', exact: true }).click(),
    ]);
    assert.match(await page.locator('[role="status"]').textContent(), /gespeichert/);
    await page.goto(baseUrl + '/time-entries');
    await page.locator('.row-action-trigger').last().click();
    await page.getByRole('link', { name: 'Bearbeiten', exact: true }).last().click();
    await page.locator('.ql-editor').waitFor();
    assert.equal(await page.locator('[name="hours"]').inputValue(), '0,75');
    assert.match(await page.locator('.ql-editor').textContent(), /Analyse der Prozessparameter/);
    assert.match(await detail.textContent(), /Arbeitspaket Produktionsstart/);
    await contained();
    await page.locator('[name="hours"]').fill('1,25');
    await Promise.all([
      page.waitForURL(baseUrl + '/time-entries'),
      page.getByRole('button', { name: 'Aenderungen speichern', exact: true }).click(),
    ]);
    assert.match(await page.locator('tbody').textContent(), /1,25 AT/);
    assert.deepEqual(errors, []);
    console.log('Desktop/mobile, light/dark, selection, descriptions, conversion, validation, create/edit: OK');
    console.log('Screenshots: ' + os.tmpdir());
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
