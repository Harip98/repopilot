from decimal import Decimal


def test_subtotal_excludes_cancelled_lines(client):
    order = client.get("/orders/1").json()
    assert order["subtotal"] == "9.99"


def test_created_order_subtotal(client):
    order = client.post("/orders", json={"customer": "c", "items": [
        {"sku": "A", "quantity": 2, "unit_price": "10.00"},
        {"sku": "W", "quantity": 1, "unit_price": "2.50", "cancelled": True},
    ]}).json()
    assert order["subtotal"] == "20.00"
    assert Decimal(order["total"]) == Decimal("21.65")
