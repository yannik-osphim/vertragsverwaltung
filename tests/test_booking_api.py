import os
import re
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import Session, SQLModel

from app import booking_api, database, main
from app.security import SESSION_COOKIE_NAME, create_session_token


class BookingApiTests(unittest.TestCase):
    def setUp(self):
        postgres_url = os.environ.get("TEST_POSTGRES_URL")
        if postgres_url:
            schema = "booking_api_test_" + uuid.uuid4().hex
            maintenance = create_engine(postgres_url)
            with maintenance.begin() as connection:
                connection.execute(CreateSchema(schema))

            def remove_test_schema():
                with maintenance.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
                maintenance.dispose()

            self.addCleanup(remove_test_schema)
            self.engine = create_engine(postgres_url, connect_args={"options": f"-c search_path={schema}"})
        else:
            self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        for target, value in (("engine", self.engine), ("IS_POSTGRES", bool(postgres_url)), ("IS_SQLITE", not postgres_url)):
            patcher = patch.object(database, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module, target, value in (
            (database, "ensure_storage", None), (database, "ensure_database_exists", None),
            (main, "billing_nav_group_count", 0),
        ):
            patcher = patch.object(module, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        SQLModel.metadata.create_all(self.engine)
        self.raw_key = booking_api.generate_api_key()
        stamp = booking_api.utc_now()
        with Session(self.engine) as session:
            session.add_all([
                database.Role(id=1, name="admin", label="Superadmin"),
                database.Role(id=2, name="consultant", label="Consultant"),
                database.Role(id=3, name="manager", label="Manager"),
                database.Permission(id=1, key="time.create", label="Stunden"),
                database.RolePermission(role_id=2, permission_id=1),
                database.User(id=1, username="admin", full_name="Admin", role_id=1, password_hash="unused", created_at=stamp),
                database.User(id=2, username="consultant", full_name="Consultant", role_id=2, password_hash="unused", created_at=stamp),
                database.User(id=3, username="manager", full_name="Manager", role_id=3, password_hash="unused", created_at=stamp),
                database.Company(id=1, name="Test", created_at=stamp, updated_at=stamp),
                database.Contract(id=1, company_id=1, contract_number="C-100", title="Projekt Alpha", start_date="2026-01-01", created_at=stamp, updated_at=stamp),
                database.Contract(id=2, company_id=1, contract_number="C-SECRET", title="Vertraulich", start_date="2026-01-01", created_at=stamp, updated_at=stamp),
                database.Service(id=1, contract_id=1, name="Workshop", hourly_rate_cents=12500, contracted_hours=72, created_at=stamp, updated_at=stamp),
                database.Service(id=2, contract_id=2, name="Geheimer Service", hourly_rate_cents=12500, created_at=stamp, updated_at=stamp),
                database.Service(id=3, contract_id=1, name="Ohne Limit", hourly_rate_cents=12500, created_at=stamp, updated_at=stamp),
                database.FlatFee(id=1, contract_id=1, name="Reisekosten", fee_kind="travel_cost", start_date="2026-01-01", created_at=stamp, updated_at=stamp),
                database.ApiKey(id=1, name="Test Key", key_hash=booking_api.hash_api_key(self.raw_key), key_prefix=self.raw_key[:12], user_id=2, created_by=1, created_at=stamp),
                database.ApiKeyContract(api_key_id=1, contract_id=1),
                database.AppSetting(key="workday_hours", value="8", updated_at=stamp),
            ])
            session.commit()
        if postgres_url:
            self.execute("SELECT setval(pg_get_serial_sequence('api_keys', 'id'), 1)")
        self.client = TestClient(main.app, base_url="https://localhost", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.headers = {"X-API-Key": self.raw_key}
        self.body = {
            "contract_number": "C-100", "service_id": 1, "start_date": "2026-11-02",
            "end_date": "2026-11-03", "amount": "0,75", "unit": "day", "description": "Workshop",
        }

    def execute(self, sql, params=()):
        with database.connect() as connection:
            return connection.execute(sql, params)

    def book(self, **overrides):
        return self.client.post("/create-booking", json={**self.body, **overrides}, headers=self.headers)

    def login(self, user_id):
        self.client.cookies.set(SESSION_COOKIE_NAME, create_session_token({"user_id": user_id}))

    def test_decimal_days_persist_as_hours_with_key_owner(self):
        response = self.book()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["hours"], 6)
        row = self.execute("SELECT * FROM service_time_entries").fetchone()
        self.assertEqual((row["user_id"], row["hours"], row["status"]), (2, 6, "submitted"))
        self.assertEqual((row["start_date"], row["end_date"]), ("2026-11-02", "2026-11-03"))
        self.assertIsNone(row["flat_fee_id"])
        self.assertEqual(row["booking_source"], "api")

    def test_explicit_hours_ignore_ui_unit_and_days_use_configured_factor(self):
        self.execute("UPDATE app_settings SET value = '7.5' WHERE key = 'workday_hours'")
        self.assertEqual(self.book(amount=1.5, unit="hour").json()["hours"], 1.5)
        self.assertEqual(self.book(amount=1.5, unit="day").json()["hours"], 11.25)

    def test_service_can_be_resolved_by_contract_specific_name(self):
        self.assertEqual(self.book(service_id=None, service_name="Workshop").status_code, 201)

    def test_ambiguous_service_name_rejected(self):
        self.execute("UPDATE services SET name = 'Workshop' WHERE id = 3")
        self.assertEqual(self.book(service_id=None, service_name="Workshop").status_code, 409)
        self.assertEqual(self.book().status_code, 201)

    def test_get_services_returns_only_scoped_aggregates(self):
        self.book()
        self.book(amount=2, unit="hour")
        self.execute("UPDATE service_time_entries SET status = 'approved' WHERE id = 2")
        response = self.client.post("/get-services", json={"contract_name": "Projekt Alpha"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        by_id = {service["service_id"]: service for service in response.json()["services"]}
        self.assertEqual(by_id[1]["contracted_hours"], 72)
        self.assertEqual(by_id[1]["booked_hours"], 8)
        self.assertEqual(by_id[1]["remaining_hours"], 64)
        self.assertEqual(by_id[1]["submitted_hours"], 6)
        self.assertEqual(by_id[1]["approved_hours"], 2)
        self.assertIsNone(by_id[3]["remaining_hours"])
        for secret in ("hourly_rate", "user_id", "description", "Geheimer", "Reisekosten"):
            self.assertNotIn(secret, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_private_contracts_and_unknown_contracts_have_same_error(self):
        hidden = self.book(contract_number="C-SECRET")
        absent = self.book(contract_number="DOES-NOT-EXIST")
        self.assertEqual((hidden.status_code, hidden.json()), (absent.status_code, absent.json()))
        self.assertEqual(hidden.status_code, 404)
        for number in ("C-SECRET", "DOES-NOT-EXIST"):
            self.assertEqual(self.client.post("/get-services", json={"contract_number": number}, headers=self.headers).status_code, 404)

    def test_wrong_service_cannot_be_booked(self):
        self.assertEqual(self.book(service_id=2).status_code, 404)
        self.assertEqual(self.book(service_id=None, service_name="Reisekosten").status_code, 404)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM service_time_entries").fetchone()[0], 0)

    def test_contract_discovery_is_not_available_to_api_key(self):
        for path in ("/contracts", "/companies", "/services", "/time-entries", "/api-keys"):
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 303)
        self.assertEqual(self.client.get("/get-services", headers=self.headers).status_code, 405)
        for body in ({}, {"contract_name": ""}, {"contract_number": "%"}):
            self.assertIn(self.client.post("/get-services", json=body, headers=self.headers).status_code, (404, 422))

    def test_missing_invalid_and_session_only_auth_rejected(self):
        self.login(1)
        for headers in ({}, {"X-API-Key": "bad"}, {"X-API-Key": booking_api.generate_api_key()}):
            for path, body in (("/create-booking", self.body), ("/get-services", {"contract_number": "C-100"})):
                self.assertEqual(self.client.post(path, json=body, headers=headers).status_code, 401)

    def test_revocation_expiry_disabled_user_and_permission_loss(self):
        for change, reset in (
            ("UPDATE api_keys SET revoked_at = '2020-01-01T00:00:00+00:00'", "UPDATE api_keys SET revoked_at = NULL"),
            ("UPDATE api_keys SET expires_at = '2020-01-01T00:00:00+00:00'", "UPDATE api_keys SET expires_at = NULL"),
            ("UPDATE users SET active = 0 WHERE id = 2", "UPDATE users SET active = 1 WHERE id = 2"),
            ("DELETE FROM role_permissions", "INSERT INTO role_permissions (role_id, permission_id) VALUES (2, 1)"),
        ):
            self.execute(change)
            self.assertEqual(self.book().status_code, 401)
            self.execute(reset)

    def test_inactive_contract_or_service_rejected(self):
        for table in ("services", "contracts"):
            self.execute(f"UPDATE {table} SET status = 'inactive' WHERE id = 1")
            self.assertEqual(self.book().status_code, 409)
            self.execute(f"UPDATE {table} SET status = 'active' WHERE id = 1")

    def test_invalid_values_return_422_and_no_booking(self):
        for changes in (
            {"amount": 0}, {"amount": -1}, {"amount": "NaN"}, {"amount": "Infinity"},
            {"amount": "0.001"}, {"amount": 1000001}, {"unit": "week"}, {"end_date": "bad"},
            {"end_date": "2026-01-01"}, {"service_name": "Workshop"}, {"service_id": None},
            {"service_id": True}, {"user_id": 1}, {"status": "approved"}, {"contract_number": " "},
            {"booking_source": "web"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.book(**changes).status_code, 422)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM service_time_entries").fetchone()[0], 0)

    def test_idempotency_replay_conflict_and_deleted_booking(self):
        headers = {**self.headers, "Idempotency-Key": "external-entry-42"}
        first = self.client.post("/create-booking", json=self.body, headers=headers)
        repeat = self.client.post("/create-booking", json=self.body, headers=headers)
        self.assertEqual((first.status_code, repeat.status_code), (201, 200))
        self.assertEqual(first.json(), repeat.json())
        self.assertEqual(self.execute("SELECT booking_source FROM service_time_entries").fetchone()[0], "api")
        changed = self.client.post("/create-booking", json={**self.body, "amount": 2}, headers=headers)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM service_time_entries").fetchone()[0], 1)
        self.execute("DELETE FROM service_time_entries")
        self.assertEqual(self.client.post("/create-booking", json=self.body, headers=headers).status_code, 409)

    @unittest.skipUnless(os.environ.get("TEST_POSTGRES_URL"), "Requires isolated PostgreSQL test database")
    def test_concurrent_retries_only_create_one_booking(self):
        def send(_):
            client = TestClient(main.app, base_url="https://localhost")
            try:
                return client.post("/create-booking", json=self.body, headers={**self.headers, "Idempotency-Key": "concurrent"})
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 201])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM service_time_entries").fetchone()[0], 1)

    def test_ambiguous_contract_name_requires_number(self):
        self.execute("UPDATE contracts SET title = 'Projekt Alpha' WHERE id = 2")
        self.execute("INSERT INTO api_key_contracts (api_key_id, contract_id) VALUES (1, 2)")
        self.assertEqual(self.client.post("/get-services", json={"contract_name": "Projekt Alpha"}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post("/get-services", json={"contract_number": "C-100"}, headers=self.headers).status_code, 200)

    def test_admin_key_lifecycle_csrf_and_hash_only_storage(self):
        self.login(1)
        page = self.client.get("/api-keys/new")
        self.assertEqual(page.status_code, 200, page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        body = {"name": "New integration", "user_id": 2, "contract_ids": [1], "csrf_token": csrf}
        self.assertEqual(self.client.post("/api-keys", data={**body, "csrf_token": "wrong"}).status_code, 403)
        created = self.client.post("/api-keys", data=body)
        self.assertEqual(created.status_code, 200, created.text)
        secret = re.search(r'<code class="secret-value">([^<]+)</code>', created.text).group(1)
        row = self.execute("SELECT * FROM api_keys WHERE id = 2").fetchone()
        self.assertEqual(row["key_hash"], booking_api.hash_api_key(secret))
        self.assertNotIn(secret, str(dict(row)))
        self.assertNotIn(secret, self.client.get("/api-keys").text)
        self.assertEqual(created.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post("/api-keys/2/revoke", data={"csrf_token": csrf}).status_code, 303)
        self.assertEqual(self.client.post("/create-booking", json=self.body, headers={"X-API-Key": secret}).status_code, 401)

    def test_admin_validates_user_scopes_and_expiry(self):
        self.login(1)
        csrf = main.api_key_csrf_token({"id": 1})
        body = {"name": "Test", "user_id": 2, "contract_ids": [1], "csrf_token": csrf}
        for change in ({"user_id": 999}, {"user_id": 3}, {"contract_ids": []}, {"contract_ids": [999]}, {"expires_on": "2020-01-01"}):
            self.assertEqual(self.client.post("/api-keys", data={**body, **change}).status_code, 400)
        expires = (date.today() + timedelta(days=2)).isoformat()
        self.assertEqual(self.client.post("/api-keys", data={**body, "expires_on": expires}).status_code, 200)

    def create_external_key(self, booking_name="Alex Extern", **overrides):
        self.login(1)
        response = self.client.post("/api-keys", data={
            "name": "Externe Zeiterfassung", "owner_type": "external", "booking_name": booking_name,
            "contract_ids": [1], "csrf_token": main.api_key_csrf_token({"id": 1}), **overrides,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return re.search(r'<code class="secret-value">([^<]+)</code>', response.text).group(1)

    def test_external_key_creates_booking_without_creating_account(self):
        self.headers = {"X-API-Key": self.create_external_key("  Alex Extern  ")}
        row = self.execute("SELECT * FROM api_keys WHERE id = 2").fetchone()
        self.assertIsNone(row["user_id"])
        self.assertEqual(row["booking_name"], "Alex Extern")
        self.assertEqual(self.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
        self.client.cookies.clear()
        result = self.book()
        self.assertEqual(result.status_code, 201, result.text)
        entry = self.execute("SELECT * FROM service_time_entries").fetchone()
        self.assertIsNone(entry["user_id"])
        self.assertEqual((entry["booking_name"], entry["hours"], entry["status"]), ("Alex Extern", 6, "submitted"))
        self.assertEqual(self.book(booking_name="Someone else").status_code, 422)
        self.assertEqual(self.client.get("/contracts", headers=self.headers).status_code, 303)
        self.assertEqual(self.book(contract_number="C-SECRET").status_code, 404)
        services = self.client.post("/get-services", json={"contract_number": "C-100"}, headers=self.headers)
        self.assertEqual(services.status_code, 200)
        self.assertEqual(services.json()["services"][1]["booked_hours"], 6)
        self.assertNotIn("Alex Extern", services.text)
        self.execute("UPDATE api_keys SET booking_name = 'Changed' WHERE id = 2")
        self.assertEqual(self.execute("SELECT booking_name FROM service_time_entries").fetchone()[0], "Alex Extern")

    def test_external_booking_visible_editable_and_preserved_after_revocation(self):
        self.headers = {"X-API-Key": self.create_external_key()}
        self.assertEqual(self.book().status_code, 201)
        for path in ("/", "/time-entries", "/time-entries/1/edit", "/contracts/1?tab=time_entries"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text[:500])
            if path != "/":
                self.assertIn("Alex Extern", response.text)
            self.assertIn('aria-label="API-Buchung"', response.text)
        self.assertEqual(main.dashboard_data()["entries"][0]["user_name"], "Alex Extern")
        edited = self.client.post("/time-entries/1/update", data={
            "contract_id": 1, "work_item_key": "service:1", "start_date": "2026-11-02",
            "end_date": "2026-11-03", "hours": "1,50", "status": "approved", "description": "Korrigiert",
            "booking_source": "web",
        })
        self.assertEqual(edited.status_code, 303, edited.text)
        entry = self.execute("SELECT * FROM service_time_entries WHERE id = 1").fetchone()
        self.assertEqual((entry["booking_name"], entry["hours"]), ("Alex Extern", 12))
        self.assertEqual(entry["booking_source"], "api")
        self.assertEqual(self.client.post("/time-entries/1/approve").status_code, 303)
        self.assertEqual(self.execute("SELECT booking_source FROM service_time_entries").fetchone()[0], "api")
        with Session(self.engine) as session:
            stamp = booking_api.utc_now()
            session.add(database.Invoice(id=1, invoice_number="TEST-1", contract_id=1, company_id=1,
                period_start="2026-11-01", period_end="2026-11-30", created_at=stamp, updated_at=stamp))
            session.commit()
        self.execute("INSERT INTO invoice_time_entries (invoice_id, time_entry_id) VALUES (1, 1)")
        self.assertEqual(main.invoice_detail_context(1)["linked_entries"][0]["user_name"], "Alex Extern")
        self.assertIn('aria-label="API-Buchung"', self.client.get("/invoices/1").text)
        self.assertEqual(self.client.post("/api-keys/2/revoke", data={"csrf_token": main.api_key_csrf_token({"id": 1})}).status_code, 303)
        self.assertEqual(self.book().status_code, 401)
        self.assertIn("Alex Extern", self.client.get("/time-entries").text)
        self.assertEqual(self.execute("SELECT booking_source FROM service_time_entries").fetchone()[0], "api")
        self.execute("DELETE FROM invoice_time_entries")
        self.assertEqual(self.client.post("/time-entries/1/delete").status_code, 303)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM service_time_entries").fetchone()[0], 0)

    def test_external_key_expiry_and_invalid_owner(self):
        self.headers = {"X-API-Key": self.create_external_key()}
        self.execute("UPDATE api_keys SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = 2")
        self.assertEqual(self.book().status_code, 401)
        self.execute("UPDATE api_keys SET expires_at = NULL, booking_name = ' ' WHERE id = 2")
        self.assertEqual(self.book().status_code, 401)

    def test_external_name_validation_and_form_retains_values(self):
        self.login(1)
        body = {"name": "Integration", "owner_type": "external", "booking_name": "Alex Extern",
                "contract_ids": [1], "csrf_token": main.api_key_csrf_token({"id": 1})}
        for changes in ({"booking_name": ""}, {"booking_name": "  "}, {"booking_name": "x" * 251},
                        {"owner_type": "invalid"}, {"contract_ids": []}):
            with self.subTest(changes=changes):
                response = self.client.post("/api-keys", data={**body, **changes})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('value="Integration"', response.text)
        # An inactive field from an older form submission cannot override ownership.
        self.create_external_key(user_id=2)
        self.assertIsNone(self.execute("SELECT user_id FROM api_keys WHERE id = 2").fetchone()[0])
        self.assertEqual(self.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0], 2)

    def test_external_key_name_escaped_and_no_account_required_in_form(self):
        self.create_external_key('<img src=x onerror=alert(1)>')
        response = self.client.get("/api-keys")
        self.assertIn("&lt;img", response.text)
        self.assertNotIn('<img src=x', response.text)
        self.assertIn("Ohne Benutzerkonto", response.text)
        with patch.object(main, "api_key_form_context", return_value={
            **main.api_key_form_context({"id": 1}), "booking_users": [],
            "values": {"name": "", "owner_type": "external", "user_id": "", "booking_name": "", "contract_ids": [], "expires_on": ""},
        }):
            response = self.client.get("/api-keys/new")
        self.assertIn('name="booking_name"', response.text)
        self.assertNotRegex(response.text, r'<button[^>]+type="submit"[^>]+disabled')

    def test_migration_preserves_existing_keys_and_adds_optional_owner(self):
        for table in ("api_keys", "service_time_entries"):
            self.execute(f"ALTER TABLE {table} DROP COLUMN booking_name")
            if database.IS_POSTGRES:
                self.execute(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL")
        database.init_db()
        database.init_db()
        self.assertEqual(self.book().status_code, 201)
        self.assertEqual(self.execute("SELECT user_id FROM api_keys WHERE id = 1").fetchone()[0], 2)
        self.headers = {"X-API-Key": self.create_external_key()}
        self.assertEqual(self.book().status_code, 201)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM contracts").fetchone()[0], 2)

    @unittest.skipUnless(os.environ.get("TEST_POSTGRES_URL"), "Requires isolated PostgreSQL test database")
    def test_failed_owner_migration_rolls_back(self):
        for table in ("api_keys", "service_time_entries"):
            self.execute(f"ALTER TABLE {table} DROP COLUMN booking_name")
            self.execute(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL")
        original = database.drop_not_null_if_postgres

        def fail_after_alter(connection, table, column):
            original(connection, table, column)
            if table == "service_time_entries" and column == "user_id":
                raise RuntimeError("Simulated migration failure")

        with patch.object(database, "drop_not_null_if_postgres", side_effect=fail_after_alter):
            with self.assertRaisesRegex(RuntimeError, "Simulated migration failure"):
                database.init_db()
        for table in ("api_keys", "service_time_entries"):
            columns = {column["name"]: column for column in inspect(self.engine).get_columns(table)}
            self.assertNotIn("booking_name", columns)
            self.assertFalse(columns["user_id"]["nullable"])
        self.assertEqual(self.execute("SELECT user_id FROM api_keys WHERE id = 1").fetchone()[0], 2)

    def test_key_management_is_superadmin_only(self):
        for user_id in (2, 3):
            self.login(user_id)
            for path in ("/api-keys", "/api-keys/new", "/api-keys/1/edit"):
                self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post("/api-keys/1/revoke", data={"csrf_token": "x"}).status_code, 403)
            self.assertEqual(self.client.post("/api-keys/1/update", data={"name": "Denied", "csrf_token": "x"}).status_code, 403)

    def update_key(self, key_id=1, **overrides):
        return self.client.post(f"/api-keys/{key_id}/update", data={
            "name": "Updated integration", "contract_ids": [1, 2], "expires_on": "",
            "csrf_token": main.api_key_csrf_token({"id": 1}), **overrides,
        })

    def test_edit_grants_reuses_key_and_keeps_booking_history(self):
        self.login(1)
        self.assertEqual(self.book().status_code, 201)
        before = dict(self.execute("SELECT * FROM api_keys WHERE id = 1").fetchone())
        page = self.client.get("/api-keys/1/edit")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('value="1" aria-label="C-100 freigeben" checked', page.text)
        self.assertNotIn(self.raw_key, page.text)
        self.assertNotIn(before["key_hash"], page.text)
        self.assertEqual(self.update_key(contract_ids=[2, 2]).status_code, 303)
        after = self.execute("SELECT * FROM api_keys WHERE id = 1").fetchone()
        for field in ("key_hash", "key_prefix", "user_id", "booking_name", "created_by", "created_at"):
            self.assertEqual(before[field], after[field])
        self.assertEqual(self.book().status_code, 404)
        self.assertEqual(self.book(contract_number="C-SECRET", service_id=2).status_code, 201)
        for number, status in (("C-100", 404), ("C-SECRET", 200)):
            self.assertEqual(self.client.post("/get-services", json={"contract_number": number}, headers=self.headers).status_code, status)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM service_time_entries WHERE contract_id = 1").fetchone()[0], 1)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM api_key_contracts WHERE api_key_id = 1").fetchone()[0], 1)

    def test_external_key_can_add_and_remove_all_grants_without_changing_owner(self):
        self.headers = {"X-API-Key": self.create_external_key()}
        response = self.update_key(2, user_id=1, booking_name="Other person", owner_type="account")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.book(contract_number="C-SECRET", service_id=2).status_code, 201)
        entry = self.execute("SELECT * FROM service_time_entries").fetchone()
        self.assertIsNone(entry["user_id"])
        self.assertEqual(entry["booking_name"], "Alex Extern")
        self.assertEqual(self.update_key(2, contract_ids=[]).status_code, 303)
        self.assertIn("Keine Vertraege", self.client.get("/api-keys").text)
        self.assertEqual(self.book().status_code, 404)
        self.assertEqual(self.book(contract_number="C-SECRET", service_id=2).status_code, 404)
        self.assertEqual(self.update_key(2, contract_ids=[1]).status_code, 303)
        self.assertEqual(self.book().status_code, 201)

    def test_invalid_key_edit_and_csrf_leave_grants_unchanged(self):
        self.login(1)
        before = dict(self.execute("SELECT * FROM api_keys WHERE id = 1").fetchone())
        self.assertEqual(self.update_key(csrf_token="wrong").status_code, 403)
        for changes in ({"name": " "}, {"name": "x" * 121}, {"contract_ids": [999]},
                        {"expires_on": "not-a-date"}, {"expires_on": "2020-01-01"}):
            with self.subTest(changes=changes):
                response = self.update_key(**changes)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('action="/api-keys/1/update"', response.text)
                self.assertEqual(before, dict(self.execute("SELECT * FROM api_keys WHERE id = 1").fetchone()))
                self.assertEqual([row[0] for row in self.execute("SELECT contract_id FROM api_key_contracts WHERE api_key_id = 1").fetchall()], [1])

    def test_edit_expiry_round_trip_and_extension_without_unblocking_disabled_owner(self):
        self.login(1)
        self.execute("UPDATE api_keys SET expires_at = '2020-01-02T00:00:00+00:00' WHERE id = 1")
        page = self.client.get("/api-keys/1/edit")
        self.assertIn('value="2020-01-01"', page.text)
        self.assertEqual(self.update_key(expires_on="2020-01-01").status_code, 303)
        self.assertEqual(self.book().status_code, 401)
        future = (date.today() + timedelta(days=5)).isoformat()
        self.assertEqual(self.update_key(expires_on=future).status_code, 303)
        self.assertIn(f'value="{future}"', self.client.get("/api-keys/1/edit").text)
        self.assertEqual(self.book().status_code, 201)
        self.execute("UPDATE users SET active = 0 WHERE id = 2")
        self.assertEqual(self.update_key().status_code, 303)
        self.assertEqual(self.book().status_code, 401)

    def test_missing_and_revoked_key_cannot_be_edited(self):
        self.login(1)
        self.assertEqual(self.client.get("/api-keys/999/edit").status_code, 404)
        self.assertEqual(self.update_key(999).status_code, 404)
        self.execute("UPDATE api_keys SET revoked_at = '2026-01-01T00:00:00+00:00' WHERE id = 1")
        self.assertEqual(self.client.get("/api-keys/1/edit").status_code, 409)
        self.assertEqual(self.update_key().status_code, 409)
        self.assertNotIn('href="/api-keys/1/edit"', self.client.get("/api-keys").text)
        self.assertEqual(self.book().status_code, 401)

    def test_failed_grant_update_rolls_back_metadata_and_grants(self):
        self.login(1)
        original = database.Connection.executemany

        def fail_after_insert(connection, sql, params):
            result = original(connection, sql, params)
            if "INSERT INTO api_key_contracts" in sql:
                raise RuntimeError("Simulated grant update failure")
            return result

        with patch.object(database.Connection, "executemany", new=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, "Simulated grant update failure"):
                self.update_key(contract_ids=[2])
        self.assertEqual(self.execute("SELECT name FROM api_keys WHERE id = 1").fetchone()[0], "Test Key")
        self.assertEqual([row[0] for row in self.execute("SELECT contract_id FROM api_key_contracts WHERE api_key_id = 1").fetchall()], [1])

    def test_user_with_key_is_disabled_instead_of_deleted(self):
        self.login(1)
        self.assertEqual(self.client.post("/users/2/delete").status_code, 303)
        self.assertEqual(self.execute("SELECT active FROM users WHERE id = 2").fetchone()[0], 0)
        self.assertEqual(self.book().status_code, 401)

    def test_startup_creates_new_tables_idempotently_without_removing_business_data(self):
        for table in (database.ApiBookingRequest.__table__, database.ApiKeyContract.__table__, database.ApiKey.__table__):
            table.drop(self.engine)
        database.init_db()
        database.init_db()
        self.assertTrue({"api_keys", "api_key_contracts", "api_booking_requests"}.issubset(inspect(self.engine).get_table_names()))
        self.assertEqual(self.execute("SELECT COUNT(*) FROM contracts").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
