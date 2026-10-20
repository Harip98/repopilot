PAYLOAD = {"customer": "mobile", "items": [{"sku": "A", "quantity": 1, "unit_price": "3.00"}]}


def test_same_key_returns_original(client):
    first = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "k-1"})
    second = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_no_duplicate_order(client):
    before = client.get("/orders").json()["total"]
    for _ in range(3):
        client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "k-2"})
    assert client.get("/orders").json()["total"] == before + 1


def test_different_keys_create_orders(client):
    a = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "a"}).json()
    b = client.post("/orders", json=PAYLOAD, headers={"Idempotency-Key": "b"}).json()
    assert a["id"] != b["id"]
