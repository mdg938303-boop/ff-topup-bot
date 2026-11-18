from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from config import CURRENCY

_TWO_PLACES = Decimal("0.01")
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_UID_RE = re.compile(r"[0-9]{6,14}")
_PHONE_RE = re.compile(r"\+?[0-9]{10,15}")
_TXN_RE = re.compile(r"[A-Z0-9]{6,32}")


def utcnow() -> datetime:
    """Naive UTC datetime (datetime.utcnow() is deprecated in Python 3.12)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def esc(value) -> str:
    """HTML-escape any dynamic value before putting it into a parse_mode=HTML message."""
    return html.escape("" if value is None else str(value), quote=False)


def to_money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def fmt_money(amount) -> str:
    return f"{CURRENCY}{to_money(amount):,.2f}"


def parse_amount(
    text: str | None,
    min_value: Decimal = Decimal("0.01"),
    max_value: Decimal = Decimal("10000000"),
) -> Decimal | None:
    """
    Safe money parser. Returns None for anything that is not a finite number
    inside [min_value, max_value]. (float('nan') / float('inf') used to slip
    through `amount <= 0` checks — Decimal + is_finite() closes that hole.)
    """
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").replace(" ", "").translate(_BN_DIGITS)
    if not cleaned or len(cleaned) > 20:
        return None
    try:
        value = Decimal(cleaned)
        if not value.is_finite():
            return None
        value = value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if value < min_value or value > max_value:
        return None
    return value


def parse_uid(text: str | None) -> str | None:
    """Free Fire UID: ASCII digits only, 6–14 long."""
    if text is None:
        return None
    cleaned = text.strip().translate(_BN_DIGITS)
    return cleaned if _UID_RE.fullmatch(cleaned) else None


def parse_phone(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = re.sub(r"[\s\-]", "", text.strip().translate(_BN_DIGITS))
    return cleaned if _PHONE_RE.fullmatch(cleaned) else None


def normalize_txn_id(text: str | None) -> str | None:
    """bKash/Nagad/Rocket TrxID: letters+digits, case-insensitive. Returns upper-case or None."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", "", text).upper()
    return cleaned if _TXN_RE.fullmatch(cleaned) else None


def is_admin(user_id: int) -> bool:
    from config import ADMIN_IDS
    return user_id in ADMIN_IDS
