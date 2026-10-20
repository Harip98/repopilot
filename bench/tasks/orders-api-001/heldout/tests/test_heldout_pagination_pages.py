def test_partial_last_page_is_counted(client):
    assert client.get("/orders", params={"page_size": 20}).json()["pages"] == 3
    assert client.get("/orders", params={"page_size": 19}).json()["pages"] == 3
    assert client.get("/orders", params={"page_size": 100}).json()["pages"] == 1


def test_last_page_is_reachable(client):
    body = client.get("/orders", params={"page": 3, "page_size": 20}).json()
    assert len(body["items"]) == 17
    assert [o["id"] for o in body["items"]] == list(range(41, 58))
