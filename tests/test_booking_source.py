import os
import unittest
from unittest.mock import patch

from sqlalchemy import inspect

from app import database
import test_booking_api


class BookingSourceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_booking_api.BookingApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_legacy_backfill_only_marks_provable_api_bookings(self):
        fixture = self.fixture
        fixture.headers["Idempotency-Key"] = "legacy-api-reference"
        self.assertEqual(fixture.book().status_code, 201)
        fixture.headers.pop("Idempotency-Key")
        # An old account booking without a reference cannot be safely classified.
        self.assertEqual(fixture.book(description="API is not proof of origin").status_code, 201)
        fixture.headers = {"X-API-Key": fixture.create_external_key()}
        self.assertEqual(fixture.book().status_code, 201)
        before = [dict(row) for row in fixture.execute("SELECT * FROM service_time_entries ORDER BY id").fetchall()]
        fixture.execute("ALTER TABLE service_time_entries DROP COLUMN booking_source")
        database.init_db()
        database.init_db()
        rows = fixture.execute("SELECT * FROM service_time_entries ORDER BY id").fetchall()
        self.assertEqual([row["booking_source"] for row in rows], ["api", None, "api"])
        for old, new in zip(before, rows):
            for field in ("hours", "description", "user_id", "booking_name", "status", "start_date", "end_date"):
                self.assertEqual(old[field], new[field])
        fixture.login(1)
        page = fixture.client.get("/time-entries")
        self.assertEqual(page.text.count('aria-label="API-Buchung"'), 2)
        self.assertIn('data-booking-source="Nicht erfasst"', page.text)
        fixture.execute("UPDATE service_time_entries SET booking_source = 'web' WHERE id = 2")
        database.init_db()
        self.assertEqual(fixture.execute("SELECT booking_source FROM service_time_entries WHERE id = 2").fetchone()[0], "web")

    @unittest.skipUnless(os.environ.get("TEST_POSTGRES_URL"), "Requires isolated PostgreSQL test database")
    def test_failed_migration_rolls_back_source_column_and_backfill(self):
        fixture = self.fixture
        fixture.headers["Idempotency-Key"] = "legacy-reference"
        self.assertEqual(fixture.book().status_code, 201)
        fixture.execute("ALTER TABLE service_time_entries DROP COLUMN booking_source")
        with patch.object(database, "drop_not_null_if_postgres", side_effect=RuntimeError("Simulated failure")):
            with self.assertRaisesRegex(RuntimeError, "Simulated failure"):
                database.init_db()
        self.assertNotIn("booking_source", {column["name"] for column in inspect(fixture.engine).get_columns("service_time_entries")})
        self.assertEqual(fixture.execute("SELECT hours FROM service_time_entries").fetchone()[0], 6)
        database.init_db()
        self.assertEqual(fixture.execute("SELECT booking_source FROM service_time_entries").fetchone()[0], "api")


if __name__ == "__main__":
    unittest.main()
