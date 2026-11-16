// Run only against the isolated TimeEntriesOverviewTests fixture.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const base = process.env.API_TEST_BASE_URL;
  assert(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('[name="username"]').fill('admin');
    await page.locator('[name="password"]').fill('preview-testing-only');
    await Promise.all([page.waitForURL(base + '/'), page.locator('button[type="submit"]').click()]);
    await page.goto(base + '/time-entries');
    const rows = page.locator('tbody tr[data-status]:visible');
    const search = page.getByRole('searchbox', { name: 'Buchungen durchsuchen', exact: true });
    const menu = name => page.getByRole('button', { name, exact: true }).click();
    async function reset() {
      await menu('Tabellenaktionen');
      await page.getByRole('button', { name: 'Ansicht zuruecksetzen' }).click();
    }
    async function contained() {
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Page overflows');
      const panels = await page.locator('.compact-table-panel:visible, .row-action-menu[open] .row-action-list').evaluateAll(nodes => nodes.map(node => {
        const r = node.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: innerWidth, height: innerHeight };
      }));
      panels.forEach(r => assert(r.left >= 0 && r.right <= r.width + 1 && r.top >= 0 && r.bottom <= r.height + 1, 'Popover is clipped: ' + JSON.stringify(r)));
    }
    assert.equal(await page.locator('.column-filter-row').count(), 0);
    assert.equal(await rows.count(), 10);
    assert.equal(await page.locator('.compact-page-info').textContent(), '1–10 / 17');
    assert.equal(await page.getByRole('button', { name: 'Vorherige Seite' }).isDisabled(), true);
    await page.getByRole('button', { name: 'Naechste Seite' }).click();
    assert.equal(await rows.count(), 7);
    assert.equal(await page.getByRole('button', { name: 'Naechste Seite' }).isDisabled(), true);
    await search.fill('Dokumentation Manke');
    assert.equal(await rows.count(), 1);
    assert.match(await rows.textContent(), /Suchwort Dokumentation/);
    await search.fill('nicht-vorhanden');
    assert.equal(await rows.count(), 0);
    assert(await page.getByText('Keine Buchungen gefunden.', { exact: true }).isVisible());
    await search.fill('');

    await search.fill('API');
    assert.equal(await rows.count(), 2);
    assert.equal(await rows.locator('.booking-source-badge').count(), 2);
    await search.fill('');
    await menu('Filter');
    const origin = page.getByRole('combobox', { name: 'Herkunft', exact: true });
    for (const [source, visibleCount] of [['API', 2], ['Web', 10], ['Nicht erfasst', 1]]) {
      await origin.selectOption(source);
      assert.equal(await rows.count(), visibleCount);
      assert((await rows.evaluateAll(nodes => nodes.map(row => row.dataset.bookingSource))).every(value => value === source));
    }
    await page.keyboard.press('Escape');
    await reset();

    await menu('Filter');
    await page.getByRole('combobox', { name: 'Leistungsart', exact: true }).selectOption('Pauschale');
    assert.equal(await rows.count(), 2);
    await page.getByLabel('Von', { exact: true }).fill('2026-11-12');
    await page.getByLabel('Von', { exact: true }).press('Tab');
    assert.equal(await rows.count(), 1, 'Date filter must include overlapping periods');
    assert.equal(await page.locator('.compact-filter-badge').textContent(), '2');
    await contained();
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('button', { name: 'Filter', exact: true }).getAttribute('aria-expanded'), 'false');
    await reset();

    await menu('Sortierung');
    await page.getByRole('combobox', { name: 'Sortieren nach', exact: true }).selectOption('4');
    await page.getByRole('combobox', { name: 'Reihenfolge', exact: true }).selectOption('desc');
    assert.equal(await rows.first().locator('td').nth(4).textContent(), '10,50 AT');
    await page.keyboard.press('Escape');
    await page.getByRole('columnheader', { name: 'Aufwand', exact: true }).click();
    assert.equal(await rows.first().locator('td').nth(4).textContent(), '0,75 AT');
    await reset();

    await menu('Tabellenaktionen');
    await page.getByRole('checkbox', { name: 'Aggregation', exact: true }).check();
    const values = await rows.locator('td.entry-effort').evaluateAll(nodes => nodes.map(n => Number(n.dataset.sortValue) / 8));
    assert(values.length === 10);
    const allValues = await page.locator('tbody tr[data-status] td.entry-effort').evaluateAll(nodes => nodes.map(n => Number(n.dataset.sortValue) / 8));
    assert.match(await page.locator('.aggregate-value').textContent(), new RegExp(allValues.reduce((a, b) => a + b, 0).toLocaleString('de-DE')));
    for (const [method, expected] of [['min', '0,75 AT'], ['max', '10,50 AT'], ['count', '17']]) {
      await page.getByRole('combobox', { name: 'Aggregationsmethode' }).selectOption(method);
      assert.equal(await page.locator('.aggregate-value').textContent(), expected);
    }
    await page.getByRole('combobox', { name: 'Zeilen pro Seite' }).selectOption('25');
    assert.equal(await rows.count(), 17);
    await page.keyboard.press('Escape');
    await search.fill('Dokumentation');
    assert.equal(await page.locator('.aggregate-value').textContent(), '1');
    await reset();

    // Show three results for readable layout snapshots, without altering any records.
    await menu('Filter');
    await page.getByLabel('Von', { exact: true }).fill('2026-11-11');
    await page.getByLabel('Von', { exact: true }).press('Tab');
    await page.keyboard.press('Escape');
    await contained();
    assert(await page.locator('.compact-table-scroll').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-overview-desktop-light.png'), fullPage: true });
    await page.locator('[data-theme-toggle]').click();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-overview-desktop-dark.png'), fullPage: true });
    await rows.first().locator('.row-action-trigger').click();
    await page.locator('.row-action-menu[open] .row-action-list').waitFor();
    await contained();
    assert(await page.locator('.compact-table-scroll').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-overview-actions.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await menu('Filter');
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-overview-filters.png'), fullPage: true });
    await page.keyboard.press('Escape');
    for (const width of [768, 390, 360]) {
      await page.setViewportSize({ width, height: 900 });
      await contained();
      await menu('Tabellenaktionen');
      await contained();
      await page.keyboard.press('Escape');
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-overview-mobile-dark.png'), fullPage: true });
    await page.locator('[data-theme-toggle]').click();
    await menu('Filter');
    await contained();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-time-overview-mobile-filter-light.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.locator('.compact-table-scroll').evaluate(el => { el.scrollLeft = el.scrollWidth; });
    await rows.first().locator('.row-action-trigger').click();
    // Native details toggles position the menu in a subsequent event task.
    await page.waitForFunction(() => {
      const menu = document.querySelector('.row-action-menu[open] .row-action-list');
      if (!menu) return false;
      const bounds = menu.getBoundingClientRect();
      return bounds.left >= 0 && bounds.right <= innerWidth + 1;
    });
    await contained();
    await page.keyboard.press('Escape');
    assert.deepEqual(errors, []);
    console.log('Overview: search, filters, sorting, aggregation, pagination, popovers and responsive layouts OK');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
