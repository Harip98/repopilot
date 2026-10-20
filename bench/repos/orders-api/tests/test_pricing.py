from decimal import Decimal

from app.models import LineItem
from app.pricing import order_total, round_money, subtotal


def test_subtotal_sums_lines():
    items = [LineItem(sku="A", quantity=2, unit_price=Decimal("10.00")),
             LineItem(sku="B", quantity=1, unit_price=Decimal("5.25"))]
    assert subtotal(items) == Decimal("25.25")


def test_total_without_discount_includes_tax():
    items = [LineItem(sku="A", quantity=1, unit_price=Decimal("100.00"))]
    assert order_total(items, Decimal("0")) == Decimal("108.25")


def test_round_money_half_up():
    assert round_money(Decimal("1.005")) == Decimal("1.01")
    assert round_money(Decimal("1.004")) == Decimal("1.00")
