from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from app.models import LineItem

TAX_RATE = Decimal("0.0825")
CENT = Decimal("0.01")


def round_money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def subtotal(items: Iterable[LineItem]) -> Decimal:
    """Sum of active line items; cancelled lines are not charged."""
    return sum(
        (item.unit_price * item.quantity for item in items if not item.cancelled),
        Decimal("0"),
    )


def apply_discount(amount: Decimal, discount_pct: Decimal) -> Decimal:
    return amount * (Decimal("100") - discount_pct) / Decimal("100")


def add_tax(amount: Decimal) -> Decimal:
    return amount * (Decimal("1") + TAX_RATE)


def order_total(items: Iterable[LineItem], discount_pct: Decimal) -> Decimal:
    """Discount first, then tax, rounded once at the end."""
    return round_money(add_tax(apply_discount(subtotal(items), discount_pct)))
