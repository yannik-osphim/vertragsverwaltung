import unittest

from sqlmodel import Session

from app import database, main
import test_booking_api
from test_time_entries_overview import prepare_overview_fixture


TABLE_PAGES = [
    "/", "/companies", "/contracts", "/licenses", "/services", "/flat-fees",
    "/catalog", "/users", "/characteristics", "/notifications", "/api-keys", "/api-keys/new",
    "/billing", "/invoices/1", "/invoices/2", "/time-entries",
] + [f"/companies/1?tab={tab}" for tab in ("contracts", "licenses", "invoices", "time")] + [
    f"/contracts/1?tab={tab}" for tab in ("invoices", "licenses", "services", "flat_fees", "variable_costs", "time_entries")
]


def prepare_all_tables_fixture(fixture):
    prepare_overview_fixture(fixture)
    stamp = main.now_iso()
    with Session(fixture.engine) as session:
        session.add_all([
            database.LicenseType(id=1, name="Insights", datev_account="4401", created_at=stamp, updated_at=stamp),
            database.ServiceType(id=1, name="Dienstleistung", datev_account="4501", created_at=stamp, updated_at=stamp),
            database.FlatFeeType(id=1, name="Arbeitspaket", datev_account="4601", created_at=stamp, updated_at=stamp),
            database.License(id=1, contract_id=1, license_type_id=1, name="Insights", annual_amount_cents=650000,
                             quantity=2.5, start_date="2026-01-01", created_at=stamp, updated_at=stamp),
            database.VariableCost(id=1, contract_id=1, name="Maschinenanbindung", rate_cents=25000,
                                  quantity=2, start_date="2026-01-01", created_at=stamp, updated_at=stamp),
            database.CharacteristicDefinition(id=1, target_type="company", key="industry", name="Branche", created_at=stamp),
            database.Notification(id=1, event_key="test-end", notification_type="contract_end", target_type="contract",
                                  target_id=1, title="Vertrag endet", message="Vertragsende pruefen.", due_date="2026-12-31",
                                  created_at=stamp, updated_at=stamp),
        ])
        for invoice_id, status in ((1, "draft"), (2, "finalized")):
            session.add(database.Invoice(id=invoice_id, invoice_number=f"ABR-2026-{invoice_id:04d}", company_id=1,
                contract_id=1, period_start="2026-01-01", period_end="2026-11-13", status=status,
                subtotal_cents=1200000, total_cents=1200000, gross_total_cents=1428000,
                created_at=stamp, updated_at=stamp))
        for index in range(12):
            session.add(database.InvoiceLineItem(id=index + 1, invoice_id=1, item_type="service",
                source_id=1, description=f"Arbeitspaket {index + 1:02d}", quantity_text="1,00 AT",
                subtotal_cents=100000, amount_cents=100000, datev_account="4501"))
        session.commit()
    fixture.execute("UPDATE services SET service_type_id = 1")
    fixture.execute("UPDATE flat_fees SET flat_fee_type_id = 1, amount_cents = 100000")


class CompactTablesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_booking_api.BookingApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        prepare_all_tables_fixture(self.fixture)
        self.fixture.login(1)

    def test_all_table_pages_load_shared_assets_once(self):
        for path in TABLE_PAGES:
            with self.subTest(path=path):
                response = self.fixture.client.get(path)
                self.assertEqual(response.status_code, 200, response.text[:500])
                self.assertIn("<table", response.text)
                for asset in main.TABLE_ASSET_VERSIONS:
                    self.assertEqual(response.text.count(f"/static/{asset}?v="), 1)
                self.assertLess(response.text.index("/static/compact-tables.js?v="), response.text.index("/static/table-controls.js?v="))

    def test_invoice_fields_and_billing_selection_remain_available(self):
        invoice = self.fixture.client.get("/invoices/1").text
        self.assertEqual(invoice.count('name="line_item_ids"'), 12)
        self.assertEqual(invoice.count('name="line_discount_values"'), 12)
        self.assertIn('data-invoice-net', invoice)
        self.assertIn('<tfoot>', invoice)
        billing = self.fixture.client.get("/billing").text
        self.assertIn('name="selected_line_keys"', billing)
        self.assertIn('form="billing-form-1"', billing)


if __name__ == "__main__":
    unittest.main()
