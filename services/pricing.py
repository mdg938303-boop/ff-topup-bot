"""
Pure pricing helpers (no database / Telegram imports -> easy to unit-test).

  selling price (BDT) = provider cost × exchange rate  +  your profit
  profit is either a PERCENT of the converted cost, or a FIXED amount in BDT.
  The result is rounded UP to a whole taka, so the price can never end up below cost.
"""
from __future__ import annotations

import re
from decimal import ROUND_UP, Decimal, InvalidOperation

PERCENT = "PERCENT"
FIXED = "FIXED"

_BN = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_MARKUP_RE = re.compile(r"^(?:৳|tk\.?|bdt)?\s*(\d+(?:\.\d+)?)\s*(%|tk\.?|bdt|taka|টাকা)?$", re.IGNORECASE)


def parse_markup(text: str | None) -> tuple[str, Decimal] | None:
    """'10%' -> (PERCENT, 10)   '15' / '৳15' / '15tk' -> (FIXED, 15).  None if invalid or negative."""
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").translate(_BN)
    match = _MARKUP_RE.match(cleaned)
    if not match:
        return None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if match.group(2) == "%":
        if value > Decimal("1000"):
            return None
        return PERCENT, value
    if value > Decimal("1000000"):
        return None
    return FIXED, value


def parse_rate(text: str | None) -> Decimal | None:
    """Exchange rate, e.g. '125' or '124.5' -> Decimal; must be > 0."""
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").translate(_BN)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value > Decimal("100000"):
        return None
    return value


def compute_sell_price(cost, rate, markup_type: str, markup_value) -> Decimal:
    """cost: provider price (e.g. USD)  rate: BDT per 1 unit of provider currency."""
    base = Decimal(str(cost)) * Decimal(str(rate))
    value = Decimal(str(markup_value))
    price = base * (Decimal(1) + value / Decimal(100)) if markup_type == PERCENT else base + value
    return price.quantize(Decimal("1"), rounding=ROUND_UP)


def cost_in_bdt(cost, rate) -> Decimal:
    return (Decimal(str(cost)) * Decimal(str(rate))).quantize(Decimal("0.01"))


def extract_amount_label(name: str) -> str:
    """'Freefire BD 100 Diamond' -> '100' (first number), else the name itself (max 32 chars)."""
    match = re.search(r"\d[\d,]*", name)
    label = match.group(0).replace(",", "") if match else name
    return label[:32]


def markup_label(markup_type: str | None, markup_value) -> str:
    if not markup_type:
        return "—"
    value = Decimal(str(markup_value)).normalize()
    return f"{format(value, 'f')}%" if markup_type == PERCENT else f"৳{format(value, 'f')}"
