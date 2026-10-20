from decimal import Decimal

from app.models import LineItem
from app.pricing import apply_discount, order_total


def test_discount_is_a_percentage():
    assert apply_discount(Decimal("200"), Decimal("10")) == Decimal("180")


def test_totals_with_discount():
    big = [LineItem(sku="A", quantity=1, unit_price=Decimal("200.00"))]
    small = [LineItem(sku="B", quantity=1, unit_price=Decimal("5.00"))]
    assert order_total(big, Decimal("10")) == Decimal("194.85")
    assert order_total(small, Decimal("10")) == Decimal("4.87")
