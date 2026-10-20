from decimal import Decimal

from app.models import LineItem
from app.pricing import order_total


def _total(price):
    return order_total([LineItem(sku="X", quantity=1, unit_price=Decimal(price))], Decimal("0"))


def test_half_cent_totals_round_up():
    assert _total("10.00") == Decimal("10.83")
    assert _total("18.00") == Decimal("19.49")
    assert _total("2.00") == Decimal("2.17")


def test_api_total(client):
    resp = client.post("/orders", json={
        "customer": "fin", "items": [{"sku": "X", "quantity": 1, "unit_price": "10.00"}],
    })
    assert resp.json()["total"] == "10.83"
