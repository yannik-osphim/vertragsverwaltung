from __future__ import annotations

import sys
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import database  # noqa: E402


BUSINESS_TABLES = [
    "invoice_time_entries",
    "invoice_line_items",
    "invoices",
    "notifications",
    "service_time_entries",
    "characteristic_values",
    "characteristic_definitions",
    "licenses",
    "services",
    "variable_costs",
    "flat_fees",
    "contracts",
    "companies",
    "license_types",
    "service_types",
    "flat_fee_types",
]

SYSTEM_TABLES = [
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "app_settings",
]


def counts(connection, tables: list[str]) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in tables
    }


def clear_directory(directory: Path) -> list[str]:
    deleted: list[str] = []
    directory.mkdir(parents=True, exist_ok=True)
    data_root = database.DATA_DIR.resolve()
    resolved_directory = directory.resolve()
    if data_root not in {resolved_directory, *resolved_directory.parents}:
        raise RuntimeError(f"Unsafe cleanup path: {resolved_directory}")
    for item in directory.iterdir():
        deleted.append(item.name)
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    return deleted


def clear_business_data() -> dict[str, object]:
    with database.connect() as connection:
        before = counts(connection, BUSINESS_TABLES + SYSTEM_TABLES)
        if database.IS_POSTGRES:
            connection.execute(
                "TRUNCATE TABLE " + ", ".join(BUSINESS_TABLES) + " RESTART IDENTITY"
            )
        else:
            for table in BUSINESS_TABLES:
                connection.execute(f"DELETE FROM {table}")
            reset_tables = [table for table in BUSINESS_TABLES if table != "invoice_time_entries"]
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ({})".format(
                    ",".join(["?"] * len(reset_tables))
                ),
                reset_tables,
            )
        after = counts(connection, BUSINESS_TABLES + SYSTEM_TABLES)
    deleted_files = {
        "contract_documents": clear_directory(database.UPLOAD_DIR),
        "company_logos": clear_directory(database.LOGO_DIR),
        "invoice_documents": clear_directory(database.INVOICE_DOCUMENT_DIR),
    }
    return {"before": before, "after": after, "deleted_files": deleted_files}


if __name__ == "__main__":
    print(clear_business_data())
