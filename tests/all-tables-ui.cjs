// Use only the isolated CompactTablesTests preview, never the live business database.
const assert = require('node:assert/strict');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const routes = ['/', '/companies', '/contracts', '/licenses', '/services', '/flat-fees', '/catalog', '/users',
  '/characteristics', '/notifications', '/api-keys', '/api-keys/new', '/billing', '/invoices/1', '/invoices/2', '/time-entries',
  ...['contracts', 'licenses', 'invoices', 'time'].map(tab => '/companies/1?tab=' + tab),
  ...['invoices', 'licenses', 'services', 'flat_fees', 'variable_costs', 'time_entries'].map(tab => '/contracts/1?tab=' + tab)];

(async () => {
  const base = process.env.API_TEST_BASE_URL;
  assert(base && /^http:\/\/127\.0\.0\.1:\d+$/.test(base));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/login');
    await page.locator('[name="username"]').fill('admin');
    await page.locator('[name="password"]').fill('preview-testing-only');
    await Promise.all([page.waitForURL(base + '/'), page.locator('button[type="submit"]').click()]);
    async function openMenu(trigger) {
      await trigger.evaluate(el => el.scrollIntoView({ block: 'center', inline: 'nearest' }));
      await page.waitForTimeout(100);
      await trigger.click();
      await page.locator('#' + await trigger.getAttribute('aria-controls')).waitFor({ state: 'visible' });
    }
    async function contained() {
      const fits = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);
      if (!fits) {
        await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-tables-overflow.png'), fullPage: true });
        console.log(await page.evaluate(() => Array.from(document.querySelectorAll('body *')).filter(el => {
          const box = el.getBoundingClientRect();
          return box.width && box.right > innerWidth + 1 && !el.closest('.compact-table-scroll');
        }).map(el => [el.tagName, el.className, el.getBoundingClientRect().width])));
      }
      assert(fits, 'Page overflow: ' + page.url());
      for (const panel of await page.locator('.compact-table-panel:visible, .row-action-menu[open] .row-action-list').all()) {
        const box = await panel.boundingBox();
        if (!box) continue;
        const view = page.viewportSize();
        assert(box.x >= 0 && box.y >= 0 && box.x + box.width <= view.width + 1 && box.y + box.height <= view.height + 1, 'Panel clipped');
      }
    }
    for (const route of routes) {
      const response = await page.goto(base + route);
      assert.equal(response.status(), 200, route);
      const tables = await page.locator('.table-wrap table').count();
      assert(tables > 0, route);
      assert.equal(await page.locator('.compact-table-tools').count(), tables, route);
      assert.equal(await page.locator('.compact-data-table').count(), tables, route);
      assert.equal(await page.locator('.column-filter-row').count(), 0, route);
      assert.equal(await page.locator('table > tfoot + tfoot').count(), 0, 'Duplicate footer');
      await contained();
      if (['/licenses', '/companies', '/catalog', '/time-entries'].includes(route)) {
        await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-all-' + route.slice(1) + '.png'), fullPage: true });
      }
      const wrap = page.locator('.compact-table-wrap:visible').first();
      await openMenu(wrap.getByRole('button', { name: 'Filter', exact: true }));
      await contained();
      assert(!/undefined/.test(await page.locator('.compact-table-panel:visible').textContent()), route);
      await page.keyboard.press('Escape');
      const search = wrap.locator('input[type="search"]');
      await search.fill('no-such-value-xyz');
      assert.equal(await wrap.locator('tbody tr:visible').count(), 1, route);
      assert(await wrap.locator('tbody .empty').isVisible(), route);
      await search.fill('');
      await page.setViewportSize({ width: 390, height: 844 });
      await contained();
      const actionTrigger = wrap.getByRole('button', { name: 'Tabellenaktionen', exact: true });
      await openMenu(actionTrigger);
      await contained();
      await page.keyboard.press('Escape');
      await page.setViewportSize({ width: 1440, height: 1000 });
      console.log('OK ' + route);
    }

    await page.goto(base + '/catalog');
    await openMenu(page.getByRole('button', { name: 'Filter', exact: true }));
    await page.getByRole('combobox', { name: 'Art', exact: true }).selectOption({ label: 'Lizenzart' });
    assert.equal(await page.locator('tbody tr:visible:not(:has(.empty))').count(), 1);
    await page.keyboard.press('Escape');
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-all-tables-catalog-light.png'), fullPage: true });

    await page.goto(base + '/billing');
    const billing = page.locator('.billing-group[open] .compact-table-wrap').first();
    const chosen = billing.locator('[name="selected_line_keys"]').first();
    await chosen.uncheck();
    const key = await chosen.inputValue();
    const formId = await chosen.getAttribute('form');
    await billing.locator('input[type="search"]').fill('no-such-value');
    await billing.locator('input[type="search"]').press('Enter');
    assert(page.url().endsWith('/billing'), 'Search must not submit a form');
    await billing.locator('input[type="search"]').fill('');
    assert.equal(await chosen.isChecked(), false);
    const submittedKeys = await page.locator('#' + formId).evaluate(form => new FormData(form).getAll('selected_line_keys'));
    assert(!submittedKeys.includes(key));

    await page.goto(base + '/time-entries');
    const timeTable = page.locator('.compact-table-wrap');
    await openMenu(timeTable.getByRole('button', { name: 'Sortierung', exact: true }));
    await timeTable.getByRole('combobox', { name: 'Sortieren nach' }).selectOption('4');
    assert.equal(await page.locator('tbody tr:visible .entry-effort').first().textContent(), '10,50 AT');
    await timeTable.getByRole('combobox', { name: 'Reihenfolge' }).selectOption('asc');
    assert.equal(await page.locator('tbody tr:visible .entry-effort').first().textContent(), '0,75 AT');
    await page.keyboard.press('Escape');
    await openMenu(timeTable.getByRole('button', { name: 'Tabellenaktionen', exact: true }));
    await timeTable.getByRole('checkbox', { name: 'Aggregation', exact: true }).check();
    for (const [method, expected] of [['min', '0,75 AT'], ['max', '10,50 AT'], ['count', '17']]) {
      await timeTable.getByRole('combobox', { name: 'Aggregationsmethode' }).selectOption(method);
      assert.equal(await timeTable.locator('tfoot .aggregate-value').textContent(), expected);
    }
    await page.keyboard.press('Escape');
    await openMenu(timeTable.getByRole('button', { name: 'Filter', exact: true }));
    await timeTable.getByRole('combobox', { name: 'Leistungsart', exact: true }).selectOption('Pauschale');
    await timeTable.getByLabel('Von', { exact: true }).fill('2026-11-12');
    await timeTable.getByLabel('Von', { exact: true }).press('Tab');
    assert.equal(await page.locator('tbody tr[data-status]:visible').count(), 1);
    await page.keyboard.press('Escape');

    await page.goto(base + '/api-keys/new');
    const scope = page.locator('.compact-table-wrap');
    await scope.locator('[name="contract_ids"]').first().check();
    const contractId = await scope.locator('[name="contract_ids"]').first().inputValue();
    await scope.locator('input[type="search"]').fill('not-present');
    const selectedContracts = await page.locator('form').evaluate(form => new FormData(form).getAll('contract_ids'));
    assert(selectedContracts.includes(contractId));

    await page.goto(base + '/invoices/1');
    const invoice = page.locator('[data-invoice-discount-form] .compact-table-wrap');
    const form = page.locator('[data-invoice-discount-form]');
    await invoice.locator('[data-line-discount-unit]').first().selectOption('percent');
    await invoice.locator('[data-line-discount-value]').first().fill('10');
    assert.match(await page.locator('[data-invoice-net]').textContent(), /11\.900/);
    await invoice.getByRole('button', { name: 'Naechste Seite' }).click();
    assert.equal(await invoice.locator('[data-invoice-line]:visible').count(), 2);
    await openMenu(invoice.getByRole('button', { name: 'Filter', exact: true }));
    await invoice.getByRole('combobox', { name: 'Suchbereich', exact: true }).selectOption('2');
    await page.keyboard.press('Escape');
    await invoice.locator('input[type="search"]').fill('Arbeitspaket 01');
    await invoice.locator('input[type="search"]').press('Enter');
    assert(page.url().endsWith('/invoices/1'));
    assert.equal(await invoice.locator('[data-invoice-line]:visible').count(), 1);
    assert.equal(await form.evaluate(form => new FormData(form).getAll('line_item_ids').length), 12);
    assert.equal(await invoice.locator('[data-line-discount-value]').first().inputValue(), '10');
    assert.match(await page.locator('[data-invoice-net]').textContent(), /11\.900/);
    assert.equal(await invoice.locator('tfoot').count(), 1);
    await openMenu(invoice.getByRole('button', { name: 'Tabellenaktionen', exact: true }));
    await invoice.getByRole('checkbox', { name: 'Aggregation', exact: true }).check();
    await page.keyboard.press('Escape');
    assert.match(await page.locator('[data-invoice-net]').textContent(), /11\.900/);
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-all-tables-invoice.png'), fullPage: true });

    await page.goto(base + '/licenses');
    await page.locator('[data-theme-toggle]').click();
    await page.locator('.row-action-trigger').first().scrollIntoViewIfNeeded();
    await page.waitForTimeout(100);
    await page.locator('.row-action-trigger').first().click();
    await page.waitForTimeout(100);
    await contained();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-all-tables-licenses-dark.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.setViewportSize({ width: 390, height: 844 });
    await openMenu(page.getByRole('button', { name: 'Filter', exact: true }));
    await contained();
    await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-all-tables-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('All tables: controls, filters, forms, selection, invoice totals, desktop/mobile and themes OK');
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(os.tmpdir(), 'osphim-all-tables-failure.png'), fullPage: true });
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
