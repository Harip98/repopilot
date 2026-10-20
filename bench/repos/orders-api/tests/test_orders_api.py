def test_list_orders_default_page(client):
    body = client.get("/orders").json()
    assert body["page"] == 1 and body["page_size"] == 20
    assert body["total"] == 57 and len(body["items"]) == 20


def test_list_orders_sorted_by_creation(client):
    items = client.get("/orders", params={"page_size": 5}).json()["items"]
    stamps = [o["created_at"] for o in items]
    assert stamps == sorted(stamps)


def test_filter_by_status(client):
    body = client.get("/orders", params={"status": "paid", "page_size": 100}).json()
    assert body["total"] > 0
    assert all(o["status"] == "paid" for o in body["items"])


def test_get_order(client):
    assert client.get("/orders/1").json()["id"] == 1
    assert client.get("/orders/999").status_code == 404


def test_create_order(client):
    resp = client.post("/orders", json={
        "customer": "acme", "items": [{"sku": "A", "quantity": 2, "unit_price": "10.00"}],
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending" and body["total"] == "21.65"


def test_cancel_pending_order(client):
    created = client.post("/orders", json={
        "customer": "acme", "items": [{"sku": "A", "quantity": 1, "unit_price": "1.00"}],
    }).json()
    resp = client.post(f"/orders/{created['id']}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
