from decimal import Decimal

from app.models import LineItem
from app.pricing import order_total


def test_api_applies_percentage_discount(client):
    order = client.post("/orders", json={
        "customer": "c", "discount_pct": "10",
        "items": [{"sku": "A", "quantity": 2, "unit_price": "100.00"}],
    }).json()
    assert order["total"] == "194.85"


def test_totals_with_discount():
    big = [LineItem(sku="A", quantity=1, unit_price=Decimal("200.00"))]
    small = [LineItem(sku="B", quantity=1, unit_price=Decimal("5.00"))]
    assert order_total(big, Decimal("10")) == Decimal("194.85")
    assert order_total(small, Decimal("10")) == Decimal("4.87")
