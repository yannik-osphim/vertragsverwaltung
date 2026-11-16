from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import database


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
NotBlank = Annotated[str, Field(min_length=1, max_length=250)]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return "osp_" + secrets.token_urlsafe(32)


class ApiBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ServicesRequest(ApiBody):
    contract_name: NotBlank | None = None
    contract_number: NotBlank | None = None

    @model_validator(mode="after")
    def one_contract_reference(self):
        if (self.contract_name is None) == (self.contract_number is None):
            raise ValueError("Genau contract_name oder contract_number angeben.")
        return self


class BookingRequest(ApiBody):
    contract_number: NotBlank
    service_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    service_name: NotBlank | None = None
    start_date: date
    end_date: date
    amount: Decimal = Field(gt=0, le=1000000, decimal_places=2, allow_inf_nan=False)
    unit: Literal["hour", "day"]
    description: str = Field(default="", max_length=10000)

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_comma(cls, value):
        return value.strip().replace(",", ".") if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_booking(self):
        if (self.service_id is None) == (self.service_name is None):
            raise ValueError("Genau service_id oder service_name angeben.")
        if self.end_date < self.start_date:
            raise ValueError("Enddatum darf nicht vor dem Startdatum liegen.")
        return self


class BookingResult(BaseModel):
    booking_id: int
    contract_number: str
    service_id: int
    start_date: date
    end_date: date
    hours: float
    workday_hours: float
    status: Literal["submitted"] = "submitted"


class ServiceResult(BaseModel):
    service_id: int
    service_name: str
    status: str
    contracted_hours: float | None
    booked_hours: float
    remaining_hours: float | None
    submitted_hours: float
    approved_hours: float
    billed_hours: float


class ServicesResult(BaseModel):
    contract_number: str
    workday_hours: float
    services: list[ServiceResult]


def require_api_key(raw_key: str | None = Depends(api_key_header)):
    unauthorized = HTTPException(status_code=401, detail="Ungueltiger oder abgelaufener API-Schluessel.")
    if not raw_key or not re.fullmatch(r"osp_[A-Za-z0-9_-]{43}", raw_key):
        raise unauthorized
    with database.connect() as connection:
        # A per-key transaction lock also makes retried booking requests atomic.
        lock = " FOR UPDATE OF api_keys" if database.IS_POSTGRES else ""
        row = connection.execute(
            """
            SELECT api_keys.id, api_keys.user_id, api_keys.booking_name, api_keys.expires_at, api_keys.revoked_at,
                   users.active, roles.name AS role_name,
                   EXISTS (
                       SELECT 1 FROM role_permissions
                       JOIN permissions ON permissions.id = role_permissions.permission_id
                       WHERE role_permissions.role_id = users.role_id AND permissions.key = 'time.create'
                   ) AS can_book
            FROM api_keys
            LEFT JOIN users ON users.id = api_keys.user_id
            LEFT JOIN roles ON roles.id = users.role_id
            WHERE api_keys.key_hash = ?
            """ + lock,
            (hash_api_key(raw_key),),
        ).fetchone()
        if row is None or row["revoked_at"]:
            raise unauthorized
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            raise unauthorized
        if row["user_id"] is not None:
            if not row["active"] or (row["role_name"] != "admin" and not row["can_book"]):
                raise unauthorized
        elif not (row["booking_name"] or "").strip():
            raise unauthorized
        connection.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (utc_now(), row["id"]))
        yield connection, dict(row)


def find_contract(connection, key_id: int, *, number: str | None = None, name: str | None = None):
    column = "contract_number" if number is not None else "title"
    rows = connection.execute(
        f"""
        SELECT contracts.id, contracts.contract_number, contracts.status
        FROM contracts
        JOIN api_key_contracts ON api_key_contracts.contract_id = contracts.id
        WHERE api_key_contracts.api_key_id = ? AND contracts.{column} = ?
        LIMIT 2
        """,
        (key_id, number if number is not None else name),
    ).fetchall()
    if not rows:
        # Do not disclose whether an inaccessible contract exists.
        raise HTTPException(status_code=404, detail="Vertrag nicht gefunden oder nicht freigegeben.")
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="Vertragsreferenz nicht eindeutig. Eindeutige Vertragsnummer verwenden.")
    return rows[0]


def create_router(settings_provider) -> APIRouter:
    router = APIRouter(tags=["Zeitbuchungs-API"])

    @router.post("/get-services", response_model=ServicesResult)
    def get_services(body: ServicesRequest, auth=Depends(require_api_key)):
        connection, key = auth
        contract = find_contract(connection, key["id"], number=body.contract_number, name=body.contract_name)
        rows = connection.execute(
            """
            SELECT services.id AS service_id, services.name AS service_name, services.status,
                   services.contracted_hours,
                   COALESCE(SUM(entries.hours), 0) AS booked_hours,
                   COALESCE(SUM(CASE WHEN entries.status = 'submitted' THEN entries.hours ELSE 0 END), 0) AS submitted_hours,
                   COALESCE(SUM(CASE WHEN entries.status = 'approved' THEN entries.hours ELSE 0 END), 0) AS approved_hours,
                   COALESCE(SUM(CASE WHEN entries.status = 'billed' THEN entries.hours ELSE 0 END), 0) AS billed_hours
            FROM services
            LEFT JOIN service_time_entries AS entries ON entries.service_id = services.id
                AND entries.contract_id = services.contract_id
            WHERE services.contract_id = ?
            GROUP BY services.id, services.name, services.status, services.contracted_hours
            ORDER BY services.name, services.id
            """,
            (contract["id"],),
        ).fetchall()
        services = []
        for row in rows:
            item = dict(row)
            for field in ("booked_hours", "submitted_hours", "approved_hours", "billed_hours"):
                item[field] = round(item[field], 2)
            item["remaining_hours"] = (
                round(item["contracted_hours"] - item["booked_hours"], 2)
                if item["contracted_hours"] is not None else None
            )
            services.append(item)
        return {
            "contract_number": contract["contract_number"],
            "workday_hours": float(settings_provider()["workday_hours"]),
            "services": services,
        }

    @router.post("/create-booking", status_code=201, response_model=BookingResult)
    def create_booking(
        body: BookingRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None,
        auth=Depends(require_api_key),
    ):
        connection, key = auth
        contract = find_contract(connection, key["id"], number=body.contract_number)
        fingerprint = hashlib.sha256(body.model_dump_json().encode("utf-8")).hexdigest()
        if idempotency_key is not None:
            previous = connection.execute(
                "SELECT * FROM api_booking_requests WHERE api_key_id = ? AND request_id = ?",
                (key["id"], idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise HTTPException(status_code=409, detail="Idempotency-Key wurde bereits fuer andere Buchungsdaten verwendet.")
                if not connection.execute("SELECT id FROM service_time_entries WHERE id = ?", (previous["time_entry_id"],)).fetchone():
                    raise HTTPException(status_code=409, detail="Die urspruengliche Buchung wurde entfernt.")
                response.status_code = 200
                return json.loads(previous["response_json"])
        if contract["status"] != "active":
            raise HTTPException(status_code=409, detail="Vertrag ist nicht aktiv.")
        column = "id" if body.service_id is not None else "name"
        services = connection.execute(
            f"SELECT id, status FROM services WHERE contract_id = ? AND {column} = ? LIMIT 2",
            (contract["id"], body.service_id if body.service_id is not None else body.service_name),
        ).fetchall()
        if not services:
            raise HTTPException(status_code=404, detail="Dienstleistungsposten nicht gefunden.")
        if len(services) > 1:
            raise HTTPException(status_code=409, detail="Dienstleistungsname nicht eindeutig. service_id verwenden.")
        service = services[0]
        if service["status"] != "active":
            raise HTTPException(status_code=409, detail="Dienstleistungsposten ist nicht aktiv.")
        day_hours = settings_provider()["workday_hours"]
        hours = body.amount * day_hours if body.unit == "day" else body.amount
        hours = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if hours <= 0:
            raise HTTPException(status_code=422, detail="Aufwand muss mindestens 0,01 Stunden ergeben.")
        timestamp = utc_now()
        result = connection.execute(
            """
            INSERT INTO service_time_entries (
                contract_id, service_id, user_id, booking_name, work_date, start_date, end_date,
                hours, description, status, booking_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', 'api', ?, ?)
            """,
            (contract["id"], service["id"], key["user_id"],
             key["booking_name"] if key["user_id"] is None else None, body.start_date.isoformat(),
             body.start_date.isoformat(), body.end_date.isoformat(), float(hours),
             body.description, timestamp, timestamp),
        )
        booking = BookingResult(
            booking_id=result.lastrowid, contract_number=contract["contract_number"],
            service_id=service["id"], start_date=body.start_date, end_date=body.end_date,
            hours=float(hours), workday_hours=float(day_hours),
        )
        if idempotency_key is not None:
            connection.execute(
                """INSERT INTO api_booking_requests
                   (api_key_id, request_id, request_hash, time_entry_id, response_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (key["id"], idempotency_key, fingerprint, result.lastrowid, booking.model_dump_json()),
            )
        return booking

    return router
