"""Deterministic synthetic seed data. Nothing here is real financial data."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Role(StrEnum):
    TELLER = "Teller"
    SUPERVISOR = "Supervisor"


@dataclass(frozen=True)
class Operator:
    operator_id: str
    access_code: str
    display_name: str
    role: Role


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    status: str
    member_since: str
    city: str


@dataclass(frozen=True)
class Account:
    account_no: str
    member_id: str
    account_type: str
    nickname: str
    balance: Decimal
    status: str


OPERATORS: dict[str, Operator] = {
    "teller1": Operator("teller1", "teller-pass", "T. Teller", Role.TELLER),
    "super1": Operator("super1", "super-pass", "S. Supervisor", Role.SUPERVISOR),
}

MEMBERS: dict[str, Member] = {
    "12345": Member("12345", "Demo Member", "Active", "03/14/2011", "Springfield"),
    "23456": Member("23456", "Alex Sample", "Active", "07/02/2016", "Shelbyville"),
    "34567": Member("34567", "Jordan Example", "Dormant", "11/30/2004", "Ogdenville"),
}

ACCOUNTS: list[Account] = [
    Account("8801234321", "12345", "Checking", "Primary Checking", Decimal("1250.50"), "Active"),
    Account("8801238765", "12345", "Savings", "Regular Savings", Decimal("8432.17"), "Active"),
    Account("8802340011", "23456", "Checking", "Everyday Checking", Decimal("310.00"), "Active"),
    Account("8802340022", "23456", "Savings", "Rainy Day", Decimal("15000.00"), "Active"),
    Account("8802340033", "23456", "Money Market", "MM Plus", Decimal("2500.00"), "Active"),
    Account("8803450044", "34567", "Savings", "Basic Savings", Decimal("42.10"), "Dormant"),
]

SUB_ACCOUNT_TYPES: tuple[str, ...] = ("Savings", "Money Market", "Holiday Club")

APP_VENDOR = "DemoBank"
APP_PRODUCT = "LegacyCore"
APP_VERSION = "1.4.2"


def accounts_for(member_id: str, extra: list[Account]) -> list[Account]:
    """Seeded accounts plus any created during this process, in display order."""
    return [a for a in [*ACCOUNTS, *extra] if a.member_id == member_id]


def format_money(value: Decimal) -> str:
    """Legacy display format: ``$8,432.17`` and ``($12.00)`` for negatives."""
    quantized = value.quantize(Decimal("0.01"))
    if quantized < 0:
        return f"(${-quantized:,.2f})"
    return f"${quantized:,.2f}"
