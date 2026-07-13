from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import database  # noqa: E402


DOMAIN_TABLES = [
    "companies",
    "contracts",
    "licenses",
    "services",
    "service_time_entries",
    "invoices",
    "notifications",
    "invoice_line_items",
    "invoice_time_entries",
    "characteristic_values",
    "characteristic_definitions",
    "users",
]

RESET_TABLES = [
    "companies",
    "contracts",
    "licenses",
    "services",
    "service_time_entries",
    "invoices",
    "notifications",
    "invoice_line_items",
    "characteristic_values",
]

SAMPLE_DEFINITION_KEYS = (
    "account_tier",
    "region",
    "contract_type",
    "sla_level",
    "metric",
    "delivery_mode",
)

DEMO_USERS = ("manager", "consultant", "viewer")
DEMO_FILES = ("demo-acme-cloud-suite.pdf", "demo-beta-service-framework.pdf")


def table_counts(connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in DOMAIN_TABLES
    }


def clear_sample_data() -> dict[str, object]:
    with database.connect() as connection:
        before = table_counts(connection)
        connection.execute("DELETE FROM invoice_time_entries")
        connection.execute("UPDATE service_time_entries SET invoice_id = NULL")
        connection.execute("DELETE FROM service_time_entries")
        connection.execute("DELETE FROM invoice_line_items")
        connection.execute("DELETE FROM invoices")
        connection.execute("DELETE FROM notifications")
        connection.execute(
            """
            DELETE FROM characteristic_values
            WHERE target_type IN ('company', 'contract', 'license', 'service')
            """
        )
        connection.execute("DELETE FROM licenses")
        connection.execute("DELETE FROM services")
        connection.execute("DELETE FROM contracts")
        connection.execute("DELETE FROM companies")
        connection.execute(
            """
            DELETE FROM characteristic_definitions
            WHERE key IN (?, ?, ?, ?, ?, ?)
            """,
            SAMPLE_DEFINITION_KEYS,
        )
        connection.executemany(
            "DELETE FROM users WHERE username = ?",
            [(username,) for username in DEMO_USERS],
        )
        if database.IS_SQLITE:
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ({})".format(
                    ",".join(["?"] * len(RESET_TABLES))
                ),
                RESET_TABLES,
            )
        after = table_counts(connection)

    deleted_files = []
    for filename in DEMO_FILES:
        path = database.UPLOAD_DIR / filename
        if path.exists():
            path.unlink()
            deleted_files.append(filename)

    return {"before": before, "after": after, "deleted_files": deleted_files}


if __name__ == "__main__":
    print(clear_sample_data())
