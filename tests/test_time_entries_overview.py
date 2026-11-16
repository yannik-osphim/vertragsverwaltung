import unittest
from datetime import date, timedelta

from sqlmodel import Session

from app import database, main
import test_booking_api
from test_time_entry_form import prepare_time_form_fixture


def prepare_overview_fixture(fixture):
    prepare_time_form_fixture(fixture)
    fixture.execute("UPDATE companies SET name = 'Manke' WHERE id = 1")
    fixture.execute("UPDATE users SET full_name = 'System Administrator' WHERE id = 1")
    fixture.execute("UPDATE users SET full_name = 'Anna Schneider' WHERE id = 2")
    fixture.execute("UPDATE services SET name = 'Projektbegleitung' WHERE id = 3")
    with Session(fixture.engine) as session:
        stamp = main.now_iso()
        for index in range(17):
            start = (date(2026, 11, 13) - timedelta(days=index)).isoformat()
            package = index in (2, 5)
            session.add(database.ServiceTimeEntry(
                id=index + 1, contract_id=1, service_id=None if package else (1 if index % 2 == 0 else 3),
                flat_fee_id=2 if package else None, user_id=2 if index % 2 else 1,
                work_date=start, start_date=start, end_date="2026-11-13" if index == 2 else start,
                hours=[12, 6, 84, 20][index % 4],
                booking_source="api" if index in (0, 4) else (None if index == 3 else "web"),
                description="<p>Analyse der Prozessparameter und Abstimmung mit dem Produktionsteam.</p>" if index != 3 else "<p>Suchwort Dokumentation</p>",
                status="billed" if index == 16 else ("approved" if index % 2 else "submitted"),
                invoice_id=1 if index == 16 else None, created_at=stamp, updated_at=stamp,
            ))
        session.commit()


class TimeEntriesOverviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_booking_api.BookingApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        prepare_overview_fixture(self.fixture)
        self.fixture.login(1)

    def test_overview_preserves_details_and_locked_actions(self):
        response = self.fixture.client.get("/time-entries")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-table-ui="compact"', response.text)
        self.assertEqual(response.text.count('data-status="'), 17)
        self.assertIn('data-sort-value="2026-11-13"', response.text)
        self.assertIn('data-sort-type="number"', response.text)
        self.assertIn("Suchwort Dokumentation", response.text)
        self.assertIn('href="/contracts/1"', response.text)
        self.assertIn('action="/time-entries/1/approve"', response.text)
        self.assertNotIn('action="/time-entries/17/delete"', response.text)
        self.assertNotIn('href="/time-entries/17/edit"', response.text)
        self.assertIn('class="entry-locked"', response.text)
        self.assertEqual(response.text.count('aria-label="API-Buchung"'), 2)
        self.assertEqual(response.text.count('data-booking-source="API"'), 2)
        self.assertEqual(response.text.count('data-booking-source="Web"'), 14)
        self.assertEqual(response.text.count('data-booking-source="Nicht erfasst"'), 1)

    def test_consultant_still_cannot_see_overview(self):
        self.fixture.login(2)
        self.assertEqual(self.fixture.client.get("/time-entries").status_code, 403)

    def test_empty_overview(self):
        self.fixture.execute("DELETE FROM service_time_entries")
        response = self.fixture.client.get("/time-entries")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Noch keine Stundenbuchungen vorhanden.", response.text)

    def test_compact_assets_are_versioned_and_loaded_before_table_controls(self):
        response = self.fixture.client.get("/time-entries")
        self.assertIn('data-column-filters="false"', response.text)
        self.assertLess(response.text.index('/static/compact-tables.js?v='), response.text.index('/static/table-controls.js?v='))
        for name, version in main.TABLE_ASSET_VERSIONS.items():
            url = f"/static/{name}?v={version}"
            self.assertIn(url, response.text)
            self.assertEqual(self.fixture.client.get(url).status_code, 200)


if __name__ == "__main__":
    unittest.main()
