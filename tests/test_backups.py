import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import backups, database
from app.security import create_session_token
import test_booking_api


def prepare_backup_fixture(fixture, directory):
    snapshot = Path(directory) / "snapshot-20261116T080000Z-ABC123"
    snapshot.mkdir()
    for filename in backups.FILES:
        (snapshot / filename).write_bytes(b"isolated-test-backup")
    (snapshot / "completed-at").write_text(str(int(time.time()) - 600))
    with database.connect() as connection:
        backups.set_value(connection, "backup_worker", json.dumps({"status": "idle", "heartbeat": int(time.time()), "version": 1}))
    return snapshot


class BackupManagementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_booking_api.BookingApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"BACKUP_MANAGEMENT_ENABLED": "1", "BACKUP_DIR": self.directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.snapshot = prepare_backup_fixture(self.fixture, self.directory.name)
        self.fixture.login(1)
        self.client = self.fixture.client
        self.csrf = create_session_token({"purpose": "backup-management", "user_id": 1})
        self.form = dict(csrf_token=self.csrf, enabled="1", interval_hours="24", retention_days="14", retry_minutes="5", timeout_minutes="60")

    def test_listing_status_and_protected_downloads(self):
        page = self.client.get("/backups")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn(self.snapshot.name, page.text)
        self.assertTrue(page.context["worker_online"])
        self.assertEqual(page.context["backup_config"]["retention_days"], 14)
        self.assertIn("Keine externe Sicherung", page.text)
        for filename in backups.FILES:
            download = self.client.get(f"/backups/{self.snapshot.name}/files/{filename}")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, b"isolated-test-backup")
            self.assertEqual(download.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get(f"/backups/{self.snapshot.name}/files/completed-at").status_code, 404)
        self.assertEqual(self.client.get("/backups/invalid/files/database.dump").status_code, 404)

    def test_missing_and_incomplete_snapshots_are_not_published(self):
        (self.snapshot / "completed-at").unlink()
        self.assertEqual(backups.snapshots()[0], [])
        self.assertEqual(self.client.get(f"/backups/{self.snapshot.name}/files/database.dump").status_code, 404)
        with patch.dict(os.environ, {"BACKUP_DIR": str(Path(self.directory.name) / "missing")}):
            page = self.client.get("/backups")
            self.assertEqual(page.status_code, 200)
            self.assertIn("nicht eingebunden", page.text)

    def test_settings_are_persisted_validated_and_can_pause_automation(self):
        response = self.client.post("/backups/settings", data={**self.form, "interval_hours": "12", "retention_days": "30"})
        self.assertEqual(response.status_code, 303, response.text)
        config = self.client.get("/backups").context["backup_config"]
        self.assertEqual(config["interval_seconds"], 43200)
        self.assertEqual(config["retention_days"], 30)
        for field, value in (("interval_hours", "0"), ("interval_hours", "1.5"), ("retention_days", "366"),
                             ("retry_minutes", "0"), ("timeout_minutes", "nan"), ("enabled", "2")):
            response = self.client.post("/backups/settings", data={**self.form, field: value})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.client.get("/backups").context["backup_config"], config)
        paused = {key: value for key, value in self.form.items() if key != "enabled"}
        self.assertEqual(self.client.post("/backups/settings", data=paused).status_code, 303)
        self.assertEqual(self.client.get("/backups").context["next_due"], "Pausiert")

    def test_manual_request_is_durable_and_duplicate_requests_rejected(self):
        self.assertEqual(self.client.post("/backups/run", data={"csrf_token": self.csrf}).status_code, 303)
        state = self.client.get("/backups/status").json()
        self.assertEqual(state["request"]["status"], "queued")
        self.assertEqual(state["request"]["requested_by"], 1)
        self.assertEqual(self.client.post("/backups/run", data={"csrf_token": self.csrf}).status_code, 409)
        self.assertEqual(self.client.get("/backups/status").json()["request"], state["request"])

    def test_offline_or_running_worker_blocks_new_requests(self):
        for state, heartbeat in (("running", int(time.time())), ("idle", 1)):
            with database.connect() as connection:
                backups.set_value(connection, "backup_worker", json.dumps({"status": state, "heartbeat": heartbeat}))
            self.assertEqual(self.client.post("/backups/run", data={"csrf_token": self.csrf}).status_code, 409)
        with patch.dict(os.environ, {"BACKUP_MANAGEMENT_ENABLED": "0"}):
            self.assertEqual(self.client.post("/backups/settings", data=self.form).status_code, 503)
            self.assertEqual(self.client.get(f"/backups/{self.snapshot.name}/files/database.dump").status_code, 404)

    def test_csrf_and_superadmin_permissions_on_every_endpoint(self):
        for token in ("bad", create_session_token({"purpose": "api-key-management", "user_id": 1}),
                      create_session_token({"purpose": "backup-management", "user_id": 2})):
            for path in ("/backups/settings", "/backups/run"):
                self.assertEqual(self.client.post(path, data={**self.form, "csrf_token": token}).status_code, 403)
        for user_id in (2, 3):
            self.fixture.login(user_id)
            for path in ("/backups", "/backups/status", f"/backups/{self.snapshot.name}/files/database.dump"):
                self.assertEqual(self.client.get(path).status_code, 403)
            for path in ("/backups/settings", "/backups/run"):
                self.assertEqual(self.client.post(path, data=self.form).status_code, 403)
            self.assertNotIn('href="/backups"', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
