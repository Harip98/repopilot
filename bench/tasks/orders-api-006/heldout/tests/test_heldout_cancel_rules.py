def _first_with_status(client, status):
    return client.get("/orders", params={"status": status}).json()["items"][0]["id"]


def test_cannot_cancel_shipped(client):
    order_id = _first_with_status(client, "shipped")
    assert client.post(f"/orders/{order_id}/cancel").status_code == 409
    assert client.get(f"/orders/{order_id}").json()["status"] == "shipped"


def test_cannot_cancel_delivered(client):
    order_id = _first_with_status(client, "delivered")
    assert client.post(f"/orders/{order_id}/cancel").status_code == 409


def test_can_cancel_paid(client):
    order_id = _first_with_status(client, "paid")
    assert client.post(f"/orders/{order_id}/cancel").status_code == 200
