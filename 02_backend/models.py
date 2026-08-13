from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=2, max_length=180)


class PasswordChangeRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)


class AdminCreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    full_name: str = Field(min_length=2, max_length=180)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(min_length=2, max_length=40)
    status: str = Field(default="ACTIVE", max_length=20)
    vendor_kode: str | None = Field(default=None, max_length=80)


class DiscrepancyPatch(BaseModel):
    ba_l: float | None = None
    adjustment_l: float | None = None
    stock_awal_override_l: float | None = None
    penerimaan_override_l: float | None = None
    fuel_keluar_override_l: float | None = None
    stock_aktual_override_l: float | None = None
    cuaca: str | None = Field(default=None, max_length=80)
    remark: str | None = Field(default=None, max_length=2000)
    pica_status: Literal["OPEN", "IN_PROGRESS", "CLOSED", "N/A"] | None = None
    pica_owner: str | None = Field(default=None, max_length=180)
    pica_due_date: date | None = None
    pica_note: str | None = Field(default=None, max_length=4000)

    @field_validator(
        "ba_l",
        "adjustment_l",
        "stock_awal_override_l",
        "penerimaan_override_l",
        "fuel_keluar_override_l",
        "stock_aktual_override_l",
    )
    @classmethod
    def finite_numbers(cls, value: float | None) -> float | None:
        if value is not None and not (-10**12 < value < 10**12):
            raise ValueError("nilai di luar batas aman")
        return value


class SS6FetchRequest(BaseModel):
    date_from: date
    date_to: date
    shift: Literal[1, 2]


class SS6SaveRequest(BaseModel):
    token: str
    row_ids: list[str]


class EvidenceUpload(BaseModel):
    modul: str = Field(pattern=r"^[a-zA-Z0-9_\-]+$", max_length=80)
    record_id: str = Field(min_length=1, max_length=180)
    photo_type: str = Field(pattern=r"^[a-zA-Z0-9_\-]+$", max_length=80)
    base64: str = Field(min_length=20)


class GenericPayload(BaseModel):
    data: dict[str, Any] | list[dict[str, Any]]
