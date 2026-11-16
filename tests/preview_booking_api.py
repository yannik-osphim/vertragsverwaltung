"""Local, disposable in-memory fixture for booking API browser tests."""
import os
import sys
import tempfile
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("TEST_POSTGRES_URL", None)
os.environ.update(
    APP_ENV="development", DATABASE_URL="sqlite://", SECURE_COOKIES="0", FORCE_HTTPS="0",
    ALLOWED_HOSTS="localhost,127.0.0.1", SESSION_SECRET="isolated-preview-testing-only",
)

from app import main
from app.security import hash_password
from test_booking_api import BookingApiTests


if __name__ == "__main__":
    fixture = BookingApiTests()
    fixture.setUp()
    backup_directory = tempfile.TemporaryDirectory()
    try:
        if "--all-tables" in sys.argv[2:]:
            from test_compact_tables import prepare_all_tables_fixture
            prepare_all_tables_fixture(fixture)
        if "--backups" in sys.argv[2:]:
            from test_backups import prepare_backup_fixture
            os.environ.update(BACKUP_MANAGEMENT_ENABLED="1", BACKUP_DIR=backup_directory.name)
            prepare_backup_fixture(fixture, backup_directory.name)
        fixture.execute("UPDATE users SET password_hash = ? WHERE id = 1", (hash_password("preview-testing-only"),))
        uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off")
    finally:
        backup_directory.cleanup()
        fixture.doCleanups()
