from __future__ import annotations

from datetime import date, timedelta

VALID_SHIFTS = {"SHIFT_1", "SHIFT_2"}


def normalize_shift(value: str) -> str:
    shift = str(value or "").strip().upper().replace(" ", "_")
    aliases = {"1": "SHIFT_1", "S1": "SHIFT_1", "SHIFT1": "SHIFT_1", "2": "SHIFT_2", "S2": "SHIFT_2", "SHIFT2": "SHIFT_2"}
    shift = aliases.get(shift, shift)
    if shift not in VALID_SHIFTS:
        raise ValueError("Shift harus SHIFT_1 atau SHIFT_2")
    return shift


def previous_shift(operation_date: date, shift: str) -> tuple[date, str]:
    shift = normalize_shift(shift)
    if shift == "SHIFT_2":
        return operation_date, "SHIFT_1"
    return operation_date - timedelta(days=1), "SHIFT_2"


def next_shift(operation_date: date, shift: str) -> tuple[date, str]:
    shift = normalize_shift(shift)
    if shift == "SHIFT_1":
        return operation_date, "SHIFT_2"
    return operation_date + timedelta(days=1), "SHIFT_1"
