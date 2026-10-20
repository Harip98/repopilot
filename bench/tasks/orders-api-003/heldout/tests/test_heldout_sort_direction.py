def _first_page(client, sort):
    return client.get("/orders", params={"sort": sort, "page_size": 100}).json()["items"]


def test_descending_created_at(client):
    stamps = [o["created_at"] for o in _first_page(client, "-created_at")]
    assert stamps == sorted(stamps, reverse=True)


def test_descending_total(client):
    totals = [float(o["total"]) for o in _first_page(client, "-total")]
    assert totals == sorted(totals, reverse=True)


def test_ascending_still_works(client):
    stamps = [o["created_at"] for o in _first_page(client, "created_at")]
    assert stamps == sorted(stamps)
