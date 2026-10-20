def _total(client, status):
    return client.get("/orders", params={"status": status}).json()["total"]


def test_status_filter_ignores_case(client):
    lower = _total(client, "paid")
    assert lower > 0
    assert _total(client, "PAID") == lower
    assert _total(client, "Paid") == lower
