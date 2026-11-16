import unittest

from sqlmodel import Session

from app import database, main
import test_booking_api


def prepare_time_form_fixture(fixture):
    fixture.execute(
        "UPDATE services SET name = ?, notes = ? WHERE id = 1",
        ("Prozessoptimierung und Begleitung des Produktionsstarts",
         "<p><strong>Arbeitspaket Produktionsstart</strong></p><p>Analyse der Prozessparameter vor Ort. "
         "Abstimmung mit dem Produktionsteam und Dokumentation der Ergebnisse.</p>"
         "<ul><li>Workshop mit den Mitarbeitenden</li><li>Nachbereitung der Massnahmen</li></ul>"),
    )
    fixture.execute("UPDATE services SET notes = 'Abschlussbericht und Ergebnisdokumentation' WHERE id = 3")
    fixture.execute("UPDATE services SET notes = 'Begleitung des zweiten Projekts' WHERE id = 2")
    with Session(fixture.engine) as session:
        stamp = main.now_iso()
        session.add(database.Contract(
            id=3, company_id=1, contract_number="C-EMPTY", title="Ohne Leistungen",
            start_date="2026-01-01", created_at=stamp, updated_at=stamp,
        ))
        session.add(database.FlatFee(
            id=2, contract_id=1, name="Abnahmeworkshop", notes="Pauschales Arbeitspaket zur Abnahme",
            fee_kind="work_package", start_date="2026-01-01", created_at=stamp, updated_at=stamp,
        ))
        session.commit()


class TimeEntryFormTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_booking_api.BookingApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        prepare_time_form_fixture(self.fixture)
        self.client = self.fixture.client
        self.fixture.login(2)
        self.form = {
            "contract_id": 1, "work_item_key": "service:1", "start_date": "2026-11-13",
            "end_date": "2026-11-13", "hours": "1,50", "description": "Meine eigene Beschreibung",
        }

    def test_contract_instance_description_without_prices_or_travel_costs(self):
        response = self.client.get("/time-entries/new")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("<strong>Arbeitspaket Produktionsstart</strong>", response.text)
        self.assertIn("Prozessoptimierung und Begleitung", response.text)
        self.assertIn("Abschlussbericht und Ergebnisdokumentation", response.text)
        self.assertIn("Pauschales Arbeitspaket zur Abnahme", response.text)
        self.assertNotIn("Reisekosten", response.text)
        self.assertNotIn("12500", response.text)
        self.assertNotIn("hourly_rate_cents", response.text)
        self.assertIn('data-work-unit="day"', response.text)
        self.assertIn('name="hours"', response.text)

    def test_service_description_is_sanitized(self):
        self.fixture.execute("UPDATE services SET notes = ? WHERE id = 1", (
            '<p onclick="attack()">Sichtbare Beschreibung</p><script>attack()</script>',
        ))
        response = self.client.get("/time-entries/new")
        self.assertIn("<p>Sichtbare Beschreibung</p>", response.text)
        self.assertNotIn('<script>attack()', response.text)
        self.assertNotIn('onclick="attack()', response.text)

    def test_decimal_days_save_own_description_not_service_description(self):
        response = self.client.post("/time-entries", data=self.form)
        self.assertEqual(response.status_code, 303, response.text)
        row = self.fixture.execute("SELECT * FROM service_time_entries").fetchone()
        self.assertEqual(row["hours"], 12)
        self.assertEqual(row["description"], "Meine eigene Beschreibung")
        self.assertEqual(row["user_id"], 2)
        self.assertEqual(row["booking_source"], "web")

    def test_web_form_cannot_spoof_api_origin(self):
        response = self.client.post("/time-entries", data={**self.form, "booking_source": "api"})
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(self.fixture.execute("SELECT booking_source FROM service_time_entries").fetchone()[0], "web")
        self.fixture.login(1)
        self.assertNotIn('aria-label="API-Buchung"', self.client.get("/time-entries").text)

    def test_validation_error_keeps_inputs_in_form(self):
        response = self.client.post("/time-entries", data={**self.form, "end_date": "2026-11-12"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('value="1,50"', response.text)
        self.assertIn("Meine eigene Beschreibung", response.text)
        self.assertIn('role="alert"', response.text)
        self.assertIn("Enddatum darf nicht", response.text)
        self.assertEqual(self.fixture.execute("SELECT COUNT(*) FROM service_time_entries").fetchone()[0], 0)

    def test_invalid_effort_and_wrong_contract_return_inline_error(self):
        for changes in ({"hours": "NaN"}, {"hours": "abc"}, {"hours": "-1"}, {"work_item_key": "service:2"}, {"work_item_key": "flat_fee:1"}):
            response = self.client.post("/time-entries", data={**self.form, **changes})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn('class="time-booking-form"', response.text)

    def test_empty_contract_and_empty_database_render(self):
        self.fixture.execute("DELETE FROM services")
        self.fixture.execute("DELETE FROM flat_fees")
        response = self.client.get("/time-entries/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-booking-submit disabled', response.text)
        self.fixture.execute("DELETE FROM contracts")
        response = self.client.get("/time-entries/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-booking-submit disabled', response.text)

    def test_edit_form_and_invalid_edit_preserve_work_amount(self):
        self.client.post("/time-entries", data=self.form)
        self.fixture.login(1)
        response = self.client.get("/time-entries/1/edit")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('value="1,50"', response.text)
        self.assertIn("Arbeitspaket Produktionsstart", response.text)
        response = self.client.post("/time-entries/1/update", data={**self.form, "end_date": "2026-11-12"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('value="1,50"', response.text)

    def test_hour_mode_is_not_multiplied_by_workday_length(self):
        self.fixture.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('billing_rate_unit', 'hour', ?)", (main.now_iso(),))
        self.assertIn('data-work-unit="hour"', self.client.get("/time-entries/new").text)
        self.client.post("/time-entries", data=self.form)
        self.assertEqual(self.fixture.execute("SELECT hours FROM service_time_entries").fetchone()[0], 1.5)


if __name__ == "__main__":
    unittest.main()
