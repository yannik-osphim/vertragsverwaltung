from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import UniqueConstraint, create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Field, SQLModel

from .config import load_env

load_env()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGURED_DATA_DIR = Path(os.environ.get("CONTRACT_DATA_DIR", BASE_DIR / "data"))
DATA_DIR = CONFIGURED_DATA_DIR if CONFIGURED_DATA_DIR.is_absolute() else BASE_DIR / CONFIGURED_DATA_DIR
UPLOAD_DIR = DATA_DIR / "contract_documents"
LOGO_DIR = DATA_DIR / "company_logos"
INVOICE_DOCUMENT_DIR = DATA_DIR / "invoice_documents"

DEFAULT_DATABASE_URL = "postgresql+psycopg://user:password@localhost:5432/contracts"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


DB_CONNECT_TIMEOUT_SECONDS = int_env("DB_CONNECT_TIMEOUT_SECONDS", 5, 1)
DB_LOCK_TIMEOUT_MS = int_env("DB_LOCK_TIMEOUT_MS", 5000, 0)
DB_STATEMENT_TIMEOUT_MS = int_env("DB_STATEMENT_TIMEOUT_MS", 60000, 0)

ENGINE_CONNECT_ARGS: dict[str, Any] = {}
if DATABASE_URL.startswith("postgresql"):
    ENGINE_CONNECT_ARGS["connect_timeout"] = DB_CONNECT_TIMEOUT_SECONDS
    postgres_options = []
    if DB_LOCK_TIMEOUT_MS:
        postgres_options.extend(["-c", f"lock_timeout={DB_LOCK_TIMEOUT_MS}"])
    if DB_STATEMENT_TIMEOUT_MS:
        postgres_options.extend(["-c", f"statement_timeout={DB_STATEMENT_TIMEOUT_MS}"])
    if postgres_options:
        ENGINE_CONNECT_ARGS["options"] = " ".join(postgres_options)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=ENGINE_CONNECT_ARGS)
IS_POSTGRES = engine.url.get_backend_name().startswith("postgresql")
IS_SQLITE = engine.url.get_backend_name().startswith("sqlite")


class Role(SQLModel, table=True):
    __tablename__ = "roles"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, nullable=False)
    label: str = Field(nullable=False)
    description: str = Field(default="")


class Permission(SQLModel, table=True):
    __tablename__ = "permissions"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, nullable=False)
    label: str = Field(nullable=False)
    description: str = Field(default="")


class RolePermission(SQLModel, table=True):
    __tablename__ = "role_permissions"

    role_id: int = Field(primary_key=True)
    permission_id: int = Field(primary_key=True)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, nullable=False)
    password_hash: str = Field(nullable=False)
    full_name: str = Field(nullable=False)
    email: str = Field(default="")
    role_id: int = Field(nullable=False)
    active: int = Field(default=1, nullable=False)
    created_at: str = Field(nullable=False)


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(nullable=False)
    legal_name: str = Field(default="")
    customer_number: str = Field(default="")
    industry: str = Field(default="")
    status: str = Field(default="active", nullable=False)
    contact_name: str = Field(default="", nullable=False)
    contact_email: str = Field(default="", nullable=False)
    contact_phone: str = Field(default="", nullable=False)
    billing_recipient_name: str = Field(default="", nullable=False)
    billing_recipient_email: str = Field(default="", nullable=False)
    billing_recipient_phone: str = Field(default="", nullable=False)
    customer_supplier_number: str = Field(default="")
    logo_original_filename: str | None = Field(default=None)
    logo_stored_filename: str | None = Field(default=None)
    notes: str = Field(default="")
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class Contract(SQLModel, table=True):
    __tablename__ = "contracts"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(nullable=False)
    contract_number: str = Field(nullable=False)
    title: str = Field(nullable=False)
    status: str = Field(default="active", nullable=False)
    start_date: str = Field(nullable=False)
    end_date: str | None = Field(default=None)
    license_billing_frequency: str = Field(default="quarterly", nullable=False)
    service_billing_frequency: str = Field(default="monthly", nullable=False)
    variable_billing_frequency: str = Field(default="monthly", nullable=False)
    service_hourly_rate_cents: int = Field(default=0, nullable=False)
    license_price_increase_percent: str = Field(default="8", nullable=False)
    vat_treatment: str = Field(default="standard", nullable=False)
    contact_name: str = Field(default="", nullable=False)
    contact_email: str = Field(default="", nullable=False)
    contact_phone: str = Field(default="", nullable=False)
    billing_recipient_name: str = Field(default="", nullable=False)
    billing_recipient_email: str = Field(default="", nullable=False)
    billing_recipient_phone: str = Field(default="", nullable=False)
    customer_order_number: str = Field(default="")
    erp_reference_number: str = Field(default="")
    currency: str = Field(default="EUR", nullable=False)
    original_filename: str | None = Field(default=None)
    stored_filename: str | None = Field(default=None)
    notes: str = Field(default="")
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class LicenseType(SQLModel, table=True):
    __tablename__ = "license_types"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, nullable=False)
    datev_account: str = Field(nullable=False)
    description: str = Field(default="")
    active: int = Field(default=1, nullable=False)
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class ServiceType(SQLModel, table=True):
    __tablename__ = "service_types"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, nullable=False)
    datev_account: str = Field(nullable=False)
    default_hourly_rate_cents: int = Field(default=0, nullable=False)
    description: str = Field(default="")
    active: int = Field(default=1, nullable=False)
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class FlatFeeType(SQLModel, table=True):
    __tablename__ = "flat_fee_types"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, nullable=False)
    datev_account: str = Field(nullable=False)
    description: str = Field(default="")
    active: int = Field(default=1, nullable=False)
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class License(SQLModel, table=True):
    __tablename__ = "licenses"

    id: int | None = Field(default=None, primary_key=True)
    contract_id: int = Field(nullable=False)
    license_type_id: int | None = Field(default=None)
    name: str = Field(nullable=False)
    annual_amount_cents: int = Field(nullable=False)
    quantity: int = Field(default=1, nullable=False)
    start_date: str = Field(nullable=False)
    end_date: str | None = Field(default=None)
    billing_frequency: str = Field(default="quarterly", nullable=False)
    billing_strategy: str = Field(default="standard", nullable=False)
    first_year_billing_frequency: str | None = Field(default=None)
    renewal_billing_frequency: str | None = Field(default=None)
    status: str = Field(default="active", nullable=False)
    notes: str = Field(default="")
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class Service(SQLModel, table=True):
    __tablename__ = "services"

    id: int | None = Field(default=None, primary_key=True)
    contract_id: int = Field(nullable=False)
    service_type_id: int | None = Field(default=None)
    name: str = Field(nullable=False)
    hourly_rate_cents: int = Field(nullable=False)
    contracted_hours: float | None = Field(default=None)
    billing_frequency: str = Field(default="monthly", nullable=False)
    status: str = Field(default="active", nullable=False)
    notes: str = Field(default="")
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class VariableCost(SQLModel, table=True):
    __tablename__ = "variable_costs"

    id: int | None = Field(default=None, primary_key=True)
    contract_id: int = Field(nullable=False)
    name: str = Field(nullable=False)
    description: str = Field(default="")
    datev_account: str = Field(default="")
    rate_cents: int = Field(default=0, nullable=False)
    quantity: float = Field(default=1, nullable=False)
    unit: str = Field(default="Einheit", nullable=False)
    start_date: str = Field(nullable=False)
    end_date: str | None = Field(default=None)
    billing_frequency: str = Field(default="monthly", nullable=False)
    status: str = Field(default="active", nullable=False)
    notes: str = Field(default="")
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class FlatFee(SQLModel, table=True):
    __tablename__ = "flat_fees"

    id: int | None = Field(default=None, primary_key=True)
    contract_id: int = Field(nullable=False)
    flat_fee_type_id: int | None = Field(default=None)
    name: str = Field(nullable=False)
    amount_cents: int = Field(default=0, nullable=False)
    fee_kind: str = Field(default="work_package", nullable=False)
    start_date: str = Field(nullable=False)
    end_date: str | None = Field(default=None)
    billing_frequency: str = Field(default="once", nullable=False)
    success_condition: str = Field(default="")
    expected_success_date: str | None = Field(default=None)
    success_date: str | None = Field(default=None)
    approval_status: str = Field(default="not_applicable", nullable=False)
    approved_by: int | None = Field(default=None)
    approved_at: str | None = Field(default=None)
    status: str = Field(default="active", nullable=False)
    notes: str = Field(default="")
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class ServiceTimeEntry(SQLModel, table=True):
    __tablename__ = "service_time_entries"

    id: int | None = Field(default=None, primary_key=True)
    contract_id: int = Field(nullable=False)
    service_id: int | None = Field(default=None)
    flat_fee_id: int | None = Field(default=None)
    user_id: int = Field(nullable=False)
    work_date: str = Field(nullable=False)
    start_date: str | None = Field(default=None)
    end_date: str | None = Field(default=None)
    hours: float = Field(nullable=False)
    description: str = Field(default="")
    status: str = Field(default="submitted", nullable=False)
    invoice_id: int | None = Field(default=None)
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class Invoice(SQLModel, table=True):
    __tablename__ = "invoices"

    id: int | None = Field(default=None, primary_key=True)
    invoice_number: str = Field(unique=True, nullable=False)
    company_id: int = Field(nullable=False)
    contract_id: int = Field(nullable=False)
    period_start: str = Field(nullable=False)
    period_end: str = Field(nullable=False)
    status: str = Field(default="draft", nullable=False)
    currency: str = Field(default="EUR", nullable=False)
    subtotal_cents: int = Field(default=0, nullable=False)
    discount_type: str = Field(default="none", nullable=False)
    discount_value: str = Field(default="")
    discount_cents: int = Field(default=0, nullable=False)
    total_cents: int = Field(default=0, nullable=False)
    vat_treatment: str = Field(default="standard", nullable=False)
    vat_rate_percent: str = Field(default="19", nullable=False)
    vat_cents: int = Field(default=0, nullable=False)
    gross_total_cents: int = Field(default=0, nullable=False)
    include_licenses: int = Field(default=1, nullable=False)
    include_services: int = Field(default=1, nullable=False)
    include_variable_costs: int = Field(default=1, nullable=False)
    include_flat_fees: int = Field(default=1, nullable=False)
    datev_invoice_number: str = Field(default="")
    datev_invoice_date: str | None = Field(default=None)
    invoice_document_original_filename: str | None = Field(default=None)
    invoice_document_stored_filename: str | None = Field(default=None)
    created_by: int | None = Field(default=None)
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class InvoiceLineItem(SQLModel, table=True):
    __tablename__ = "invoice_line_items"

    id: int | None = Field(default=None, primary_key=True)
    invoice_id: int = Field(nullable=False)
    item_type: str = Field(nullable=False)
    source_id: int | None = Field(default=None)
    datev_account: str = Field(default="")
    billing_key: str = Field(default="")
    description: str = Field(nullable=False)
    quantity_text: str = Field(default="")
    subtotal_cents: int = Field(default=0, nullable=False)
    discount_type: str = Field(default="none", nullable=False)
    discount_value: str = Field(default="")
    discount_cents: int = Field(default=0, nullable=False)
    amount_cents: int = Field(nullable=False)


class InvoiceTimeEntry(SQLModel, table=True):
    __tablename__ = "invoice_time_entries"

    invoice_id: int = Field(primary_key=True)
    time_entry_id: int = Field(primary_key=True)


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    event_key: str = Field(unique=True, nullable=False)
    notification_type: str = Field(nullable=False)
    target_type: str = Field(nullable=False)
    target_id: int = Field(nullable=False)
    severity: str = Field(default="info", nullable=False)
    title: str = Field(nullable=False)
    message: str = Field(default="", nullable=False)
    due_date: str | None = Field(default=None)
    acknowledged_at: str | None = Field(default=None)
    acknowledged_by: int | None = Field(default=None)
    created_at: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class CharacteristicDefinition(SQLModel, table=True):
    __tablename__ = "characteristic_definitions"
    __table_args__ = (UniqueConstraint("target_type", "key"),)

    id: int | None = Field(default=None, primary_key=True)
    target_type: str = Field(nullable=False)
    key: str = Field(nullable=False)
    name: str = Field(nullable=False)
    data_type: str = Field(default="text", nullable=False)
    is_standard: int = Field(default=0, nullable=False)
    created_at: str = Field(nullable=False)


class CharacteristicValue(SQLModel, table=True):
    __tablename__ = "characteristic_values"
    __table_args__ = (UniqueConstraint("definition_id", "target_type", "target_id"),)

    id: int | None = Field(default=None, primary_key=True)
    definition_id: int = Field(nullable=False)
    target_type: str = Field(nullable=False)
    target_id: int = Field(nullable=False)
    value_text: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str = Field(nullable=False)
    updated_at: str = Field(nullable=False)


ID_TABLES = {
    "roles",
    "permissions",
    "users",
    "companies",
    "contracts",
    "license_types",
    "service_types",
    "flat_fee_types",
    "licenses",
    "services",
    "variable_costs",
    "flat_fees",
    "service_time_entries",
    "invoices",
    "invoice_line_items",
    "notifications",
    "characteristic_definitions",
    "characteristic_values",
}

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_contracts_company ON contracts(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_license_types_active ON license_types(active, name)",
    "CREATE INDEX IF NOT EXISTS idx_service_types_active ON service_types(active, name)",
    "CREATE INDEX IF NOT EXISTS idx_flat_fee_types_active ON flat_fee_types(active, name)",
    "CREATE INDEX IF NOT EXISTS idx_licenses_contract ON licenses(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_services_contract ON services(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_variable_costs_contract ON variable_costs(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_flat_fees_contract ON flat_fees(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_time_entries_contract ON service_time_entries(contract_id, work_date)",
    "CREATE INDEX IF NOT EXISTS idx_time_entries_invoice ON service_time_entries(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_time_entries_flat_fee ON service_time_entries(flat_fee_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_contract ON invoices(contract_id, period_start, period_end)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_ack ON notifications(acknowledged_at, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_target ON notifications(target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_characteristic_values_target ON characteristic_values(target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_licenses_type ON licenses(license_type_id)",
    "CREATE INDEX IF NOT EXISTS idx_services_type ON services(service_type_id)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_invoice_line_items_billing_key
        ON invoice_line_items(billing_key)
        WHERE billing_key IS NOT NULL AND billing_key != ''
    """,
)


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    INVOICE_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)


def ensure_database_exists() -> None:
    if not IS_POSTGRES:
        return

    database_name = engine.url.database
    if not database_name:
        return

    try:
        with engine.connect():
            return
    except OperationalError as exc:
        if "does not exist" not in str(exc).lower():
            raise

    maintenance_database = os.environ.get("POSTGRES_MAINTENANCE_DB", "postgres")
    maintenance_engine = create_engine(
        engine.url.set(database=maintenance_database),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
        connect_args=ENGINE_CONNECT_ARGS,
    )
    with maintenance_engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
            {"database_name": database_name},
        ).scalar()
        if not exists:
            quoted_database_name = maintenance_engine.dialect.identifier_preparer.quote(database_name)
            connection.execute(text(f"CREATE DATABASE {quoted_database_name}"))


class Row:
    def __init__(self, mapping: dict[str, Any]):
        self._data = dict(mapping)
        self._keys = list(self._data.keys())
        self._values = list(self._data.values())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._data[key]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self):
        return self._keys

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class Result:
    def __init__(self, rows: list[Row] | None = None, rowcount: int = -1, lastrowid: int | None = None):
        self._rows = rows or []
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Row | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[Row]:
        remaining = self._rows[self._index :]
        self._index = len(self._rows)
        return remaining


class Connection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql: str, params: Any = None) -> Result:
        translated, translated_params, insert_table = translate_sql(sql, params)
        if translated is None:
            return Result(rowcount=0)

        result = self._connection.execute(text(translated), translated_params)
        rows: list[Row] = []
        lastrowid = None
        if result.returns_rows:
            rows = [Row(dict(row)) for row in result.mappings().all()]
            if insert_table and rows and "id" in rows[0].keys():
                lastrowid = rows[0]["id"]
        return Result(rows=rows, rowcount=result.rowcount, lastrowid=lastrowid)

    def executemany(self, sql: str, param_sets: list[tuple[Any, ...]] | tuple[tuple[Any, ...], ...]) -> Result:
        last_result = Result(rowcount=0)
        for params in param_sets:
            last_result = self.execute(sql, params)
        return last_result


def replace_qmarks(sql: str, params: tuple[Any, ...] | list[Any]) -> tuple[str, dict[str, Any]]:
    values = list(params)
    pieces = sql.split("?")
    if len(pieces) == 1:
        return sql, {}
    if len(pieces) - 1 != len(values):
        raise ValueError("SQL parameter count does not match placeholders.")
    rebuilt = [pieces[0]]
    bind_params: dict[str, Any] = {}
    for index, value in enumerate(values):
        key = f"p{index}"
        rebuilt.append(f":{key}")
        rebuilt.append(pieces[index + 1])
        bind_params[key] = value
    return "".join(rebuilt), bind_params


def append_clause(sql: str, clause: str) -> str:
    stripped = sql.rstrip()
    suffix = ";" if stripped.endswith(";") else ""
    base = stripped[:-1].rstrip() if suffix else stripped
    return f"{base} {clause}{suffix}"


def returning_table(sql: str) -> str | None:
    match = re.match(r"\s*INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE)
    if not match:
        return None
    table = match.group(1)
    upper = sql.upper()
    if table not in ID_TABLES or "RETURNING" in upper or "ON CONFLICT" in upper:
        return None
    return table


def translate_sql(sql: str, params: Any = None) -> tuple[str | None, dict[str, Any], str | None]:
    normalized = sql.strip()
    if not normalized:
        return None, {}, None
    if not IS_SQLITE and normalized.upper().startswith("DELETE FROM SQLITE_SEQUENCE"):
        return None, {}, None

    changed_ignore = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", normalized, re.IGNORECASE))
    normalized = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", normalized, flags=re.IGNORECASE)
    if changed_ignore and "ON CONFLICT" not in normalized.upper():
        normalized = append_clause(normalized, "ON CONFLICT DO NOTHING")

    insert_table = returning_table(normalized)
    if insert_table:
        normalized = append_clause(normalized, "RETURNING id")

    if params is None:
        return normalized, {}, insert_table
    if isinstance(params, dict):
        return normalized, params, insert_table
    if isinstance(params, (tuple, list)):
        translated, bind_params = replace_qmarks(normalized, params)
        return translated, bind_params, insert_table
    return normalized, {"p0": params}, insert_table


@contextmanager
def connect():
    ensure_storage()
    with engine.begin() as raw_connection:
        yield Connection(raw_connection)


def init_db() -> None:
    ensure_storage()
    ensure_database_exists()
    SQLModel.metadata.create_all(engine)
    with connect() as connection:
        migrate_schema(connection)
        for statement in INDEX_STATEMENTS:
            connection.execute(statement)


def validate_identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", value):
        raise ValueError(f"Invalid SQL identifier: {value}")
    return value


def column_names(connection: Connection, table: str) -> set[str]:
    validate_identifier(table)
    if IS_POSTGRES:
        rows = connection.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ?
            """,
            (table,),
        ).fetchall()
        return {row["name"] for row in rows}
    if IS_SQLITE:
        rows = connection.execute(
            "SELECT name FROM pragma_table_info(?)",
            (table,),
        ).fetchall()
        return {row["name"] for row in rows}
    return {column["name"] for column in inspect(engine).get_columns(table)}


def add_column_if_missing(connection: Connection, table: str, column: str, definition: str) -> None:
    validate_identifier(table)
    validate_identifier(column)
    if column not in column_names(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def drop_not_null_if_postgres(connection: Connection, table: str, column: str) -> None:
    if IS_POSTGRES:
        validate_identifier(table)
        validate_identifier(column)
        connection.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")


def migrate_schema(connection: Connection) -> None:
    add_column_if_missing(connection, "companies", "contact_name", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "companies", "contact_email", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "companies", "contact_phone", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "companies", "billing_recipient_name", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "companies", "billing_recipient_email", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "companies", "billing_recipient_phone", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "companies", "customer_supplier_number", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "companies", "logo_original_filename", "TEXT")
    add_column_if_missing(connection, "companies", "logo_stored_filename", "TEXT")
    add_column_if_missing(connection, "licenses", "license_type_id", "INTEGER")
    add_column_if_missing(connection, "licenses", "billing_frequency", "TEXT")
    add_column_if_missing(connection, "licenses", "billing_strategy", "TEXT NOT NULL DEFAULT 'standard'")
    add_column_if_missing(connection, "licenses", "first_year_billing_frequency", "TEXT")
    add_column_if_missing(connection, "licenses", "renewal_billing_frequency", "TEXT")
    add_column_if_missing(connection, "services", "service_type_id", "INTEGER")
    add_column_if_missing(connection, "services", "contracted_hours", "REAL")
    add_column_if_missing(connection, "services", "billing_frequency", "TEXT")
    add_column_if_missing(connection, "flat_fees", "flat_fee_type_id", "INTEGER")
    add_column_if_missing(connection, "flat_fees", "fee_kind", "TEXT NOT NULL DEFAULT 'work_package'")
    add_column_if_missing(connection, "flat_fees", "success_condition", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "flat_fees", "expected_success_date", "TEXT")
    add_column_if_missing(connection, "flat_fees", "success_date", "TEXT")
    add_column_if_missing(connection, "flat_fees", "approval_status", "TEXT NOT NULL DEFAULT 'not_applicable'")
    add_column_if_missing(connection, "flat_fees", "approved_by", "INTEGER")
    add_column_if_missing(connection, "flat_fees", "approved_at", "TEXT")
    add_column_if_missing(connection, "contracts", "variable_billing_frequency", "TEXT NOT NULL DEFAULT 'monthly'")
    add_column_if_missing(connection, "contracts", "license_price_increase_percent", "TEXT NOT NULL DEFAULT '8'")
    add_column_if_missing(connection, "contracts", "vat_treatment", "TEXT NOT NULL DEFAULT 'standard'")
    add_column_if_missing(connection, "contracts", "contact_name", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "contracts", "contact_email", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "contracts", "contact_phone", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "contracts", "billing_recipient_name", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "contracts", "billing_recipient_email", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "contracts", "billing_recipient_phone", "TEXT NOT NULL DEFAULT ''")
    add_column_if_missing(connection, "contracts", "customer_order_number", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "contracts", "erp_reference_number", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "service_time_entries", "start_date", "TEXT")
    add_column_if_missing(connection, "service_time_entries", "end_date", "TEXT")
    add_column_if_missing(connection, "service_time_entries", "flat_fee_id", "INTEGER")
    drop_not_null_if_postgres(connection, "service_time_entries", "service_id")
    add_column_if_missing(connection, "invoices", "include_variable_costs", "INTEGER NOT NULL DEFAULT 1")
    add_column_if_missing(connection, "invoices", "include_flat_fees", "INTEGER NOT NULL DEFAULT 1")
    add_column_if_missing(connection, "invoices", "datev_invoice_number", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "invoices", "datev_invoice_date", "TEXT")
    add_column_if_missing(connection, "invoices", "subtotal_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "invoices", "discount_type", "TEXT NOT NULL DEFAULT 'none'")
    add_column_if_missing(connection, "invoices", "discount_value", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "invoices", "discount_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "invoices", "vat_treatment", "TEXT NOT NULL DEFAULT 'standard'")
    add_column_if_missing(connection, "invoices", "vat_rate_percent", "TEXT NOT NULL DEFAULT '19'")
    add_column_if_missing(connection, "invoices", "vat_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "invoices", "gross_total_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "invoices", "invoice_document_original_filename", "TEXT")
    add_column_if_missing(connection, "invoices", "invoice_document_stored_filename", "TEXT")
    add_column_if_missing(connection, "variable_costs", "datev_account", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "variable_costs", "billing_frequency", "TEXT")
    add_column_if_missing(connection, "invoice_line_items", "datev_account", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "invoice_line_items", "billing_key", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "invoice_line_items", "subtotal_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(connection, "invoice_line_items", "discount_type", "TEXT NOT NULL DEFAULT 'none'")
    add_column_if_missing(connection, "invoice_line_items", "discount_value", "TEXT DEFAULT ''")
    add_column_if_missing(connection, "invoice_line_items", "discount_cents", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(
        connection,
        "characteristic_definitions",
        "is_standard",
        "INTEGER NOT NULL DEFAULT 0",
    )
    connection.execute(
        """
        UPDATE service_time_entries
        SET start_date = work_date
        WHERE start_date IS NULL OR start_date = ''
        """
    )
    connection.execute(
        """
        UPDATE service_time_entries
        SET end_date = work_date
        WHERE end_date IS NULL OR end_date = ''
        """
    )
    connection.execute(
        """
        UPDATE invoices
        SET subtotal_cents = total_cents + COALESCE(discount_cents, 0)
        WHERE subtotal_cents IS NULL OR subtotal_cents = 0
        """
    )
    connection.execute(
        """
        UPDATE invoices
        SET discount_type = 'none'
        WHERE discount_type IS NULL OR discount_type = ''
        """
    )
    connection.execute(
        """
        UPDATE invoice_line_items
        SET subtotal_cents = amount_cents + COALESCE(discount_cents, 0)
        WHERE subtotal_cents IS NULL OR subtotal_cents = 0
        """
    )
    connection.execute(
        """
        UPDATE invoice_line_items
        SET discount_type = 'none'
        WHERE discount_type IS NULL OR discount_type = ''
        """
    )
    connection.execute(
        """
        UPDATE invoices
        SET gross_total_cents = total_cents + vat_cents
        WHERE gross_total_cents IS NULL OR gross_total_cents = 0
        """
    )
    connection.execute(
        """
        UPDATE licenses
        SET billing_frequency = (
            SELECT contracts.license_billing_frequency
            FROM contracts
            WHERE contracts.id = licenses.contract_id
        )
        WHERE billing_frequency IS NULL OR billing_frequency = ''
        """
    )
    connection.execute(
        """
        UPDATE licenses
        SET billing_strategy = 'standard'
        WHERE billing_strategy IS NULL OR billing_strategy = ''
        """
    )
    connection.execute(
        """
        UPDATE licenses
        SET first_year_billing_frequency = billing_frequency
        WHERE first_year_billing_frequency IS NULL OR first_year_billing_frequency = ''
        """
    )
    connection.execute(
        """
        UPDATE licenses
        SET renewal_billing_frequency = billing_frequency
        WHERE renewal_billing_frequency IS NULL OR renewal_billing_frequency = ''
        """
    )
    connection.execute(
        """
        UPDATE services
        SET billing_frequency = (
            SELECT contracts.service_billing_frequency
            FROM contracts
            WHERE contracts.id = services.contract_id
        )
        WHERE billing_frequency IS NULL OR billing_frequency = ''
        """
    )
    connection.execute(
        """
        UPDATE variable_costs
        SET billing_frequency = (
            SELECT contracts.variable_billing_frequency
            FROM contracts
            WHERE contracts.id = variable_costs.contract_id
        )
        WHERE billing_frequency IS NULL OR billing_frequency = ''
        """
    )
    connection.execute(
        """
        UPDATE flat_fees
        SET fee_kind = 'work_package'
        WHERE fee_kind IS NULL OR fee_kind = ''
        """
    )
    connection.execute(
        """
        UPDATE flat_fees
        SET approval_status = 'not_applicable'
        WHERE fee_kind != 'success_bonus'
          AND (approval_status IS NULL OR approval_status = '' OR approval_status = 'pending')
        """
    )
    connection.execute(
        """
        UPDATE flat_fees
        SET approval_status = 'pending'
        WHERE fee_kind = 'success_bonus'
          AND (approval_status IS NULL OR approval_status = '' OR approval_status = 'not_applicable')
        """
    )
    connection.execute(
        """
        UPDATE service_time_entries
        SET invoice_id = NULL,
            status = CASE WHEN status = 'billed' THEN 'approved' ELSE status END
        WHERE invoice_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM invoices
              WHERE invoices.id = service_time_entries.invoice_id
          )
        """
    )
    connection.execute(
        """
        DELETE FROM invoice_time_entries
        WHERE NOT EXISTS (
            SELECT 1
            FROM invoices
            WHERE invoices.id = invoice_time_entries.invoice_id
        )
        """
    )
    connection.execute(
        """
        DELETE FROM invoice_line_items
        WHERE NOT EXISTS (
            SELECT 1
            FROM invoices
            WHERE invoices.id = invoice_line_items.invoice_id
        )
        """
    )
