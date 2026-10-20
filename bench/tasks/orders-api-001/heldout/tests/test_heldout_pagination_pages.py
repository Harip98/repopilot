from app.pagination import page_count, paginate


def test_partial_last_page_is_counted():
    assert page_count(57, 20) == 3
    assert page_count(60, 20) == 3
    assert page_count(1, 20) == 1


def test_last_page_items():
    page = paginate(list(range(57)), 3, 20)
    assert page.pages == 3 and page.items == list(range(40, 57))


def test_api_reports_three_pages(client):
    assert client.get("/orders", params={"page_size": 20}).json()["pages"] == 3
