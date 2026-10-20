def test_page_past_end_is_empty(client):
    resp = client.get("/orders", params={"page": 99, "page_size": 20})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [] and body["total"] == 57 and body["pages"] == 3


def test_no_matches_is_empty_first_page(client):
    resp = client.get("/orders", params={"status": "refunded"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [] and body["total"] == 0 and body["pages"] == 0
