from __future__ import annotations

import calendar
from html import escape as html_escape
from html.parser import HTMLParser
from io import BytesIO
import logging
import os
import secrets
import shutil
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import database
from .excel_export import create_database_export_workbook
from .security import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    generate_password,
    hash_password,
    read_session_token,
    verify_password,
)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
logger = logging.getLogger(__name__)


def bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def list_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


ENABLE_SECURITY_HEADERS = bool_env("ENABLE_SECURITY_HEADERS", True)
FORCE_HTTPS = bool_env("FORCE_HTTPS", False)
SECURE_COOKIES = bool_env("SECURE_COOKIES", FORCE_HTTPS)
ALLOWED_HOSTS = list_env("ALLOWED_HOSTS", "*")
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()


def validate_runtime_security() -> None:
    if APP_ENV != "production":
        return
    weak_values = {
        "SESSION_SECRET": {"", "change-me-in-production", "change-me-before-production", "dev-change-this-session-secret"},
        "ADMIN_PASSWORD": {"", "admin", "admin123", "change-me-before-first-start"},
        "POSTGRES_PASSWORD": {"", "password"},
    }
    for name, weak in weak_values.items():
        value = os.environ.get(name, "")
        if value in weak:
            raise RuntimeError(f"{name} must be changed before production deployment.")
    if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
        raise RuntimeError("ALLOWED_HOSTS must be set to concrete host names in production.")

PERMISSIONS = [
    ("companies.view", "Unternehmen ansehen", "Unternehmen und Vertragsuebersichten ansehen."),
    ("companies.manage", "Unternehmen verwalten", "Unternehmen anlegen und bearbeiten."),
    ("contracts.view", "Vertraege ansehen", "Vertraege, Lizenzen, Dienstleistungen und Pauschalen ansehen."),
    ("contracts.manage", "Vertraege verwalten", "Vertraege, Lizenzen, Dienstleistungen und Pauschalen pflegen."),
    ("time.create", "Stunden erfassen", "Eigene Aufwaende erfassen."),
    ("time.approve", "Stunden freigeben", "Aufwaende fuer Controlling und Abrechnung freigeben."),
    ("billing.create", "Rechnungen erstellen", "Rechnungen erzeugen und finalisieren."),
    ("analytics.view", "Analytics ansehen", "Auswertungen und betriebliche Kennzahlen ansehen."),
    ("catalog.manage", "Katalog verwalten", "Lizenz-, Dienstleistungs- und Pauschalarten inklusive DATEV-Konten pflegen."),
    ("characteristics.manage", "Charakteristiken verwalten", "Dynamische Merkmale definieren und pflegen."),
    ("settings.manage", "Einstellungen verwalten", "Softwareweite Abrechnungs- und Anzeigeeinstellungen pflegen."),
    ("users.manage", "Benutzer verwalten", "Benutzer und Rollen pflegen."),
]

ROLE_DEFINITIONS = [
    ("admin", "Superadmin", "Voller Zugriff auf das System inklusive Loeschrechten."),
    ("manager", "Manager", "Operativer Zugriff ohne Verwaltung."),
    ("consultant", "Consultant", "Ausschliesslich Erfassung eigener Dienstleistungsstunden."),
]

ACTIVE_ROLE_NAMES = ("admin", "manager", "consultant")

ROLE_PERMISSIONS = {
    "admin": [key for key, _, _ in PERMISSIONS],
    "manager": [
        "companies.view",
        "companies.manage",
        "contracts.view",
        "contracts.manage",
        "time.create",
        "time.approve",
        "billing.create",
    ],
    "consultant": ["time.create"],
}

TARGET_TYPES = {
    "company": "Unternehmen",
    "contract": "Vertrag",
    "license": "Lizenz",
    "service": "Dienstleistung",
    "flat_fee": "Pauschale",
}

TARGET_TABLES = {
    "company": "companies",
    "contract": "contracts",
    "license": "licenses",
    "service": "services",
    "flat_fee": "flat_fees",
}

DATA_TYPES = {
    "text": "Text",
    "number": "Zahl",
    "date": "Datum",
    "boolean": "Ja/Nein",
}

BILLING_FREQUENCIES = {
    "once": {"label": "Einmalig", "months": 0, "periods_per_year": 1},
    "monthly": {"label": "Monatlich", "months": 1, "periods_per_year": 12},
    "quarterly": {"label": "Vierteljaehrlich", "months": 3, "periods_per_year": 4},
    "semiannual": {"label": "Halbjaehrlich", "months": 6, "periods_per_year": 2},
    "annual": {"label": "Jaehrlich", "months": 12, "periods_per_year": 1},
}

BILLING_STRATEGIES = {
    "standard": {"label": "Standard"},
    "first_year_then_renewal": {"label": "1. Lizenzjahr abweichend"},
}

VAT_TREATMENTS = {
    "standard": "Mit Umsatzsteuer",
    "no_vat": "Ohne Umsatzsteuer",
}

FLAT_FEE_KINDS = {
    "work_package": "Arbeitspaket / Festpreis",
    "success_bonus": "Erfolgsbonus",
}

FLAT_FEE_APPROVAL_STATUSES = {
    "not_applicable": "Nicht erforderlich",
    "pending": "Offen",
    "approved": "Freigegeben",
    "rejected": "Verworfen",
}

STATUS_LABELS = {
    "active": "Aktiv",
    "draft": "Entwurf",
    "paused": "Pausiert",
    "ended": "Beendet",
    "submitted": "Eingereicht",
    "pending": "Offen",
    "approved": "Freigegeben",
    "rejected": "Verworfen",
    "billed": "Abgerechnet",
    "finalized": "Finalisiert",
    "inactive": "Inaktiv",
}

CONTRACT_ITEM_STATUSES = ("active", "paused", "ended", "inactive")
COMPANY_STATUSES = ("active", "paused", "inactive")
CONTRACT_STATUSES = ("active", "draft", "paused", "ended")
TIME_ENTRY_EDIT_STATUSES = ("submitted", "approved")

LOGO_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

DEFAULT_LICENSE_TYPES = [
    ("Cloud Suite Core", "4400", "Wiederkehrende SaaS-Kernlizenz"),
    ("Analytics Modul", "4401", "Lizenz fuer Reporting- und Analytics-Funktionen"),
    ("Compliance Reporting Seats", "4402", "Benutzerbasierte Compliance-Lizenz"),
    ("API Add-on", "4403", "Technisches Integrationsmodul"),
]

DEFAULT_SERVICE_TYPES = [
    ("Fachberatung", "8400", 15000, "Beratungsleistung nach Aufwand"),
    ("Integration", "8401", 17500, "Technische Integrationsleistung"),
    ("Regulatorischer Support", "8402", 16500, "Support fuer Audit, Compliance und Reporting"),
    ("Projektmanagement", "8403", 14000, "Projektsteuerung und Koordination"),
]

DEFAULT_LICENSE_TYPE_NAMES = {name for name, _, _ in DEFAULT_LICENSE_TYPES}
DEFAULT_SERVICE_TYPE_NAMES = {name for name, _, _, _ in DEFAULT_SERVICE_TYPES}

DEFAULT_SETTINGS = {
    "license_billable_lead_days": "30",
    "billing_rate_unit": "day",
    "workday_hours": "8",
    "contract_end_notification_days": "90",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def date_de(value: str | date | None) -> str:
    if value is None or value == "":
        return "-"
    parsed = parse_iso_date(value) if isinstance(value, str) else value
    return parsed.strftime("%d.%m.%Y")


def datetime_de(value: str | None) -> str:
    if not value:
        return "-"
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone().strftime("%d.%m.%Y %H:%M")


def parse_amount_to_cents(value: str) -> int:
    normalized = value.strip().replace(" ", "")
    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")

    try:
        amount = Decimal(normalized).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Bitte einen gueltigen Betrag eingeben.") from exc

    if amount < 0:
        raise ValueError("Der Betrag darf nicht negativ sein.")

    return int(amount * 100)


def parse_hours(value: str) -> Decimal:
    normalized = value.strip().replace(",", ".")
    try:
        hours = Decimal(normalized).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Bitte einen gueltigen Aufwand eingeben.") from exc
    if hours <= 0:
        raise ValueError("Aufwand muss groesser als 0 sein.")
    return hours


def parse_optional_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().replace(",", ".")
    try:
        amount = Decimal(normalized).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Bitte eine gueltige Zahl eingeben.") from exc
    if amount < 0:
        raise ValueError("Der Wert darf nicht negativ sein.")
    return amount


def parse_optional_positive_decimal(value: str | None, default: Decimal | None = None) -> Decimal | None:
    amount = parse_optional_decimal(value)
    if amount is None:
        return default
    if amount <= 0:
        raise ValueError("Der Wert muss groesser als 0 sein.")
    return amount


def parse_percent(value: str | None, default: Decimal = Decimal("0")) -> Decimal:
    amount = parse_optional_decimal(value)
    if amount is None:
        amount = default
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def required_text(value: str | None, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} ist verpflichtend.")
    return cleaned


RICH_TEXT_ALLOWED_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li", "a", "div"}
RICH_TEXT_VOID_TAGS = {"br"}
RICH_TEXT_ALLOWED_PROTOCOLS = ("http://", "https://", "mailto:")


class RichTextSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in RICH_TEXT_ALLOWED_TAGS:
            return
        if tag == "a":
            href = ""
            for key, value in attrs:
                if key.lower() == "href" and value:
                    candidate = value.strip()
                    lower_candidate = candidate.lower()
                    if lower_candidate.startswith(RICH_TEXT_ALLOWED_PROTOCOLS) or candidate.startswith(("/", "#")):
                        href = candidate
                    break
            if href:
                self.parts.append(
                    f'<a href="{html_escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
                )
            else:
                self.parts.append("<a>")
            return
        if tag in RICH_TEXT_VOID_TAGS:
            self.parts.append(f"<{tag}>")
            return
        self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in RICH_TEXT_VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth or tag not in RICH_TEXT_ALLOWED_TAGS or tag in RICH_TEXT_VOID_TAGS:
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(html_escape(data))

    def sanitized(self) -> str:
        return "".join(self.parts)


def sanitize_rich_text(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    lowered = text.lower()
    has_allowed_markup = any(f"<{tag}" in lowered or f"</{tag}" in lowered for tag in RICH_TEXT_ALLOWED_TAGS)
    if not has_allowed_markup:
        return html_escape(text).replace("\n", "<br>")

    sanitizer = RichTextSanitizer()
    sanitizer.feed(text)
    sanitizer.close()
    return sanitizer.sanitized().strip()


def rich_text(value: str | None) -> Markup:
    return Markup(sanitize_rich_text(value) or "-")


def normalize_characteristic_key(value: str | None) -> str:
    cleaned = required_text(value, "Schluessel").lower().replace(" ", "_")
    if not all(character.isalnum() or character == "_" for character in cleaned):
        raise ValueError("Der Schluessel darf nur Buchstaben, Zahlen und Unterstriche enthalten.")
    return cleaned


def money(value: int | None, currency: str = "EUR") -> str:
    cents = value or 0
    amount = cents / 100
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} {currency}"


def amount_input(value: int | None) -> str:
    amount = Decimal(value or 0) / Decimal(100)
    return f"{amount:.2f}".replace(".", ",")


def hours_de(value: float | Decimal | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(amount).replace(".", ",")


def ensure_default_settings() -> None:
    timestamp = now_iso()
    with database.connect() as connection:
        for key, value in DEFAULT_SETTINGS.items():
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, value, timestamp),
            )


def app_settings() -> dict[str, Any]:
    with database.connect() as connection:
        rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
    values = {**DEFAULT_SETTINGS, **{row["key"]: row["value"] for row in rows}}
    try:
        lead_days = max(0, int(values["license_billable_lead_days"]))
    except (TypeError, ValueError):
        lead_days = int(DEFAULT_SETTINGS["license_billable_lead_days"])
    try:
        contract_end_notification_days = max(0, int(values["contract_end_notification_days"]))
    except (TypeError, ValueError):
        contract_end_notification_days = int(DEFAULT_SETTINGS["contract_end_notification_days"])
    rate_unit = values["billing_rate_unit"] if values["billing_rate_unit"] in {"hour", "day"} else "day"
    try:
        workday_hours = Decimal(str(values["workday_hours"]).replace(",", ".")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError):
        workday_hours = Decimal(DEFAULT_SETTINGS["workday_hours"])
    if workday_hours <= 0:
        workday_hours = Decimal(DEFAULT_SETTINGS["workday_hours"])
    return {
        "license_billable_lead_days": lead_days,
        "billing_rate_unit": rate_unit,
        "workday_hours": workday_hours,
        "contract_end_notification_days": contract_end_notification_days,
    }


def rate_unit_label() -> str:
    return "Tagessatz" if app_settings()["billing_rate_unit"] == "day" else "Stundensatz"


def rate_cents_for_display(hourly_rate_cents: int | None) -> int:
    settings = app_settings()
    cents = Decimal(hourly_rate_cents or 0)
    if settings["billing_rate_unit"] == "day":
        cents *= settings["workday_hours"]
    return cents_from_decimal(cents)


def rate_money(hourly_rate_cents: int | None, currency: str = "EUR") -> str:
    return money(rate_cents_for_display(hourly_rate_cents), currency)


def rate_input(hourly_rate_cents: int | None) -> str:
    return amount_input(rate_cents_for_display(hourly_rate_cents))


def work_quantity_text(hours: float | Decimal | None) -> str:
    settings = app_settings()
    hour_value = Decimal(str(hours or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if settings["billing_rate_unit"] == "day":
        days = (hour_value / settings["workday_hours"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{hours_de(days)} AT"
    return f"{hours_de(hour_value)} h"


def work_amount_input(hours: float | Decimal | None) -> str:
    settings = app_settings()
    hour_value = Decimal(str(hours or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if settings["billing_rate_unit"] == "day":
        return hours_de(hour_value / settings["workday_hours"])
    return hours_de(hour_value)


def parse_work_amount_to_hours(value: str) -> Decimal:
    amount = parse_hours(value)
    settings = app_settings()
    if settings["billing_rate_unit"] == "day":
        return (amount * settings["workday_hours"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return amount


def parse_rate_to_hourly_cents(value: str) -> int:
    cents = Decimal(parse_amount_to_cents(value))
    settings = app_settings()
    if settings["billing_rate_unit"] == "day":
        cents = cents / settings["workday_hours"]
    return cents_from_decimal(cents)


def validate_contract_item_status(value: str) -> str:
    if value not in CONTRACT_ITEM_STATUSES:
        raise ValueError("Bitte einen gueltigen Status auswaehlen.")
    return value


def validate_flat_fee_kind(value: str | None) -> str:
    selected = (value or "work_package").strip()
    if selected not in FLAT_FEE_KINDS:
        raise ValueError("Bitte eine gueltige Pauschalenart auswaehlen.")
    return selected


def validate_flat_fee_approval_status(value: str | None, fee_kind: str) -> str:
    if fee_kind != "success_bonus":
        return "not_applicable"
    selected = (value or "pending").strip()
    if selected == "not_applicable":
        return "pending"
    if selected not in {"pending", "approved", "rejected"}:
        raise ValueError("Bitte einen gueltigen Freigabestatus auswaehlen.")
    return selected


def frequency_label(value: str | None) -> str:
    if not value:
        return "-"
    return BILLING_FREQUENCIES.get(value, {}).get("label", value)


def billing_strategy_label(value: str | None) -> str:
    if not value:
        return BILLING_STRATEGIES["standard"]["label"]
    return BILLING_STRATEGIES.get(value, {}).get("label", value)


def vat_treatment_label(value: str | None) -> str:
    return VAT_TREATMENTS.get(value or "standard", value or "-")


def flat_fee_kind_label(value: str | None) -> str:
    return FLAT_FEE_KINDS.get(value or "work_package", value or "-")


def flat_fee_approval_label(value: str | None) -> str:
    return FLAT_FEE_APPROVAL_STATUSES.get(value or "not_applicable", value or "-")


def validate_vat_treatment(value: str | None) -> str:
    selected = (value or "standard").strip()
    if selected not in VAT_TREATMENTS:
        raise ValueError("Bitte eine gueltige Umsatzsteuer-Option auswaehlen.")
    return selected


def validate_billing_frequency(value: str, fallback: str | None = None) -> str:
    selected = (value or fallback or "").strip()
    if selected not in BILLING_FREQUENCIES:
        raise ValueError("Bitte eine gueltige Abrechnungsmodalitaet auswaehlen.")
    return selected


def normalize_flat_fee_fields(
    fee_kind: str | None,
    billing_frequency: str | None,
    success_condition: str | None,
    expected_success_date: str | None,
    success_date: str | None,
    approval_status: str | None,
) -> dict[str, Any]:
    item_kind = validate_flat_fee_kind(fee_kind)
    item_billing_frequency = validate_billing_frequency(billing_frequency or "", "once")
    cleaned_success_condition = (success_condition or "").strip()
    requested_approval_status = (approval_status or "").strip()
    if item_kind != "success_bonus" and requested_approval_status in {"pending", "approved", "rejected"}:
        item_kind = "success_bonus"
    try:
        parsed_expected_success_date = parse_iso_date(expected_success_date)
        parsed_success_date = parse_iso_date(success_date)
    except ValueError as exc:
        raise ValueError("Bitte gueltige Erfolgsdaten eingeben.") from exc
    item_approval_status = validate_flat_fee_approval_status(approval_status, item_kind)

    if item_kind == "success_bonus":
        if not cleaned_success_condition:
            raise ValueError("Erfolgsbedingung ist fuer Erfolgsboni verpflichtend.")
        item_billing_frequency = "once"
        if item_approval_status == "approved" and parsed_success_date is None:
            parsed_success_date = date.today()
    else:
        cleaned_success_condition = ""
        parsed_expected_success_date = None
        parsed_success_date = None
        item_approval_status = "not_applicable"

    return {
        "fee_kind": item_kind,
        "billing_frequency": item_billing_frequency,
        "success_condition": cleaned_success_condition,
        "expected_success_date": parsed_expected_success_date,
        "success_date": parsed_success_date,
        "approval_status": item_approval_status,
    }


def validate_billing_strategy(value: str | None) -> str:
    selected = (value or "standard").strip()
    if selected not in BILLING_STRATEGIES:
        raise ValueError("Bitte eine gueltige Abrechnungsstrategie auswaehlen.")
    return selected


def normalize_license_billing_config(
    billing_strategy: str | None,
    billing_frequency: str | None,
    first_year_billing_frequency: str | None,
    renewal_billing_frequency: str | None,
    fallback: str,
) -> tuple[str, str, str, str]:
    base_frequency = validate_billing_frequency(billing_frequency or "", fallback)
    strategy = validate_billing_strategy(billing_strategy)
    first_year_frequency = validate_billing_frequency(first_year_billing_frequency or "", base_frequency)
    renewal_frequency = validate_billing_frequency(renewal_billing_frequency or "", base_frequency)
    if strategy == "standard":
        first_year_frequency = base_frequency
        renewal_frequency = base_frequency
    return strategy, base_frequency, first_year_frequency, renewal_frequency


def license_billing_strategy_summary(item: dict[str, Any]) -> str:
    strategy = item.get("billing_strategy") or "standard"
    base_frequency = item.get("billing_frequency")
    if strategy == "first_year_then_renewal":
        first_year_frequency = item.get("first_year_billing_frequency") or base_frequency
        renewal_frequency = item.get("renewal_billing_frequency") or base_frequency
        return (
            f"1. Jahr: {frequency_label(first_year_frequency)} / "
            f"Folgejahre: {frequency_label(renewal_frequency)}"
        )
    return frequency_label(base_frequency)


def append_description_detail(description: str, detail: str | None) -> str:
    clean_description = (description or "").strip()
    clean_detail = (detail or "").strip()
    if not clean_detail or clean_detail in clean_description:
        return clean_description
    if not clean_description:
        return clean_detail
    return f"{clean_description} - {clean_detail}"


def billing_position_description(name: str, detail: str | None, *suffixes: str) -> str:
    parts = [append_description_detail(name, detail)]
    parts.extend(suffix for suffix in suffixes if suffix)
    return " - ".join(parts)


def status_label(value: str | None) -> str:
    if not value:
        return "-"
    return STATUS_LABELS.get(value, value)


ITEM_TYPE_LABELS = {
    "license": "Lizenz",
    "service": "Dienstleistung",
    "variable_cost": "Variabler Kostensatz",
    "flat_fee": "Pauschale",
}


def item_type_label(value: str | None) -> str:
    if not value:
        return "-"
    return ITEM_TYPE_LABELS.get(value, value)


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def cents_from_decimal(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


CONTRACT_SECTION_TABS = {
    "licenses": "licenses",
    "services": "services",
    "variable-costs": "variable_costs",
    "flat-fees": "flat_fees",
}


def contract_section_path(contract_id: int | str, section: str | None = None) -> str:
    tab = CONTRACT_SECTION_TABS.get(section or "")
    return f"/contracts/{contract_id}?tab={tab}" if tab else f"/contracts/{contract_id}"


def upload_has_file(document: UploadFile | None) -> bool:
    return bool(document and document.filename)


def save_contract_pdf(document: UploadFile | None) -> tuple[str | None, str | None]:
    if not upload_has_file(document):
        return None, None

    original_filename = Path(document.filename or "").name
    extension = Path(original_filename).suffix.lower()
    if extension != ".pdf":
        raise ValueError("Bitte ein Vertragsdokument als PDF hochladen.")

    stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(8)}.pdf"
    target = database.UPLOAD_DIR / stored_filename
    with target.open("wb") as buffer:
        shutil.copyfileobj(document.file, buffer)
    return original_filename, stored_filename


def save_company_logo(logo: UploadFile | None) -> tuple[str | None, str | None]:
    if not upload_has_file(logo):
        return None, None

    original_filename = Path(logo.filename or "").name
    extension = Path(original_filename).suffix.lower()
    if extension not in LOGO_EXTENSIONS:
        raise ValueError("Bitte ein gueltiges Logo als PNG, JPG oder WebP hochladen.")

    stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(8)}{extension}"
    target = database.LOGO_DIR / stored_filename
    with target.open("wb") as buffer:
        shutil.copyfileobj(logo.file, buffer)
    return original_filename, stored_filename


def save_invoice_document(document: UploadFile | None) -> tuple[str | None, str | None]:
    if not upload_has_file(document):
        return None, None

    original_filename = Path(document.filename or "").name
    extension = Path(original_filename).suffix.lower()
    if extension != ".pdf":
        raise ValueError("Bitte das Rechnungsdokument als PDF hochladen.")

    stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(8)}.pdf"
    target = database.INVOICE_DOCUMENT_DIR / stored_filename
    with target.open("wb") as buffer:
        shutil.copyfileobj(document.file, buffer)
    return original_filename, stored_filename


def logo_media_type(filename: str) -> str:
    return LOGO_EXTENSIONS.get(Path(filename).suffix.lower(), "application/octet-stream")


def role_and_permission_ids(connection) -> tuple[dict[str, int], dict[str, int]]:
    roles = {
        row["name"]: row["id"]
        for row in connection.execute("SELECT id, name FROM roles").fetchall()
    }
    permissions = {
        row["key"]: row["id"]
        for row in connection.execute("SELECT id, key FROM permissions").fetchall()
    }
    return roles, permissions


def ensure_roles_permissions() -> None:
    with database.connect() as connection:
        for name, label, description in ROLE_DEFINITIONS:
            connection.execute(
                """
                INSERT INTO roles (name, label, description)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET label = excluded.label, description = excluded.description
                """,
                (name, label, description),
            )

        for key, label, description in PERMISSIONS:
            connection.execute(
                """
                INSERT INTO permissions (key, label, description)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET label = excluded.label, description = excluded.description
                """,
                (key, label, description),
            )

        roles, permissions = role_and_permission_ids(connection)
        if "viewer" in roles and "consultant" in roles:
            connection.execute(
                "UPDATE users SET role_id = ? WHERE role_id = ?",
                (roles["consultant"], roles["viewer"]),
            )
            connection.execute("DELETE FROM role_permissions WHERE role_id = ?", (roles["viewer"],))
            connection.execute("DELETE FROM roles WHERE id = ?", (roles["viewer"],))
            roles, permissions = role_and_permission_ids(connection)

        active_role_ids = [roles[name] for name in ACTIVE_ROLE_NAMES if name in roles]
        if active_role_ids:
            placeholders = ",".join("?" for _ in active_role_ids)
            connection.execute(
                f"DELETE FROM role_permissions WHERE role_id NOT IN ({placeholders})",
                tuple(active_role_ids),
            )

        for role_name, permission_keys in ROLE_PERMISSIONS.items():
            role_id = roles.get(role_name)
            if role_id is None:
                continue
            connection.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
            for permission_key in permission_keys:
                permission_id = permissions.get(permission_key)
                if permission_id is None:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO role_permissions (role_id, permission_id)
                    VALUES (?, ?)
                    """,
                    (role_id, permission_id),
                )


def ensure_catalog_data() -> None:
    timestamp = now_iso()
    with database.connect() as connection:
        for name, datev_account, description in DEFAULT_LICENSE_TYPES:
            connection.execute(
                """
                INSERT INTO license_types (name, datev_account, description, active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    datev_account = excluded.datev_account,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (name, datev_account, description, timestamp, timestamp),
            )

        for name, datev_account, default_rate_cents, description in DEFAULT_SERVICE_TYPES:
            connection.execute(
                """
                INSERT INTO service_types (
                    name, datev_account, default_hourly_rate_cents,
                    description, active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    datev_account = excluded.datev_account,
                    default_hourly_rate_cents = excluded.default_hourly_rate_cents,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (name, datev_account, default_rate_cents, description, timestamp, timestamp),
            )

        connection.execute(
            """
            UPDATE licenses
            SET license_type_id = (
                SELECT license_types.id
                FROM license_types
                WHERE license_types.name = licenses.name
            )
            WHERE license_type_id IS NULL
              AND EXISTS (
                SELECT 1
                FROM license_types
                WHERE license_types.name = licenses.name
              )
            """
        )
        connection.execute(
            """
            UPDATE services
            SET service_type_id = (
                SELECT service_types.id
                FROM service_types
                WHERE service_types.name = services.name
            )
            WHERE service_type_id IS NULL
              AND EXISTS (
                SELECT 1
                FROM service_types
                WHERE service_types.name = services.name
              )
            """
        )
        connection.execute(
            """
            UPDATE invoice_line_items
            SET datev_account = COALESCE((
                SELECT license_types.datev_account
                FROM licenses
                JOIN license_types ON license_types.id = licenses.license_type_id
                WHERE invoice_line_items.item_type = 'license'
                  AND licenses.id = invoice_line_items.source_id
            ), datev_account)
            WHERE item_type = 'license'
              AND (datev_account IS NULL OR datev_account = '')
            """
        )
        connection.execute(
            """
            UPDATE invoice_line_items
            SET datev_account = COALESCE((
                SELECT service_types.datev_account
                FROM services
                JOIN service_types ON service_types.id = services.service_type_id
                WHERE invoice_line_items.item_type = 'service'
                  AND services.id = invoice_line_items.source_id
            ), datev_account)
            WHERE item_type = 'service'
              AND (datev_account IS NULL OR datev_account = '')
            """
        )


def license_type_id_by_name(connection, name: str) -> int:
    row = connection.execute(
        "SELECT id FROM license_types WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Lizenzart fehlt im Katalog: {name}")
    return row["id"]


def service_type_id_by_name(connection, name: str) -> int:
    row = connection.execute(
        "SELECT id FROM service_types WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Dienstleistungsart fehlt im Katalog: {name}")
    return row["id"]


def create_user_if_missing(
    connection,
    username: str,
    password: str,
    full_name: str,
    email: str,
    role_name: str,
) -> int:
    roles, _ = role_and_permission_ids(connection)
    existing = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if existing:
        return existing["id"]

    cursor = connection.execute(
        """
        INSERT INTO users (username, password_hash, full_name, email, role_id, active, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (
            username,
            hash_password(password),
            full_name,
            email,
            roles[role_name],
            now_iso(),
        ),
    )
    return cursor.lastrowid


def write_demo_pdf(filename: str, title: str) -> str:
    database.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = filename
    target = database.UPLOAD_DIR / stored_filename
    if not target.exists():
        safe_title = title.replace("(", "").replace(")", "")
        target.write_bytes(
            (
                "%PDF-1.4\n"
                "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
                "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
                "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
                f"4 0 obj << /Length 86 >> stream\nBT /F1 18 Tf 72 760 Td ({safe_title}) Tj "
                "0 -32 Td (Demo-Vertragsdokument) Tj ET\nendstream endobj\n"
                "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
                "xref\n0 6\n0000000000 65535 f \n"
                "0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
                "0000000250 00000 n \n0000000386 00000 n \n"
                "trailer << /Size 6 /Root 1 0 R >>\nstartxref\n456\n%%EOF\n"
            ).encode("ascii")
        )
    return stored_filename


def ensure_admin_user() -> None:
    with database.connect() as connection:
        create_user_if_missing(
            connection,
            os.environ.get("ADMIN_USERNAME", "admin"),
            os.environ.get("ADMIN_PASSWORD", "admin123"),
            "System Administrator",
            "admin@example.local",
            "admin",
        )


def ensure_sample_data() -> None:
    if os.environ.get("ENABLE_SAMPLE_DATA") != "1":
        return

    with database.connect() as connection:
        company_count = connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        if company_count > 0:
            return

        timestamp = now_iso()
        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")

        admin_id = create_user_if_missing(
            connection,
            admin_username,
            admin_password,
            "System Administrator",
            "admin@example.local",
            "admin",
        )
        manager_id = create_user_if_missing(
            connection,
            "manager",
            "manager123",
            "Mara Manager",
            "manager@example.local",
            "manager",
        )
        consultant_id = create_user_if_missing(
            connection,
            "consultant",
            "consultant123",
            "Conrad Consultant",
            "consultant@example.local",
            "consultant",
        )

        def insert_company(name: str, legal_name: str, number: str, industry: str, notes: str) -> int:
            cursor = connection.execute(
                """
                INSERT INTO companies (
                    name, legal_name, customer_number, industry, status, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (name, legal_name, number, industry, notes, timestamp, timestamp),
            )
            return cursor.lastrowid

        acme_id = insert_company(
            "Acme Manufacturing",
            "Acme Manufacturing GmbH",
            "K-10001",
            "Industrie",
            "Strategischer Kunde mit mehreren rollierenden Lizenz- und Beratungsvertraegen.",
        )
        beta_id = insert_company(
            "Beta Health",
            "Beta Health AG",
            "K-10002",
            "Healthcare",
            "Regulierter Kunde mit hohem Bedarf an Dokumentation und SLA-Nachweisen.",
        )

        demo_pdf_one = write_demo_pdf("demo-acme-cloud-suite.pdf", "Acme Cloud Suite 2026")
        demo_pdf_two = write_demo_pdf("demo-beta-service-framework.pdf", "Beta Service Framework")

        def insert_contract(
            company_id: int,
            number: str,
            title: str,
            start_date: str,
            end_date: str,
            license_frequency: str,
            rate_cents: int,
            original_filename: str,
            stored_filename: str,
        ) -> int:
            cursor = connection.execute(
                """
                INSERT INTO contracts (
                    company_id, contract_number, title, status, start_date, end_date,
                    license_billing_frequency, service_billing_frequency, variable_billing_frequency,
                    service_hourly_rate_cents,
                    currency, original_filename, stored_filename, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, 'active', ?, ?, ?, 'monthly', 'monthly', ?, 'EUR', ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    number,
                    title,
                    start_date,
                    end_date,
                    license_frequency,
                    rate_cents,
                    original_filename,
                    stored_filename,
                    "Demo-Vertrag mit Lizenzen, Dienstleistungen und dynamischen Merkmalen.",
                    timestamp,
                    timestamp,
                ),
            )
            return cursor.lastrowid

        acme_contract_id = insert_contract(
            acme_id,
            "ACME-ERP-2026",
            "Cloud Suite und Beratungsrahmenvertrag",
            "2026-01-01",
            "2026-12-31",
            "quarterly",
            15000,
            "acme-cloud-suite.pdf",
            demo_pdf_one,
        )
        beta_contract_id = insert_contract(
            beta_id,
            "BETA-SLA-2026",
            "Service Framework und Reporting",
            "2026-04-01",
            "2027-03-31",
            "monthly",
            16500,
            "beta-service-framework.pdf",
            demo_pdf_two,
        )

        def insert_license(contract_id: int, name: str, annual_cents: int, quantity: int, start: str) -> int:
            license_type_id = license_type_id_by_name(connection, name)
            cursor = connection.execute(
                """
                INSERT INTO licenses (
                    contract_id, license_type_id, name, annual_amount_cents, quantity, start_date,
                    status, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', '', ?, ?)
                """,
                (contract_id, license_type_id, name, annual_cents, quantity, start, timestamp, timestamp),
            )
            return cursor.lastrowid

        core_license_id = insert_license(acme_contract_id, "Cloud Suite Core", 1_000_000, 1, "2026-01-01")
        analytics_license_id = insert_license(acme_contract_id, "Analytics Modul", 480_000, 1, "2026-01-01")
        insert_license(beta_contract_id, "Compliance Reporting Seats", 2_400_000, 1, "2026-04-01")

        def insert_service(
            contract_id: int,
            name: str,
            rate_cents: int,
            contracted_hours: float | None = None,
        ) -> int:
            service_type_id = service_type_id_by_name(connection, name)
            cursor = connection.execute(
                """
                INSERT INTO services (
                    contract_id, service_type_id, name, hourly_rate_cents, contracted_hours,
                    status, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'active', '', ?, ?)
                """,
                (contract_id, service_type_id, name, rate_cents, contracted_hours, timestamp, timestamp),
            )
            return cursor.lastrowid

        consulting_id = insert_service(acme_contract_id, "Fachberatung", 15000, 72)
        integration_id = insert_service(acme_contract_id, "Integration", 17500, 40)
        support_id = insert_service(beta_contract_id, "Regulatorischer Support", 16500, 96)

        time_entries = [
            (acme_contract_id, consulting_id, consultant_id, "2026-01-14", 6.5, "Workshop Lizenzmetriken", "billed"),
            (acme_contract_id, integration_id, consultant_id, "2026-02-03", 5.0, "DATEV-Export Voranalyse", "billed"),
            (acme_contract_id, consulting_id, consultant_id, "2026-04-10", 7.25, "Quartalsreview Fachbereich", "approved"),
            (acme_contract_id, integration_id, manager_id, "2026-04-22", 3.5, "Schnittstellenabstimmung", "approved"),
            (beta_contract_id, support_id, consultant_id, "2026-05-08", 4.0, "Audit-Fragenkatalog", "submitted"),
            (beta_contract_id, support_id, consultant_id, "2026-06-11", 6.0, "SLA-Reporting Review", "approved"),
        ]

        entry_ids: list[int] = []
        for contract_id, service_id, user_id, work_date, hours, description, entry_status in time_entries:
            cursor = connection.execute(
                """
                INSERT INTO service_time_entries (
                    contract_id, service_id, user_id, work_date, hours, description,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    service_id,
                    user_id,
                    work_date,
                    hours,
                    description,
                    entry_status,
                    timestamp,
                    timestamp,
                ),
            )
            entry_ids.append(cursor.lastrowid)

        invoice_cursor = connection.execute(
            """
            INSERT INTO invoices (
                invoice_number, company_id, contract_id, period_start, period_end,
                status, currency, total_cents, include_licenses, include_services,
                include_variable_costs, created_by, created_at, updated_at
            )
            VALUES ('ABR-2026-0001', ?, ?, '2026-01-01', '2026-03-31',
                    'finalized', 'EUR', ?, 1, 1, 0, ?, ?, ?)
            """,
            (acme_id, acme_contract_id, 542500, admin_id, timestamp, timestamp),
        )
        invoice_id = invoice_cursor.lastrowid
        line_items = [
            (invoice_id, "license", core_license_id, "4400", "Cloud Suite Core - Q1 2026", "Quartal", 250000),
            (invoice_id, "license", analytics_license_id, "4401", "Analytics Modul - Q1 2026", "Quartal", 120000),
            (invoice_id, "service", consulting_id, "8400", "Dienstleistungsstunden Q1 2026", "11,50 h", 172500),
        ]
        connection.executemany(
            """
            INSERT INTO invoice_line_items (
                invoice_id, item_type, source_id, datev_account, description, quantity_text, amount_cents
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            line_items,
        )
        for entry_id in entry_ids[:2]:
            connection.execute(
                "INSERT INTO invoice_time_entries (invoice_id, time_entry_id) VALUES (?, ?)",
                (invoice_id, entry_id),
            )
            connection.execute(
                """
                UPDATE service_time_entries
                SET invoice_id = ?, status = 'billed', updated_at = ?
                WHERE id = ?
                """,
                (invoice_id, timestamp, entry_id),
            )

        characteristic_defs = [
            ("company", "account_tier", "Account Tier", "text"),
            ("company", "region", "Region", "text"),
            ("contract", "contract_type", "Vertragsart", "text"),
            ("contract", "sla_level", "SLA-Stufe", "text"),
            ("license", "metric", "Lizenzmetrik", "text"),
            ("service", "delivery_mode", "Erbringungsform", "text"),
        ]
        definition_ids: dict[tuple[str, str], int] = {}
        for target_type, key, name, data_type in characteristic_defs:
            cursor = connection.execute(
                """
                INSERT INTO characteristic_definitions (target_type, key, name, data_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (target_type, key, name, data_type, timestamp),
            )
            definition_ids[(target_type, key)] = cursor.lastrowid

        values = [
            (definition_ids[("company", "account_tier")], "company", acme_id, "Enterprise"),
            (definition_ids[("company", "region")], "company", acme_id, "DACH"),
            (definition_ids[("company", "account_tier")], "company", beta_id, "Strategic"),
            (definition_ids[("contract", "contract_type")], "contract", acme_contract_id, "SaaS + Services"),
            (definition_ids[("contract", "sla_level")], "contract", acme_contract_id, "Gold"),
            (definition_ids[("contract", "contract_type")], "contract", beta_contract_id, "Managed Service"),
            (definition_ids[("contract", "sla_level")], "contract", beta_contract_id, "Platinum"),
            (definition_ids[("license", "metric")], "license", core_license_id, "Tenant"),
            (definition_ids[("license", "metric")], "license", analytics_license_id, "Modul"),
            (definition_ids[("service", "delivery_mode")], "service", consulting_id, "Remote"),
        ]
        connection.executemany(
            """
            INSERT INTO characteristic_values (
                definition_id, target_type, target_id, value_text, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [(definition_id, target_type, target_id, value, timestamp) for definition_id, target_type, target_id, value in values],
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_security()
    database.init_db()
    ensure_roles_permissions()
    ensure_default_settings()
    ensure_admin_user()
    yield


app = FastAPI(title="Vertragsverwaltung", lifespan=lifespan)
if ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
if FORCE_HTTPS:
    app.add_middleware(HTTPSRedirectMiddleware)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    if ENABLE_SECURITY_HEADERS:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'self'; "
            "frame-src 'self'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        if FORCE_HTTPS:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["money"] = money
templates.env.filters["date_de"] = date_de
templates.env.filters["datetime_de"] = datetime_de
templates.env.filters["hours_de"] = hours_de
templates.env.filters["amount_input"] = amount_input
templates.env.filters["rate_money"] = rate_money
templates.env.filters["rate_input"] = rate_input
templates.env.filters["work_quantity"] = work_quantity_text
templates.env.filters["work_amount_input"] = work_amount_input
templates.env.filters["frequency_label"] = frequency_label
templates.env.filters["billing_strategy_label"] = billing_strategy_label
templates.env.filters["vat_treatment_label"] = vat_treatment_label
templates.env.filters["flat_fee_kind_label"] = flat_fee_kind_label
templates.env.filters["flat_fee_approval_label"] = flat_fee_approval_label
templates.env.filters["status_label"] = status_label
templates.env.filters["item_type_label"] = item_type_label
templates.env.filters["rich_text"] = rich_text


def permissions_for_user(user_id: int) -> list[str]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT permissions.key
            FROM permissions
            JOIN role_permissions ON role_permissions.permission_id = permissions.id
            JOIN users ON users.role_id = role_permissions.role_id
            WHERE users.id = ?
            ORDER BY permissions.key
            """,
            (user_id,),
        ).fetchall()
    return [row["key"] for row in rows]


def get_current_user(request: Request) -> dict[str, Any] | None:
    payload = read_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    if not payload:
        return None

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT users.id, users.username, users.full_name, users.email, users.active,
                   roles.name AS role_name, roles.label AS role_label
            FROM users
            JOIN roles ON roles.id = users.role_id
            WHERE users.id = ?
            """,
            (payload.get("user_id"),),
        ).fetchone()

    if row is None or not row["active"]:
        return None

    user = dict(row)
    user["permissions"] = permissions_for_user(user["id"])
    return user


def is_superadmin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role_name") == "admin")


def require_user(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def has_permission(user: dict[str, Any] | None, permission: str) -> bool:
    return bool(user and (is_superadmin(user) or permission in user.get("permissions", [])))


def require_permission(permission: str):
    def dependency(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail="Keine Berechtigung fuer diese Aktion.")
        return user

    return dependency


def require_superadmin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if not is_superadmin(user):
        raise HTTPException(status_code=403, detail="Diese Aktion ist Superadmins vorbehalten.")
    return user


def breadcrumbs_for_path(path: str) -> list[dict[str, str]]:
    if path == "/":
        return []

    if path.startswith("/companies"):
        items = [{"label": "Entitaeten", "href": "/companies"}, {"label": "Unternehmen", "href": "/companies"}]
        if path == "/companies/new":
            items.append({"label": "Neu", "href": path})
        elif path.endswith("/edit"):
            company_path = path.rsplit("/", 1)[0]
            items.append({"label": "Detail", "href": company_path})
            items.append({"label": "Bearbeiten", "href": path})
        elif path != "/companies":
            items.append({"label": "Detail", "href": path})
        return items

    if path.startswith("/contracts"):
        items = [{"label": "Entitaeten", "href": "/companies"}, {"label": "Vertraege", "href": "/contracts"}]
        if path == "/contracts/new":
            items.append({"label": "Neu", "href": path})
        elif path.endswith("/document"):
            contract_path = path.rsplit("/", 1)[0]
            items.append({"label": "Detail", "href": contract_path})
            items.append({"label": "Dokument", "href": path})
        elif path.endswith("/new"):
            parts = path.strip("/").split("/")
            contract_path = f"/contracts/{parts[1]}" if len(parts) > 1 else "/contracts"
            labels = {
                "licenses": "Lizenz hinzufuegen",
                "services": "Dienstleistung hinzufuegen",
                "variable-costs": "Kostensatz hinzufuegen",
                "flat-fees": "Pauschale hinzufuegen",
            }
            items.append({"label": "Detail", "href": contract_path})
            items.append({"label": labels.get(parts[2], "Neu") if len(parts) > 2 else "Neu", "href": path})
        elif path.endswith("/edit"):
            parts = path.strip("/").split("/")
            contract_path = f"/contracts/{parts[1]}" if len(parts) > 1 else "/contracts"
            items.append({"label": "Detail", "href": contract_path})
            items.append({"label": "Bearbeiten", "href": path})
        elif path != "/contracts":
            items.append({"label": "Detail", "href": path})
        return items

    if path.startswith("/licenses"):
        return [{"label": "Entitaeten", "href": "/companies"}, {"label": "Lizenzen", "href": "/licenses"}]

    if path.startswith("/services"):
        return [{"label": "Entitaeten", "href": "/companies"}, {"label": "Dienstleistungen", "href": "/services"}]

    if path.startswith("/flat-fees"):
        return [{"label": "Entitaeten", "href": "/companies"}, {"label": "Pauschalen", "href": "/flat-fees"}]

    if path.startswith("/time-entries"):
        items = [{"label": "Stunden", "href": "/time-entries/new"}]
        if path == "/time-entries":
            items[0]["href"] = "/time-entries"
            items.append({"label": "Uebersicht", "href": path})
        elif path == "/time-entries/new":
            items.append({"label": "Erfassen", "href": path})
        elif path.endswith("/edit"):
            items[0]["href"] = "/time-entries"
            items.append({"label": "Bearbeiten", "href": path})
        return items

    if path.startswith("/billing") or path.startswith("/invoices"):
        items = [{"label": "Abrechnung", "href": "/billing"}]
        if path.startswith("/invoices/"):
            items.append({"label": "Rechnung", "href": path})
        return items

    if path.startswith("/analytics"):
        return [{"label": "Analytics", "href": "/analytics"}]

    if path.startswith("/catalog"):
        items = [{"label": "Verwaltung", "href": "/catalog"}, {"label": "Katalog", "href": "/catalog"}]
        if "/license-types/" in path and path.endswith("/edit"):
            items.append({"label": "Lizenzart bearbeiten", "href": path})
        elif "/service-types/" in path and path.endswith("/edit"):
            items.append({"label": "Dienstleistungsart bearbeiten", "href": path})
        elif "/flat-fee-types/" in path and path.endswith("/edit"):
            items.append({"label": "Pauschalart bearbeiten", "href": path})
        return items

    if path.startswith("/characteristics"):
        return [{"label": "Verwaltung", "href": "/characteristics"}, {"label": "Merkmale", "href": path}]

    if path.startswith("/users"):
        return [{"label": "Verwaltung", "href": "/users"}, {"label": "Benutzer", "href": path}]

    if path.startswith("/settings"):
        return [{"label": "Verwaltung", "href": "/settings"}, {"label": "Einstellungen", "href": path}]

    if path.startswith("/notifications"):
        return [
            {"label": "Verwaltung", "href": "/notifications"},
            {"label": "Benachrichtigungen", "href": path},
        ]

    return []


def back_url_for_path(path: str, user: dict[str, Any] | None) -> str:
    if path in {"/", "/login"}:
        return ""
    if path == "/time-entries/new":
        return "/time-entries" if has_permission(user, "time.approve") else "/"
    if path.startswith("/time-entries/") and path.endswith("/edit"):
        return "/time-entries"
    if path.endswith("/document"):
        return path.rsplit("/", 1)[0]
    if path.startswith("/companies/") and path.endswith("/edit"):
        return path.rsplit("/", 1)[0]
    if path.startswith("/contracts/") and path.endswith("/edit"):
        parts = path.strip("/").split("/")
        section = parts[2] if len(parts) > 3 else None
        return contract_section_path(parts[1], section) if len(parts) > 1 else "/contracts"
    if path.startswith("/contracts/") and path.endswith("/new"):
        parts = path.strip("/").split("/")
        if len(parts) > 2:
            return contract_section_path(parts[1], parts[2])
    if path.startswith("/companies/") and path != "/companies/new":
        return "/companies"
    if path.startswith("/contracts/") and path != "/contracts/new":
        return "/contracts"
    if path.startswith("/catalog/license-types/"):
        return "/catalog?tab=licenses"
    if path.startswith("/catalog/service-types/"):
        return "/catalog?tab=services"
    if path.startswith("/catalog/flat-fee-types/"):
        return "/catalog?tab=flat_fees"
    parent_paths = {
        "/companies/new": "/companies",
        "/contracts/new": "/contracts",
        "/licenses": "/contracts",
        "/services": "/contracts",
        "/flat-fees": "/contracts",
        "/time-entries": "/",
        "/billing": "/",
        "/analytics": "/",
        "/catalog": "/",
        "/characteristics": "/",
        "/notifications": "/",
        "/settings": "/",
        "/users": "/",
    }
    return parent_paths.get(path, "/")


def billing_nav_group_count(user: dict[str, Any] | None) -> int:
    if not has_permission(user, "billing.create"):
        return 0
    try:
        groups = grouped_billing_lines(
            contract_options(),
            default_billing_start(),
            date.today(),
            True,
            True,
            True,
            True,
        )
    except Exception:
        logger.exception("Could not calculate billing navigation count.")
        return 0
    return len(groups)


def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
):
    user = get_current_user(request)
    path = request.url.path
    settings = app_settings()
    base_context = {
        "request": request,
        "user": user,
        "today": today_iso(),
        "settings": settings,
        "rate_unit_label": "Tagessatz" if settings["billing_rate_unit"] == "day" else "Stundensatz",
        "work_unit_label": "Arbeitstage" if settings["billing_rate_unit"] == "day" else "Stunden",
        "hours_per_workday": hours_de(settings["workday_hours"]),
        "contract_item_statuses": CONTRACT_ITEM_STATUSES,
        "target_types": TARGET_TYPES,
        "data_types": DATA_TYPES,
        "billing_frequencies": BILLING_FREQUENCIES,
        "billing_strategies": BILLING_STRATEGIES,
        "vat_treatments": VAT_TREATMENTS,
        "flat_fee_kinds": FLAT_FEE_KINDS,
        "flat_fee_approval_statuses": FLAT_FEE_APPROVAL_STATUSES,
        "status_labels": STATUS_LABELS,
        "has_permission": lambda key: has_permission(user, key),
        "is_superadmin": is_superadmin(user),
        "billing_nav_count": billing_nav_group_count(user),
        "breadcrumbs": breadcrumbs_for_path(path) if user else [],
        "back_url": back_url_for_path(path, user) if user else "",
    }
    if context:
        base_context.update(context)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=base_context,
        status_code=status_code,
    )


def fetch_one(query: str, params: tuple[Any, ...], not_found: str):
    with database.connect() as connection:
        row = connection.execute(query, params).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=not_found)
    return row


def list_characteristics(target_type: str, target_id: int) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT characteristic_definitions.id AS definition_id,
                   characteristic_definitions.name,
                   characteristic_definitions.key,
                   characteristic_definitions.data_type,
                   characteristic_definitions.is_standard,
                   COALESCE(characteristic_values.value_text, '') AS value_text
            FROM characteristic_definitions
            LEFT JOIN characteristic_values
                ON characteristic_values.definition_id = characteristic_definitions.id
               AND characteristic_values.target_type = characteristic_definitions.target_type
               AND characteristic_values.target_id = ?
            WHERE characteristic_definitions.target_type = ?
              AND (characteristic_values.id IS NOT NULL OR characteristic_definitions.is_standard = 1)
            ORDER BY characteristic_definitions.name
            """,
            (target_id, target_type),
        ).fetchall()
    return [dict(row) for row in rows]


def list_characteristics_for_targets(
    connection,
    target_type: str,
    target_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not target_ids:
        return {}
    placeholders = ",".join("?" for _ in target_ids)
    rows = connection.execute(
        f"""
        SELECT characteristic_values.target_id,
               characteristic_definitions.id AS definition_id,
               characteristic_definitions.name,
               characteristic_definitions.key,
               characteristic_definitions.data_type,
               characteristic_values.value_text
        FROM characteristic_values
        JOIN characteristic_definitions
            ON characteristic_definitions.id = characteristic_values.definition_id
        WHERE characteristic_values.target_type = ?
          AND characteristic_values.target_id IN ({placeholders})
        ORDER BY characteristic_definitions.name
        """,
        (target_type, *target_ids),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["target_id"]].append(dict(row))
    return dict(grouped)


def list_characteristic_definitions(target_type: str) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM characteristic_definitions
            WHERE target_type = ?
            ORDER BY name
            """,
            (target_type,),
        ).fetchall()
    return [dict(row) for row in rows]


def materialize_standard_characteristics(connection, target_type: str, target_id: int) -> None:
    timestamp = now_iso()
    definitions = connection.execute(
        """
        SELECT id
        FROM characteristic_definitions
        WHERE target_type = ? AND is_standard = 1
        """,
        (target_type,),
    ).fetchall()
    for definition in definitions:
        connection.execute(
            """
            INSERT OR IGNORE INTO characteristic_values (
                definition_id, target_type, target_id, value_text, updated_at
            )
            VALUES (?, ?, ?, '', ?)
            """,
            (definition["id"], target_type, target_id, timestamp),
        )


def materialize_standard_characteristic_for_existing(
    connection,
    definition_id: int,
    target_type: str,
) -> None:
    table_name = TARGET_TABLES[target_type]
    timestamp = now_iso()
    connection.execute(
        f"""
        INSERT OR IGNORE INTO characteristic_values (
            definition_id, target_type, target_id, value_text, updated_at
        )
        SELECT ?, ?, id, '', ?
        FROM {table_name}
        """,
        (definition_id, target_type, timestamp),
    )


def set_characteristic_value(
    target_type: str,
    target_id: int,
    definition_id: int,
    value_text: str,
) -> None:
    timestamp = now_iso()
    with database.connect() as connection:
        definition = connection.execute(
            """
            SELECT *
            FROM characteristic_definitions
            WHERE id = ? AND target_type = ?
            """,
            (definition_id, target_type),
        ).fetchone()
        if definition is None:
            raise HTTPException(status_code=400, detail="Charakteristik passt nicht zum Zielobjekt.")
        connection.execute(
            """
            INSERT INTO characteristic_values (
                definition_id, target_type, target_id, value_text, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(definition_id, target_type, target_id)
            DO UPDATE SET value_text = excluded.value_text, updated_at = excluded.updated_at
            """,
            (definition_id, target_type, target_id, value_text.strip(), timestamp),
        )


def company_options() -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT id, name, contact_name, contact_email, contact_phone,
                   billing_recipient_name, billing_recipient_email, billing_recipient_phone,
                   customer_supplier_number
            FROM companies
            ORDER BY name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def company_contact_defaults(company_id: int | None) -> dict[str, Any] | None:
    if company_id is None:
        return None
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT id, name, contact_name, contact_email, contact_phone,
                   billing_recipient_name, billing_recipient_email, billing_recipient_phone
            FROM companies
            WHERE id = ?
            """,
            (company_id,),
        ).fetchone()
    return dict(row) if row else None


def apply_company_contact_defaults(values: dict[str, Any], company: dict[str, Any] | None) -> None:
    if not company:
        return
    for field in (
        "contact_name",
        "contact_email",
        "contact_phone",
        "billing_recipient_name",
        "billing_recipient_email",
        "billing_recipient_phone",
    ):
        if not str(values.get(field) or "").strip():
            values[field] = company.get(field) or ""


def contract_options() -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT contracts.id, contracts.contract_number, contracts.title, companies.name AS company_name
            FROM contracts
            JOIN companies ON companies.id = contracts.company_id
            ORDER BY companies.name, contracts.title
            """
        ).fetchall()
    return [dict(row) for row in rows]


def notification_severity_for_due_date(due_date: date) -> str:
    days_remaining = (due_date - date.today()).days
    if days_remaining < 0:
        return "overdue"
    if days_remaining <= 30:
        return "reminder"
    return "open"


def notification_timing_text(due_date: date) -> str:
    days_remaining = (due_date - date.today()).days
    if days_remaining < 0:
        return f"seit {abs(days_remaining)} Tag(en) abgelaufen"
    if days_remaining == 0:
        return "endet heute"
    return f"in {days_remaining} Tag(en)"


def sync_contract_end_notifications() -> None:
    settings = app_settings()
    threshold = settings["contract_end_notification_days"]
    window_end = date.today() + timedelta(days=threshold)
    timestamp = now_iso()
    with database.connect() as connection:
        contracts = connection.execute(
            """
            SELECT contracts.id, contracts.contract_number, contracts.title, contracts.end_date,
                   companies.name AS company_name
            FROM contracts
            JOIN companies ON companies.id = contracts.company_id
            WHERE contracts.end_date IS NOT NULL
              AND contracts.end_date != ''
              AND contracts.end_date <= ?
              AND contracts.status != 'ended'
            ORDER BY contracts.end_date ASC, contracts.contract_number
            """,
            (window_end.isoformat(),),
        ).fetchall()
        for contract in contracts:
            due_date = parse_iso_date(contract["end_date"])
            if due_date is None:
                continue
            event_key = f"contract_end:{contract['id']}:{due_date.isoformat()}"
            title = f"Vertragsende: {contract['contract_number']}"
            timing = notification_timing_text(due_date)
            message = (
                f"{contract['company_name']} - {contract['title']} endet am {date_de(due_date)} "
                f"({timing})."
            )
            severity = notification_severity_for_due_date(due_date)
            connection.execute(
                """
                INSERT INTO notifications (
                    event_key, notification_type, target_type, target_id, severity,
                    title, message, due_date, created_at, updated_at
                )
                VALUES (?, 'contract_end', 'contract', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key)
                DO UPDATE SET severity = excluded.severity,
                              title = excluded.title,
                              message = excluded.message,
                              due_date = excluded.due_date,
                              updated_at = excluded.updated_at
                """,
                (
                    event_key,
                    contract["id"],
                    severity,
                    title,
                    message,
                    due_date.isoformat(),
                    timestamp,
                    timestamp,
                ),
            )


def enrich_notification_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for row in rows:
        item = dict(row)
        due_date = parse_iso_date(item.get("due_date"))
        item["timing_text"] = notification_timing_text(due_date) if due_date else "-"
        item["severity_label"] = {
            "overdue": "Ueberfaellig",
            "reminder": "Bald faellig",
            "open": "Hinweis",
        }.get(item.get("severity"), "Hinweis")
        enriched.append(item)
    return enriched


def notification_panel_data(user: dict[str, Any] | None) -> dict[str, Any]:
    if not has_permission(user, "contracts.view"):
        return {"open": [], "acknowledged": []}
    sync_contract_end_notifications()
    settings = app_settings()
    window_end = (date.today() + timedelta(days=settings["contract_end_notification_days"])).isoformat()
    with database.connect() as connection:
        open_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT notifications.*, contracts.contract_number, contracts.title AS contract_title,
                       companies.name AS company_name,
                       users.full_name AS acknowledged_by_name
                FROM notifications
                JOIN contracts
                    ON notifications.target_type = 'contract'
                   AND notifications.target_id = contracts.id
                JOIN companies ON companies.id = contracts.company_id
                LEFT JOIN users ON users.id = notifications.acknowledged_by
                WHERE notifications.notification_type = 'contract_end'
                  AND notifications.acknowledged_at IS NULL
                  AND contracts.status != 'ended'
                  AND contracts.end_date = notifications.due_date
                  AND notifications.due_date <= ?
                ORDER BY notifications.due_date ASC, notifications.id ASC
                """,
                (window_end,),
            ).fetchall()
        ]
        acknowledged_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT notifications.*, contracts.contract_number, contracts.title AS contract_title,
                       companies.name AS company_name,
                       users.full_name AS acknowledged_by_name
                FROM notifications
                JOIN contracts
                    ON notifications.target_type = 'contract'
                   AND notifications.target_id = contracts.id
                JOIN companies ON companies.id = contracts.company_id
                LEFT JOIN users ON users.id = notifications.acknowledged_by
                WHERE notifications.notification_type = 'contract_end'
                  AND notifications.acknowledged_at IS NOT NULL
                ORDER BY notifications.acknowledged_at DESC, notifications.id DESC
                LIMIT 8
                """
            ).fetchall()
        ]
    return {
        "open": enrich_notification_rows(open_rows),
        "acknowledged": enrich_notification_rows(acknowledged_rows),
    }


def notifications_table_data() -> dict[str, Any]:
    sync_contract_end_notifications()
    with database.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT notifications.*, contracts.contract_number, contracts.title AS contract_title,
                       companies.name AS company_name,
                       users.full_name AS acknowledged_by_name
                FROM notifications
                LEFT JOIN contracts
                    ON notifications.target_type = 'contract'
                   AND notifications.target_id = contracts.id
                LEFT JOIN companies ON companies.id = contracts.company_id
                LEFT JOIN users ON users.id = notifications.acknowledged_by
                ORDER BY CASE WHEN notifications.acknowledged_at IS NULL THEN 0 ELSE 1 END,
                         notifications.due_date ASC,
                         notifications.updated_at DESC,
                         notifications.id DESC
                """
            ).fetchall()
        ]
    enriched = enrich_notification_rows(rows)
    return {
        "notifications": enriched,
        "open_count": sum(1 for item in enriched if not item.get("acknowledged_at")),
        "acknowledged_count": sum(1 for item in enriched if item.get("acknowledged_at")),
    }


def service_options(include_service_id: int | None = None) -> list[dict[str, Any]]:
    with database.connect() as connection:
        params: tuple[Any, ...] = ()
        status_filter = "services.status = 'active'"
        if include_service_id is not None:
            status_filter = "(services.status = 'active' OR services.id = ?)"
            params = (include_service_id,)
        rows = connection.execute(
            f"""
            SELECT services.id, services.contract_id, services.name,
                   contracts.contract_number, companies.name AS company_name
            FROM services
            JOIN contracts ON contracts.id = services.contract_id
            JOIN companies ON companies.id = contracts.company_id
            WHERE {status_filter}
            ORDER BY companies.name, contracts.contract_number, services.name
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def license_type_options() -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM license_types
            WHERE active = 1
            ORDER BY name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def service_type_options() -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM service_types
            WHERE active = 1
            ORDER BY name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def flat_fee_type_options() -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM flat_fee_types
            WHERE active = 1
            ORDER BY name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def bookable_work_item_options(
    include_service_id: int | None = None,
    include_flat_fee_id: int | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for service in service_options(include_service_id):
        item = dict(service)
        item.update({"key": f"service:{item['id']}", "kind": "service", "label": "Dienstleistung"})
        items.append(item)

    status_filter = "flat_fees.status = 'active'"
    params: tuple[Any, ...] = ()
    if include_flat_fee_id is not None:
        status_filter = "(flat_fees.status = 'active' OR flat_fees.id = ?)"
        params = (include_flat_fee_id,)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT flat_fees.id, flat_fees.contract_id, flat_fees.name,
                   contracts.contract_number, companies.name AS company_name
            FROM flat_fees
            JOIN contracts ON contracts.id = flat_fees.contract_id
            JOIN companies ON companies.id = contracts.company_id
            WHERE {status_filter}
            ORDER BY companies.name, contracts.contract_number, flat_fees.name
            """,
            params,
        ).fetchall()
    for flat_fee in rows:
        item = dict(flat_fee)
        item.update({"key": f"flat_fee:{item['id']}", "kind": "flat_fee", "label": "Pauschale"})
        items.append(item)
    return items


def parse_work_item_key(value: str) -> tuple[str, int]:
    kind, _, raw_id = (value or "").partition(":")
    if kind not in {"service", "flat_fee"} or not raw_id:
        raise ValueError("Bitte eine Dienstleistung oder Pauschale auswaehlen.")
    try:
        item_id = int(raw_id)
    except ValueError as exc:
        raise ValueError("Bitte eine gueltige Leistung auswaehlen.") from exc
    return kind, item_id


def empty_service_volume_summary() -> dict[str, Any]:
    return {
        "approved_unbilled_hours": 0,
        "approved_unbilled_cents": 0,
        "submitted_unbilled_hours": 0,
        "submitted_unbilled_cents": 0,
        "total_unbilled_hours": 0,
        "total_unbilled_cents": 0,
    }


def service_volume_summaries(connection, contract_id: int | None = None) -> dict[int, dict[str, Any]]:
    contract_filter = ""
    params: tuple[Any, ...] = ()
    if contract_id is not None:
        contract_filter = "AND service_time_entries.contract_id = ?"
        params = (contract_id,)

    rows = connection.execute(
        f"""
        WITH grouped_entries AS (
            SELECT service_time_entries.contract_id,
                   service_time_entries.service_id,
                   service_time_entries.status,
                   SUM(service_time_entries.hours) AS hours,
                   services.hourly_rate_cents
            FROM service_time_entries
            JOIN services ON services.id = service_time_entries.service_id
            WHERE service_time_entries.invoice_id IS NULL
              AND service_time_entries.status IN ('approved', 'submitted')
              AND NOT EXISTS (
                  SELECT 1
                  FROM invoice_time_entries
                  WHERE invoice_time_entries.time_entry_id = service_time_entries.id
              )
              {contract_filter}
            GROUP BY service_time_entries.contract_id,
                     service_time_entries.service_id,
                     service_time_entries.status,
                     services.hourly_rate_cents
        )
        SELECT contract_id,
               COALESCE(SUM(CASE WHEN status = 'approved' THEN hours ELSE 0 END), 0)
                   AS approved_unbilled_hours,
               CAST(COALESCE(SUM(
                   CASE WHEN status = 'approved' THEN ROUND(hours * hourly_rate_cents) ELSE 0 END
               ), 0) AS INTEGER) AS approved_unbilled_cents,
               COALESCE(SUM(CASE WHEN status = 'submitted' THEN hours ELSE 0 END), 0)
                   AS submitted_unbilled_hours,
               CAST(COALESCE(SUM(
                   CASE WHEN status = 'submitted' THEN ROUND(hours * hourly_rate_cents) ELSE 0 END
               ), 0) AS INTEGER) AS submitted_unbilled_cents,
               COALESCE(SUM(hours), 0) AS total_unbilled_hours,
               CAST(COALESCE(SUM(ROUND(hours * hourly_rate_cents)), 0) AS INTEGER)
                   AS total_unbilled_cents
        FROM grouped_entries
        GROUP BY contract_id
        """,
        params,
    ).fetchall()
    return {row["contract_id"]: dict(row) for row in rows}


def contracted_service_summaries(connection) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        """
        WITH service_usage AS (
            SELECT services.id,
                   services.contract_id,
                   COALESCE(services.contracted_hours, 0) AS contracted_hours,
                   services.hourly_rate_cents,
                   COALESCE(SUM(service_time_entries.hours), 0) AS booked_hours
            FROM services
            LEFT JOIN service_time_entries ON service_time_entries.service_id = services.id
            WHERE services.status = 'active'
            GROUP BY services.id
        )
        SELECT contract_id,
               COALESCE(SUM(contracted_hours), 0) AS contracted_service_hours,
               CAST(COALESCE(SUM(
                   ROUND(contracted_hours * hourly_rate_cents)
               ), 0) AS INTEGER) AS contracted_service_cents,
               COALESCE(SUM(
                   CASE
                       WHEN contracted_hours > booked_hours THEN contracted_hours - booked_hours
                       ELSE 0
                   END
               ), 0) AS free_service_hours,
               CAST(COALESCE(SUM(
                   CASE
                       WHEN contracted_hours > booked_hours THEN ROUND((contracted_hours - booked_hours) * hourly_rate_cents)
                       ELSE 0
                   END
               ), 0) AS INTEGER) AS free_service_cents
        FROM service_usage
        GROUP BY contract_id
        """
    ).fetchall()
    return {row["contract_id"]: dict(row) for row in rows}


def default_billing_start() -> date:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT MIN(start_value) AS start_value
            FROM (
                SELECT MIN(start_date) AS start_value
                FROM contracts
                UNION ALL
                SELECT MIN(COALESCE(service_time_entries.start_date, service_time_entries.work_date)) AS start_value
                FROM service_time_entries
                WHERE service_time_entries.status = 'approved'
                  AND service_time_entries.invoice_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM invoice_time_entries
                      WHERE invoice_time_entries.time_entry_id = service_time_entries.id
                  )
                UNION ALL
                SELECT MIN(start_date) AS start_value
                FROM variable_costs
                WHERE status = 'active'
                UNION ALL
                SELECT MIN(start_date) AS start_value
                FROM flat_fees
                WHERE status = 'active'
            )
            WHERE start_value IS NOT NULL
            """
        ).fetchone()
    parsed = parse_iso_date(row["start_value"]) if row and row["start_value"] else None
    fallback = date(date.today().year, 1, 1)
    if parsed is None or parsed > date.today():
        return fallback
    return parsed


def fetch_contract_bundle(contract_id: int) -> dict[str, Any]:
    with database.connect() as connection:
        contract = connection.execute(
            """
            SELECT contracts.*, companies.name AS company_name, companies.id AS company_id
            FROM contracts
            JOIN companies ON companies.id = contracts.company_id
            WHERE contracts.id = ?
            """,
            (contract_id,),
        ).fetchone()
        if contract is None:
            raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")

        licenses = connection.execute(
            """
            SELECT licenses.*, license_types.datev_account, license_types.description AS type_description
            FROM licenses
            LEFT JOIN license_types ON license_types.id = licenses.license_type_id
            WHERE licenses.contract_id = ?
            ORDER BY licenses.name
            """,
            (contract_id,),
        ).fetchall()
        license_items = [dict(row) for row in licenses]
        license_characteristics = list_characteristics_for_targets(
            connection,
            "license",
            [item["id"] for item in license_items],
        )
        for item in license_items:
            item["characteristics"] = license_characteristics.get(item["id"], [])
            item["annual_total_cents"] = (item["annual_amount_cents"] or 0) * (item["quantity"] or 0)
            item["billing_summary"] = license_billing_strategy_summary(item)

        services = connection.execute(
            """
            SELECT services.*, service_types.datev_account, service_types.description AS type_description
            FROM services
            LEFT JOIN service_types ON service_types.id = services.service_type_id
            WHERE services.contract_id = ?
            ORDER BY services.name
            """,
            (contract_id,),
        ).fetchall()
        service_items = [dict(row) for row in services]
        settings = app_settings()
        service_characteristics = list_characteristics_for_targets(
            connection,
            "service",
            [item["id"] for item in service_items],
        )
        for item in service_items:
            item["characteristics"] = service_characteristics.get(item["id"], [])
            item["contracted_days"] = None
            if item.get("contracted_hours") is not None:
                item["contracted_days"] = (
                    Decimal(str(item["contracted_hours"])) / settings["workday_hours"]
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        variable_costs = connection.execute(
            """
            SELECT *
            FROM variable_costs
            WHERE contract_id = ?
            ORDER BY name
            """,
            (contract_id,),
        ).fetchall()

        flat_fees = connection.execute(
            """
            SELECT flat_fees.*, flat_fee_types.datev_account,
                   flat_fee_types.description AS type_description
            FROM flat_fees
            LEFT JOIN flat_fee_types ON flat_fee_types.id = flat_fees.flat_fee_type_id
            WHERE flat_fees.contract_id = ?
            ORDER BY flat_fees.name
            """,
            (contract_id,),
        ).fetchall()
        flat_fee_items = [dict(row) for row in flat_fees]
        flat_fee_characteristics = list_characteristics_for_targets(
            connection,
            "flat_fee",
            [item["id"] for item in flat_fee_items],
        )
        for item in flat_fee_items:
            item["characteristics"] = flat_fee_characteristics.get(item["id"], [])

        time_entries = connection.execute(
            """
            SELECT service_time_entries.*,
                   COALESCE(services.name, flat_fees.name) AS service_name,
                   CASE
                       WHEN service_time_entries.flat_fee_id IS NULL THEN 'Dienstleistung'
                       ELSE 'Pauschale'
                   END AS work_item_type,
                   users.full_name AS user_name
            FROM service_time_entries
            LEFT JOIN services ON services.id = service_time_entries.service_id
            LEFT JOIN flat_fees ON flat_fees.id = service_time_entries.flat_fee_id
            JOIN users ON users.id = service_time_entries.user_id
            WHERE service_time_entries.contract_id = ?
            ORDER BY COALESCE(service_time_entries.start_date, service_time_entries.work_date) DESC,
                     service_time_entries.id DESC
            LIMIT 10
            """,
            (contract_id,),
        ).fetchall()
        invoices = connection.execute(
            """
            SELECT *
            FROM invoices
            WHERE contract_id = ?
              AND status = 'finalized'
            ORDER BY period_start DESC, id DESC
            """,
            (contract_id,),
        ).fetchall()
        contract_dependency_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM licenses WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM services WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM variable_costs WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM flat_fees WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM service_time_entries WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM invoices WHERE contract_id = ?)
            """,
            (contract_id, contract_id, contract_id, contract_id, contract_id, contract_id),
        ).fetchone()[0]
        service_volume = service_volume_summaries(connection, contract_id).get(
            contract_id,
            empty_service_volume_summary(),
        )

    contract_dict = dict(contract)
    apply_company_contact_defaults(contract_dict, company_contact_defaults(contract_dict.get("company_id")))
    contract_dict["document_url"] = f"/contracts/{contract_id}/document" if contract["stored_filename"] else None
    return {
        "contract": contract_dict,
        "licenses": license_items,
        "services": service_items,
        "variable_costs": [dict(row) for row in variable_costs],
        "flat_fees": flat_fee_items,
        "time_entries": [dict(row) for row in time_entries],
        "invoices": [dict(row) for row in invoices],
        "contract_can_delete": contract_dependency_count == 0,
        "service_volume": service_volume,
        "characteristics": list_characteristics("contract", contract_id),
        "characteristic_definitions": list_characteristic_definitions("contract"),
        "license_types": license_type_options(),
        "service_types": service_type_options(),
        "flat_fee_types": flat_fee_type_options(),
    }


def license_billing_lines(contract: dict[str, Any], period_start: date, period_end: date) -> list[dict[str, Any]]:
    contract_start = parse_iso_date(contract["start_date"]) or period_start
    contract_end = parse_iso_date(contract["end_date"]) or date(9999, 12, 31)
    lead_days = app_settings()["license_billable_lead_days"]
    billable_window_end = period_end + timedelta(days=lead_days)
    if contract_end < period_start or contract_start > billable_window_end:
        return []

    with database.connect() as connection:
        licenses = connection.execute(
            """
            SELECT licenses.*, license_types.datev_account
            FROM licenses
            JOIN license_types ON license_types.id = licenses.license_type_id
            WHERE licenses.contract_id = ? AND licenses.status = 'active'
            ORDER BY licenses.name
            """,
            (contract["id"],),
        ).fetchall()
        reserved_keys = {
            row["billing_key"]
            for row in connection.execute(
                """
                SELECT billing_key
                FROM invoice_line_items
                WHERE item_type = 'license'
                  AND billing_key IS NOT NULL
                  AND billing_key != ''
                """
            ).fetchall()
        }

    lines: list[dict[str, Any]] = []

    def append_license_period_lines(
        license_row,
        frequency_key: str,
        anchor_start: date,
        segment_start: date,
        segment_end: date,
        once_key_suffix: str,
    ) -> None:
        if segment_start > segment_end:
            return

        if frequency_key not in BILLING_FREQUENCIES:
            frequency_key = contract["license_billing_frequency"]
        frequency = BILLING_FREQUENCIES[frequency_key]

        if frequency_key == "once":
            billable_from = segment_start - timedelta(days=lead_days)
            if segment_end < period_start or billable_from > period_end:
                return
            billing_key = f"license:{license_row['id']}:{once_key_suffix}"
            if billing_key in reserved_keys:
                return
            price_license_start = parse_iso_date(license_row["start_date"]) or contract_start
            multiplier = license_price_multiplier(contract, price_license_start, segment_start)
            amount_cents = cents_from_decimal(
                Decimal(license_row["annual_amount_cents"] or 0)
                * Decimal(license_row["quantity"] or 0)
                * multiplier
            )
            if amount_cents <= 0:
                return
            line_end = segment_end if segment_end.year < 9999 else segment_start
            lines.append(
                {
                    "item_type": "license",
                    "source_id": license_row["id"],
                    "datev_account": license_row["datev_account"],
                    "billing_key": billing_key,
                    "selection_key": billing_key,
                    "description": billing_position_description(
                        license_row["name"],
                        license_row["notes"],
                        f"{frequency['label']} ab {date_de(segment_start)}",
                    ),
                    "quantity_text": f"{license_row['quantity']} Lizenz(en)",
                    "amount_cents": amount_cents,
                    "period_start": segment_start,
                    "period_end": line_end,
                    "time_entry_ids": [],
                }
            )
            return

        months_per_period = frequency["months"]
        periods_per_year = Decimal(frequency["periods_per_year"])
        interval_start = anchor_start
        while interval_start <= billable_window_end and interval_start <= segment_end:
            interval_end = add_months(interval_start, months_per_period) - timedelta(days=1)
            billable_from = interval_start - timedelta(days=lead_days)
            if interval_end >= period_start and billable_from <= period_end:
                overlap_start = max(interval_start, segment_start)
                overlap_end = min(interval_end, segment_end)
                if overlap_start <= overlap_end:
                    billing_key = (
                        f"license:{license_row['id']}:{interval_start.isoformat()}:{interval_end.isoformat()}"
                    )
                    if billing_key not in reserved_keys:
                        interval_days = Decimal((interval_end - interval_start).days + 1)
                        overlap_days = Decimal((overlap_end - overlap_start).days + 1)
                        price_license_start = parse_iso_date(license_row["start_date"]) or contract_start
                        multiplier = license_price_multiplier(contract, price_license_start, interval_start)
                        period_amount = (
                            Decimal(license_row["annual_amount_cents"])
                            * Decimal(license_row["quantity"])
                            * multiplier
                            / periods_per_year
                        )
                        amount_cents = cents_from_decimal(period_amount * overlap_days / interval_days)
                        if amount_cents > 0:
                            lines.append(
                                {
                                    "item_type": "license",
                                    "source_id": license_row["id"],
                                    "datev_account": license_row["datev_account"],
                                    "billing_key": billing_key,
                                    "selection_key": billing_key,
                                    "description": billing_position_description(
                                        license_row["name"],
                                        license_row["notes"],
                                        f"{frequency['label']} {date_de(overlap_start)} bis {date_de(overlap_end)}",
                                    ),
                                    "quantity_text": f"{license_row['quantity']} Lizenz(en)",
                                    "amount_cents": amount_cents,
                                    "period_start": overlap_start,
                                    "period_end": overlap_end,
                                    "time_entry_ids": [],
                                }
                            )
            interval_start = add_months(interval_start, months_per_period)

    for license_row in licenses:
        license_start = parse_iso_date(license_row["start_date"]) or contract_start
        license_end = parse_iso_date(license_row["end_date"]) or contract_end
        effective_start = max(license_start, contract_start)
        effective_end = min(license_end, contract_end)
        if effective_start > effective_end:
            continue

        strategy, base_frequency, first_year_frequency, renewal_frequency = normalize_license_billing_config(
            license_row["billing_strategy"],
            license_row["billing_frequency"],
            license_row["first_year_billing_frequency"],
            license_row["renewal_billing_frequency"],
            contract["license_billing_frequency"],
        )
        if strategy == "first_year_then_renewal":
            first_year_end = add_months(license_start, 12) - timedelta(days=1)
            append_license_period_lines(
                license_row,
                first_year_frequency,
                license_start,
                effective_start,
                min(first_year_end, effective_end),
                f"first-year-once:{license_start.isoformat()}:{first_year_end.isoformat()}",
            )
            renewal_start = add_months(license_start, 12)
            append_license_period_lines(
                license_row,
                renewal_frequency,
                renewal_start,
                max(renewal_start, effective_start),
                effective_end,
                f"renewal-once:{renewal_start.isoformat()}:{effective_end.isoformat()}",
            )
        else:
            append_license_period_lines(
                license_row,
                base_frequency,
                license_start,
                effective_start,
                effective_end,
                "once",
            )

    return lines


def variable_cost_billing_lines(contract: dict[str, Any], period_start: date, period_end: date) -> list[dict[str, Any]]:
    contract_start = parse_iso_date(contract["start_date"]) or period_start
    contract_end = parse_iso_date(contract["end_date"]) or date(9999, 12, 31)
    if contract_end < period_start or contract_start > period_end:
        return []

    with database.connect() as connection:
        variable_costs = connection.execute(
            """
            SELECT *
            FROM variable_costs
            WHERE contract_id = ? AND status = 'active'
            ORDER BY name
            """,
            (contract["id"],),
        ).fetchall()
        reserved_keys = {
            row["billing_key"]
            for row in connection.execute(
                """
                SELECT billing_key
                FROM invoice_line_items
                WHERE item_type = 'variable_cost'
                  AND billing_key IS NOT NULL
                  AND billing_key != ''
                """
            ).fetchall()
        }

    lines: list[dict[str, Any]] = []
    for row in variable_costs:
        frequency_key = row["billing_frequency"] or contract["variable_billing_frequency"]
        if frequency_key not in BILLING_FREQUENCIES:
            frequency_key = contract["variable_billing_frequency"]
        frequency = BILLING_FREQUENCIES[frequency_key]
        cost_start = parse_iso_date(row["start_date"]) or contract_start
        cost_end = parse_iso_date(row["end_date"]) or contract_end

        if frequency_key == "once":
            if cost_end < period_start or cost_start > period_end:
                continue
            billing_key = f"variable:{row['id']}:once"
            if billing_key in reserved_keys:
                continue
            amount_cents = cents_from_decimal(Decimal(row["rate_cents"]) * Decimal(str(row["quantity"])))
            if amount_cents <= 0:
                continue
            line_start = max(cost_start, contract_start)
            line_end = min(cost_end, contract_end)
            if line_end.year >= 9999:
                line_end = line_start
            lines.append(
                {
                    "item_type": "variable_cost",
                    "source_id": row["id"],
                    "datev_account": row["datev_account"],
                    "billing_key": billing_key,
                    "selection_key": billing_key,
                    "description": billing_position_description(
                        row["name"],
                        row["description"],
                        f"{frequency['label']} ab {date_de(line_start)}",
                    ),
                    "quantity_text": f"{hours_de(row['quantity'])} {row['unit']}",
                    "amount_cents": amount_cents,
                    "period_start": line_start,
                    "period_end": line_end,
                    "time_entry_ids": [],
                }
            )
            continue

        months_per_period = frequency["months"]
        interval_start = contract_start
        while interval_start <= period_end:
            interval_end = add_months(interval_start, months_per_period) - timedelta(days=1)
            if interval_end >= period_start and interval_start <= period_end:
                overlap_start = max(interval_start, cost_start, contract_start, period_start)
                overlap_end = min(interval_end, cost_end, contract_end, period_end)
                if overlap_start <= overlap_end:
                    billing_key = f"variable:{row['id']}:{interval_start.isoformat()}:{interval_end.isoformat()}"
                    if billing_key not in reserved_keys:
                        interval_days = Decimal((interval_end - interval_start).days + 1)
                        overlap_days = Decimal((overlap_end - overlap_start).days + 1)
                        amount_cents = cents_from_decimal(
                            Decimal(row["rate_cents"]) * Decimal(str(row["quantity"])) * overlap_days / interval_days
                        )
                        if amount_cents > 0:
                            lines.append(
                                {
                                    "item_type": "variable_cost",
                                    "source_id": row["id"],
                                    "datev_account": row["datev_account"],
                                    "billing_key": billing_key,
                                    "selection_key": billing_key,
                                    "description": billing_position_description(
                                        row["name"],
                                        row["description"],
                                        f"{frequency['label']} {date_de(overlap_start)} bis {date_de(overlap_end)}",
                                    ),
                                    "quantity_text": f"{hours_de(row['quantity'])} {row['unit']}",
                                    "amount_cents": amount_cents,
                                    "period_start": overlap_start,
                                    "period_end": overlap_end,
                                    "time_entry_ids": [],
                                }
                            )
            interval_start = add_months(interval_start, months_per_period)

    return lines


def flat_fee_billing_lines(contract: dict[str, Any], period_start: date, period_end: date) -> list[dict[str, Any]]:
    contract_start = parse_iso_date(contract["start_date"]) or period_start
    contract_end = parse_iso_date(contract["end_date"]) or date(9999, 12, 31)
    if contract_end < period_start or contract_start > period_end:
        return []

    with database.connect() as connection:
        flat_fees = connection.execute(
            """
            SELECT flat_fees.*, flat_fee_types.datev_account
            FROM flat_fees
            JOIN flat_fee_types ON flat_fee_types.id = flat_fees.flat_fee_type_id
            WHERE flat_fees.contract_id = ? AND flat_fees.status = 'active'
            ORDER BY flat_fees.name
            """,
            (contract["id"],),
        ).fetchall()
        reserved_keys = {
            row["billing_key"]
            for row in connection.execute(
                """
                SELECT billing_key
                FROM invoice_line_items
                WHERE item_type = 'flat_fee'
                  AND billing_key IS NOT NULL
                  AND billing_key != ''
                """
            ).fetchall()
        }

    lines: list[dict[str, Any]] = []
    for row in flat_fees:
        is_success_bonus = (row["fee_kind"] or "work_package") == "success_bonus"
        if is_success_bonus and row["approval_status"] != "approved":
            continue
        frequency_key = row["billing_frequency"] or "once"
        if is_success_bonus:
            frequency_key = "once"
        if frequency_key not in BILLING_FREQUENCIES:
            frequency_key = "once"
        frequency = BILLING_FREQUENCIES[frequency_key]
        fee_start = (
            parse_iso_date(row["success_date"])
            or parse_iso_date(row["expected_success_date"])
            or parse_iso_date(row["start_date"])
            or contract_start
        )
        fee_end = parse_iso_date(row["end_date"]) or contract_end
        if is_success_bonus:
            fee_end = fee_start
        description_prefix = "Erfolgsbonus" if is_success_bonus else "Pauschale"
        success_suffix = ""
        if is_success_bonus and row["success_condition"]:
            success_suffix = f"Erfolgsbedingung: {row['success_condition']}"

        if frequency_key == "once":
            if fee_end < period_start or fee_start > period_end:
                continue
            billing_key = f"flat_fee:{row['id']}:once"
            if billing_key in reserved_keys:
                continue
            amount_cents = int(row["amount_cents"] or 0)
            if amount_cents <= 0:
                continue
            line_start = max(fee_start, contract_start)
            line_end = min(fee_end, contract_end)
            if line_end.year >= 9999:
                line_end = line_start
            lines.append(
                {
                    "item_type": "flat_fee",
                    "source_id": row["id"],
                    "datev_account": row["datev_account"],
                    "billing_key": billing_key,
                    "selection_key": billing_key,
                    "description": billing_position_description(
                        f"{description_prefix}: {row['name']}",
                        row["notes"],
                        success_suffix,
                        f"{frequency['label']} ab {date_de(line_start)}",
                    ),
                    "quantity_text": "1 Pauschale",
                    "amount_cents": amount_cents,
                    "period_start": line_start,
                    "period_end": line_end,
                    "time_entry_ids": [],
                }
            )
            continue

        months_per_period = frequency["months"]
        interval_start = contract_start
        while interval_start <= period_end:
            interval_end = add_months(interval_start, months_per_period) - timedelta(days=1)
            if interval_end >= period_start and interval_start <= period_end:
                overlap_start = max(interval_start, fee_start, contract_start, period_start)
                overlap_end = min(interval_end, fee_end, contract_end, period_end)
                if overlap_start <= overlap_end:
                    billing_key = f"flat_fee:{row['id']}:{interval_start.isoformat()}:{interval_end.isoformat()}"
                    if billing_key not in reserved_keys:
                        interval_days = Decimal((interval_end - interval_start).days + 1)
                        overlap_days = Decimal((overlap_end - overlap_start).days + 1)
                        amount_cents = cents_from_decimal(
                            Decimal(row["amount_cents"]) * overlap_days / interval_days
                        )
                        if amount_cents > 0:
                            lines.append(
                                {
                                    "item_type": "flat_fee",
                                    "source_id": row["id"],
                                    "datev_account": row["datev_account"],
                                    "billing_key": billing_key,
                                    "selection_key": billing_key,
                                    "description": billing_position_description(
                                        f"Pauschale: {row['name']}",
                                        row["notes"],
                                        f"{frequency['label']} {date_de(overlap_start)} bis {date_de(overlap_end)}",
                                    ),
                                    "quantity_text": "1 Pauschale",
                                    "amount_cents": amount_cents,
                                    "period_start": overlap_start,
                                    "period_end": overlap_end,
                                    "time_entry_ids": [],
                                }
                            )
            interval_start = add_months(interval_start, months_per_period)

    return lines


def service_billing_lines(contract: dict[str, Any], period_start: date, period_end: date) -> list[dict[str, Any]]:
    contract_id = contract["id"]
    contract_start = parse_iso_date(contract["start_date"]) or period_start
    effective_start = min(period_start, contract_start)
    with database.connect() as connection:
        entries = connection.execute(
            """
            SELECT service_time_entries.*, services.name AS service_name,
                   services.hourly_rate_cents, services.billing_frequency,
                   services.notes AS service_description,
                   service_types.datev_account
            FROM service_time_entries
            JOIN services ON services.id = service_time_entries.service_id
            JOIN service_types ON service_types.id = services.service_type_id
            WHERE service_time_entries.contract_id = ?
              AND service_time_entries.status = 'approved'
              AND service_time_entries.invoice_id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM invoice_time_entries
                  WHERE invoice_time_entries.time_entry_id = service_time_entries.id
              )
              AND COALESCE(service_time_entries.end_date, service_time_entries.work_date) >= ?
              AND COALESCE(service_time_entries.start_date, service_time_entries.work_date) <= ?
            ORDER BY services.name, COALESCE(service_time_entries.start_date, service_time_entries.work_date)
            """,
            (contract_id, effective_start.isoformat(), period_end.isoformat()),
        ).fetchall()

    grouped: dict[int, dict[str, Any]] = {}
    for entry in entries:
        entry_start = parse_iso_date(entry["start_date"] or entry["work_date"]) or period_start
        entry_end = parse_iso_date(entry["end_date"] or entry["work_date"]) or entry_start
        frequency_key = entry["billing_frequency"] or contract["service_billing_frequency"]
        if frequency_key not in BILLING_FREQUENCIES:
            frequency_key = contract["service_billing_frequency"]
        if frequency_key != "once" and entry_end < period_start:
            continue
        service_id = entry["service_id"]
        if service_id not in grouped:
            frequency = BILLING_FREQUENCIES[frequency_key]
            grouped[service_id] = {
                "item_type": "service",
                "source_id": service_id,
                "description": billing_position_description(
                    f"Dienstleistung: {entry['service_name']}",
                    entry["service_description"],
                    frequency["label"],
                ),
                "hours": Decimal("0"),
                "hourly_rate_cents": entry["hourly_rate_cents"],
                "datev_account": entry["datev_account"],
                "time_entry_ids": [],
                "period_start": entry_start,
                "period_end": entry_end,
            }
        grouped[service_id]["hours"] += Decimal(str(entry["hours"]))
        grouped[service_id]["time_entry_ids"].append(entry["id"])
        grouped[service_id]["period_start"] = min(grouped[service_id]["period_start"], entry_start)
        grouped[service_id]["period_end"] = max(grouped[service_id]["period_end"], entry_end)

    lines = []
    for item in grouped.values():
        amount_cents = cents_from_decimal(item["hours"] * Decimal(item["hourly_rate_cents"]))
        lines.append(
            {
                "item_type": "service",
                "source_id": item["source_id"],
                "datev_account": item["datev_account"],
                "selection_key": f"service:{item['source_id']}:{period_start.isoformat()}:{period_end.isoformat()}",
                "description": item["description"],
                "quantity_text": work_quantity_text(item["hours"]),
                "amount_cents": amount_cents,
                "period_start": item["period_start"],
                "period_end": item["period_end"],
                "time_entry_ids": item["time_entry_ids"],
            }
        )
    return lines


def billing_lines_for_contract(
    contract_id: int,
    period_start: date,
    period_end: date,
    include_licenses: bool,
    include_services: bool,
    include_variable_costs: bool = True,
    include_flat_fees: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bundle = fetch_contract_bundle(contract_id)
    contract = bundle["contract"]
    lines: list[dict[str, Any]] = []
    if include_licenses:
        lines.extend(license_billing_lines(contract, period_start, period_end))
    if include_services:
        lines.extend(service_billing_lines(contract, period_start, period_end))
    if include_variable_costs:
        lines.extend(variable_cost_billing_lines(contract, period_start, period_end))
    if include_flat_fees:
        lines.extend(flat_fee_billing_lines(contract, period_start, period_end))
    return contract, lines


def grouped_billing_lines(
    contracts: list[dict[str, Any]],
    period_start: date,
    period_end: date,
    include_licenses: bool,
    include_services: bool,
    include_variable_costs: bool = True,
    include_flat_fees: bool = True,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for contract_option in contracts:
        contract, lines = billing_lines_for_contract(
            contract_option["id"],
            period_start,
            period_end,
            include_licenses,
            include_services,
            include_variable_costs,
            include_flat_fees,
        )
        if not lines:
            continue
        groups.append(
            {
                "contract": contract,
                "lines": lines,
                "total_cents": sum(line["amount_cents"] for line in lines),
            }
        )
    return groups


def grouped_potential_success_bonuses(period_start: date, period_end: date) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT flat_fees.*, flat_fee_types.datev_account,
                   contracts.id AS contract_id,
                   contracts.contract_number,
                   contracts.title AS contract_title,
                   contracts.currency,
                   companies.name AS company_name
            FROM flat_fees
            JOIN contracts ON contracts.id = flat_fees.contract_id
            JOIN companies ON companies.id = contracts.company_id
            LEFT JOIN flat_fee_types ON flat_fee_types.id = flat_fees.flat_fee_type_id
            WHERE flat_fees.status = 'active'
              AND flat_fees.fee_kind = 'success_bonus'
              AND flat_fees.approval_status = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM invoice_line_items
                  WHERE invoice_line_items.item_type = 'flat_fee'
                    AND invoice_line_items.billing_key = (
                        :flat_fee_prefix || CAST(flat_fees.id AS TEXT) || :flat_fee_suffix
                    )
              )
            ORDER BY companies.name, contracts.contract_number, flat_fees.name
            """,
            {"flat_fee_prefix": "flat_fee:", "flat_fee_suffix": ":once"},
        ).fetchall()

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        reference_date = (
            parse_iso_date(row["expected_success_date"])
            or parse_iso_date(row["success_date"])
            or parse_iso_date(row["start_date"])
        )
        if reference_date and (reference_date < period_start or reference_date > period_end):
            continue
        contract_id = row["contract_id"]
        group = grouped.setdefault(
            contract_id,
            {
                "contract": {
                    "id": contract_id,
                    "contract_number": row["contract_number"],
                    "title": row["contract_title"],
                    "company_name": row["company_name"],
                    "currency": row["currency"],
                },
                "lines": [],
                "total_cents": 0,
            },
        )
        item = dict(row)
        item["reference_date"] = reference_date
        group["lines"].append(item)
        group["total_cents"] += int(row["amount_cents"] or 0)
    return list(grouped.values())


def next_invoice_number(connection) -> str:
    current_year = date.today().year
    count = connection.execute(
        """
        SELECT COUNT(*)
        FROM invoices
        WHERE invoice_number LIKE ?
        """,
        (f"ABR-{current_year}-%",),
    ).fetchone()[0]
    return f"ABR-{current_year}-{count + 1:04d}"


def license_price_multiplier(contract: dict[str, Any], license_start: date, interval_start: date) -> Decimal:
    increase_percent = parse_percent(
        str(contract.get("license_price_increase_percent") or "8"),
        Decimal("8"),
    )
    if increase_percent == 0 or interval_start < license_start:
        return Decimal("1")

    completed_license_years = 0
    next_year_start = add_months(license_start, 12)
    while interval_start >= next_year_start:
        completed_license_years += 1
        next_year_start = add_months(license_start, 12 * (completed_license_years + 1))

    if completed_license_years == 0:
        return Decimal("1")
    annual_factor = Decimal("1") + (increase_percent / Decimal("100"))
    return annual_factor ** completed_license_years


def invoice_vat_amounts(contract: dict[str, Any], net_total_cents: int) -> dict[str, Any]:
    vat_treatment = validate_vat_treatment(contract.get("vat_treatment"))
    vat_rate = Decimal("19") if vat_treatment == "standard" else Decimal("0")
    vat_cents = cents_from_decimal(Decimal(net_total_cents) * vat_rate / Decimal("100"))
    return {
        "vat_treatment": vat_treatment,
        "vat_rate_percent": str(vat_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "vat_cents": vat_cents,
        "gross_total_cents": net_total_cents + vat_cents,
    }


def create_billing_invoice(
    contract_id: int,
    period_start: date,
    period_end: date,
    include_licenses: bool,
    include_services: bool,
    include_variable_costs: bool,
    include_flat_fees: bool,
    user_id: int,
    selected_line_keys: set[str] | None = None,
) -> int | None:
    contract, lines = billing_lines_for_contract(
        contract_id,
        period_start,
        period_end,
        include_licenses,
        include_services,
        include_variable_costs,
        include_flat_fees,
    )
    if selected_line_keys is not None:
        lines = [line for line in lines if line.get("selection_key") in selected_line_keys]
    if not lines:
        return None

    total_cents = sum(line["amount_cents"] for line in lines)
    vat_amounts = invoice_vat_amounts(contract, total_cents)
    line_period_starts = [line["period_start"] for line in lines if line.get("period_start")]
    line_period_ends = [line["period_end"] for line in lines if line.get("period_end")]
    invoice_period_start = min(line_period_starts) if line_period_starts else period_start
    invoice_period_end = max(line_period_ends) if line_period_ends else period_end
    timestamp = now_iso()
    with database.connect() as connection:
        invoice_number = next_invoice_number(connection)
        cursor = connection.execute(
            """
            INSERT INTO invoices (
                invoice_number, company_id, contract_id, period_start, period_end,
                status, currency, total_cents, vat_treatment, vat_rate_percent,
                vat_cents, gross_total_cents, include_licenses, include_services,
                include_variable_costs, include_flat_fees, datev_invoice_number,
                created_by, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_number,
                contract["company_id"],
                contract_id,
                invoice_period_start.isoformat(),
                invoice_period_end.isoformat(),
                contract["currency"],
                total_cents,
                vat_amounts["vat_treatment"],
                vat_amounts["vat_rate_percent"],
                vat_amounts["vat_cents"],
                vat_amounts["gross_total_cents"],
                1 if include_licenses else 0,
                1 if include_services else 0,
                1 if include_variable_costs else 0,
                1 if include_flat_fees else 0,
                "",
                user_id,
                timestamp,
                timestamp,
            ),
        )
        invoice_id = cursor.lastrowid
        for line in lines:
            connection.execute(
                """
                INSERT INTO invoice_line_items (
                    invoice_id, item_type, source_id, datev_account, billing_key,
                    description, quantity_text, amount_cents
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_id,
                    line["item_type"],
                    line["source_id"],
                    line.get("datev_account", ""),
                    line.get("billing_key", ""),
                    line["description"],
                    line["quantity_text"],
                    line["amount_cents"],
                ),
            )
            for entry_id in line.get("time_entry_ids", []):
                connection.execute(
                    "INSERT OR IGNORE INTO invoice_time_entries (invoice_id, time_entry_id) VALUES (?, ?)",
                    (invoice_id, entry_id),
                )
    return invoice_id


def release_draft_invoice_links(connection, invoice_id: int, timestamp: str) -> None:
    connection.execute(
        """
        UPDATE service_time_entries
        SET invoice_id = NULL,
            status = CASE WHEN status = 'billed' THEN 'approved' ELSE status END,
            updated_at = ?
        WHERE invoice_id = ?
           OR id IN (
               SELECT time_entry_id
               FROM invoice_time_entries
               WHERE invoice_id = ?
           )
        """,
        (timestamp, invoice_id, invoice_id),
    )
    connection.execute("DELETE FROM invoice_time_entries WHERE invoice_id = ?", (invoice_id,))
    connection.execute("DELETE FROM invoice_line_items WHERE invoice_id = ?", (invoice_id,))


def dashboard_data(user: dict[str, Any] | None = None) -> dict[str, Any]:
    with database.connect() as connection:
        metrics = {
            "company_count": connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "active_contract_count": connection.execute(
                "SELECT COUNT(*) FROM contracts WHERE status = 'active'"
            ).fetchone()[0],
            "active_license_count": connection.execute(
                "SELECT COUNT(*) FROM licenses WHERE status = 'active'"
            ).fetchone()[0],
            "draft_invoice_count": connection.execute(
                "SELECT COUNT(*) FROM invoices WHERE status = 'draft'"
            ).fetchone()[0],
            "approved_unbilled_hours": connection.execute(
                """
                SELECT COALESCE(SUM(hours), 0)
                FROM service_time_entries
                WHERE status = 'approved'
                  AND invoice_id IS NULL
                  AND service_id IS NOT NULL
                """
            ).fetchone()[0],
            "draft_total_cents": connection.execute(
                "SELECT COALESCE(SUM(total_cents), 0) FROM invoices WHERE status = 'draft'"
            ).fetchone()[0],
        }
        contracts = connection.execute(
            """
            SELECT contracts.*, companies.name AS company_name
            FROM contracts
            JOIN companies ON companies.id = contracts.company_id
            ORDER BY contracts.updated_at DESC
            LIMIT 6
            """
        ).fetchall()
        invoices = connection.execute(
            """
            SELECT invoices.*, companies.name AS company_name, contracts.contract_number
            FROM invoices
            JOIN companies ON companies.id = invoices.company_id
            JOIN contracts ON contracts.id = invoices.contract_id
            WHERE invoices.status = 'draft'
            ORDER BY invoices.created_at DESC
            LIMIT 6
            """
        ).fetchall()
        entries = connection.execute(
            """
            SELECT service_time_entries.*,
                   COALESCE(services.name, flat_fees.name) AS service_name,
                   CASE
                       WHEN service_time_entries.flat_fee_id IS NULL THEN 'Dienstleistung'
                       ELSE 'Pauschale'
                   END AS work_item_type,
                   contracts.contract_number, companies.name AS company_name,
                   users.full_name AS user_name
            FROM service_time_entries
            LEFT JOIN services ON services.id = service_time_entries.service_id
            LEFT JOIN flat_fees ON flat_fees.id = service_time_entries.flat_fee_id
            JOIN contracts ON contracts.id = service_time_entries.contract_id
            JOIN companies ON companies.id = contracts.company_id
            JOIN users ON users.id = service_time_entries.user_id
            ORDER BY COALESCE(service_time_entries.start_date, service_time_entries.work_date) DESC
            LIMIT 6
            """
        ).fetchall()

    if has_permission(user, "billing.create"):
        billing_start = default_billing_start()
        billing_end = date.today()
        billable_groups = grouped_billing_lines(
            contract_options(),
            billing_start,
            billing_end,
            True,
            True,
        )
        metrics.update(
            {
                "billable_total_cents": sum(group["total_cents"] for group in billable_groups),
                "billable_contract_count": len(billable_groups),
                "billable_line_count": sum(len(group["lines"]) for group in billable_groups),
            }
        )
    else:
        metrics.update(
            {
                "billable_total_cents": 0,
                "billable_contract_count": 0,
                "billable_line_count": 0,
            }
        )

    return {
        "metrics": metrics,
        "contracts": [dict(row) for row in contracts],
        "invoices": [dict(row) for row in invoices],
        "entries": [dict(row) for row in entries],
        "notification_panel": notification_panel_data(user),
    }


def percent(value: int | float | Decimal, maximum: int | float | Decimal) -> int:
    if not maximum:
        return 0
    return max(0, min(100, round(float(value) / float(maximum) * 100)))


def growth_metric(current: int | float | Decimal, previous: int | float | Decimal) -> dict[str, Any]:
    previous_value = Decimal(str(previous or 0))
    if previous_value == 0:
        return {"label": "-", "value": None, "class": ""}
    current_value = Decimal(str(current or 0))
    value = ((current_value - previous_value) * Decimal(100) / previous_value).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )
    prefix = "+" if value > 0 else ""
    return {
        "label": f"{prefix}{value}%".replace(".", ","),
        "value": value,
        "class": "success" if value >= 0 else "warning",
    }


def analytics_data(year: int) -> dict[str, Any]:
    today = date.today()
    current_month_start = today.replace(day=1)
    next_month_start = add_months(current_month_start, 1)
    previous_month_start = add_months(current_month_start, -1)
    previous_month_end = current_month_start - timedelta(days=1)
    previous_year_month_start = date(current_month_start.year - 1, current_month_start.month, 1)
    previous_year_month_end = add_months(previous_year_month_start, 1) - timedelta(days=1)
    months_in_scope = today.month if year == today.year else 12
    invoice_date_sql = (
        "COALESCE(NULLIF(invoices.datev_invoice_date, ''), substr(invoices.updated_at, 1, 10), "
        "substr(invoices.created_at, 1, 10), invoices.period_start)"
    )

    with database.connect() as connection:
        active_license_arr = connection.execute(
            """
            SELECT COALESCE(SUM(annual_amount_cents * quantity), 0)
            FROM licenses
            WHERE status = 'active'
            """
        ).fetchone()[0]
        finalized_revenue = connection.execute(
            """
            SELECT COALESCE(SUM(total_cents), 0)
            FROM invoices
            WHERE status = 'finalized'
            """
        ).fetchone()[0]
        selected_year_revenue = connection.execute(
            f"""
            SELECT COALESCE(SUM(total_cents), 0)
            FROM invoices
            WHERE status = 'finalized'
              AND substr({invoice_date_sql}, 1, 4) = ?
            """,
            (str(year),),
        ).fetchone()[0]
        current_month_revenue = connection.execute(
            f"""
            SELECT COALESCE(SUM(total_cents), 0)
            FROM invoices
            WHERE status = 'finalized'
              AND {invoice_date_sql} >= ?
              AND {invoice_date_sql} < ?
            """,
            (current_month_start.isoformat(), next_month_start.isoformat()),
        ).fetchone()[0]
        previous_month_revenue = connection.execute(
            f"""
            SELECT COALESCE(SUM(total_cents), 0)
            FROM invoices
            WHERE status = 'finalized'
              AND {invoice_date_sql} >= ?
              AND {invoice_date_sql} <= ?
            """,
            (previous_month_start.isoformat(), previous_month_end.isoformat()),
        ).fetchone()[0]
        previous_year_month_revenue = connection.execute(
            f"""
            SELECT COALESCE(SUM(total_cents), 0)
            FROM invoices
            WHERE status = 'finalized'
              AND {invoice_date_sql} >= ?
              AND {invoice_date_sql} <= ?
            """,
            (previous_year_month_start.isoformat(), previous_year_month_end.isoformat()),
        ).fetchone()[0]
        draft_pipeline = connection.execute(
            """
            SELECT COALESCE(SUM(total_cents), 0)
            FROM invoices
            WHERE status = 'draft'
            """
        ).fetchone()[0]
        unbilled = connection.execute(
            """
            SELECT COALESCE(SUM(service_time_entries.hours), 0) AS hours,
                   COALESCE(SUM(service_time_entries.hours * services.hourly_rate_cents), 0) AS amount
            FROM service_time_entries
            JOIN services ON services.id = service_time_entries.service_id
            WHERE service_time_entries.status = 'approved'
              AND service_time_entries.invoice_id IS NULL
            """
        ).fetchone()
        contract_count = connection.execute(
            "SELECT COUNT(*) FROM contracts WHERE status = 'active'"
        ).fetchone()[0]
        active_license_totals = connection.execute(
            """
            SELECT COUNT(*) AS license_count,
                   COALESCE(SUM(quantity), 0) AS license_quantity
            FROM licenses
            WHERE status = 'active'
            """
        ).fetchone()
        new_license_totals = connection.execute(
            """
            SELECT COUNT(*) AS license_count,
                   COALESCE(SUM(quantity), 0) AS license_quantity
            FROM licenses
            WHERE start_date >= ?
              AND start_date < ?
            """,
            (current_month_start.isoformat(), next_month_start.isoformat()),
        ).fetchone()
        revenue_by_type_rows = connection.execute(
            """
            SELECT invoice_line_items.item_type,
                   COALESCE(SUM(invoice_line_items.amount_cents), 0) AS amount_cents
            FROM invoice_line_items
            JOIN invoices ON invoices.id = invoice_line_items.invoice_id
            WHERE invoices.status = 'finalized'
            GROUP BY invoice_line_items.item_type
            """
        ).fetchall()
        line_rows = connection.execute(
            f"""
            SELECT billed_lines.*
            FROM (
                SELECT invoice_line_items.*, invoices.status, invoices.period_start,
                       {invoice_date_sql} AS invoice_date,
                       invoices.currency, companies.name AS company_name
                FROM invoice_line_items
                JOIN invoices ON invoices.id = invoice_line_items.invoice_id
                JOIN companies ON companies.id = invoices.company_id
            ) AS billed_lines
            WHERE substr(billed_lines.invoice_date, 1, 4) = ?
            """,
            (str(year),),
        ).fetchall()
        company_rows = connection.execute(
            f"""
            SELECT companies.name AS company_name,
                   SUM(CASE WHEN invoices.status = 'finalized' THEN invoices.total_cents ELSE 0 END) AS finalized_cents,
                   0 AS draft_cents
            FROM invoices
            JOIN companies ON companies.id = invoices.company_id
            WHERE invoices.status = 'finalized'
              AND substr({invoice_date_sql}, 1, 4) = ?
            GROUP BY companies.id
            ORDER BY finalized_cents DESC
            LIMIT 10
            """,
            (str(year),),
        ).fetchall()
        invoice_month_rows = connection.execute(
            f"""
            SELECT CAST(substr({invoice_date_sql}, 6, 2) AS INTEGER) AS month,
                   COUNT(*) AS invoice_count,
                   COALESCE(SUM(total_cents), 0) AS invoice_amount_cents
            FROM invoices
            WHERE status = 'finalized'
              AND substr({invoice_date_sql}, 1, 4) = ?
            GROUP BY CAST(substr({invoice_date_sql}, 6, 2) AS INTEGER)
            """,
            (str(year),),
        ).fetchall()
        revenue_comparison_rows = connection.execute(
            f"""
            SELECT CAST(substr({invoice_date_sql}, 1, 4) AS INTEGER) AS revenue_year,
                   CAST(substr({invoice_date_sql}, 6, 2) AS INTEGER) AS month,
                   COALESCE(SUM(total_cents), 0) AS amount_cents
            FROM invoices
            WHERE status = 'finalized'
              AND substr({invoice_date_sql}, 1, 4) IN (?, ?)
            GROUP BY CAST(substr({invoice_date_sql}, 1, 4) AS INTEGER),
                     CAST(substr({invoice_date_sql}, 6, 2) AS INTEGER)
            """,
            (str(year), str(year - 1)),
        ).fetchall()
        booked_hours_rows = connection.execute(
            """
            SELECT CAST(substr(COALESCE(start_date, work_date), 6, 2) AS INTEGER) AS month,
                   COALESCE(SUM(hours), 0) AS hours
            FROM service_time_entries
            WHERE substr(COALESCE(start_date, work_date), 1, 4) = ?
            GROUP BY CAST(substr(COALESCE(start_date, work_date), 6, 2) AS INTEGER)
            """,
            (str(year),),
        ).fetchall()
        license_timeline_rows = connection.execute(
            """
            SELECT licenses.annual_amount_cents, licenses.quantity,
                   licenses.start_date, licenses.end_date,
                   COALESCE(license_types.name, licenses.name) AS type_name
            FROM licenses
            LEFT JOIN license_types ON license_types.id = licenses.license_type_id
            WHERE status = 'active'
            ORDER BY type_name, licenses.name
            """
        ).fetchall()
        service_rows = connection.execute(
            """
            SELECT service_types.name, service_types.datev_account,
                   COALESCE(SUM(service_time_entries.hours), 0) AS hours,
                   COALESCE(SUM(service_time_entries.hours * services.hourly_rate_cents), 0) AS amount_cents
            FROM service_time_entries
            JOIN services ON services.id = service_time_entries.service_id
            JOIN service_types ON service_types.id = services.service_type_id
            WHERE service_time_entries.status = 'approved'
              AND service_time_entries.invoice_id IS NULL
            GROUP BY service_types.id
            ORDER BY amount_cents DESC
            """
        ).fetchall()
        license_rows = connection.execute(
            """
            SELECT license_types.name, license_types.datev_account,
                   COALESCE(SUM(licenses.annual_amount_cents * licenses.quantity), 0) AS arr_cents,
                   COUNT(licenses.id) AS license_count,
                   COALESCE(SUM(licenses.quantity), 0) AS license_quantity
            FROM license_types
            LEFT JOIN licenses ON licenses.license_type_id = license_types.id
                              AND licenses.status = 'active'
            GROUP BY license_types.id
            ORDER BY arr_cents DESC, license_types.name
            """
        ).fetchall()
        years = connection.execute(
            f"""
            SELECT DISTINCT substr({invoice_date_sql}, 1, 4) AS year
            FROM invoices
            WHERE status = 'finalized'
            ORDER BY year DESC
            """
        ).fetchall()

    monthly = [
        {
            "month": month,
            "label": date(year, month, 1).strftime("%m/%Y"),
            "finalized_cents": 0,
            "draft_cents": 0,
            "license_cents": 0,
            "service_cents": 0,
            "variable_cost_cents": 0,
            "flat_fee_cents": 0,
            "invoice_count": 0,
            "invoice_amount_cents": 0,
            "avg_invoice_cents": 0,
        }
        for month in range(1, 13)
    ]
    for row in invoice_month_rows:
        if row["month"]:
            monthly[row["month"] - 1]["invoice_count"] = row["invoice_count"]
            monthly[row["month"] - 1]["invoice_amount_cents"] = int(row["invoice_amount_cents"] or 0)
            monthly[row["month"] - 1]["avg_invoice_cents"] = (
                round((row["invoice_amount_cents"] or 0) / row["invoice_count"])
                if row["invoice_count"]
                else 0
            )

    revenue_mix = {"license": 0, "service": 0, "variable_cost": 0, "flat_fee": 0}
    for row in line_rows:
        invoice_date = parse_iso_date(row["invoice_date"])
        if not invoice_date:
            continue
        item = monthly[invoice_date.month - 1]
        if row["status"] == "finalized":
            item["finalized_cents"] += row["amount_cents"]
            revenue_mix[row["item_type"]] = revenue_mix.get(row["item_type"], 0) + row["amount_cents"]
            if row["item_type"] == "license":
                item["license_cents"] += row["amount_cents"]
            elif row["item_type"] == "service":
                item["service_cents"] += row["amount_cents"]
            elif row["item_type"] == "variable_cost":
                item["variable_cost_cents"] += row["amount_cents"]
            elif row["item_type"] == "flat_fee":
                item["flat_fee_cents"] += row["amount_cents"]
        elif row["status"] == "draft":
            item["draft_cents"] += row["amount_cents"]

    revenue_mix_total = sum(revenue_mix.values())
    revenue_mix["total_cents"] = revenue_mix_total
    revenue_mix["license_percent"] = percent(revenue_mix.get("license", 0), revenue_mix_total)
    revenue_mix["service_percent"] = percent(revenue_mix.get("service", 0), revenue_mix_total)
    revenue_mix["variable_cost_percent"] = percent(revenue_mix.get("variable_cost", 0), revenue_mix_total)
    revenue_mix["flat_fee_percent"] = percent(revenue_mix.get("flat_fee", 0), revenue_mix_total)
    revenue_mix["service_end_percent"] = revenue_mix["license_percent"] + revenue_mix["service_percent"]
    revenue_mix["variable_end_percent"] = revenue_mix["service_end_percent"] + revenue_mix["variable_cost_percent"]

    max_monthly = max(
        [month["finalized_cents"] for month in monthly]
        + [month["draft_cents"] for month in monthly]
        + [1]
    )
    for item in monthly:
        item["finalized_percent"] = percent(item["finalized_cents"], max_monthly)
        item["draft_percent"] = percent(item["draft_cents"], max_monthly)
    max_invoice_count = max([month["invoice_count"] for month in monthly] + [1])
    for item in monthly:
        item["invoice_count_percent"] = percent(item["invoice_count"], max_invoice_count)

    month_labels = [date(year, month, 1).strftime("%m/%Y") for month in range(1, 13)]
    short_month_labels = [date(year, month, 1).strftime("%m") for month in range(1, 13)]
    current_year_revenue = [0 for _ in range(12)]
    previous_year_revenue = [0 for _ in range(12)]
    for row in revenue_comparison_rows:
        month = row["month"]
        if not month:
            continue
        amount_cents = int(row["amount_cents"] or 0)
        if row["revenue_year"] == year:
            current_year_revenue[month - 1] = amount_cents
        elif row["revenue_year"] == year - 1:
            previous_year_revenue[month - 1] = amount_cents
    revenue_difference = [
        current_year_revenue[index] - previous_year_revenue[index]
        for index in range(12)
    ]

    booked_hours_by_month = [0.0 for _ in range(12)]
    for row in booked_hours_rows:
        month = row["month"]
        if month:
            booked_hours_by_month[month - 1] = float(row["hours"] or 0)

    license_instance_count_by_month = [0 for _ in range(12)]
    license_count_by_month = [0 for _ in range(12)]
    license_arr_by_month = [0 for _ in range(12)]
    license_type_names = sorted(
        {
            (row["type_name"] or "Ohne Katalogart")
            for row in license_timeline_rows
        }
    )
    license_type_series = {name: [0 for _ in range(12)] for name in license_type_names}
    for month in range(1, 13):
        month_start = date(year, month, 1)
        month_end = add_months(month_start, 1) - timedelta(days=1)
        for row in license_timeline_rows:
            license_start = parse_iso_date(row["start_date"])
            license_end = parse_iso_date(row["end_date"])
            if not license_start or license_start > month_end:
                continue
            if license_end and license_end < month_start:
                continue
            quantity = int(row["quantity"] or 0)
            type_name = row["type_name"] or "Ohne Katalogart"
            license_instance_count_by_month[month - 1] += 1
            license_count_by_month[month - 1] += quantity
            license_type_series[type_name][month - 1] += quantity
            license_arr_by_month[month - 1] += int(row["annual_amount_cents"] or 0) * quantity
    license_mrr_by_month = [round(amount / 12) for amount in license_arr_by_month]
    avg_license_revenue_by_month = [
        round(monthly[index]["license_cents"] / license_count_by_month[index])
        if license_count_by_month[index]
        else 0
        for index in range(12)
    ]

    analytics_charts = {
        "labels": month_labels,
        "shortLabels": short_month_labels,
        "revenueTrend": {
            "label": f"Umsatz {year}",
            "values": current_year_revenue,
        },
        "revenueComponents": {
            "license": [int(item["license_cents"] or 0) for item in monthly],
            "service": [int(item["service_cents"] or 0) for item in monthly],
            "flatFee": [int(item["flat_fee_cents"] or 0) for item in monthly],
            "variableCost": [int(item["variable_cost_cents"] or 0) for item in monthly],
        },
        "yearComparison": {
            "currentYear": year,
            "previousYear": year - 1,
            "current": current_year_revenue,
            "previous": previous_year_revenue,
            "difference": revenue_difference,
        },
        "recurringRevenue": {
            "mrr": license_mrr_by_month,
            "arr": license_arr_by_month,
        },
        "activeLicenses": {
            "count": license_count_by_month,
            "instances": license_instance_count_by_month,
            "averageRevenue": avg_license_revenue_by_month,
        },
        "licenseTypes": {
            "series": [
                {"label": name, "values": values}
                for name, values in license_type_series.items()
            ],
        },
        "invoiceActivity": {
            "count": [int(item["invoice_count"] or 0) for item in monthly],
            "averageAmount": [int(item["avg_invoice_cents"] or 0) for item in monthly],
        },
        "bookedHours": {
            "hours": booked_hours_by_month,
        },
    }

    max_company = max(
        [row["finalized_cents"] or 0 for row in company_rows]
        + [row["draft_cents"] or 0 for row in company_rows]
        + [1]
    )
    top_companies = []
    for row in company_rows:
        item = dict(row)
        item["finalized_percent"] = percent(item["finalized_cents"] or 0, max_company)
        item["draft_percent"] = percent(item["draft_cents"] or 0, max_company)
        top_companies.append(item)

    max_license = max([row["arr_cents"] or 0 for row in license_rows] + [1])
    license_by_type = []
    for row in license_rows:
        item = dict(row)
        item["percent"] = percent(item["arr_cents"] or 0, max_license)
        license_by_type.append(item)

    max_service = max([row["amount_cents"] or 0 for row in service_rows] + [1])
    unbilled_by_service = []
    for row in service_rows:
        item = dict(row)
        item["percent"] = percent(item["amount_cents"] or 0, max_service)
        unbilled_by_service.append(item)

    available_years = [int(row["year"]) for row in years if row["year"]]
    if year not in available_years:
        available_years.insert(0, year)

    revenue_by_type = {row["item_type"]: row["amount_cents"] for row in revenue_by_type_rows}
    active_license_count = active_license_totals["license_count"] or 0
    active_license_quantity = active_license_totals["license_quantity"] or 0
    new_license_count = new_license_totals["license_count"] or 0
    new_license_quantity = new_license_totals["license_quantity"] or 0
    avg_monthly_revenue = round(selected_year_revenue / months_in_scope) if months_in_scope else 0
    avg_revenue_per_license = (
        round(active_license_arr / active_license_quantity) if active_license_quantity else 0
    )
    mrr = round(active_license_arr / 12) if active_license_arr else 0

    return {
        "year": year,
        "available_years": available_years,
        "metrics": {
            "total_revenue": finalized_revenue,
            "active_license_arr": active_license_arr,
            "mrr": mrr,
            "finalized_revenue": finalized_revenue,
            "selected_year_revenue": selected_year_revenue,
            "current_month_revenue": current_month_revenue,
            "previous_month_revenue": previous_month_revenue,
            "previous_year_month_revenue": previous_year_month_revenue,
            "mom_growth": growth_metric(current_month_revenue, previous_month_revenue),
            "yoy_growth": growth_metric(current_month_revenue, previous_year_month_revenue),
            "avg_monthly_revenue": avg_monthly_revenue,
            "draft_pipeline": draft_pipeline,
            "license_revenue_total": revenue_by_type.get("license", 0),
            "service_revenue_total": revenue_by_type.get("service", 0),
            "variable_cost_revenue_total": revenue_by_type.get("variable_cost", 0),
            "flat_fee_revenue_total": revenue_by_type.get("flat_fee", 0),
            "active_license_count": active_license_count,
            "active_license_quantity": active_license_quantity,
            "new_license_count": new_license_count,
            "new_license_quantity": new_license_quantity,
            "avg_revenue_per_license": avg_revenue_per_license,
            "unbilled_hours": unbilled["hours"],
            "unbilled_amount": cents_from_decimal(Decimal(str(unbilled["amount"]))),
            "avg_arr_per_active_contract": round(active_license_arr / contract_count) if contract_count else 0,
        },
        "monthly": monthly,
        "revenue_mix": revenue_mix,
        "analytics_charts": analytics_charts,
        "top_companies": top_companies,
        "license_by_type": license_by_type,
        "unbilled_by_service": unbilled_by_service,
    }


@app.get("/login")
def login_form(request: Request):
    if get_current_user(request):
        return redirect_to("/")
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT users.*, roles.name AS role_name
            FROM users
            JOIN roles ON roles.id = users.role_id
            WHERE users.username = ?
            """,
            (username.strip(),),
        ).fetchone()

    if row is None or not row["active"] or not verify_password(password, row["password_hash"]):
        return render(
            request,
            "login.html",
            {"error": "Benutzername oder Passwort ist falsch."},
            status_code=400,
        )

    response = redirect_to("/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token({"user_id": row["id"]}),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite="lax",
    )
    return response


@app.get("/logout")
def logout():
    response = redirect_to("/login")
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def dashboard(request: Request, user: dict[str, Any] = Depends(require_user)):
    return render(request, "dashboard.html", dashboard_data(user))


@app.post("/notifications/{notification_id}/acknowledge")
def acknowledge_notification(
    notification_id: int,
    return_to: str = Form("/"),
    user: dict[str, Any] = Depends(require_permission("contracts.view")),
):
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/"
    timestamp = now_iso()
    with database.connect() as connection:
        result = connection.execute(
            """
            UPDATE notifications
            SET acknowledged_at = ?,
                acknowledged_by = ?,
                updated_at = ?
            WHERE id = ?
              AND acknowledged_at IS NULL
            """,
            (timestamp, user["id"], timestamp, notification_id),
        )
        if result.rowcount == 0:
            notification = connection.execute(
                "SELECT id FROM notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
            if notification is None:
                raise HTTPException(status_code=404, detail="Benachrichtigung nicht gefunden.")
    return redirect_to(target)


@app.get("/analytics")
def analytics(
    request: Request,
    year: int | None = Query(None),
    _: dict[str, Any] = Depends(require_permission("analytics.view")),
):
    selected_year = year or date.today().year
    return render(request, "analytics.html", analytics_data(selected_year))


@app.get("/notifications")
def notifications_index(
    request: Request,
    _: dict[str, Any] = Depends(require_superadmin),
):
    return render(request, "notifications.html", notifications_table_data())


@app.get("/companies")
def companies_index(request: Request, _: dict[str, Any] = Depends(require_permission("companies.view"))):
    with database.connect() as connection:
        companies = connection.execute(
            """
            SELECT companies.*,
                   COUNT(contracts.id) AS contract_count
            FROM companies
            LEFT JOIN contracts ON contracts.company_id = companies.id
            GROUP BY companies.id
            ORDER BY companies.name
            """
        ).fetchall()
    return render(request, "companies.html", {"companies": [dict(row) for row in companies]})


@app.get("/companies/new")
def company_form(request: Request, _: dict[str, Any] = Depends(require_permission("companies.manage"))):
    return render(
        request,
        "company_form.html",
        {
            "form_action": "/companies",
            "form": {"status": "active"},
        },
    )


@app.post("/companies")
def create_company(
    request: Request,
    name: str = Form(...),
    legal_name: str = Form(""),
    customer_number: str = Form(""),
    industry: str = Form(""),
    status_value: str = Form("active", alias="status"),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    billing_recipient_name: str = Form(""),
    billing_recipient_email: str = Form(""),
    billing_recipient_phone: str = Form(""),
    customer_supplier_number: str = Form(""),
    notes: str = Form(""),
    logo: UploadFile | None = File(None),
    _: dict[str, Any] = Depends(require_permission("companies.manage")),
):
    form_values = {
        "name": name,
        "legal_name": legal_name,
        "customer_number": customer_number,
        "industry": industry,
        "status": status_value,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "billing_recipient_name": billing_recipient_name,
        "billing_recipient_email": billing_recipient_email,
        "billing_recipient_phone": billing_recipient_phone,
        "customer_supplier_number": customer_supplier_number,
        "notes": notes,
    }
    try:
        if status_value not in COMPANY_STATUSES:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
        cleaned_name = required_text(name, "Name")
        cleaned_contact_name = required_text(contact_name, "Ansprechpartner Name")
        cleaned_contact_email = required_text(contact_email, "Ansprechpartner E-Mail")
        cleaned_contact_phone = required_text(contact_phone, "Ansprechpartner Telefonnummer")
        cleaned_billing_name = required_text(billing_recipient_name, "Rechnungsempfaenger Name")
        cleaned_billing_email = required_text(billing_recipient_email, "Rechnungsempfaenger E-Mail")
        cleaned_billing_phone = required_text(billing_recipient_phone, "Rechnungsempfaenger Telefonnummer")
        logo_original_filename, logo_stored_filename = save_company_logo(logo)
    except (ValueError, OSError) as exc:
        return render(
            request,
            "company_form.html",
            {
                "form_action": "/companies",
                "cancel_url": "/companies",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    timestamp = now_iso()
    with database.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO companies (
                name, legal_name, customer_number, industry, status,
                contact_name, contact_email, contact_phone,
                billing_recipient_name, billing_recipient_email, billing_recipient_phone,
                customer_supplier_number,
                logo_original_filename, logo_stored_filename,
                notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_name,
                legal_name.strip(),
                customer_number.strip(),
                industry.strip(),
                status_value,
                cleaned_contact_name,
                cleaned_contact_email,
                cleaned_contact_phone,
                cleaned_billing_name,
                cleaned_billing_email,
                cleaned_billing_phone,
                customer_supplier_number.strip(),
                logo_original_filename,
                logo_stored_filename,
                notes.strip(),
                timestamp,
                timestamp,
            ),
        )
        company_id = cursor.lastrowid
        materialize_standard_characteristics(connection, "company", company_id)
    return redirect_to(f"/companies/{company_id}")


@app.get("/companies/{company_id}/edit")
def edit_company_form(
    request: Request,
    company_id: int,
    _: dict[str, Any] = Depends(require_permission("companies.manage")),
):
    company = fetch_one(
        "SELECT * FROM companies WHERE id = ?",
        (company_id,),
        "Unternehmen nicht gefunden.",
    )
    return render(
        request,
        "company_form.html",
        {
            "form_title": "Unternehmen bearbeiten",
            "form_action": f"/companies/{company_id}/update",
            "cancel_url": f"/companies/{company_id}",
            "form": dict(company),
        },
    )


@app.post("/companies/{company_id}/update")
def update_company(
    request: Request,
    company_id: int,
    name: str = Form(...),
    legal_name: str = Form(""),
    customer_number: str = Form(""),
    industry: str = Form(""),
    status_value: str = Form("active", alias="status"),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    billing_recipient_name: str = Form(""),
    billing_recipient_email: str = Form(""),
    billing_recipient_phone: str = Form(""),
    customer_supplier_number: str = Form(""),
    notes: str = Form(""),
    logo: UploadFile | None = File(None),
    _: dict[str, Any] = Depends(require_permission("companies.manage")),
):
    existing = fetch_one(
        "SELECT * FROM companies WHERE id = ?",
        (company_id,),
        "Unternehmen nicht gefunden.",
    )
    form_values = {
        "id": company_id,
        "name": name,
        "legal_name": legal_name,
        "customer_number": customer_number,
        "industry": industry,
        "status": status_value,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "billing_recipient_name": billing_recipient_name,
        "billing_recipient_email": billing_recipient_email,
        "billing_recipient_phone": billing_recipient_phone,
        "customer_supplier_number": customer_supplier_number,
        "notes": notes,
        "logo_original_filename": existing["logo_original_filename"],
        "logo_stored_filename": existing["logo_stored_filename"],
    }
    try:
        if status_value not in COMPANY_STATUSES:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
        cleaned_name = required_text(name, "Name")
        cleaned_contact_name = required_text(contact_name, "Ansprechpartner Name")
        cleaned_contact_email = required_text(contact_email, "Ansprechpartner E-Mail")
        cleaned_contact_phone = required_text(contact_phone, "Ansprechpartner Telefonnummer")
        cleaned_billing_name = required_text(billing_recipient_name, "Rechnungsempfaenger Name")
        cleaned_billing_email = required_text(billing_recipient_email, "Rechnungsempfaenger E-Mail")
        cleaned_billing_phone = required_text(billing_recipient_phone, "Rechnungsempfaenger Telefonnummer")
        new_logo_original_filename, new_logo_stored_filename = save_company_logo(logo)
    except (ValueError, OSError) as exc:
        return render(
            request,
            "company_form.html",
            {
                "form_title": "Unternehmen bearbeiten",
                "form_action": f"/companies/{company_id}/update",
                "cancel_url": f"/companies/{company_id}",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    logo_original_filename = new_logo_original_filename or existing["logo_original_filename"]
    logo_stored_filename = new_logo_stored_filename or existing["logo_stored_filename"]
    timestamp = now_iso()
    with database.connect() as connection:
        result = connection.execute(
            """
            UPDATE companies
            SET name = ?,
                legal_name = ?,
                customer_number = ?,
                industry = ?,
                status = ?,
                contact_name = ?,
                contact_email = ?,
                contact_phone = ?,
                billing_recipient_name = ?,
                billing_recipient_email = ?,
                billing_recipient_phone = ?,
                customer_supplier_number = ?,
                logo_original_filename = ?,
                logo_stored_filename = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                cleaned_name,
                legal_name.strip(),
                customer_number.strip(),
                industry.strip(),
                status_value,
                cleaned_contact_name,
                cleaned_contact_email,
                cleaned_contact_phone,
                cleaned_billing_name,
                cleaned_billing_email,
                cleaned_billing_phone,
                customer_supplier_number.strip(),
                logo_original_filename,
                logo_stored_filename,
                notes.strip(),
                timestamp,
                company_id,
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden.")
    return redirect_to(f"/companies/{company_id}")


@app.get("/companies/{company_id}/logo")
def company_logo(company_id: int, _: dict[str, Any] = Depends(require_permission("companies.view"))):
    company = fetch_one(
        "SELECT logo_original_filename, logo_stored_filename FROM companies WHERE id = ?",
        (company_id,),
        "Unternehmen nicht gefunden.",
    )
    if not company["logo_stored_filename"]:
        raise HTTPException(status_code=404, detail="Kein Unternehmenslogo hinterlegt.")
    path = database.LOGO_DIR / company["logo_stored_filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Unternehmenslogo nicht gefunden.")
    return FileResponse(
        path,
        media_type=logo_media_type(company["logo_stored_filename"]),
        filename=company["logo_original_filename"] or company["logo_stored_filename"],
    )


@app.post("/companies/{company_id}/delete")
def delete_company(
    company_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    with database.connect() as connection:
        company = connection.execute("SELECT id FROM companies WHERE id = ?", (company_id,)).fetchone()
        if company is None:
            raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden.")
        dependency_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM contracts WHERE company_id = ?)
                + (SELECT COUNT(*) FROM invoices WHERE company_id = ?)
            """,
            (company_id, company_id),
        ).fetchone()[0]
        if dependency_count:
            raise HTTPException(
                status_code=400,
                detail="Unternehmen mit abhaengigen Vertraegen oder Rechnungen koennen nicht geloescht werden.",
            )
        connection.execute(
            "DELETE FROM characteristic_values WHERE target_type = 'company' AND target_id = ?",
            (company_id,),
        )
        connection.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return redirect_to("/companies")


@app.get("/companies/{company_id}")
def company_detail(
    request: Request,
    company_id: int,
    tab: str = Query("overview"),
    user: dict[str, Any] = Depends(require_permission("companies.view")),
):
    with database.connect() as connection:
        company = connection.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if company is None:
            raise HTTPException(status_code=404, detail="Unternehmen nicht gefunden.")
        company_dependency_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM contracts WHERE company_id = ?)
                + (SELECT COUNT(*) FROM invoices WHERE company_id = ?)
            """,
            (company_id, company_id),
        ).fetchone()[0]
        contracts = connection.execute(
            """
            SELECT *
            FROM contracts
            WHERE company_id = ?
            ORDER BY start_date DESC
            """,
            (company_id,),
        ).fetchall()
        active_licenses = connection.execute(
            """
            SELECT licenses.*, license_types.datev_account,
                   contracts.id AS contract_id, contracts.contract_number,
                   contracts.title AS contract_title, contracts.currency
            FROM licenses
            JOIN contracts ON contracts.id = licenses.contract_id
            JOIN license_types ON license_types.id = licenses.license_type_id
            WHERE contracts.company_id = ?
              AND licenses.status = 'active'
            ORDER BY contracts.contract_number, licenses.name
            """,
            (company_id,),
        ).fetchall()
        invoice_summary = {
            "total_cents": 0,
            "license_cents": 0,
            "service_cents": 0,
            "variable_cost_cents": 0,
            "flat_fee_cents": 0,
        }
        finalized_invoices: list[dict[str, Any]] = []
        if has_permission(user, "billing.create") or has_permission(user, "analytics.view"):
            invoice_total = connection.execute(
                """
                SELECT COALESCE(SUM(total_cents), 0)
                FROM invoices
                WHERE company_id = ?
                  AND status = 'finalized'
                """,
                (company_id,),
            ).fetchone()[0]
            invoice_summary["total_cents"] = invoice_total
            invoice_split_rows = connection.execute(
                """
                SELECT invoice_line_items.item_type,
                       COALESCE(SUM(invoice_line_items.amount_cents), 0) AS amount_cents
                FROM invoice_line_items
                JOIN invoices ON invoices.id = invoice_line_items.invoice_id
                WHERE invoices.company_id = ?
                  AND invoices.status = 'finalized'
                GROUP BY invoice_line_items.item_type
                """,
                (company_id,),
            ).fetchall()
            for row in invoice_split_rows:
                if row["item_type"] == "license":
                    invoice_summary["license_cents"] = row["amount_cents"]
                elif row["item_type"] == "service":
                    invoice_summary["service_cents"] = row["amount_cents"]
                elif row["item_type"] == "variable_cost":
                    invoice_summary["variable_cost_cents"] = row["amount_cents"]
                elif row["item_type"] == "flat_fee":
                    invoice_summary["flat_fee_cents"] = row["amount_cents"]
            finalized_invoices = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT invoices.*, contracts.contract_number, contracts.title AS contract_title
                    FROM invoices
                    JOIN contracts ON contracts.id = invoices.contract_id
                    WHERE invoices.company_id = ?
                      AND invoices.status = 'finalized'
                    ORDER BY COALESCE(invoices.datev_invoice_date, invoices.period_end) DESC,
                             invoices.id DESC
                    """,
                    (company_id,),
                ).fetchall()
            ]

        booked_hours = 0
        time_aggregates: list[dict[str, Any]] = []
        if has_permission(user, "time.approve"):
            booked_hours = connection.execute(
                """
                SELECT COALESCE(SUM(service_time_entries.hours), 0)
                FROM service_time_entries
                JOIN contracts ON contracts.id = service_time_entries.contract_id
                WHERE contracts.company_id = ?
                """,
                (company_id,),
            ).fetchone()[0]
            time_aggregates = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT contracts.id AS contract_id, contracts.contract_number,
                           contracts.title AS contract_title,
                           COALESCE(services.name, flat_fees.name) AS service_name,
                           CASE
                               WHEN service_time_entries.flat_fee_id IS NULL THEN 'Dienstleistung'
                               ELSE 'Pauschale'
                           END AS work_item_type,
                           COALESCE(service_types.datev_account, flat_fee_types.datev_account) AS datev_account,
                           service_time_entries.status,
                           COUNT(service_time_entries.id) AS entry_count,
                           COALESCE(SUM(service_time_entries.hours), 0) AS hours,
                           CAST(COALESCE(SUM(
                               CASE
                                   WHEN services.id IS NULL THEN 0
                                   ELSE ROUND(service_time_entries.hours * services.hourly_rate_cents)
                               END
                           ), 0) AS INTEGER) AS amount_cents,
                           MIN(COALESCE(service_time_entries.start_date, service_time_entries.work_date)) AS first_work_date,
                           MAX(COALESCE(service_time_entries.end_date, service_time_entries.work_date)) AS last_work_date
                    FROM service_time_entries
                    JOIN contracts ON contracts.id = service_time_entries.contract_id
                    LEFT JOIN services ON services.id = service_time_entries.service_id
                    LEFT JOIN service_types ON service_types.id = services.service_type_id
                    LEFT JOIN flat_fees ON flat_fees.id = service_time_entries.flat_fee_id
                    LEFT JOIN flat_fee_types ON flat_fee_types.id = flat_fees.flat_fee_type_id
                    WHERE contracts.company_id = ?
                    GROUP BY contracts.id,
                             contracts.contract_number,
                             contracts.title,
                             service_time_entries.flat_fee_id,
                             services.id,
                             services.name,
                             flat_fees.id,
                             flat_fees.name,
                             service_types.datev_account,
                             flat_fee_types.datev_account,
                             service_time_entries.status
                    ORDER BY contracts.contract_number,
                             COALESCE(services.name, flat_fees.name),
                             service_time_entries.status
                    """,
                    (company_id,),
                ).fetchall()
            ]

    active_license_items = [dict(row) for row in active_licenses]
    license_quantity = sum(item["quantity"] or 0 for item in active_license_items)
    active_license_arr = 0
    for item in active_license_items:
        item["annual_total_cents"] = (item["annual_amount_cents"] or 0) * (item["quantity"] or 0)
        item["billing_summary"] = license_billing_strategy_summary(item)
        active_license_arr += item["annual_total_cents"]

    company_metrics = {
        "active_license_count": len(active_license_items),
        "active_license_quantity": license_quantity,
        "active_license_arr_cents": active_license_arr,
        "booked_hours": booked_hours,
    }
    available_tabs = {"overview", "contracts", "licenses"}
    if has_permission(user, "billing.create") or has_permission(user, "analytics.view"):
        available_tabs.add("invoices")
    if has_permission(user, "time.approve"):
        available_tabs.add("time")
    active_company_tab = tab if tab in available_tabs else "overview"
    return render(
        request,
        "company_detail.html",
        {
            "company": dict(company),
            "company_can_delete": company_dependency_count == 0,
            "contracts": [dict(row) for row in contracts],
            "active_licenses": active_license_items,
            "invoice_summary": invoice_summary,
            "finalized_invoices": finalized_invoices,
            "time_aggregates": time_aggregates,
            "company_metrics": company_metrics,
            "active_company_tab": active_company_tab,
            "characteristics": list_characteristics("company", company_id),
            "characteristic_definitions": list_characteristic_definitions("company"),
        },
    )


@app.post("/companies/{company_id}/characteristics")
def set_company_characteristic(
    company_id: int,
    definition_id: int = Form(...),
    value_text: str = Form(...),
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    set_characteristic_value("company", company_id, definition_id, value_text)
    return redirect_to(f"/companies/{company_id}")


@app.get("/contracts")
def contracts_index(request: Request, _: dict[str, Any] = Depends(require_permission("contracts.view"))):
    settings = app_settings()
    with database.connect() as connection:
        contracts = connection.execute(
            """
            SELECT contracts.*, companies.id AS company_id, companies.name AS company_name,
                   companies.logo_stored_filename AS company_logo_stored_filename,
                   COUNT(licenses.id) AS license_count
            FROM contracts
            JOIN companies ON companies.id = contracts.company_id
            LEFT JOIN licenses ON licenses.contract_id = contracts.id
            GROUP BY contracts.id,
                     companies.id,
                     companies.name,
                     companies.logo_stored_filename
            ORDER BY companies.name, contracts.start_date DESC
            """
        ).fetchall()
        service_volumes = contracted_service_summaries(connection)
    contract_items = [dict(row) for row in contracts]
    for contract in contract_items:
        contract.update(
            service_volumes.get(
                contract["id"],
                {
                    "contracted_service_hours": 0,
                    "contracted_service_cents": 0,
                    "free_service_hours": 0,
                    "free_service_cents": 0,
                },
            )
        )
        contract["contracted_service_days"] = (
            Decimal(str(contract["contracted_service_hours"])) / settings["workday_hours"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        contract["free_service_days"] = (
            Decimal(str(contract["free_service_hours"])) / settings["workday_hours"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        contract["free_service_percent"] = percent(
            contract["free_service_cents"],
            contract["contracted_service_cents"],
        )
    return render(request, "contracts.html", {"contracts": contract_items})


@app.get("/licenses")
def licenses_index(request: Request, _: dict[str, Any] = Depends(require_permission("contracts.view"))):
    with database.connect() as connection:
        licenses = connection.execute(
            """
            SELECT licenses.*, license_types.datev_account,
                   contracts.id AS contract_id, contracts.contract_number, contracts.title AS contract_title,
                   contracts.currency,
                   companies.id AS company_id, companies.name AS company_name,
                   companies.logo_stored_filename AS company_logo_stored_filename
            FROM licenses
            JOIN contracts ON contracts.id = licenses.contract_id
            JOIN companies ON companies.id = contracts.company_id
            LEFT JOIN license_types ON license_types.id = licenses.license_type_id
            ORDER BY companies.name, contracts.contract_number, licenses.name
            """
        ).fetchall()
    license_items = [dict(row) for row in licenses]
    for item in license_items:
        item["annual_total_cents"] = (item["annual_amount_cents"] or 0) * (item["quantity"] or 0)
        item["billing_summary"] = license_billing_strategy_summary(item)
    return render(request, "licenses.html", {"licenses": license_items})


@app.get("/services")
def services_index(request: Request, _: dict[str, Any] = Depends(require_permission("contracts.view"))):
    settings = app_settings()
    with database.connect() as connection:
        services = connection.execute(
            """
            SELECT services.*, service_types.datev_account,
                   contracts.id AS contract_id, contracts.contract_number, contracts.title AS contract_title,
                   contracts.currency,
                   companies.id AS company_id, companies.name AS company_name,
                   companies.logo_stored_filename AS company_logo_stored_filename
            FROM services
            JOIN contracts ON contracts.id = services.contract_id
            JOIN companies ON companies.id = contracts.company_id
            LEFT JOIN service_types ON service_types.id = services.service_type_id
            ORDER BY companies.name, contracts.contract_number, services.name
            """
        ).fetchall()
    service_items = [dict(row) for row in services]
    for item in service_items:
        contracted_hours = item.get("contracted_hours")
        item["contracted_days"] = None
        item["contracted_volume_cents"] = 0
        if contracted_hours is not None:
            hours = Decimal(str(contracted_hours))
            item["contracted_days"] = (hours / settings["workday_hours"]).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            item["contracted_volume_cents"] = cents_from_decimal(hours * Decimal(item["hourly_rate_cents"]))
    return render(request, "services.html", {"services": service_items})


@app.get("/flat-fees")
def flat_fees_index(request: Request, _: dict[str, Any] = Depends(require_permission("contracts.view"))):
    with database.connect() as connection:
        flat_fees = connection.execute(
            """
            SELECT flat_fees.*, flat_fee_types.datev_account,
                   contracts.id AS contract_id, contracts.contract_number, contracts.title AS contract_title,
                   contracts.currency,
                   companies.id AS company_id, companies.name AS company_name,
                   companies.logo_stored_filename AS company_logo_stored_filename
            FROM flat_fees
            JOIN contracts ON contracts.id = flat_fees.contract_id
            JOIN companies ON companies.id = contracts.company_id
            LEFT JOIN flat_fee_types ON flat_fee_types.id = flat_fees.flat_fee_type_id
            ORDER BY companies.name, contracts.contract_number, flat_fees.name
            """
        ).fetchall()
    return render(request, "flat_fees.html", {"flat_fees": [dict(row) for row in flat_fees]})


@app.get("/contracts/new")
def contract_form(
    request: Request,
    company_id: int | None = Query(None),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    companies = company_options()
    form_values = {
        "company_id": company_id,
        "status": "active",
        "start_date": today_iso(),
        "license_billing_frequency": "quarterly",
        "service_billing_frequency": "monthly",
        "variable_billing_frequency": "monthly",
        "license_price_increase_percent": "8",
        "vat_treatment": "standard",
        "currency": "EUR",
        "service_hourly_rate": "0,00",
    }
    apply_company_contact_defaults(form_values, company_contact_defaults(company_id))
    return render(
        request,
        "contract_form.html",
        {
            "companies": companies,
            "form_action": "/contracts",
            "form": form_values,
        },
    )


@app.post("/contracts")
def create_contract(
    request: Request,
    company_id: int = Form(...),
    contract_number: str = Form(...),
    title: str = Form(...),
    status_value: str = Form("active", alias="status"),
    start_date: str = Form(...),
    end_date: str = Form(""),
    license_billing_frequency: str = Form("quarterly"),
    service_billing_frequency: str = Form("monthly"),
    variable_billing_frequency: str = Form("monthly"),
    service_hourly_rate: str = Form(""),
    license_price_increase_percent: str = Form(""),
    vat_treatment: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    billing_recipient_name: str = Form(""),
    billing_recipient_email: str = Form(""),
    billing_recipient_phone: str = Form(""),
    customer_order_number: str = Form(""),
    erp_reference_number: str = Form(""),
    currency: str = Form("EUR"),
    notes: str = Form(""),
    document: UploadFile | None = File(None),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    form_values = {
        "company_id": company_id,
        "contract_number": contract_number,
        "title": title,
        "status": status_value,
        "start_date": start_date,
        "end_date": end_date,
        "license_billing_frequency": license_billing_frequency,
        "service_billing_frequency": service_billing_frequency,
        "variable_billing_frequency": variable_billing_frequency,
        "service_hourly_rate": service_hourly_rate,
        "license_price_increase_percent": license_price_increase_percent,
        "vat_treatment": vat_treatment,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "billing_recipient_name": billing_recipient_name,
        "billing_recipient_email": billing_recipient_email,
        "billing_recipient_phone": billing_recipient_phone,
        "customer_order_number": customer_order_number,
        "erp_reference_number": erp_reference_number,
        "currency": currency,
        "notes": notes,
    }
    try:
        company = company_contact_defaults(company_id)
        if company is None:
            raise ValueError("Unternehmen nicht gefunden.")
        apply_company_contact_defaults(form_values, company)
        if status_value not in CONTRACT_STATUSES:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
        license_billing_frequency = validate_billing_frequency(license_billing_frequency, "quarterly")
        service_billing_frequency = validate_billing_frequency(service_billing_frequency, "monthly")
        variable_billing_frequency = validate_billing_frequency(variable_billing_frequency, "monthly")
        cleaned_service_rate = required_text(service_hourly_rate, f"Standard-{rate_unit_label()}")
        cleaned_price_increase = required_text(
            license_price_increase_percent,
            "Preissteigerung Lizenzkosten ab 2. Jahr",
        )
        selected_vat_treatment = validate_vat_treatment(required_text(vat_treatment, "Umsatzsteuer"))
        parsed_price_increase = parse_percent(cleaned_price_increase, Decimal("8"))
        cleaned_contact_name = required_text(form_values.get("contact_name"), "Ansprechpartner Name")
        cleaned_contact_email = required_text(form_values.get("contact_email"), "Ansprechpartner E-Mail")
        cleaned_contact_phone = required_text(form_values.get("contact_phone"), "Ansprechpartner Telefonnummer")
        cleaned_billing_name = required_text(form_values.get("billing_recipient_name"), "Rechnungsempfaenger Name")
        cleaned_billing_email = required_text(form_values.get("billing_recipient_email"), "Rechnungsempfaenger E-Mail")
        cleaned_billing_phone = required_text(form_values.get("billing_recipient_phone"), "Rechnungsempfaenger Telefonnummer")
        service_hourly_rate_cents = parse_rate_to_hourly_cents(cleaned_service_rate)
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Bitte ein Startdatum angeben.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        original_filename, stored_filename = save_contract_pdf(document)
    except (ValueError, OSError) as exc:
        return render(
            request,
            "contract_form.html",
            {
                "companies": company_options(),
                "form_action": "/contracts",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    timestamp = now_iso()
    with database.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO contracts (
                company_id, contract_number, title, status, start_date, end_date,
                license_billing_frequency, service_billing_frequency, variable_billing_frequency,
                service_hourly_rate_cents, license_price_increase_percent, vat_treatment,
                contact_name, contact_email, contact_phone,
                billing_recipient_name, billing_recipient_email, billing_recipient_phone,
                customer_order_number, erp_reference_number,
                currency, original_filename, stored_filename, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                contract_number.strip(),
                title.strip(),
                status_value,
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                license_billing_frequency,
                service_billing_frequency,
                variable_billing_frequency,
                service_hourly_rate_cents,
                str(parsed_price_increase),
                selected_vat_treatment,
                cleaned_contact_name,
                cleaned_contact_email,
                cleaned_contact_phone,
                cleaned_billing_name,
                cleaned_billing_email,
                cleaned_billing_phone,
                customer_order_number.strip(),
                erp_reference_number.strip(),
                currency.strip().upper()[:3] or "EUR",
                original_filename,
                stored_filename,
                notes.strip(),
                timestamp,
                timestamp,
            ),
        )
        contract_id = cursor.lastrowid
        materialize_standard_characteristics(connection, "contract", contract_id)
    return redirect_to(f"/contracts/{contract_id}")


@app.get("/contracts/{contract_id}/edit")
def edit_contract_form(
    request: Request,
    contract_id: int,
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    contract = fetch_one(
        "SELECT * FROM contracts WHERE id = ?",
        (contract_id,),
        "Vertrag nicht gefunden.",
    )
    form_values = dict(contract)
    form_values["service_hourly_rate"] = rate_input(form_values.get("service_hourly_rate_cents"))
    form_values["license_price_increase_percent"] = form_values.get("license_price_increase_percent") or "8"
    form_values["vat_treatment"] = form_values.get("vat_treatment") or "standard"
    apply_company_contact_defaults(form_values, company_contact_defaults(form_values.get("company_id")))
    return render(
        request,
        "contract_form.html",
        {
            "form_title": "Vertrag bearbeiten",
            "companies": company_options(),
            "form_action": f"/contracts/{contract_id}/update",
            "cancel_url": f"/contracts/{contract_id}",
            "form": form_values,
        },
    )


@app.post("/contracts/{contract_id}/update")
def update_contract(
    request: Request,
    contract_id: int,
    company_id: int = Form(...),
    contract_number: str = Form(...),
    title: str = Form(...),
    status_value: str = Form("active", alias="status"),
    start_date: str = Form(...),
    end_date: str = Form(""),
    license_billing_frequency: str = Form("quarterly"),
    service_billing_frequency: str = Form("monthly"),
    variable_billing_frequency: str = Form("monthly"),
    service_hourly_rate: str = Form(""),
    license_price_increase_percent: str = Form(""),
    vat_treatment: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    billing_recipient_name: str = Form(""),
    billing_recipient_email: str = Form(""),
    billing_recipient_phone: str = Form(""),
    customer_order_number: str = Form(""),
    erp_reference_number: str = Form(""),
    currency: str = Form("EUR"),
    notes: str = Form(""),
    document: UploadFile | None = File(None),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    existing = fetch_one(
        "SELECT * FROM contracts WHERE id = ?",
        (contract_id,),
        "Vertrag nicht gefunden.",
    )
    form_values = {
        "id": contract_id,
        "company_id": company_id,
        "contract_number": contract_number,
        "title": title,
        "status": status_value,
        "start_date": start_date,
        "end_date": end_date,
        "license_billing_frequency": license_billing_frequency,
        "service_billing_frequency": service_billing_frequency,
        "variable_billing_frequency": variable_billing_frequency,
        "service_hourly_rate": service_hourly_rate,
        "license_price_increase_percent": license_price_increase_percent,
        "vat_treatment": vat_treatment,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "billing_recipient_name": billing_recipient_name,
        "billing_recipient_email": billing_recipient_email,
        "billing_recipient_phone": billing_recipient_phone,
        "customer_order_number": customer_order_number,
        "erp_reference_number": erp_reference_number,
        "currency": currency,
        "notes": notes,
        "original_filename": existing["original_filename"],
    }

    try:
        company = company_contact_defaults(company_id)
        if company is None:
            raise ValueError("Unternehmen nicht gefunden.")
        apply_company_contact_defaults(form_values, company)
        if status_value not in CONTRACT_STATUSES:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
        license_billing_frequency = validate_billing_frequency(license_billing_frequency, "quarterly")
        service_billing_frequency = validate_billing_frequency(service_billing_frequency, "monthly")
        variable_billing_frequency = validate_billing_frequency(variable_billing_frequency, "monthly")
        cleaned_service_rate = required_text(service_hourly_rate, f"Standard-{rate_unit_label()}")
        cleaned_price_increase = required_text(
            license_price_increase_percent,
            "Preissteigerung Lizenzkosten ab 2. Jahr",
        )
        selected_vat_treatment = validate_vat_treatment(required_text(vat_treatment, "Umsatzsteuer"))
        parsed_price_increase = parse_percent(cleaned_price_increase, Decimal("8"))
        cleaned_contact_name = required_text(form_values.get("contact_name"), "Ansprechpartner Name")
        cleaned_contact_email = required_text(form_values.get("contact_email"), "Ansprechpartner E-Mail")
        cleaned_contact_phone = required_text(form_values.get("contact_phone"), "Ansprechpartner Telefonnummer")
        cleaned_billing_name = required_text(form_values.get("billing_recipient_name"), "Rechnungsempfaenger Name")
        cleaned_billing_email = required_text(form_values.get("billing_recipient_email"), "Rechnungsempfaenger E-Mail")
        cleaned_billing_phone = required_text(form_values.get("billing_recipient_phone"), "Rechnungsempfaenger Telefonnummer")
        service_hourly_rate_cents = parse_rate_to_hourly_cents(cleaned_service_rate)
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Bitte ein Startdatum angeben.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        new_original_filename, new_stored_filename = save_contract_pdf(document)
    except (ValueError, OSError) as exc:
        return render(
            request,
            "contract_form.html",
            {
                "form_title": "Vertrag bearbeiten",
                "companies": company_options(),
                "form_action": f"/contracts/{contract_id}/update",
                "cancel_url": f"/contracts/{contract_id}",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    original_filename = new_original_filename or existing["original_filename"]
    stored_filename = new_stored_filename or existing["stored_filename"]
    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE contracts
            SET company_id = ?,
                contract_number = ?,
                title = ?,
                status = ?,
                start_date = ?,
                end_date = ?,
                license_billing_frequency = ?,
                service_billing_frequency = ?,
                variable_billing_frequency = ?,
                service_hourly_rate_cents = ?,
                license_price_increase_percent = ?,
                vat_treatment = ?,
                contact_name = ?,
                contact_email = ?,
                contact_phone = ?,
                billing_recipient_name = ?,
                billing_recipient_email = ?,
                billing_recipient_phone = ?,
                customer_order_number = ?,
                erp_reference_number = ?,
                currency = ?,
                original_filename = ?,
                stored_filename = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                company_id,
                contract_number.strip(),
                title.strip(),
                status_value,
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                license_billing_frequency,
                service_billing_frequency,
                variable_billing_frequency,
                service_hourly_rate_cents,
                str(parsed_price_increase),
                selected_vat_treatment,
                cleaned_contact_name,
                cleaned_contact_email,
                cleaned_contact_phone,
                cleaned_billing_name,
                cleaned_billing_email,
                cleaned_billing_phone,
                customer_order_number.strip(),
                erp_reference_number.strip(),
                currency.strip().upper()[:3] or "EUR",
                original_filename,
                stored_filename,
                notes.strip(),
                timestamp,
                contract_id,
            ),
        )
        connection.execute(
            """
            UPDATE invoices
            SET company_id = ?
            WHERE contract_id = ?
            """,
            (company_id, contract_id),
        )
    return redirect_to(f"/contracts/{contract_id}")


@app.post("/contracts/{contract_id}/delete")
def delete_contract(
    contract_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    with database.connect() as connection:
        contract = connection.execute("SELECT id FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if contract is None:
            raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")
        dependency_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM licenses WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM services WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM variable_costs WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM flat_fees WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM service_time_entries WHERE contract_id = ?)
                + (SELECT COUNT(*) FROM invoices WHERE contract_id = ?)
            """,
            (contract_id, contract_id, contract_id, contract_id, contract_id, contract_id),
        ).fetchone()[0]
        if dependency_count:
            raise HTTPException(
                status_code=400,
                detail="Vertraege mit abhaengigen Positionen, Stunden oder Rechnungen koennen nicht geloescht werden.",
            )
        connection.execute(
            "DELETE FROM characteristic_values WHERE target_type = 'contract' AND target_id = ?",
            (contract_id,),
        )
        connection.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
    return redirect_to("/contracts")


@app.get("/contracts/{contract_id}")
def contract_detail(
    request: Request,
    contract_id: int,
    tab: str = Query("overview"),
    _: dict[str, Any] = Depends(require_permission("contracts.view")),
):
    active_contract_tab = (
        tab
        if tab in {"overview", "invoices", "licenses", "services", "flat_fees", "variable_costs", "time_entries"}
        else "overview"
    )
    bundle = fetch_contract_bundle(contract_id)
    bundle["active_contract_tab"] = active_contract_tab
    return render(request, "contract_detail.html", bundle)


@app.get("/contracts/{contract_id}/document")
def contract_document(contract_id: int, _: dict[str, Any] = Depends(require_permission("contracts.view"))):
    contract = fetch_one(
        "SELECT * FROM contracts WHERE id = ?",
        (contract_id,),
        "Vertrag nicht gefunden.",
    )
    if not contract["stored_filename"]:
        raise HTTPException(status_code=404, detail="Kein Vertragsdokument hinterlegt.")
    path = database.UPLOAD_DIR / contract["stored_filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Vertragsdokument nicht gefunden.")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=contract["original_filename"],
        headers={"Content-Disposition": f"inline; filename=\"{contract['original_filename']}\""},
    )


def contract_item_form_base(contract_id: int) -> dict[str, Any]:
    with database.connect() as connection:
        contract = connection.execute(
            """
            SELECT contracts.*, companies.name AS company_name
            FROM contracts
            JOIN companies ON companies.id = contracts.company_id
            WHERE contracts.id = ?
            """,
            (contract_id,),
        ).fetchone()
    if contract is None:
        raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")
    return dict(contract)


@app.get("/contracts/{contract_id}/licenses/new")
def new_license_form(
    request: Request,
    contract_id: int,
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    contract = contract_item_form_base(contract_id)
    return render(
        request,
        "license_form.html",
        {
            "form_title": "Lizenz hinzufuegen",
            "submit_label": "Lizenz speichern",
            "show_status": False,
            "license": {
                "company_name": contract["company_name"],
                "contract_title": contract["title"],
                "contract_id": contract_id,
                "currency": contract["currency"],
                "license_type_id": None,
                "annual_amount_cents": 0,
                "quantity": 1,
                "start_date": contract["start_date"],
                "end_date": "",
                "billing_frequency": contract["license_billing_frequency"],
                "billing_strategy": "standard",
                "first_year_billing_frequency": contract["license_billing_frequency"],
                "renewal_billing_frequency": contract["license_billing_frequency"],
                "status": "active",
                "notes": "",
            },
            "license_types": license_type_options(),
            "form_action": f"/contracts/{contract_id}/licenses",
            "cancel_url": contract_section_path(contract_id, "licenses"),
        },
    )


@app.get("/contracts/{contract_id}/services/new")
def new_service_form(
    request: Request,
    contract_id: int,
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    contract = contract_item_form_base(contract_id)
    return render(
        request,
        "service_form.html",
        {
            "form_title": "Dienstleistung hinzufuegen",
            "submit_label": "Dienstleistung speichern",
            "show_status": False,
            "service": {
                "company_name": contract["company_name"],
                "contract_title": contract["title"],
                "contract_id": contract_id,
                "currency": contract["currency"],
                "service_type_id": None,
                "hourly_rate_cents": contract["service_hourly_rate_cents"],
                "contracted_hours": None,
                "billing_frequency": contract["service_billing_frequency"],
                "status": "active",
                "notes": "",
            },
            "service_types": service_type_options(),
            "form_action": f"/contracts/{contract_id}/services",
            "cancel_url": contract_section_path(contract_id, "services"),
        },
    )


@app.get("/contracts/{contract_id}/variable-costs/new")
def new_variable_cost_form(
    request: Request,
    contract_id: int,
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    contract = contract_item_form_base(contract_id)
    return render(
        request,
        "variable_cost_form.html",
        {
            "form_title": "Variablen Kostensatz hinzufuegen",
            "submit_label": "Kostensatz speichern",
            "show_status": False,
            "item": {
                "company_name": contract["company_name"],
                "contract_title": contract["title"],
                "contract_id": contract_id,
                "currency": contract["currency"],
                "name": "",
                "description": "",
                "datev_account": "",
                "rate_cents": 0,
                "quantity": 1,
                "unit": "Einheit",
                "start_date": contract["start_date"],
                "end_date": "",
                "billing_frequency": contract["variable_billing_frequency"],
                "status": "active",
                "notes": "",
            },
            "form_action": f"/contracts/{contract_id}/variable-costs",
            "cancel_url": contract_section_path(contract_id, "variable-costs"),
        },
    )


@app.get("/contracts/{contract_id}/flat-fees/new")
def new_flat_fee_form(
    request: Request,
    contract_id: int,
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    contract = contract_item_form_base(contract_id)
    return render(
        request,
        "flat_fee_form.html",
        {
            "form_title": "Pauschale hinzufuegen",
            "submit_label": "Pauschale speichern",
            "show_status": False,
            "item": {
                "company_name": contract["company_name"],
                "contract_title": contract["title"],
                "contract_id": contract_id,
                "currency": contract["currency"],
                "flat_fee_type_id": None,
                "amount_cents": 0,
                "fee_kind": "work_package",
                "start_date": contract["start_date"],
                "end_date": "",
                "billing_frequency": "once",
                "success_condition": "",
                "expected_success_date": "",
                "success_date": "",
                "approval_status": "pending",
                "status": "active",
                "notes": "",
            },
            "flat_fee_types": flat_fee_type_options(),
            "form_action": f"/contracts/{contract_id}/flat-fees",
            "cancel_url": contract_section_path(contract_id, "flat-fees"),
        },
    )


@app.get("/contracts/{contract_id}/licenses/{license_id}/edit")
def edit_license_form(
    request: Request,
    contract_id: int,
    license_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    with database.connect() as connection:
        license_row = connection.execute(
            """
            SELECT licenses.*, contracts.title AS contract_title, contracts.currency,
                   companies.name AS company_name
            FROM licenses
            JOIN contracts ON contracts.id = licenses.contract_id
            JOIN companies ON companies.id = contracts.company_id
            WHERE licenses.id = ? AND licenses.contract_id = ?
            """,
            (license_id, contract_id),
        ).fetchone()
        if license_row is None:
            raise HTTPException(status_code=404, detail="Lizenz nicht gefunden.")
        license_types = connection.execute(
            """
            SELECT *
            FROM license_types
            WHERE active = 1 OR id = ?
            ORDER BY name
            """,
            (license_row["license_type_id"],),
        ).fetchall()
    return render(
        request,
        "license_form.html",
        {
            "license": dict(license_row),
            "license_types": [dict(row) for row in license_types],
            "form_action": f"/contracts/{contract_id}/licenses/{license_id}/update",
            "cancel_url": contract_section_path(contract_id, "licenses"),
        },
    )


@app.get("/contracts/{contract_id}/services/{service_id}/edit")
def edit_service_form(
    request: Request,
    contract_id: int,
    service_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    with database.connect() as connection:
        service_row = connection.execute(
            """
            SELECT services.*, contracts.title AS contract_title, contracts.currency,
                   companies.name AS company_name
            FROM services
            JOIN contracts ON contracts.id = services.contract_id
            JOIN companies ON companies.id = contracts.company_id
            WHERE services.id = ? AND services.contract_id = ?
            """,
            (service_id, contract_id),
        ).fetchone()
        if service_row is None:
            raise HTTPException(status_code=404, detail="Dienstleistung nicht gefunden.")
        service_types = connection.execute(
            """
            SELECT *
            FROM service_types
            WHERE active = 1 OR id = ?
            ORDER BY name
            """,
            (service_row["service_type_id"],),
        ).fetchall()
    return render(
        request,
        "service_form.html",
        {
            "service": dict(service_row),
            "service_types": [dict(row) for row in service_types],
            "form_action": f"/contracts/{contract_id}/services/{service_id}/update",
            "cancel_url": contract_section_path(contract_id, "services"),
        },
    )


@app.get("/contracts/{contract_id}/variable-costs/{cost_id}/edit")
def edit_variable_cost_form(
    request: Request,
    contract_id: int,
    cost_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    with database.connect() as connection:
        variable_cost = connection.execute(
            """
            SELECT variable_costs.*, contracts.title AS contract_title, contracts.currency,
                   companies.name AS company_name
            FROM variable_costs
            JOIN contracts ON contracts.id = variable_costs.contract_id
            JOIN companies ON companies.id = contracts.company_id
            WHERE variable_costs.id = ? AND variable_costs.contract_id = ?
            """,
            (cost_id, contract_id),
        ).fetchone()
        if variable_cost is None:
            raise HTTPException(status_code=404, detail="Variabler Kostensatz nicht gefunden.")
    return render(
        request,
        "variable_cost_form.html",
        {
            "item": dict(variable_cost),
            "form_action": f"/contracts/{contract_id}/variable-costs/{cost_id}/update",
            "cancel_url": contract_section_path(contract_id, "variable-costs"),
        },
    )


@app.get("/contracts/{contract_id}/flat-fees/{flat_fee_id}/edit")
def edit_flat_fee_form(
    request: Request,
    contract_id: int,
    flat_fee_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    with database.connect() as connection:
        flat_fee = connection.execute(
            """
            SELECT flat_fees.*, contracts.title AS contract_title, contracts.currency,
                   companies.name AS company_name
            FROM flat_fees
            JOIN contracts ON contracts.id = flat_fees.contract_id
            JOIN companies ON companies.id = contracts.company_id
            WHERE flat_fees.id = ? AND flat_fees.contract_id = ?
            """,
            (flat_fee_id, contract_id),
        ).fetchone()
        if flat_fee is None:
            raise HTTPException(status_code=404, detail="Pauschale nicht gefunden.")
        flat_fee_types = connection.execute(
            """
            SELECT *
            FROM flat_fee_types
            WHERE active = 1 OR id = ?
            ORDER BY name
            """,
            (flat_fee["flat_fee_type_id"],),
        ).fetchall()
    return render(
        request,
        "flat_fee_form.html",
        {
            "item": dict(flat_fee),
            "flat_fee_types": [dict(row) for row in flat_fee_types],
            "form_action": f"/contracts/{contract_id}/flat-fees/{flat_fee_id}/update",
            "cancel_url": contract_section_path(contract_id, "flat-fees"),
        },
    )


@app.post("/contracts/{contract_id}/licenses")
def create_license(
    contract_id: int,
    license_type_id: int = Form(...),
    annual_amount: str = Form(...),
    quantity: int = Form(1),
    start_date: str = Form(...),
    end_date: str = Form(""),
    billing_frequency: str = Form(""),
    billing_strategy: str = Form("standard"),
    first_year_billing_frequency: str = Form(""),
    renewal_billing_frequency: str = Form(""),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    try:
        annual_amount_cents = parse_amount_to_cents(annual_amount)
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Startdatum fehlt.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    timestamp = now_iso()
    with database.connect() as connection:
        contract = connection.execute(
            "SELECT id, license_billing_frequency FROM contracts WHERE id = ?",
            (contract_id,),
        ).fetchone()
        if contract is None:
            raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")
        try:
            (
                item_billing_strategy,
                item_billing_frequency,
                item_first_year_billing_frequency,
                item_renewal_billing_frequency,
            ) = normalize_license_billing_config(
                billing_strategy,
                billing_frequency,
                first_year_billing_frequency,
                renewal_billing_frequency,
                contract["license_billing_frequency"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        license_type = connection.execute(
            "SELECT * FROM license_types WHERE id = ? AND active = 1",
            (license_type_id,),
        ).fetchone()
        if license_type is None:
            raise HTTPException(status_code=400, detail="Lizenzart ist nicht im aktiven Katalog vorhanden.")
        cursor = connection.execute(
            """
            INSERT INTO licenses (
                contract_id, license_type_id, name, annual_amount_cents, quantity, start_date, end_date,
                billing_frequency, billing_strategy, first_year_billing_frequency, renewal_billing_frequency,
                status, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                contract_id,
                license_type_id,
                license_type["name"],
                annual_amount_cents,
                max(quantity, 1),
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                item_billing_frequency,
                item_billing_strategy,
                item_first_year_billing_frequency,
                item_renewal_billing_frequency,
                notes.strip(),
                timestamp,
                timestamp,
            ),
        )
        materialize_standard_characteristics(connection, "license", cursor.lastrowid)
    return redirect_to(contract_section_path(contract_id, "licenses"))


@app.post("/contracts/{contract_id}/licenses/{license_id}/update")
def update_license(
    contract_id: int,
    license_id: int,
    license_type_id: int = Form(...),
    annual_amount: str = Form(...),
    quantity: int = Form(1),
    start_date: str = Form(...),
    end_date: str = Form(""),
    billing_frequency: str = Form(...),
    billing_strategy: str = Form("standard"),
    first_year_billing_frequency: str = Form(""),
    renewal_billing_frequency: str = Form(""),
    status_value: str = Form("active", alias="status"),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_superadmin),
):
    try:
        annual_amount_cents = parse_amount_to_cents(annual_amount)
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Startdatum fehlt.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        (
            item_billing_strategy,
            item_billing_frequency,
            item_first_year_billing_frequency,
            item_renewal_billing_frequency,
        ) = normalize_license_billing_config(
            billing_strategy,
            billing_frequency,
            first_year_billing_frequency,
            renewal_billing_frequency,
            billing_frequency,
        )
        item_status = validate_contract_item_status(status_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id, license_type_id FROM licenses WHERE id = ? AND contract_id = ?",
            (license_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Lizenz nicht gefunden.")
        license_type = connection.execute(
            "SELECT * FROM license_types WHERE id = ?",
            (license_type_id,),
        ).fetchone()
        if license_type is None or (not license_type["active"] and license_type_id != existing["license_type_id"]):
            raise HTTPException(status_code=400, detail="Lizenzart ist nicht im aktiven Katalog vorhanden.")
        connection.execute(
            """
            UPDATE licenses
            SET license_type_id = ?,
                name = ?,
                annual_amount_cents = ?,
                quantity = ?,
                start_date = ?,
                end_date = ?,
                billing_frequency = ?,
                billing_strategy = ?,
                first_year_billing_frequency = ?,
                renewal_billing_frequency = ?,
                status = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ? AND contract_id = ?
            """,
            (
                license_type_id,
                license_type["name"],
                annual_amount_cents,
                max(quantity, 1),
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                item_billing_frequency,
                item_billing_strategy,
                item_first_year_billing_frequency,
                item_renewal_billing_frequency,
                item_status,
                notes.strip(),
                timestamp,
                license_id,
                contract_id,
            ),
        )
    return redirect_to(contract_section_path(contract_id, "licenses"))


@app.post("/contracts/{contract_id}/licenses/{license_id}/delete")
def delete_license(
    contract_id: int,
    license_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM licenses WHERE id = ? AND contract_id = ?",
            (license_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Lizenz nicht gefunden.")
        reference_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM invoice_line_items
            WHERE item_type = 'license' AND source_id = ?
            """,
            (license_id,),
        ).fetchone()[0]
        if reference_count:
            connection.execute(
                "UPDATE licenses SET status = 'inactive', updated_at = ? WHERE id = ?",
                (timestamp, license_id),
            )
        else:
            connection.execute(
                "DELETE FROM characteristic_values WHERE target_type = 'license' AND target_id = ?",
                (license_id,),
            )
            connection.execute("DELETE FROM licenses WHERE id = ?", (license_id,))
    return redirect_to(contract_section_path(contract_id, "licenses"))


@app.post("/contracts/{contract_id}/services")
def create_service(
    contract_id: int,
    service_type_id: int = Form(...),
    hourly_rate: str = Form(...),
    contracted_hours: str = Form(""),
    billing_frequency: str = Form(""),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    try:
        hourly_rate_cents = parse_rate_to_hourly_cents(hourly_rate)
        parsed_contracted_hours = parse_optional_decimal(contracted_hours)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    timestamp = now_iso()
    with database.connect() as connection:
        contract = connection.execute(
            "SELECT id, service_billing_frequency FROM contracts WHERE id = ?",
            (contract_id,),
        ).fetchone()
        if contract is None:
            raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")
        try:
            item_billing_frequency = validate_billing_frequency(
                billing_frequency,
                contract["service_billing_frequency"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service_type = connection.execute(
            "SELECT * FROM service_types WHERE id = ? AND active = 1",
            (service_type_id,),
        ).fetchone()
        if service_type is None:
            raise HTTPException(status_code=400, detail="Dienstleistungsart ist nicht im aktiven Katalog vorhanden.")
        cursor = connection.execute(
            """
            INSERT INTO services (
                contract_id, service_type_id, name, hourly_rate_cents, contracted_hours,
                billing_frequency, status, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                contract_id,
                service_type_id,
                service_type["name"],
                hourly_rate_cents,
                float(parsed_contracted_hours) if parsed_contracted_hours is not None else None,
                item_billing_frequency,
                notes.strip(),
                timestamp,
                timestamp,
            ),
        )
        materialize_standard_characteristics(connection, "service", cursor.lastrowid)
    return redirect_to(contract_section_path(contract_id, "services"))


@app.post("/contracts/{contract_id}/services/{service_id}/update")
def update_service(
    contract_id: int,
    service_id: int,
    service_type_id: int = Form(...),
    hourly_rate: str = Form(...),
    contracted_hours: str = Form(""),
    billing_frequency: str = Form(...),
    status_value: str = Form("active", alias="status"),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_superadmin),
):
    try:
        hourly_rate_cents = parse_rate_to_hourly_cents(hourly_rate)
        parsed_contracted_hours = parse_optional_decimal(contracted_hours)
        item_billing_frequency = validate_billing_frequency(billing_frequency)
        item_status = validate_contract_item_status(status_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id, service_type_id FROM services WHERE id = ? AND contract_id = ?",
            (service_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Dienstleistung nicht gefunden.")
        service_type = connection.execute(
            "SELECT * FROM service_types WHERE id = ?",
            (service_type_id,),
        ).fetchone()
        if service_type is None or (not service_type["active"] and service_type_id != existing["service_type_id"]):
            raise HTTPException(status_code=400, detail="Dienstleistungsart ist nicht im aktiven Katalog vorhanden.")
        connection.execute(
            """
            UPDATE services
            SET service_type_id = ?,
                name = ?,
                hourly_rate_cents = ?,
                contracted_hours = ?,
                billing_frequency = ?,
                status = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ? AND contract_id = ?
            """,
            (
                service_type_id,
                service_type["name"],
                hourly_rate_cents,
                float(parsed_contracted_hours) if parsed_contracted_hours is not None else None,
                item_billing_frequency,
                item_status,
                notes.strip(),
                timestamp,
                service_id,
                contract_id,
            ),
        )
    return redirect_to(contract_section_path(contract_id, "services"))


@app.post("/contracts/{contract_id}/services/{service_id}/delete")
def delete_service(
    contract_id: int,
    service_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM services WHERE id = ? AND contract_id = ?",
            (service_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Dienstleistung nicht gefunden.")
        reference_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM invoice_line_items WHERE item_type = 'service' AND source_id = ?)
                + (SELECT COUNT(*) FROM service_time_entries WHERE service_id = ?)
            """,
            (service_id, service_id),
        ).fetchone()[0]
        if reference_count:
            connection.execute(
                "UPDATE services SET status = 'inactive', updated_at = ? WHERE id = ?",
                (timestamp, service_id),
            )
        else:
            connection.execute(
                "DELETE FROM characteristic_values WHERE target_type = 'service' AND target_id = ?",
                (service_id,),
            )
            connection.execute("DELETE FROM services WHERE id = ?", (service_id,))
    return redirect_to(contract_section_path(contract_id, "services"))


@app.post("/contracts/{contract_id}/variable-costs")
def create_variable_cost(
    contract_id: int,
    name: str = Form(...),
    description: str = Form(""),
    datev_account: str = Form(""),
    rate: str = Form(...),
    quantity: str = Form("1"),
    unit: str = Form("Einheit"),
    start_date: str = Form(...),
    end_date: str = Form(""),
    billing_frequency: str = Form(""),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    try:
        rate_cents = parse_amount_to_cents(rate)
        parsed_quantity = parse_optional_positive_decimal(quantity, Decimal("1"))
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Startdatum fehlt.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    timestamp = now_iso()
    with database.connect() as connection:
        contract = connection.execute(
            "SELECT id, variable_billing_frequency FROM contracts WHERE id = ?",
            (contract_id,),
        ).fetchone()
        if contract is None:
            raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")
        try:
            item_billing_frequency = validate_billing_frequency(
                billing_frequency,
                contract["variable_billing_frequency"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        connection.execute(
            """
            INSERT INTO variable_costs (
                contract_id, name, description, datev_account, rate_cents, quantity, unit,
                start_date, end_date, billing_frequency, status, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                contract_id,
                name.strip(),
                description.strip(),
                datev_account.strip(),
                rate_cents,
                float(parsed_quantity or Decimal("1")),
                unit.strip() or "Einheit",
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                item_billing_frequency,
                notes.strip(),
                timestamp,
                timestamp,
            ),
        )
    return redirect_to(contract_section_path(contract_id, "variable-costs"))


@app.post("/contracts/{contract_id}/variable-costs/{cost_id}/update")
def update_variable_cost(
    contract_id: int,
    cost_id: int,
    name: str = Form(...),
    description: str = Form(""),
    datev_account: str = Form(""),
    rate: str = Form(...),
    quantity: str = Form("1"),
    unit: str = Form("Einheit"),
    start_date: str = Form(...),
    end_date: str = Form(""),
    billing_frequency: str = Form(...),
    status_value: str = Form("active", alias="status"),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_superadmin),
):
    try:
        rate_cents = parse_amount_to_cents(rate)
        parsed_quantity = parse_optional_positive_decimal(quantity, Decimal("1"))
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Startdatum fehlt.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        item_billing_frequency = validate_billing_frequency(billing_frequency)
        item_status = validate_contract_item_status(status_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM variable_costs WHERE id = ? AND contract_id = ?",
            (cost_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Variabler Kostensatz nicht gefunden.")
        connection.execute(
            """
            UPDATE variable_costs
            SET name = ?,
                description = ?,
                datev_account = ?,
                rate_cents = ?,
                quantity = ?,
                unit = ?,
                start_date = ?,
                end_date = ?,
                billing_frequency = ?,
                status = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ? AND contract_id = ?
            """,
            (
                name.strip(),
                description.strip(),
                datev_account.strip(),
                rate_cents,
                float(parsed_quantity or Decimal("1")),
                unit.strip() or "Einheit",
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                item_billing_frequency,
                item_status,
                notes.strip(),
                timestamp,
                cost_id,
                contract_id,
            ),
        )
    return redirect_to(contract_section_path(contract_id, "variable-costs"))


@app.post("/contracts/{contract_id}/variable-costs/{cost_id}/delete")
def delete_variable_cost(
    contract_id: int,
    cost_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM variable_costs WHERE id = ? AND contract_id = ?",
            (cost_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Variabler Kostensatz nicht gefunden.")
        reference_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM invoice_line_items
            WHERE item_type = 'variable_cost' AND source_id = ?
            """,
            (cost_id,),
        ).fetchone()[0]
        if reference_count:
            connection.execute(
                "UPDATE variable_costs SET status = 'inactive', updated_at = ? WHERE id = ?",
                (timestamp, cost_id),
            )
        else:
            connection.execute("DELETE FROM variable_costs WHERE id = ?", (cost_id,))
    return redirect_to(contract_section_path(contract_id, "variable-costs"))


@app.post("/contracts/{contract_id}/flat-fees")
def create_flat_fee(
    contract_id: int,
    flat_fee_type_id: int = Form(...),
    amount: str = Form(...),
    fee_kind: str = Form("work_package"),
    start_date: str = Form(...),
    end_date: str = Form(""),
    billing_frequency: str = Form("once"),
    success_condition: str = Form(""),
    expected_success_date: str = Form(""),
    success_date: str = Form(""),
    approval_status: str = Form("pending"),
    notes: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("contracts.manage")),
):
    try:
        amount_cents = parse_amount_to_cents(amount)
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Startdatum fehlt.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        flat_fee_fields = normalize_flat_fee_fields(
            fee_kind,
            billing_frequency,
            success_condition,
            expected_success_date,
            success_date,
            approval_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    timestamp = now_iso()
    with database.connect() as connection:
        contract = connection.execute("SELECT id FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if contract is None:
            raise HTTPException(status_code=404, detail="Vertrag nicht gefunden.")
        flat_fee_type = connection.execute(
            "SELECT * FROM flat_fee_types WHERE id = ? AND active = 1",
            (flat_fee_type_id,),
        ).fetchone()
        if flat_fee_type is None:
            raise HTTPException(status_code=400, detail="Pauschalart ist nicht im aktiven Katalog vorhanden.")
        cursor = connection.execute(
            """
            INSERT INTO flat_fees (
                contract_id, flat_fee_type_id, name, amount_cents, fee_kind, start_date, end_date,
                billing_frequency, success_condition, expected_success_date, success_date,
                approval_status, status, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                contract_id,
                flat_fee_type_id,
                flat_fee_type["name"],
                amount_cents,
                flat_fee_fields["fee_kind"],
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                flat_fee_fields["billing_frequency"],
                flat_fee_fields["success_condition"],
                flat_fee_fields["expected_success_date"].isoformat()
                if flat_fee_fields["expected_success_date"]
                else None,
                flat_fee_fields["success_date"].isoformat() if flat_fee_fields["success_date"] else None,
                flat_fee_fields["approval_status"],
                notes.strip(),
                timestamp,
                timestamp,
            ),
        )
        materialize_standard_characteristics(connection, "flat_fee", cursor.lastrowid)
    return redirect_to(contract_section_path(contract_id, "flat-fees"))


@app.post("/contracts/{contract_id}/flat-fees/{flat_fee_id}/update")
def update_flat_fee(
    contract_id: int,
    flat_fee_id: int,
    flat_fee_type_id: int = Form(...),
    amount: str = Form(...),
    fee_kind: str = Form("work_package"),
    start_date: str = Form(...),
    end_date: str = Form(""),
    billing_frequency: str = Form("once"),
    success_condition: str = Form(""),
    expected_success_date: str = Form(""),
    success_date: str = Form(""),
    approval_status: str = Form("not_applicable"),
    status_value: str = Form("active", alias="status"),
    notes: str = Form(""),
    user: dict[str, Any] = Depends(require_superadmin),
):
    try:
        amount_cents = parse_amount_to_cents(amount)
        parsed_start = parse_iso_date(start_date)
        if parsed_start is None:
            raise ValueError("Startdatum fehlt.")
        parsed_end = parse_iso_date(end_date)
        if parsed_end and parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        flat_fee_fields = normalize_flat_fee_fields(
            fee_kind,
            billing_frequency,
            success_condition,
            expected_success_date,
            success_date,
            approval_status,
        )
        item_status = validate_contract_item_status(status_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id, flat_fee_type_id, approval_status, approved_by, approved_at FROM flat_fees WHERE id = ? AND contract_id = ?",
            (flat_fee_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Pauschale nicht gefunden.")
        flat_fee_type = connection.execute(
            "SELECT * FROM flat_fee_types WHERE id = ?",
            (flat_fee_type_id,),
        ).fetchone()
        if flat_fee_type is None or (
            not flat_fee_type["active"] and flat_fee_type_id != existing["flat_fee_type_id"]
        ):
            raise HTTPException(status_code=400, detail="Pauschalart ist nicht im aktiven Katalog vorhanden.")
        approved_by = existing["approved_by"]
        approved_at = existing["approved_at"]
        if flat_fee_fields["approval_status"] == "approved":
            if existing["approval_status"] != "approved" or not approved_by or not approved_at:
                approved_by = user["id"]
                approved_at = timestamp
        else:
            approved_by = None
            approved_at = None
        connection.execute(
            """
            UPDATE flat_fees
            SET flat_fee_type_id = ?,
                name = ?,
                amount_cents = ?,
                fee_kind = ?,
                start_date = ?,
                end_date = ?,
                billing_frequency = ?,
                success_condition = ?,
                expected_success_date = ?,
                success_date = ?,
                approval_status = ?,
                approved_by = ?,
                approved_at = ?,
                status = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ? AND contract_id = ?
            """,
            (
                flat_fee_type_id,
                flat_fee_type["name"],
                amount_cents,
                flat_fee_fields["fee_kind"],
                parsed_start.isoformat(),
                parsed_end.isoformat() if parsed_end else None,
                flat_fee_fields["billing_frequency"],
                flat_fee_fields["success_condition"],
                flat_fee_fields["expected_success_date"].isoformat()
                if flat_fee_fields["expected_success_date"]
                else None,
                flat_fee_fields["success_date"].isoformat() if flat_fee_fields["success_date"] else None,
                flat_fee_fields["approval_status"],
                approved_by,
                approved_at,
                item_status,
                notes.strip(),
                timestamp,
                flat_fee_id,
                contract_id,
            ),
        )
    return redirect_to(contract_section_path(contract_id, "flat-fees"))


@app.post("/contracts/{contract_id}/flat-fees/{flat_fee_id}/approval")
def update_flat_fee_approval(
    contract_id: int,
    flat_fee_id: int,
    approval_status: str = Form(...),
    user: dict[str, Any] = Depends(require_superadmin),
):
    try:
        selected_status = validate_flat_fee_approval_status(approval_status, "success_bonus")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            """
            SELECT id, fee_kind, success_date
            FROM flat_fees
            WHERE id = ? AND contract_id = ?
            """,
            (flat_fee_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Pauschale nicht gefunden.")
        if existing["fee_kind"] != "success_bonus":
            raise HTTPException(status_code=400, detail="Nur Erfolgsboni koennen freigegeben werden.")

        if selected_status == "approved":
            success_date_value = existing["success_date"] or today_iso()
            connection.execute(
                """
                UPDATE flat_fees
                SET approval_status = 'approved',
                    success_date = ?,
                    approved_by = ?,
                    approved_at = ?,
                    updated_at = ?
                WHERE id = ? AND contract_id = ?
                """,
                (success_date_value, user["id"], timestamp, timestamp, flat_fee_id, contract_id),
            )
        else:
            connection.execute(
                """
                UPDATE flat_fees
                SET approval_status = ?,
                    approved_by = NULL,
                    approved_at = NULL,
                    updated_at = ?
                WHERE id = ? AND contract_id = ?
                """,
                (selected_status, timestamp, flat_fee_id, contract_id),
            )
    return redirect_to(contract_section_path(contract_id, "flat-fees"))


@app.post("/contracts/{contract_id}/flat-fees/{flat_fee_id}/delete")
def delete_flat_fee(
    contract_id: int,
    flat_fee_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM flat_fees WHERE id = ? AND contract_id = ?",
            (flat_fee_id, contract_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Pauschale nicht gefunden.")
        reference_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM invoice_line_items WHERE item_type = 'flat_fee' AND source_id = ?)
                + (SELECT COUNT(*) FROM service_time_entries WHERE flat_fee_id = ?)
            """,
            (flat_fee_id, flat_fee_id),
        ).fetchone()[0]
        if reference_count:
            connection.execute(
                "UPDATE flat_fees SET status = 'inactive', updated_at = ? WHERE id = ?",
                (timestamp, flat_fee_id),
            )
        else:
            connection.execute(
                "DELETE FROM characteristic_values WHERE target_type = 'flat_fee' AND target_id = ?",
                (flat_fee_id,),
            )
            connection.execute("DELETE FROM flat_fees WHERE id = ?", (flat_fee_id,))
    return redirect_to(contract_section_path(contract_id, "flat-fees"))


@app.post("/contracts/{contract_id}/characteristics")
def set_contract_characteristic(
    contract_id: int,
    definition_id: int = Form(...),
    value_text: str = Form(...),
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    set_characteristic_value("contract", contract_id, definition_id, value_text)
    return redirect_to(f"/contracts/{contract_id}")


@app.get("/time-entries")
def time_entries_index(
    request: Request,
    _: dict[str, Any] = Depends(require_permission("time.approve")),
):
    with database.connect() as connection:
        entries = connection.execute(
            """
            SELECT service_time_entries.*,
                   COALESCE(services.name, flat_fees.name) AS service_name,
                   CASE
                       WHEN service_time_entries.flat_fee_id IS NULL THEN 'Dienstleistung'
                       ELSE 'Pauschale'
                   END AS work_item_type,
                   contracts.contract_number, contracts.title AS contract_title,
                   companies.name AS company_name, users.full_name AS user_name,
                   EXISTS (
                       SELECT 1
                       FROM invoice_time_entries
                       WHERE invoice_time_entries.time_entry_id = service_time_entries.id
                   ) AS has_invoice_link
            FROM service_time_entries
            LEFT JOIN services ON services.id = service_time_entries.service_id
            LEFT JOIN flat_fees ON flat_fees.id = service_time_entries.flat_fee_id
            JOIN contracts ON contracts.id = service_time_entries.contract_id
            JOIN companies ON companies.id = contracts.company_id
            JOIN users ON users.id = service_time_entries.user_id
            ORDER BY COALESCE(service_time_entries.start_date, service_time_entries.work_date) DESC,
                     service_time_entries.id DESC
            """
        ).fetchall()
    return render(
        request,
        "time_entries.html",
        {
            "entries": [dict(row) for row in entries],
            "contracts": contract_options(),
            "work_items": bookable_work_item_options(),
            "return_to": "/time-entries",
        },
    )


@app.get("/time-entries/new")
def time_entry_form(
    request: Request,
    saved: bool = Query(False),
    _: dict[str, Any] = Depends(require_permission("time.create")),
):
    return render(
        request,
        "time_entry_form.html",
        {
            "contracts": contract_options(),
            "work_items": bookable_work_item_options(),
            "return_to": "/time-entries/new",
            "saved": saved,
        },
    )


@app.get("/time-entries/{entry_id}/edit")
def edit_time_entry_form(
    request: Request,
    entry_id: int,
    _: dict[str, Any] = Depends(require_permission("time.approve")),
):
    with database.connect() as connection:
        entry = connection.execute(
            """
            SELECT service_time_entries.*,
                   COALESCE(services.name, flat_fees.name) AS service_name,
                   CASE
                       WHEN service_time_entries.flat_fee_id IS NULL THEN 'Dienstleistung'
                       ELSE 'Pauschale'
                   END AS work_item_type,
                   contracts.contract_number, contracts.title AS contract_title,
                   companies.name AS company_name, users.full_name AS user_name,
                   EXISTS (
                       SELECT 1
                       FROM invoice_time_entries
                       WHERE invoice_time_entries.time_entry_id = service_time_entries.id
                   ) AS has_invoice_link
            FROM service_time_entries
            LEFT JOIN services ON services.id = service_time_entries.service_id
            LEFT JOIN flat_fees ON flat_fees.id = service_time_entries.flat_fee_id
            JOIN contracts ON contracts.id = service_time_entries.contract_id
            JOIN companies ON companies.id = contracts.company_id
            JOIN users ON users.id = service_time_entries.user_id
            WHERE service_time_entries.id = ?
            """,
            (entry_id,),
        ).fetchone()
        if entry is None:
            raise HTTPException(status_code=404, detail="Stundenbuchung nicht gefunden.")
    entry_dict = dict(entry)
    entry_dict["work_item_key"] = (
        f"flat_fee:{entry_dict['flat_fee_id']}"
        if entry_dict.get("flat_fee_id")
        else f"service:{entry_dict.get('service_id')}"
    )
    locked = bool(entry_dict.get("invoice_id") or entry_dict.get("has_invoice_link"))
    return render(
        request,
        "time_entry_edit_form.html",
        {
            "entry": entry_dict,
            "contracts": contract_options(),
            "work_items": bookable_work_item_options(entry_dict.get("service_id"), entry_dict.get("flat_fee_id")),
            "form_action": f"/time-entries/{entry_id}/update",
            "cancel_url": "/time-entries",
            "locked": locked,
            "time_entry_statuses": TIME_ENTRY_EDIT_STATUSES,
        },
    )


@app.post("/time-entries")
def create_time_entry(
    contract_id: int = Form(...),
    work_item_key: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    hours: str = Form(...),
    description: str = Form(""),
    return_to: str = Form("/time-entries/new"),
    user: dict[str, Any] = Depends(require_permission("time.create")),
):
    parsed_start = parse_iso_date(start_date)
    parsed_end = parse_iso_date(end_date)
    if parsed_start is None or parsed_end is None:
        raise HTTPException(status_code=400, detail="Start- und Enddatum fehlen.")
    if parsed_end < parsed_start:
        raise HTTPException(status_code=400, detail="Enddatum darf nicht vor dem Startdatum liegen.")
    parsed_hours = parse_work_amount_to_hours(hours)
    timestamp = now_iso()
    with database.connect() as connection:
        try:
            work_item_kind, work_item_id = parse_work_item_key(work_item_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service_id = None
        flat_fee_id = None
        if work_item_kind == "service":
            service = connection.execute(
                "SELECT id FROM services WHERE id = ? AND contract_id = ?",
                (work_item_id, contract_id),
            ).fetchone()
            if service is None:
                raise HTTPException(status_code=400, detail="Dienstleistung passt nicht zum Vertrag.")
            service_id = work_item_id
        else:
            flat_fee = connection.execute(
                "SELECT id FROM flat_fees WHERE id = ? AND contract_id = ?",
                (work_item_id, contract_id),
            ).fetchone()
            if flat_fee is None:
                raise HTTPException(status_code=400, detail="Pauschale passt nicht zum Vertrag.")
            flat_fee_id = work_item_id
        connection.execute(
            """
            INSERT INTO service_time_entries (
                contract_id, service_id, flat_fee_id, user_id, work_date, start_date, end_date, hours, description,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
            """,
            (
                contract_id,
                service_id,
                flat_fee_id,
                user["id"],
                parsed_start.isoformat(),
                parsed_start.isoformat(),
                parsed_end.isoformat(),
                float(parsed_hours),
                description.strip(),
                timestamp,
                timestamp,
            ),
        )
    if return_to == "/time-entries" and has_permission(user, "time.approve"):
        return redirect_to("/time-entries")
    return redirect_to("/time-entries/new?saved=true")


@app.post("/time-entries/{entry_id}/update")
def update_time_entry(
    request: Request,
    entry_id: int,
    contract_id: int = Form(...),
    work_item_key: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    hours: str = Form(...),
    status_value: str = Form("submitted", alias="status"),
    description: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("time.approve")),
):
    with database.connect() as connection:
        existing = connection.execute(
            """
            SELECT service_time_entries.*, users.full_name AS user_name,
                   EXISTS (
                       SELECT 1
                       FROM invoice_time_entries
                       WHERE invoice_time_entries.time_entry_id = service_time_entries.id
                   ) AS has_invoice_link
            FROM service_time_entries
            JOIN users ON users.id = service_time_entries.user_id
            WHERE service_time_entries.id = ?
            """,
            (entry_id,),
        ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Stundenbuchung nicht gefunden.")

    entry_values = dict(existing)
    entry_values.update(
        {
            "contract_id": contract_id,
            "work_item_key": work_item_key,
            "work_date": start_date,
            "start_date": start_date,
            "end_date": end_date,
            "hours": hours,
            "status": status_value,
            "description": description,
        }
    )
    locked = bool(existing["invoice_id"] or existing["has_invoice_link"])

    try:
        if locked:
            raise ValueError("Bereits Rechnungen zugeordnete Stundenbuchungen koennen nicht bearbeitet werden.")
        if status_value not in TIME_ENTRY_EDIT_STATUSES:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
        parsed_start = parse_iso_date(start_date)
        parsed_end = parse_iso_date(end_date)
        if parsed_start is None or parsed_end is None:
            raise ValueError("Start- und Enddatum fehlen.")
        if parsed_end < parsed_start:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        parsed_hours = parse_work_amount_to_hours(hours)
        work_item_kind, work_item_id = parse_work_item_key(work_item_key)
        with database.connect() as connection:
            if work_item_kind == "service":
                service = connection.execute(
                    "SELECT id FROM services WHERE id = ? AND contract_id = ?",
                    (work_item_id, contract_id),
                ).fetchone()
                if service is None:
                    raise ValueError("Dienstleistung passt nicht zum Vertrag.")
                service_id = work_item_id
                flat_fee_id = None
            else:
                flat_fee = connection.execute(
                    "SELECT id FROM flat_fees WHERE id = ? AND contract_id = ?",
                    (work_item_id, contract_id),
                ).fetchone()
                if flat_fee is None:
                    raise ValueError("Pauschale passt nicht zum Vertrag.")
                service_id = None
                flat_fee_id = work_item_id
    except ValueError as exc:
        return render(
            request,
            "time_entry_edit_form.html",
            {
                "entry": entry_values,
                "contracts": contract_options(),
                "work_items": bookable_work_item_options(
                    entry_values.get("service_id"),
                    entry_values.get("flat_fee_id"),
                ),
                "form_action": f"/time-entries/{entry_id}/update",
                "cancel_url": "/time-entries",
                "locked": locked,
                "time_entry_statuses": TIME_ENTRY_EDIT_STATUSES,
                "error": str(exc),
            },
            status_code=400,
        )

    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE service_time_entries
            SET contract_id = ?,
                service_id = ?,
                flat_fee_id = ?,
                work_date = ?,
                start_date = ?,
                end_date = ?,
                hours = ?,
                description = ?,
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                contract_id,
                service_id,
                flat_fee_id,
                parsed_start.isoformat(),
                parsed_start.isoformat(),
                parsed_end.isoformat(),
                float(parsed_hours),
                description.strip(),
                status_value,
                timestamp,
                entry_id,
            ),
        )
    return redirect_to("/time-entries")


@app.post("/time-entries/{entry_id}/delete")
def delete_time_entry(
    entry_id: int,
    _: dict[str, Any] = Depends(require_permission("time.approve")),
):
    with database.connect() as connection:
        entry = connection.execute(
            """
            SELECT service_time_entries.id, service_time_entries.invoice_id,
                   EXISTS (
                       SELECT 1
                       FROM invoice_time_entries
                       WHERE invoice_time_entries.time_entry_id = service_time_entries.id
                   ) AS has_invoice_link
            FROM service_time_entries
            WHERE service_time_entries.id = ?
            """,
            (entry_id,),
        ).fetchone()
        if entry is None:
            raise HTTPException(status_code=404, detail="Stundenbuchung nicht gefunden.")
        if entry["invoice_id"] or entry["has_invoice_link"]:
            raise HTTPException(
                status_code=400,
                detail="Bereits Rechnungen zugeordnete Stundenbuchungen koennen nicht geloescht werden.",
            )
        connection.execute("DELETE FROM service_time_entries WHERE id = ?", (entry_id,))
    return redirect_to("/time-entries")


@app.post("/time-entries/{entry_id}/approve")
def approve_time_entry(
    entry_id: int,
    _: dict[str, Any] = Depends(require_permission("time.approve")),
):
    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE service_time_entries
            SET status = 'approved', updated_at = ?
            WHERE id = ? AND status = 'submitted'
            """,
            (timestamp, entry_id),
        )
    return redirect_to("/time-entries")


@app.get("/billing")
def billing_index(
    request: Request,
    contract_id: int | None = Query(None),
    period_start: str | None = Query(None),
    period_end: str | None = Query(None),
    include_licenses: bool = Query(True),
    include_services: bool = Query(True),
    include_variable_costs: bool = Query(True),
    include_flat_fees: bool = Query(True),
    _: dict[str, Any] = Depends(require_permission("billing.create")),
):
    contracts = contract_options()
    start = parse_iso_date(period_start) if period_start else default_billing_start()
    end = parse_iso_date(period_end) if period_end else date.today()
    billing_groups: list[dict[str, Any]] = []
    potential_bonus_groups: list[dict[str, Any]] = []
    if start and end and start <= end:
        billing_groups = grouped_billing_lines(
            contracts,
            start,
            end,
            include_licenses,
            include_services,
            include_variable_costs,
            include_flat_fees,
        )
        if include_flat_fees:
            potential_bonus_groups = grouped_potential_success_bonuses(start, end)

    with database.connect() as connection:
        invoices = connection.execute(
            """
            SELECT invoices.*, companies.name AS company_name, contracts.contract_number
            FROM invoices
            JOIN companies ON companies.id = invoices.company_id
            JOIN contracts ON contracts.id = invoices.contract_id
            ORDER BY invoices.created_at DESC
            """
        ).fetchall()

    return render(
        request,
        "billing.html",
        {
            "contracts": contracts,
            "selected_contract_id": contract_id,
            "period_start": start.isoformat() if start else "",
            "period_end": end.isoformat() if end else "",
            "include_licenses": include_licenses,
            "include_services": include_services,
            "include_variable_costs": include_variable_costs,
            "include_flat_fees": include_flat_fees,
            "billing_groups": billing_groups,
            "potential_bonus_groups": potential_bonus_groups,
            "preview_total": sum(group["total_cents"] for group in billing_groups),
            "invoices": [dict(row) for row in invoices],
        },
    )


@app.post("/billing/create")
def create_billing(
    request: Request,
    contract_id: int = Form(...),
    period_start: str = Form(...),
    period_end: str = Form(...),
    include_licenses: bool = Form(False),
    include_services: bool = Form(False),
    include_variable_costs: bool = Form(False),
    include_flat_fees: bool = Form(False),
    selected_line_keys: list[str] | None = Form(None),
    user: dict[str, Any] = Depends(require_permission("billing.create")),
):
    start = parse_iso_date(period_start)
    end = parse_iso_date(period_end)
    if start is None or end is None or start > end:
        raise HTTPException(status_code=400, detail="Bitte einen gueltigen Zeitraum angeben.")
    invoice_id = create_billing_invoice(
        contract_id,
        start,
        end,
        include_licenses,
        include_services,
        include_variable_costs,
        include_flat_fees,
        user["id"],
        set(selected_line_keys or []),
    )
    if invoice_id is None:
        query = (
            f"/billing?contract_id={contract_id}&period_start={start.isoformat()}"
            f"&period_end={end.isoformat()}&include_licenses={str(include_licenses).lower()}"
            f"&include_services={str(include_services).lower()}"
            f"&include_variable_costs={str(include_variable_costs).lower()}"
            f"&include_flat_fees={str(include_flat_fees).lower()}"
        )
        return render(
            request,
            "billing_empty.html",
            {"back_url": query},
            status_code=400,
        )
    return redirect_to(f"/invoices/{invoice_id}")


@app.get("/invoices")
def invoices_index(request: Request, _: dict[str, Any] = Depends(require_permission("billing.create"))):
    return redirect_to("/billing")


def invoice_detail_context(invoice_id: int) -> dict[str, Any]:
    with database.connect() as connection:
        invoice = connection.execute(
            """
            SELECT invoices.*, companies.name AS company_name,
                   contracts.contract_number, contracts.title AS contract_title
            FROM invoices
            JOIN companies ON companies.id = invoices.company_id
            JOIN contracts ON contracts.id = invoices.contract_id
            WHERE invoices.id = ?
            """,
            (invoice_id,),
        ).fetchone()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Rechnung nicht gefunden.")
        lines = connection.execute(
            """
            SELECT invoice_line_items.*,
                   CASE invoice_line_items.item_type
                       WHEN 'license' THEN COALESCE((SELECT notes FROM licenses WHERE licenses.id = invoice_line_items.source_id), '')
                       WHEN 'service' THEN COALESCE((SELECT notes FROM services WHERE services.id = invoice_line_items.source_id), '')
                       WHEN 'variable_cost' THEN COALESCE((SELECT description FROM variable_costs WHERE variable_costs.id = invoice_line_items.source_id), '')
                       WHEN 'flat_fee' THEN COALESCE((SELECT notes FROM flat_fees WHERE flat_fees.id = invoice_line_items.source_id), '')
                       ELSE ''
                   END AS source_description
            FROM invoice_line_items
            WHERE invoice_id = ?
            ORDER BY id
            """,
            (invoice_id,),
        ).fetchall()
        linked_entries = connection.execute(
            """
            SELECT service_time_entries.*,
                   COALESCE(services.name, flat_fees.name) AS service_name,
                   CASE
                       WHEN service_time_entries.flat_fee_id IS NULL THEN 'Dienstleistung'
                       ELSE 'Pauschale'
                   END AS work_item_type,
                   users.full_name AS user_name
            FROM invoice_time_entries
            JOIN service_time_entries ON service_time_entries.id = invoice_time_entries.time_entry_id
            LEFT JOIN services ON services.id = service_time_entries.service_id
            LEFT JOIN flat_fees ON flat_fees.id = service_time_entries.flat_fee_id
            JOIN users ON users.id = service_time_entries.user_id
            WHERE invoice_time_entries.invoice_id = ?
            ORDER BY COALESCE(service_time_entries.start_date, service_time_entries.work_date)
            """,
            (invoice_id,),
        ).fetchall()
    line_items = [dict(row) for row in lines]
    counted_license_sources: set[int] = set()
    for line in line_items:
        line["description"] = append_description_detail(line.get("description", ""), line.get("source_description"))
        quantity_text = line.get("quantity_text") or ""
        quantity_parts = quantity_text.split(" ", 1)
        try:
            quantity_value = parse_optional_decimal(quantity_parts[0]) if quantity_parts else None
        except ValueError:
            quantity_value = None
        quantity_suffix = quantity_parts[1] if len(quantity_parts) > 1 else ""
        aggregate_quantity = quantity_value
        if line["item_type"] == "license" and line.get("source_id") is not None:
            if line["source_id"] in counted_license_sources:
                aggregate_quantity = Decimal("0")
            else:
                counted_license_sources.add(line["source_id"])
        line["aggregate_quantity_value"] = hours_de(aggregate_quantity) if aggregate_quantity is not None else ""
        line["aggregate_quantity_suffix"] = quantity_suffix

    invoice_dict = dict(invoice)
    invoice_dict["document_url"] = (
        f"/invoices/{invoice_id}/document" if invoice_dict.get("invoice_document_stored_filename") else None
    )
    return {
        "invoice": invoice_dict,
        "lines": line_items,
        "linked_entries": [dict(row) for row in linked_entries],
        "form": {},
    }


@app.get("/invoices/{invoice_id}")
def invoice_detail(
    request: Request,
    invoice_id: int,
    _: dict[str, Any] = Depends(require_permission("billing.create")),
):
    return render(request, "invoice_detail.html", invoice_detail_context(invoice_id))


@app.get("/invoices/{invoice_id}/document")
def invoice_document(invoice_id: int, _: dict[str, Any] = Depends(require_permission("billing.create"))):
    invoice = fetch_one(
        "SELECT invoice_document_original_filename, invoice_document_stored_filename FROM invoices WHERE id = ?",
        (invoice_id,),
        "Rechnung nicht gefunden.",
    )
    if not invoice["invoice_document_stored_filename"]:
        raise HTTPException(status_code=404, detail="Kein Rechnungsdokument hinterlegt.")
    path = database.INVOICE_DOCUMENT_DIR / invoice["invoice_document_stored_filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Rechnungsdokument nicht gefunden.")
    filename = invoice["invoice_document_original_filename"] or invoice["invoice_document_stored_filename"]
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f"inline; filename=\"{filename}\""},
    )


@app.post("/invoices/{invoice_id}/document")
def upload_invoice_document(
    request: Request,
    invoice_id: int,
    document: UploadFile | None = File(None),
    _: dict[str, Any] = Depends(require_permission("billing.create")),
):
    try:
        original_filename, stored_filename = save_invoice_document(document)
        if not stored_filename:
            raise ValueError("Bitte ein Rechnungsdokument auswaehlen.")
    except (ValueError, OSError) as exc:
        context = invoice_detail_context(invoice_id)
        context.update({"document_error": str(exc)})
        return render(request, "invoice_detail.html", context, status_code=400)

    timestamp = now_iso()
    with database.connect() as connection:
        result = connection.execute(
            """
            UPDATE invoices
            SET invoice_document_original_filename = ?,
                invoice_document_stored_filename = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (original_filename, stored_filename, timestamp, invoice_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Rechnung nicht gefunden.")
    return redirect_to(f"/invoices/{invoice_id}")


@app.post("/invoices/{invoice_id}/delete")
def delete_invoice(
    invoice_id: int,
    _: dict[str, Any] = Depends(require_permission("billing.create")),
):
    timestamp = now_iso()
    with database.connect() as connection:
        invoice = connection.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Rechnung nicht gefunden.")
        if invoice["status"] != "draft":
            raise HTTPException(status_code=400, detail="Nur Rechnungsentwuerfe koennen geloescht werden.")
        release_draft_invoice_links(connection, invoice_id, timestamp)
        connection.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    return redirect_to("/billing")


@app.get("/settings")
def settings_form(
    request: Request,
    _: dict[str, Any] = Depends(require_permission("settings.manage")),
):
    settings = app_settings()
    return render(
        request,
        "settings.html",
        {
            "form": {
                "license_billable_lead_days": settings["license_billable_lead_days"],
                "contract_end_notification_days": settings["contract_end_notification_days"],
                "billing_rate_unit": settings["billing_rate_unit"],
                "workday_hours": hours_de(settings["workday_hours"]),
            }
        },
    )


@app.post("/settings")
def update_settings(
    request: Request,
    license_billable_lead_days: int = Form(30),
    contract_end_notification_days: int = Form(90),
    billing_rate_unit: str = Form("day"),
    workday_hours: str = Form("8"),
    _: dict[str, Any] = Depends(require_permission("settings.manage")),
):
    form_values = {
        "license_billable_lead_days": license_billable_lead_days,
        "contract_end_notification_days": contract_end_notification_days,
        "billing_rate_unit": billing_rate_unit,
        "workday_hours": workday_hours,
    }
    try:
        lead_days = max(0, int(license_billable_lead_days))
        contract_end_days = max(0, int(contract_end_notification_days))
        if billing_rate_unit not in {"hour", "day"}:
            raise ValueError("Bitte Stunden- oder Tagesansicht auswaehlen.")
        parsed_workday_hours = parse_optional_positive_decimal(workday_hours, Decimal("8"))
        if parsed_workday_hours is None:
            parsed_workday_hours = Decimal("8")
    except (TypeError, ValueError) as exc:
        return render(
            request,
            "settings.html",
            {"form": form_values, "error": str(exc)},
            status_code=400,
        )

    timestamp = now_iso()
    values = {
        "license_billable_lead_days": str(lead_days),
        "contract_end_notification_days": str(contract_end_days),
        "billing_rate_unit": billing_rate_unit,
        "workday_hours": str(parsed_workday_hours),
    }
    with database.connect() as connection:
        for key, value in values.items():
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )
    return redirect_to("/settings")


@app.get("/settings/export.xlsx")
def export_all_data(
    user: dict[str, Any] = Depends(require_superadmin),
):
    workbook, filename = create_database_export_workbook(user["username"])
    return StreamingResponse(
        BytesIO(workbook),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/catalog")
def catalog_index(
    request: Request,
    tab: str = Query("licenses"),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    active_tab = tab if tab in {"licenses", "services", "flat_fees"} else "licenses"
    with database.connect() as connection:
        license_types = connection.execute(
            """
            SELECT license_types.*,
                   COUNT(licenses.id) AS usage_count
            FROM license_types
            LEFT JOIN licenses ON licenses.license_type_id = license_types.id
            GROUP BY license_types.id
            ORDER BY license_types.name
            """
        ).fetchall()
        service_types = connection.execute(
            """
            SELECT service_types.*,
                   COUNT(services.id) AS usage_count
            FROM service_types
            LEFT JOIN services ON services.service_type_id = service_types.id
            GROUP BY service_types.id
            ORDER BY service_types.name
            """
        ).fetchall()
        flat_fee_types = connection.execute(
            """
            SELECT flat_fee_types.*,
                   COUNT(flat_fees.id) AS usage_count
            FROM flat_fee_types
            LEFT JOIN flat_fees ON flat_fees.flat_fee_type_id = flat_fee_types.id
            GROUP BY flat_fee_types.id
            ORDER BY flat_fee_types.name
            """
        ).fetchall()
    license_type_items = [dict(row) for row in license_types]
    for item in license_type_items:
        item["is_seeded"] = item["name"] in DEFAULT_LICENSE_TYPE_NAMES
    service_type_items = [dict(row) for row in service_types]
    for item in service_type_items:
        item["is_seeded"] = item["name"] in DEFAULT_SERVICE_TYPE_NAMES
    flat_fee_type_items = [dict(row) for row in flat_fee_types]
    for item in flat_fee_type_items:
        item["is_seeded"] = False

    return render(
        request,
        "catalog.html",
        {
            "active_tab": active_tab,
            "license_types": license_type_items,
            "service_types": service_type_items,
            "flat_fee_types": flat_fee_type_items,
        },
    )


@app.post("/catalog/license-types")
def create_license_type(
    name: str = Form(...),
    datev_account: str = Form(...),
    description: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO license_types (name, datev_account, description, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                datev_account = excluded.datev_account,
                description = excluded.description,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (name.strip(), datev_account.strip(), description.strip(), timestamp, timestamp),
        )
    return redirect_to("/catalog?tab=licenses")


@app.get("/catalog/license-types/{type_id}/edit")
def edit_license_type_form(
    request: Request,
    type_id: int,
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    with database.connect() as connection:
        license_type = connection.execute(
            "SELECT * FROM license_types WHERE id = ?",
            (type_id,),
        ).fetchone()
    if license_type is None:
        raise HTTPException(status_code=404, detail="Lizenzart nicht gefunden.")
    return render(
        request,
        "catalog_type_form.html",
        {
            "catalog_kind": "license",
            "form_title": "Lizenzart bearbeiten",
            "form_action": f"/catalog/license-types/{type_id}/update",
            "cancel_url": "/catalog?tab=licenses",
            "form": dict(license_type),
        },
    )


@app.post("/catalog/license-types/{type_id}/update")
def update_license_type(
    request: Request,
    type_id: int,
    name: str = Form(...),
    datev_account: str = Form(...),
    description: str = Form(""),
    active: str = Form("1"),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    form_values = {
        "id": type_id,
        "name": name,
        "datev_account": datev_account,
        "description": description,
        "active": 1 if active == "1" else 0,
    }
    try:
        cleaned_name = required_text(name, "Name")
        cleaned_datev_account = required_text(datev_account, "DATEV-Konto")
        if active not in {"0", "1"}:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
    except ValueError as exc:
        return render(
            request,
            "catalog_type_form.html",
            {
                "catalog_kind": "license",
                "form_title": "Lizenzart bearbeiten",
                "form_action": f"/catalog/license-types/{type_id}/update",
                "cancel_url": "/catalog?tab=licenses",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM license_types WHERE id = ?",
            (type_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Lizenzart nicht gefunden.")
        duplicate = connection.execute(
            "SELECT id FROM license_types WHERE name = ? AND id != ?",
            (cleaned_name, type_id),
        ).fetchone()
        if duplicate is not None:
            return render(
                request,
                "catalog_type_form.html",
                {
                    "catalog_kind": "license",
                    "form_title": "Lizenzart bearbeiten",
                    "form_action": f"/catalog/license-types/{type_id}/update",
                    "cancel_url": "/catalog?tab=licenses",
                    "form": form_values,
                    "error": "Eine Lizenzart mit diesem Namen existiert bereits.",
                },
                status_code=400,
            )
        connection.execute(
            """
            UPDATE license_types
            SET name = ?,
                datev_account = ?,
                description = ?,
                active = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                cleaned_name,
                cleaned_datev_account,
                description.strip(),
                form_values["active"],
                timestamp,
                type_id,
            ),
        )
    return redirect_to("/catalog?tab=licenses")


@app.post("/catalog/license-types/{type_id}/activate")
def activate_license_type(
    type_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        result = connection.execute(
            """
            UPDATE license_types
            SET active = 1, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, type_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Lizenzart nicht gefunden.")
    return redirect_to("/catalog?tab=licenses")


@app.post("/catalog/license-types/{type_id}/delete")
def delete_license_type(
    type_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        license_type = connection.execute(
            "SELECT * FROM license_types WHERE id = ?",
            (type_id,),
        ).fetchone()
        if license_type is None:
            raise HTTPException(status_code=404, detail="Lizenzart nicht gefunden.")
        usage_count = connection.execute(
            "SELECT COUNT(*) FROM licenses WHERE license_type_id = ?",
            (type_id,),
        ).fetchone()[0]
        if usage_count == 0 and (not license_type["active"] or license_type["name"] not in DEFAULT_LICENSE_TYPE_NAMES):
            connection.execute("DELETE FROM license_types WHERE id = ?", (type_id,))
        else:
            connection.execute(
                """
                UPDATE license_types
                SET active = 0, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, type_id),
            )
    return redirect_to("/catalog?tab=licenses")


@app.post("/catalog/service-types")
def create_service_type(
    name: str = Form(...),
    datev_account: str = Form(...),
    default_hourly_rate: str = Form("0"),
    description: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    default_rate_cents = parse_rate_to_hourly_cents(default_hourly_rate)
    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO service_types (
                name, datev_account, default_hourly_rate_cents,
                description, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                datev_account = excluded.datev_account,
                default_hourly_rate_cents = excluded.default_hourly_rate_cents,
                description = excluded.description,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                name.strip(),
                datev_account.strip(),
                default_rate_cents,
                description.strip(),
                timestamp,
                timestamp,
            ),
        )
    return redirect_to("/catalog?tab=services")


@app.get("/catalog/service-types/{type_id}/edit")
def edit_service_type_form(
    request: Request,
    type_id: int,
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    with database.connect() as connection:
        service_type = connection.execute(
            "SELECT * FROM service_types WHERE id = ?",
            (type_id,),
        ).fetchone()
    if service_type is None:
        raise HTTPException(status_code=404, detail="Dienstleistungsart nicht gefunden.")
    form_values = dict(service_type)
    form_values["default_hourly_rate"] = rate_input(form_values.get("default_hourly_rate_cents"))
    return render(
        request,
        "catalog_type_form.html",
        {
            "catalog_kind": "service",
            "form_title": "Dienstleistungsart bearbeiten",
            "form_action": f"/catalog/service-types/{type_id}/update",
            "cancel_url": "/catalog?tab=services",
            "form": form_values,
        },
    )


@app.post("/catalog/service-types/{type_id}/update")
def update_service_type(
    request: Request,
    type_id: int,
    name: str = Form(...),
    datev_account: str = Form(...),
    default_hourly_rate: str = Form("0"),
    description: str = Form(""),
    active: str = Form("1"),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    form_values = {
        "id": type_id,
        "name": name,
        "datev_account": datev_account,
        "default_hourly_rate": default_hourly_rate,
        "description": description,
        "active": 1 if active == "1" else 0,
    }
    try:
        cleaned_name = required_text(name, "Name")
        cleaned_datev_account = required_text(datev_account, "DATEV-Konto")
        cleaned_rate = required_text(default_hourly_rate, f"Standard-{rate_unit_label()}")
        default_rate_cents = parse_rate_to_hourly_cents(cleaned_rate)
        if active not in {"0", "1"}:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
    except ValueError as exc:
        return render(
            request,
            "catalog_type_form.html",
            {
                "catalog_kind": "service",
                "form_title": "Dienstleistungsart bearbeiten",
                "form_action": f"/catalog/service-types/{type_id}/update",
                "cancel_url": "/catalog?tab=services",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM service_types WHERE id = ?",
            (type_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Dienstleistungsart nicht gefunden.")
        duplicate = connection.execute(
            "SELECT id FROM service_types WHERE name = ? AND id != ?",
            (cleaned_name, type_id),
        ).fetchone()
        if duplicate is not None:
            return render(
                request,
                "catalog_type_form.html",
                {
                    "catalog_kind": "service",
                    "form_title": "Dienstleistungsart bearbeiten",
                    "form_action": f"/catalog/service-types/{type_id}/update",
                    "cancel_url": "/catalog?tab=services",
                    "form": form_values,
                    "error": "Eine Dienstleistungsart mit diesem Namen existiert bereits.",
                },
                status_code=400,
            )
        connection.execute(
            """
            UPDATE service_types
            SET name = ?,
                datev_account = ?,
                default_hourly_rate_cents = ?,
                description = ?,
                active = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                cleaned_name,
                cleaned_datev_account,
                default_rate_cents,
                description.strip(),
                form_values["active"],
                timestamp,
                type_id,
            ),
        )
    return redirect_to("/catalog?tab=services")


@app.post("/catalog/service-types/{type_id}/activate")
def activate_service_type(
    type_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        result = connection.execute(
            """
            UPDATE service_types
            SET active = 1, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, type_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Dienstleistungsart nicht gefunden.")
    return redirect_to("/catalog?tab=services")


@app.post("/catalog/service-types/{type_id}/delete")
def delete_service_type(
    type_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        service_type = connection.execute(
            "SELECT * FROM service_types WHERE id = ?",
            (type_id,),
        ).fetchone()
        if service_type is None:
            raise HTTPException(status_code=404, detail="Dienstleistungsart nicht gefunden.")
        usage_count = connection.execute(
            "SELECT COUNT(*) FROM services WHERE service_type_id = ?",
            (type_id,),
        ).fetchone()[0]
        if usage_count == 0 and (not service_type["active"] or service_type["name"] not in DEFAULT_SERVICE_TYPE_NAMES):
            connection.execute("DELETE FROM service_types WHERE id = ?", (type_id,))
        else:
            connection.execute(
                """
                UPDATE service_types
                SET active = 0, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, type_id),
            )
    return redirect_to("/catalog?tab=services")


@app.post("/catalog/flat-fee-types")
def create_flat_fee_type(
    name: str = Form(...),
    datev_account: str = Form(...),
    description: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO flat_fee_types (name, datev_account, description, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                datev_account = excluded.datev_account,
                description = excluded.description,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (name.strip(), datev_account.strip(), description.strip(), timestamp, timestamp),
        )
    return redirect_to("/catalog?tab=flat_fees")


@app.get("/catalog/flat-fee-types/{type_id}/edit")
def edit_flat_fee_type_form(
    request: Request,
    type_id: int,
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    with database.connect() as connection:
        flat_fee_type = connection.execute(
            "SELECT * FROM flat_fee_types WHERE id = ?",
            (type_id,),
        ).fetchone()
    if flat_fee_type is None:
        raise HTTPException(status_code=404, detail="Pauschalart nicht gefunden.")
    return render(
        request,
        "catalog_type_form.html",
        {
            "catalog_kind": "flat_fee",
            "form_title": "Pauschalart bearbeiten",
            "form_action": f"/catalog/flat-fee-types/{type_id}/update",
            "cancel_url": "/catalog?tab=flat_fees",
            "form": dict(flat_fee_type),
        },
    )


@app.post("/catalog/flat-fee-types/{type_id}/update")
def update_flat_fee_type(
    request: Request,
    type_id: int,
    name: str = Form(...),
    datev_account: str = Form(...),
    description: str = Form(""),
    active: str = Form("1"),
    _: dict[str, Any] = Depends(require_permission("catalog.manage")),
):
    form_values = {
        "id": type_id,
        "name": name,
        "datev_account": datev_account,
        "description": description,
        "active": 1 if active == "1" else 0,
    }
    try:
        cleaned_name = required_text(name, "Name")
        cleaned_datev_account = required_text(datev_account, "DATEV-Konto")
        if active not in {"0", "1"}:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
    except ValueError as exc:
        return render(
            request,
            "catalog_type_form.html",
            {
                "catalog_kind": "flat_fee",
                "form_title": "Pauschalart bearbeiten",
                "form_action": f"/catalog/flat-fee-types/{type_id}/update",
                "cancel_url": "/catalog?tab=flat_fees",
                "form": form_values,
                "error": str(exc),
            },
            status_code=400,
        )

    timestamp = now_iso()
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM flat_fee_types WHERE id = ?",
            (type_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Pauschalart nicht gefunden.")
        duplicate = connection.execute(
            "SELECT id FROM flat_fee_types WHERE name = ? AND id != ?",
            (cleaned_name, type_id),
        ).fetchone()
        if duplicate is not None:
            return render(
                request,
                "catalog_type_form.html",
                {
                    "catalog_kind": "flat_fee",
                    "form_title": "Pauschalart bearbeiten",
                    "form_action": f"/catalog/flat-fee-types/{type_id}/update",
                    "cancel_url": "/catalog?tab=flat_fees",
                    "form": form_values,
                    "error": "Eine Pauschalart mit diesem Namen existiert bereits.",
                },
                status_code=400,
            )
        connection.execute(
            """
            UPDATE flat_fee_types
            SET name = ?,
                datev_account = ?,
                description = ?,
                active = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                cleaned_name,
                cleaned_datev_account,
                description.strip(),
                form_values["active"],
                timestamp,
                type_id,
            ),
        )
    return redirect_to("/catalog?tab=flat_fees")


@app.post("/catalog/flat-fee-types/{type_id}/activate")
def activate_flat_fee_type(
    type_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        result = connection.execute(
            """
            UPDATE flat_fee_types
            SET active = 1, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, type_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pauschalart nicht gefunden.")
    return redirect_to("/catalog?tab=flat_fees")


@app.post("/catalog/flat-fee-types/{type_id}/delete")
def delete_flat_fee_type(
    type_id: int,
    _: dict[str, Any] = Depends(require_superadmin),
):
    timestamp = now_iso()
    with database.connect() as connection:
        flat_fee_type = connection.execute(
            "SELECT * FROM flat_fee_types WHERE id = ?",
            (type_id,),
        ).fetchone()
        if flat_fee_type is None:
            raise HTTPException(status_code=404, detail="Pauschalart nicht gefunden.")
        usage_count = connection.execute(
            "SELECT COUNT(*) FROM flat_fees WHERE flat_fee_type_id = ?",
            (type_id,),
        ).fetchone()[0]
        if usage_count == 0:
            connection.execute("DELETE FROM flat_fee_types WHERE id = ?", (type_id,))
        else:
            connection.execute(
                """
                UPDATE flat_fee_types
                SET active = 0, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, type_id),
            )
    return redirect_to("/catalog?tab=flat_fees")


@app.post("/invoices/{invoice_id}/finalize")
def finalize_invoice(
    request: Request,
    invoice_id: int,
    datev_invoice_number: str = Form(""),
    datev_invoice_date: str = Form(""),
    _: dict[str, Any] = Depends(require_permission("billing.create")),
):
    form_values = {
        "datev_invoice_number": datev_invoice_number,
        "datev_invoice_date": datev_invoice_date,
    }
    try:
        cleaned_datev_invoice_number = datev_invoice_number.strip()
        if not cleaned_datev_invoice_number:
            raise ValueError("Bitte die DATEV-Rechnungsnummer eingeben.")
        parsed_datev_invoice_date = parse_iso_date(datev_invoice_date)
        if parsed_datev_invoice_date is None:
            raise ValueError("Bitte das DATEV-Rechnungsdatum eingeben.")
    except ValueError as exc:
        context = invoice_detail_context(invoice_id)
        context.update({"finalize_error": str(exc), "form": form_values})
        return render(request, "invoice_detail.html", context, status_code=400)

    timestamp = now_iso()
    with database.connect() as connection:
        invoice = connection.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Rechnung nicht gefunden.")
        if invoice["status"] != "draft":
            return redirect_to(f"/invoices/{invoice_id}")
        connection.execute(
            """
            UPDATE invoices
            SET status = 'finalized',
                datev_invoice_number = ?,
                datev_invoice_date = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                cleaned_datev_invoice_number,
                parsed_datev_invoice_date.isoformat(),
                timestamp,
                invoice_id,
            ),
        )
        connection.execute(
            """
            UPDATE service_time_entries
            SET status = 'billed', invoice_id = ?, updated_at = ?
            WHERE id IN (
                SELECT time_entry_id
                FROM invoice_time_entries
                WHERE invoice_id = ?
            )
            """,
            (invoice_id, timestamp, invoice_id),
        )
    return redirect_to(f"/invoices/{invoice_id}")


@app.get("/characteristics")
def characteristics_index(
    request: Request,
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    with database.connect() as connection:
        definitions = connection.execute(
            """
            SELECT characteristic_definitions.*,
                   (
                       SELECT COUNT(*)
                       FROM characteristic_values
                       WHERE characteristic_values.definition_id = characteristic_definitions.id
                   ) AS usage_count
            FROM characteristic_definitions
            ORDER BY target_type, name
            """
        ).fetchall()
    return render(
        request,
        "characteristics.html",
        {"definitions": [dict(row) for row in definitions]},
    )


@app.post("/characteristics")
def create_characteristic_definition(
    target_type: str = Form(...),
    key: str = Form(...),
    name: str = Form(...),
    data_type: str = Form("text"),
    is_standard: bool = Form(False),
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    if target_type not in TARGET_TYPES or data_type not in DATA_TYPES:
        raise HTTPException(status_code=400, detail="Ungueltige Charakteristik.")
    try:
        cleaned_key = normalize_characteristic_key(key)
        cleaned_name = required_text(name, "Anzeigename")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    timestamp = now_iso()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO characteristic_definitions (
                target_type, key, name, data_type, is_standard, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_type, key)
            DO UPDATE SET
                name = excluded.name,
                data_type = excluded.data_type,
                is_standard = excluded.is_standard
            """,
            (
                target_type,
                cleaned_key,
                cleaned_name,
                data_type,
                1 if is_standard else 0,
                timestamp,
            ),
        )
        definition = connection.execute(
            """
            SELECT id
            FROM characteristic_definitions
            WHERE target_type = ? AND key = ?
            """,
            (target_type, cleaned_key),
        ).fetchone()
        if is_standard and definition is not None:
            materialize_standard_characteristic_for_existing(connection, definition["id"], target_type)
    return redirect_to("/characteristics")


@app.get("/characteristics/{definition_id}/edit")
def edit_characteristic_definition_form(
    request: Request,
    definition_id: int,
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    with database.connect() as connection:
        definition = connection.execute(
            """
            SELECT characteristic_definitions.*,
                   (
                       SELECT COUNT(*)
                       FROM characteristic_values
                       WHERE characteristic_values.definition_id = characteristic_definitions.id
                   ) AS usage_count
            FROM characteristic_definitions
            WHERE id = ?
            """,
            (definition_id,),
        ).fetchone()
    if definition is None:
        raise HTTPException(status_code=404, detail="Merkmal nicht gefunden.")
    return render(
        request,
        "characteristic_form.html",
        {
            "form_title": "Merkmal bearbeiten",
            "form_action": f"/characteristics/{definition_id}/update",
            "cancel_url": "/characteristics",
            "form": dict(definition),
            "target_locked": definition["usage_count"] > 0,
        },
    )


@app.post("/characteristics/{definition_id}/update")
def update_characteristic_definition(
    request: Request,
    definition_id: int,
    target_type: str = Form(...),
    key: str = Form(...),
    name: str = Form(...),
    data_type: str = Form("text"),
    is_standard: bool = Form(False),
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    form_values = {
        "id": definition_id,
        "target_type": target_type,
        "key": key,
        "name": name,
        "data_type": data_type,
        "is_standard": 1 if is_standard else 0,
        "usage_count": 0,
    }
    try:
        cleaned_key = normalize_characteristic_key(key)
        cleaned_name = required_text(name, "Anzeigename")
        if target_type not in TARGET_TYPES:
            raise ValueError("Bitte ein gueltiges Zielobjekt auswaehlen.")
        if data_type not in DATA_TYPES:
            raise ValueError("Bitte einen gueltigen Datentyp auswaehlen.")
    except ValueError as exc:
        return render(
            request,
            "characteristic_form.html",
            {
                "form_title": "Merkmal bearbeiten",
                "form_action": f"/characteristics/{definition_id}/update",
                "cancel_url": "/characteristics",
                "form": form_values,
                "target_locked": False,
                "error": str(exc),
            },
            status_code=400,
        )

    with database.connect() as connection:
        existing = connection.execute(
            """
            SELECT characteristic_definitions.*,
                   (
                       SELECT COUNT(*)
                       FROM characteristic_values
                       WHERE characteristic_values.definition_id = characteristic_definitions.id
                   ) AS usage_count
            FROM characteristic_definitions
            WHERE id = ?
            """,
            (definition_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Merkmal nicht gefunden.")

        target_locked = existing["usage_count"] > 0
        if target_locked:
            target_type = existing["target_type"]
            form_values["target_type"] = target_type
        form_values["usage_count"] = existing["usage_count"]

        duplicate = connection.execute(
            """
            SELECT id
            FROM characteristic_definitions
            WHERE target_type = ? AND key = ? AND id != ?
            """,
            (target_type, cleaned_key, definition_id),
        ).fetchone()
        if duplicate is not None:
            return render(
                request,
                "characteristic_form.html",
                {
                    "form_title": "Merkmal bearbeiten",
                    "form_action": f"/characteristics/{definition_id}/update",
                    "cancel_url": "/characteristics",
                    "form": form_values,
                    "target_locked": target_locked,
                    "error": "Ein Merkmal mit diesem Schluessel existiert fuer dieses Zielobjekt bereits.",
                },
                status_code=400,
            )

        connection.execute(
            """
            UPDATE characteristic_definitions
            SET target_type = ?,
                key = ?,
                name = ?,
                data_type = ?,
                is_standard = ?
            WHERE id = ?
            """,
            (
                target_type,
                cleaned_key,
                cleaned_name,
                data_type,
                1 if is_standard else 0,
                definition_id,
            ),
        )
        if is_standard:
            materialize_standard_characteristic_for_existing(connection, definition_id, target_type)
    return redirect_to("/characteristics")


@app.post("/characteristics/{definition_id}/delete")
def delete_characteristic_definition(
    definition_id: int,
    _: dict[str, Any] = Depends(require_permission("characteristics.manage")),
):
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM characteristic_definitions WHERE id = ?",
            (definition_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Merkmal nicht gefunden.")
        connection.execute(
            "DELETE FROM characteristic_values WHERE definition_id = ?",
            (definition_id,),
        )
        connection.execute(
            "DELETE FROM characteristic_definitions WHERE id = ?",
            (definition_id,),
        )
    return redirect_to("/characteristics")


@app.get("/users")
def users_index(request: Request, _: dict[str, Any] = Depends(require_permission("users.manage"))):
    return render(request, "users.html", user_management_context())


def user_management_context(generated_password: dict[str, str] | None = None) -> dict[str, Any]:
    with database.connect() as connection:
        users = connection.execute(
            """
            SELECT users.id, users.username, users.full_name, users.email, users.active,
                   roles.label AS role_label, roles.name AS role_name
            FROM users
            JOIN roles ON roles.id = users.role_id
            ORDER BY users.username
            """
        ).fetchall()
        roles = connection.execute(
            """
            SELECT *
            FROM roles
            WHERE name IN ('admin', 'manager', 'consultant')
            ORDER BY CASE name
                WHEN 'admin' THEN 1
                WHEN 'manager' THEN 2
                WHEN 'consultant' THEN 3
                ELSE 4
            END
            """
        ).fetchall()
        all_permissions = connection.execute("SELECT * FROM permissions ORDER BY key").fetchall()
        permissions = connection.execute(
            """
            SELECT role_permissions.role_id, role_permissions.permission_id
            FROM role_permissions
            ORDER BY role_permissions.role_id, role_permissions.permission_id
            """
        ).fetchall()

    permission_matrix: dict[int, set[int]] = defaultdict(set)
    for row in permissions:
        permission_matrix[row["role_id"]].add(row["permission_id"])

    return {
        "users": [dict(row) for row in users],
        "roles": [dict(row) for row in roles],
        "permissions": [dict(row) for row in all_permissions],
        "permission_matrix": dict(permission_matrix),
        "generated_password": generated_password,
    }


@app.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(""),
    role_id: int = Form(...),
    _: dict[str, Any] = Depends(require_permission("users.manage")),
):
    try:
        cleaned_username = required_text(username, "Benutzername")
        cleaned_full_name = required_text(full_name, "Name")
    except ValueError as exc:
        return render(
            request,
            "users.html",
            {**user_management_context(), "error": str(exc)},
            status_code=400,
        )

    timestamp = now_iso()
    generated = generate_password()
    with database.connect() as connection:
        role = connection.execute(
            "SELECT id FROM roles WHERE id = ? AND name IN ('admin', 'manager', 'consultant')",
            (role_id,),
        ).fetchone()
        if role is None:
            return render(
                request,
                "users.html",
                {**user_management_context(), "error": "Ungueltige Rolle."},
                status_code=400,
            )
        duplicate = connection.execute(
            "SELECT id FROM users WHERE username = ?",
            (cleaned_username,),
        ).fetchone()
        if duplicate is not None:
            return render(
                request,
                "users.html",
                {**user_management_context(), "error": "Dieser Benutzername ist bereits vergeben."},
                status_code=400,
            )
        connection.execute(
            """
            INSERT INTO users (username, password_hash, full_name, email, role_id, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                cleaned_username,
                hash_password(generated),
                cleaned_full_name,
                email.strip(),
                role_id,
                timestamp,
            ),
        )
    return render(
        request,
        "users.html",
        user_management_context(
            {
                "username": cleaned_username,
                "password": generated,
                "message": "Initiales Passwort wurde generiert. Es ist nur jetzt sichtbar.",
            }
        ),
    )


@app.get("/users/{user_id}/edit")
def edit_user_form(
    request: Request,
    user_id: int,
    _: dict[str, Any] = Depends(require_permission("users.manage")),
):
    with database.connect() as connection:
        user_row = connection.execute(
            """
            SELECT users.id, users.username, users.full_name, users.email,
                   users.role_id, users.active
            FROM users
            WHERE users.id = ?
            """,
            (user_id,),
        ).fetchone()
        roles = connection.execute(
            """
            SELECT *
            FROM roles
            WHERE name IN ('admin', 'manager', 'consultant')
            ORDER BY CASE name
                WHEN 'admin' THEN 1
                WHEN 'manager' THEN 2
                WHEN 'consultant' THEN 3
                ELSE 4
            END
            """
        ).fetchall()
    if user_row is None:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden.")
    return render(
        request,
        "user_form.html",
        {
            "form_title": "Benutzer bearbeiten",
            "form_action": f"/users/{user_id}/update",
            "cancel_url": "/users",
            "form": dict(user_row),
            "roles": [dict(row) for row in roles],
        },
    )


@app.post("/users/{user_id}/update")
def update_user(
    request: Request,
    user_id: int,
    username: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(""),
    role_id: int = Form(...),
    active: str = Form("1"),
    generate_new_password: str = Form("0"),
    current_user: dict[str, Any] = Depends(require_permission("users.manage")),
):
    form_values = {
        "id": user_id,
        "username": username,
        "full_name": full_name,
        "email": email,
        "role_id": role_id,
        "active": 1 if active == "1" else 0,
    }
    try:
        cleaned_username = required_text(username, "Benutzername")
        cleaned_full_name = required_text(full_name, "Name")
        if active not in {"0", "1"}:
            raise ValueError("Bitte einen gueltigen Status auswaehlen.")
    except ValueError as exc:
        with database.connect() as connection:
            roles = connection.execute(
                """
                SELECT *
                FROM roles
                WHERE name IN ('admin', 'manager', 'consultant')
                ORDER BY CASE name
                    WHEN 'admin' THEN 1
                    WHEN 'manager' THEN 2
                    WHEN 'consultant' THEN 3
                    ELSE 4
                END
                """
            ).fetchall()
        return render(
            request,
            "user_form.html",
            {
                "form_title": "Benutzer bearbeiten",
                "form_action": f"/users/{user_id}/update",
                "cancel_url": "/users",
                "form": form_values,
                "roles": [dict(row) for row in roles],
                "error": str(exc),
            },
            status_code=400,
        )

    with database.connect() as connection:
        roles = connection.execute(
            """
            SELECT *
            FROM roles
            WHERE name IN ('admin', 'manager', 'consultant')
            ORDER BY CASE name
                WHEN 'admin' THEN 1
                WHEN 'manager' THEN 2
                WHEN 'consultant' THEN 3
                ELSE 4
            END
            """
        ).fetchall()
        existing = connection.execute(
            "SELECT id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Benutzer nicht gefunden.")
        role = connection.execute(
            "SELECT id FROM roles WHERE id = ? AND name IN ('admin', 'manager', 'consultant')",
            (role_id,),
        ).fetchone()
        if role is None:
            return render(
                request,
                "user_form.html",
                {
                    "form_title": "Benutzer bearbeiten",
                    "form_action": f"/users/{user_id}/update",
                    "cancel_url": "/users",
                    "form": form_values,
                    "roles": [dict(row) for row in roles],
                    "error": "Bitte eine gueltige Rolle auswaehlen.",
                },
                status_code=400,
            )
        duplicate = connection.execute(
            "SELECT id FROM users WHERE username = ? AND id != ?",
            (cleaned_username, user_id),
        ).fetchone()
        if duplicate is not None:
            return render(
                request,
                "user_form.html",
                {
                    "form_title": "Benutzer bearbeiten",
                    "form_action": f"/users/{user_id}/update",
                    "cancel_url": "/users",
                    "form": form_values,
                    "roles": [dict(row) for row in roles],
                    "error": "Dieser Benutzername ist bereits vergeben.",
                },
                status_code=400,
            )
        if user_id == current_user["id"] and active != "1":
            return render(
                request,
                "user_form.html",
                {
                    "form_title": "Benutzer bearbeiten",
                    "form_action": f"/users/{user_id}/update",
                    "cancel_url": "/users",
                    "form": form_values,
                    "roles": [dict(row) for row in roles],
                    "error": "Der eigene Benutzer kann nicht deaktiviert werden.",
                },
                status_code=400,
            )

        generated = generate_password() if generate_new_password == "1" else None
        if generated:
            connection.execute(
                """
                UPDATE users
                SET username = ?,
                    password_hash = ?,
                    full_name = ?,
                    email = ?,
                    role_id = ?,
                    active = ?
                WHERE id = ?
                """,
                (
                    cleaned_username,
                    hash_password(generated),
                    cleaned_full_name,
                    email.strip(),
                    role_id,
                    1 if active == "1" else 0,
                    user_id,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE users
                SET username = ?,
                    full_name = ?,
                    email = ?,
                    role_id = ?,
                    active = ?
                WHERE id = ?
                """,
                (
                    cleaned_username,
                    cleaned_full_name,
                    email.strip(),
                    role_id,
                    1 if active == "1" else 0,
                    user_id,
                ),
            )
    if generated:
        return render(
            request,
            "users.html",
            user_management_context(
                {
                    "username": cleaned_username,
                    "password": generated,
                    "message": "Neues Passwort wurde generiert. Es ist nur jetzt sichtbar.",
                }
            ),
        )
    return redirect_to("/users")


@app.post("/users/{user_id}/delete")
def delete_user(
    user_id: int,
    current_user: dict[str, Any] = Depends(require_permission("users.manage")),
):
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="Der eigene Benutzer kann nicht entfernt werden.")
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Benutzer nicht gefunden.")
        reference_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM service_time_entries WHERE user_id = ?)
                + (SELECT COUNT(*) FROM invoices WHERE created_by = ?)
                + (SELECT COUNT(*) FROM flat_fees WHERE approved_by = ?)
            """,
            (user_id, user_id, user_id),
        ).fetchone()[0]
        if reference_count:
            connection.execute(
                "UPDATE users SET active = 0 WHERE id = ?",
                (user_id,),
            )
        else:
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return redirect_to("/users")


@app.post("/users/permissions")
def update_role_permissions(
    _: dict[str, Any] = Depends(require_superadmin),
):
    ensure_roles_permissions()
    return redirect_to("/users")
